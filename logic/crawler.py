"""
crawler.py — main.py의 안정적인 로직을 100% 반영한 크롤러
"""

import undetected_chromedriver as uc
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import re
import os

def _get_options():
    """main.py와 동일한 브라우저 옵션 설정"""
    options = uc.ChromeOptions()
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--headless')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--disable-gpu')
    return options

def _setup_driver():
    """main.py의 강력한 드라이버 복구 로직"""
    try:
        driver = uc.Chrome(options=_get_options())
    except Exception as e:
        match = re.search(r"Current browser version is (\d+)", str(e))
        if match:
            version = int(match.group(1))
            print(f"🔄 v{version} 버전 감지. ChromeDriverManager로 재설치 후 재시동합니다.")
            # 에러 발생 시 드라이버를 즉시 다운로드하여 실행
            driver_path = ChromeDriverManager().install()
            driver = uc.Chrome(options=_get_options(), driver_executable_path=driver_path, version_main=version)
        else: 
            raise e
    return driver

def get_product_data(url):
    """main.py의 4단계 수집 로직을 그대로 수행합니다."""
    url = url.strip()
    if "danawa.com" not in url: return None

    driver = _setup_driver()
    product_data = {"source": "Danawa", "url": url, "model_name": "", "specs": {}, "raw_specs": "", "screenshot_path": ""}
    
    try:
        print(f"💻 [1/3] 페이지 접속 및 로딩 중...")
        driver.get(url)
        time.sleep(3)
        
        # 1. 상품명 추출
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

        # 2. '더보기' 버튼 클릭 (중앙 스크롤 로직 포함)
        try:
            more_button = driver.find_element(By.XPATH, "//*[contains(text(), '상품정보 더보기') or contains(text(), '상세정보 더보기')]")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", more_button)
            time.sleep(2)
            driver.execute_script("arguments[0].click();", more_button)
            print("✅ [더보기] 버튼 클릭 성공!")
            time.sleep(3) 
        except Exception:
            pass

        # 3. 무한 스크롤 로직 (이미지 로딩 짤림 방지 핵심)
        print("🔍 로딩 짤림 방지: 모든 이미지가 뜰 때까지 무한 스크롤합니다...")
        last_height = driver.execute_script("return document.body.scrollHeight")
        while True:
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(1.2)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break 
            last_height = new_height
        time.sleep(3)

        # 4. 마케팅 영역 정밀 스캔 및 캡처
        print("📸 [2/3] 상세 영역 스캔 및 캡처 중...")
        safe_name = re.sub(r'[\\/*?:"<>|]', "", model_name)
        folder_path = os.path.join("product_images", safe_name)
        os.makedirs(folder_path, exist_ok=True)
        screenshot_file = os.path.join(folder_path, "detail_scan.png")
        
        try:
            detail_area = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#detail_content_wrap, #productDescriptionArea, .product_detail_area"))
            )

            # 창 높이 최대 12000px로 제한 (너무 크면 스크린샷 타임아웃 발생)
            total_width = driver.execute_script("return document.body.parentNode.scrollWidth")
            total_height = min(driver.execute_script("return document.body.parentNode.scrollHeight"), 12000)
            driver.set_window_size(total_width, total_height)
            driver.execute_script("arguments[0].scrollIntoView(true);", detail_area)
            time.sleep(3)

            area_height = detail_area.size['height']
            if area_height > 800:
                # 요소 스크린샷 대신 전체 페이지 스크린샷 사용 (안정성 향상)
                driver.save_screenshot(screenshot_file)
                product_data["screenshot_path"] = screenshot_file
                print(f"✅ 상세 이미지 캡처 성공")
        except Exception as e:
            print(f"⚠️ 상세 영역을 찾지 못해 캡처를 스킵합니다. ({e})")

        # 5. 텍스트 스펙 표 및 spec_list 보완 로직
        print("📋 [3/3] 텍스트 스펙 수집 중...")
        spec_table = driver.find_elements(By.CLASS_NAME, "prod_spec_table")
        if spec_table:
            rows = spec_table[0].find_elements(By.TAG_NAME, "tr")
            for row in rows:
                ths = row.find_elements(By.TAG_NAME, "th"); tds = row.find_elements(By.TAG_NAME, "td")
                for i in range(len(ths)):
                    key = ths[i].text.strip()
                    if key: product_data["specs"][key] = tds[i].text.strip() if i < len(tds) else "지원"
                    
        if len(product_data["specs"]) < 3: # 스펙 표가 부실할 때 리스트에서 추출
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
            try: driver.quit()
            except: pass
                
    return product_data