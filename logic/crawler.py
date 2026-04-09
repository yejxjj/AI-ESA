"""
crawler.py — 다나와 상품 페이지 크롤러

다나와 상품 URL을 입력받아 상품 정보를 수집합니다.

주요 기능:
    - 상품명, 텍스트 스펙 표 수집
    - 상세 이미지 영역(마케팅 페이지) 스크린샷 캡처
    - 헤드리스 Chrome 환경에서 이미지 짤림 방지를 위한 딥 스크롤 처리
    - 크롬 버전 불일치 시 자동 재시도

반환값 (dict):
    - model_name: 상품명
    - specs: 텍스트 스펙 표 (dict)
    - raw_specs: 스펙 원문 텍스트
    - screenshot_path: 상세 이미지 스크린샷 경로
"""

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import re
import os


def _setup_driver():
    """헤드리스 Chrome 드라이버를 초기화합니다. 버전 불일치 시 감지된 버전으로 재시도합니다."""
    def get_new_options():
        options = uc.ChromeOptions()
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--headless')
        options.add_argument('--window-size=1920,1080')
        return options

    try:
        driver = uc.Chrome(options=get_new_options())
    except Exception as e:
        match = re.search(r"Current browser version is (\d+)", str(e))
        if match:
            print(f"⚠️ 크롬 버전 불일치 감지. {match.group(1)} 버전으로 재시도합니다...")
            driver = uc.Chrome(options=get_new_options(), version_main=int(match.group(1)))
        else:
            raise e

    return driver


def get_product_data(url):
    """
    다나와 상품 URL을 크롤링하여 상품 데이터를 반환합니다.

    수집 순서:
        1. 상품명 추출 (CSS 셀렉터 → fallback: 페이지 타이틀)
        2. '상품정보 더보기' 버튼 클릭 (있을 경우)
        3. 느린 스크롤로 모든 이미지 로딩 대기
        4. 상세 영역(#detail_content_wrap 등) 스크린샷 저장 → product_images/<상품명>/detail_scan.png
        5. 텍스트 스펙 표(.prod_spec_table) 파싱
    """
    url = url.strip()
    if "danawa.com" not in url:
        return None

    driver = _setup_driver()
    product_data = {"source": "Danawa", "url": url, "model_name": "", "specs": {}, "raw_specs": "", "screenshot_path": ""}

    try:
        print(f"💻 [1/3] 페이지 접속 및 로딩 중...")
        driver.get(url)
        time.sleep(3)

        # 상품명 추출
        try:
            wait = WebDriverWait(driver, 10)
            title_elem = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "h3.prod_tit, .prod_tit, h2.title")))
            raw_title = title_elem.text
        except Exception:
            raw_title = driver.title

        raw_title = raw_title.replace("상세정보", "").replace("상품비교", "").replace("Ai 가격비교 Beta", "").replace("다나와", "")
        model_name = re.sub(r'\s+', ' ', raw_title).strip()
        product_data["model_name"] = model_name
        print(f"✅ 상품명: {model_name}")

        # '상품정보 더보기' 버튼 클릭
        try:
            more_button = driver.find_element(By.XPATH, "//*[contains(text(), '상품정보 더보기') or contains(text(), '상세정보 더보기')]")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", more_button)
            time.sleep(2)
            driver.execute_script("arguments[0].click();", more_button)
            print("✅ [더보기] 버튼 클릭 성공!")
            time.sleep(3)
        except Exception:
            pass

        # 느린 스크롤로 모든 이미지 로딩 대기
        print("🔍 로딩 짤림 방지: 천천히 스크롤을 내리며 이미지를 불러옵니다...")
        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(1.2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        time.sleep(3)

        # 상세 영역 스캔 및 캡처
        print("📸 [2/3] 마케팅 영역 스캔 중...")
        safe_name = re.sub(r'[\\/*?:"<>|]', "", model_name)
        folder_path = os.path.join("product_images", safe_name)
        os.makedirs(folder_path, exist_ok=True)
        screenshot_file = os.path.join(folder_path, "detail_scan.png")

        try:
            detail_area = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#detail_content_wrap, #productDescriptionArea, .product_detail_area"))
            )

            # 헤드리스 모드에서 짤림 방지: 전체 페이지 높이로 창 크기 확장
            total_width = driver.execute_script("return document.body.parentNode.scrollWidth")
            total_height = driver.execute_script("return document.body.parentNode.scrollHeight")
            driver.set_window_size(total_width, total_height + 2000)
            driver.execute_script("arguments[0].scrollIntoView(true);", detail_area)
            time.sleep(4)

            area_height = detail_area.size['height']
            print(f"📏 타겟 영역 세로 길이: {area_height}px")

            if area_height < 800:
                print("⚠️ [스킵] 이 상품은 다나와 내부에 제공되는 상세 이미지가 없습니다.")
                return None

            detail_area.screenshot(screenshot_file)
            product_data["screenshot_path"] = screenshot_file
            print(f"✅ [성공] 상세페이지 스크린샷 완료: {screenshot_file}")

        except Exception as e:
            print(f"⚠️ 마케팅 영역 캡처 실패: {e}")
            return None

        # 텍스트 스펙 수집
        spec_table = driver.find_elements(By.CLASS_NAME, "prod_spec_table")
        if spec_table:
            rows = spec_table[0].find_elements(By.TAG_NAME, "tr")
            for row in rows:
                ths = row.find_elements(By.TAG_NAME, "th")
                tds = row.find_elements(By.TAG_NAME, "td")
                for i in range(len(ths)):
                    key = ths[i].text.strip()
                    if key:
                        product_data["specs"][key] = tds[i].text.strip() if i < len(tds) else "지원"

        # 스펙 표가 부족하면 .spec_list 텍스트로 보완
        if len(product_data["specs"]) < 3:
            spec_list = driver.find_elements(By.CLASS_NAME, "spec_list")
            if spec_list:
                full_text = re.sub(r'\s+', ' ', spec_list[0].text).strip()
                product_data["raw_specs"] = full_text
                for item in full_text.split(' / '):
                    if ':' in item:
                        k, v = item.split(':', 1)
                        product_data["specs"][k.strip()] = v.strip()
                    elif item.strip():
                        product_data["specs"][item.strip()] = "지원"

    except Exception as e:
        print(f"❌ 크롤링 오류 발생: {e}")
        return None
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return product_data
