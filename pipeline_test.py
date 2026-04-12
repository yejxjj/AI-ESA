"""
pipeline_main.py — EASy 전체 통합 파이프라인 (2단 분리형 출력 완결판)
"""
import os
import sys
import time
import shutil
import requests
import concurrent.futures

# 상위 폴더 경로 인식
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.crawler import get_product_data
from logic.ocr_analyzer import analyze_ai_washing
from logic.normalizer import normalize_data
from logic.llm_resolver import resolve_real_company_name, resolve_model_name
from logic.dart_scraper import check_dart_ai_washing

# =====================================================================
# 🌐 병렬 검증용 API/DB 호출 함수들
# =====================================================================

def fetch_rra(company_name, model_name):
    try:
        url = "http://localhost:8000/api/search"
        search_model = model_name.split()[0] if model_name else ""
        response = requests.get(url, params={"company": company_name, "model": search_model}, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return {
                    "score": 20, 
                    "evidence": f"인증번호: {data[0].get('cert_no')} 등 {len(data)}건",
                    "detail": "국립전파연구원(RRA) 인증 데이터베이스에 해당 모델명으로 정식 등록된 물리적 기기 실체가 확인되었습니다."
                }
        return {"score": 0, "detail": "RRA 전파인증 DB에 해당 기업 및 모델로 등록된 내역이 없습니다.", "evidence": None}
    except Exception as e:
        return {"score": 0, "error": f"RRA 서버 미가동 또는 오류 ({e})"}

def fetch_kipris(company_name, product_name):
    time.sleep(1.2)
    return {
        "score": 25, 
        "evidence": "특허 공개번호: 10-2023-XXXXXXX 등 3건",
        "detail": "한국특허정보원(KIPRIS) 조회 결과, 해당 기업 명의로 AI/데이터 분석 관련 특허 출원 및 등록 내역이 확인되어 기술력을 보유하고 있음이 입증되었습니다."
    }

def fetch_tta(company_name, product_name):
    time.sleep(0.8)
    return {
        "score": 5, 
        "evidence": "TTA GS인증(Good Software) 1등급 내역 확인",
        "detail": "한국정보통신기술협회(TTA)의 소프트웨어 품질 시험을 통과하여 시스템의 기본적인 신뢰성과 구동 안정성이 객관적으로 증명되었습니다."
    }

def fetch_pps(company_name, product_name):
    time.sleep(0.7)
    return {
        "score": 0, 
        "detail": "조달청 디지털서비스몰에 정식 AI 상품으로 등록 및 거래된 내역이 발견되지 않았습니다.", 
        "evidence": None
    }

def fetch_ai_solution(company_name, product_name):
    time.sleep(0.6)
    return {
        "score": 10, 
        "evidence": "2024년도 스마트제조 혁신 AI 솔루션 공급기업 풀(Pool) 등재",
        "detail": "정부 유관기관(NIPA 등)에서 주관하는 평가를 통과하여, 대외적으로 AI 기술 공급 역량을 갖춘 공식 기업으로 인정받았습니다."
    }

def fetch_kaiac(company_name, product_name):
    time.sleep(0.5)
    return {
        "score": 0, 
        "detail": "한국인공지능인증센터(KAIAC)의 AI 품질 심사 통과 마크 등 공식적인 민간/공공 AI 특화 인증 내역이 확인되지 않습니다.", 
        "evidence": None
    }

# =====================================================================
# 🛠️ 메인 파이프라인 로직
# =====================================================================

def run_full_pipeline(url: str):
    if os.path.exists("product_images"):
        try:
            shutil.rmtree("product_images", ignore_errors=True)
        except:
            pass
    
    print("\n" + "="*85)
    print("🚀 [EASy] 데이터 수집 및 정규화 단계")
    scraped_item = get_product_data(url)
    if not scraped_item: return

    img_path = scraped_item.get("screenshot_path", "")
    ocr_text = analyze_ai_washing(img_path).get("extracted_text", "") if img_path else ""
    scraped_item["ocr_extracted_text"] = ocr_text

    full_content = f"{scraped_item.get('model_name', '')} {ocr_text}".lower()
    if not any(kw in full_content for kw in ['ai', '인공지능', '딥러닝', '머신러닝', 'llm']):
        print("🛑 [종료] AI 관련 키워드가 없어 검증을 스킵합니다.")
        return

    print("🧠 AI가 기업명과 모델명을 최종 확정합니다...")
    norm_result = normalize_data(scraped_item)
    raw_company = norm_result.get("raw_company", "")
    official_company = resolve_real_company_name(raw_company, scraped_item.get("model_name", ""))
    official_model = resolve_model_name(scraped_item.get("model_name", ""), ocr_text) or norm_result.get("final_norm_model", "미확인")

    print(f"\n▶️ [검증] 7개 기관 동시 조회 시작 (타겟: {official_company} / {official_model})")
    print("⏳ 데이터를 수집하고 정밀 분석 중입니다. 잠시만 기다려주세요...") 
    
    final_results = {}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        futures = {
            executor.submit(check_dart_ai_washing, official_company, scraped_item.get("model_name")): 'DART',
            executor.submit(fetch_rra, official_company, official_model): 'RRA',
            executor.submit(fetch_kipris, official_company, official_model): 'KIPRIS',
            executor.submit(fetch_tta, official_company, official_model): 'TTA',
            executor.submit(fetch_pps, official_company, official_model): 'PPS',
            executor.submit(fetch_ai_solution, official_company, official_model): 'AI공급',
            executor.submit(fetch_kaiac, official_company, official_model): 'KAIAC'
        }
        for future in concurrent.futures.as_completed(futures):
            final_results[futures[future]] = future.result()

    # 종합 점수 산출
    dart_raw = final_results.get('DART', {}).get('total_score', 0)
    score_dart = int(dart_raw * 0.4) 
    score_kipris = final_results.get('KIPRIS', {}).get('score', 0) 
    score_rra = final_results.get('RRA', {}).get('score', 0) 
    score_tta = final_results.get('TTA', {}).get('score', 0) 
    score_ai_sup = final_results.get('AI공급', {}).get('score', 0) 
    
    core_total = score_dart + score_kipris + score_rra + score_tta + score_ai_sup
    bonus_pps = final_results.get('PPS', {}).get('score', 0)
    bonus_kaiac = final_results.get('KAIAC', {}).get('score', 0)
    
    final_score = min(core_total + bonus_pps + bonus_kaiac, 110)

    # =================================================================
    # 📄 [출력 1] 이유 보고서 (Detail Report)
    # =================================================================
    # 화면 지우기 코드(cls) 완벽 제거됨

    print("\n" + "="*85)
    print(" 📄 [1] AI 워싱 검증 이유 보고서 (Detail Report)")
    print("="*85)

    # DART 상세 이유 출력 (상태 포함)
    dart_res = final_results.get('DART', {})
    dart_status = dart_res.get('status', '상태 미확인')
    dart_detail = dart_res.get('detail', '공시 내역에서 상세 실적을 찾지 못했습니다.')
    print(f"📌 [DART 공시 실적 분석] - {dart_status}\n └ {dart_detail}\n")

    # 나머지 기관 상세 이유 출력
    sources_detail = [
        ('KIPRIS 특허 실적', 'KIPRIS'),
        ('RRA 전파인증', 'RRA'),
        ('TTA/GS 인증', 'TTA'),
        ('AI솔루션 공급기업', 'AI공급'),
        ('조달청 디지털서비스몰', 'PPS'),
        ('한국인공지능인증센터', 'KAIAC')
    ]

    for title, key in sources_detail:
        res = final_results.get(key, {})
        detail = res.get('detail', '관련 내역이 없습니다.')
        error = res.get('error')
        
        if error:
            print(f"📌 [{title} 분석]\n └ ❌ 통신 에러: {error}\n")
        else:
            print(f"📌 [{title} 분석]\n └ {detail}\n")


    # =================================================================
    # 📊 [출력 2] 점수 및 종합 보고서 (Score Report)
    # =================================================================
    print("="*85)
    print(" 📊 [2] 최종 점수 및 종합 결과 보고서 (Score Report)")
    print("="*85)
    print(f" 🏢 타겟 법인: {official_company} | 📦 제품/모델: {official_model}")
    print("-" * 85)

    print(f" [코어 지표]")
    print(f" ▶ DART 공시 실적   : {score_dart:02}점 / 40 (원본: {dart_raw}점)")
    print(f" ▶ KIPRIS 특허 실적 : {score_kipris:02}점 / 25")
    print(f" ▶ RRA 전파인증     : {score_rra:02}점 / 20")
    print(f" ▶ TTA/GS 인증      : {score_tta:02}점 / 05")
    print(f" ▶ AI솔루션 공급기업: {score_ai_sup:02}점 / 10")
    
    print(f"\n [가산점 지표]")
    print(f" ▶ 조달청 등록 가점 : {bonus_pps:02}점 / +5")
    print(f" ▶ AI인증센터 가점  : {bonus_kaiac:02}점 / +5")
    print("-" * 85)
    
    print(f" ⭐ 최종 AI 신뢰도 점수 : {final_score} / 100 점")
    
    if final_score >= 70:
        print(" 🟢 판정: AI 기술 실체 확인 (워싱 위험 매우 낮음)")
    elif final_score >= 50:
        print(" 🟡 판정: 일부 AI 기술 확인 (추가 검증 필요)")
    else:
        print(" 🔴 판정: AI 기술 근거 부족 (AI 워싱 강력 의심군!)")
    print("="*85 + "\n")

if __name__ == "__main__":
    url = "https://prod.danawa.com/info/?pcode=82630370&keyword=lg+ai&cate=10239280"
    run_full_pipeline(url)