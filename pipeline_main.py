"""
pipeline_main.py — EASy 전체 통합 파이프라인 (범용 정규화 및 테스트 모드 지원)
"""
import os
import sys
import shutil
import concurrent.futures
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.crawler import get_product_data
from logic.ocr_analyzer import analyze_ai_washing
from logic.normalizer import normalize_data
from logic.llm_resolver import resolve_real_company_name, resolve_model_name
from logic.dart_scraper import check_dart_ai_washing

# 통합 API 모듈 임포트
from logic.api import (
    verify_rra, verify_tta, verify_kipris, 
    verify_koneps, verify_pps_mall, verify_nipa_solution, verify_kaiac
)

def generate_search_terms(raw_name, scraping_aliases):
    """
    [하드코딩 제거 완료]
    LG, 삼성 등 특정 기업 하드코딩을 빼고, 
    들어온 이름에 '(주)', '주식회사' 등을 조합하여 범용 검색 리스트를 자동 생성합니다.
    """
    base_list = scraping_aliases if scraping_aliases and len(scraping_aliases) > 1 else [raw_name]
    extended_list = list(base_list)
    
    for name in base_list:
        if not name: continue
        # (주), 주식회사 등의 껍데기를 벗긴 순수 이름 추출
        clean = re.sub(r'\(주\)|주식회사|\(유\)|주\s|' , '', name).strip()
        
        if clean and clean not in extended_list:
            extended_list.append(clean)
            extended_list.append(f"{clean} 주식회사")
            extended_list.append(f"{clean}(주)")
            extended_list.append(f"(주){clean}")
            
    return list(set([n.strip() for n in extended_list if n]))

def run_full_pipeline(url: str):
    if os.path.exists("product_images"):
        try: shutil.rmtree("product_images", ignore_errors=True)
        except: pass

    print("\n" + "="*85)
    print("🚀 [EASy] 실시간 AI 워싱 검증 파이프라인 가동")
    print("="*85)
    
    scraped_item = get_product_data(url)
    if not scraped_item: return
    img_path = scraped_item.get("screenshot_path", "")
    ocr_text = analyze_ai_washing(img_path).get("extracted_text", "") if img_path else ""
    
    norm_result = normalize_data(scraped_item)
    official_company = resolve_real_company_name(norm_result.get("raw_company", ""), scraped_item.get("model_name", ""))
    official_model = resolve_model_name(scraped_item.get("model_name", ""), ocr_text) or norm_result.get("final_norm_model", "미확인")
    product_category = scraped_item.get("category", "") if isinstance(scraped_item.get("category"), str) else ""


    llm_aliases = scraped_item.get("aliases", [])
    search_aliases = generate_search_terms(official_company, llm_aliases)
    
    if official_company not in search_aliases:
        search_aliases.insert(0, official_company)


    # =====================================================================
    TEST_MODE = True
    
    if TEST_MODE:
        # 🟢 DART용 공식 명칭 (영문 포함 그대로)
        official_company = "LG전자"
        
        # 🔵 KIPRIS, NIPA, 조달청 등 융통성 없는 서버를 위한 별칭(Alias) 총알들
        search_aliases = ["LG전자", "엘지전자", "엘지전자 주식회사", "(주)엘지전자"]
        
        print(f"\n⚠️ [TEST MODE ON] API 생존 검증을 위해 타겟을 '{official_company}'(으)로 고정합니다.")
    # =====================================================================

    print(f"\n🔍 분석 대상: {official_company} / {official_model}")
    print(f"📡 활용 별칭(Aliases): {search_aliases}") 
    print("⏳ 다중 API 통신 및 로컬 DB를 병렬로 긁어옵니다...")

    final_results = {}
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as executor:
        futures = {
            executor.submit(check_dart_ai_washing, official_company, scraped_item.get("model_name")): 'DART',
            executor.submit(verify_kipris, search_aliases, product_category): 'KIPRIS',
            executor.submit(verify_rra, search_aliases, official_model): 'RRA',
            executor.submit(verify_tta, search_aliases): 'TTA',
            executor.submit(verify_nipa_solution, search_aliases): 'AI공급',
            executor.submit(verify_pps_mall, search_aliases): '조달몰',
            executor.submit(verify_koneps, search_aliases): '나라장터',
            executor.submit(verify_kaiac, search_aliases): 'KAIAC'
        }
        for future in concurrent.futures.as_completed(futures):
            final_results[futures[future]] = future.result()

    score_dart = int(final_results.get('DART', {}).get('total_score', 0) * 0.4)
    score_kipris = final_results.get('KIPRIS', {}).get('score', 0)
    score_rra = final_results.get('RRA', {}).get('score', 0)
    score_tta = final_results.get('TTA', {}).get('score', 0)
    score_nipa = final_results.get('AI공급', {}).get('score', 0)
    
    bonus_pps = final_results.get('조달몰', {}).get('score', 0)
    bonus_koneps = final_results.get('나라장터', {}).get('score', 0)
    bonus_kaiac = final_results.get('KAIAC', {}).get('score', 0)
    
    final_score = min(score_dart + score_kipris + score_rra + score_tta + score_nipa + bonus_pps + bonus_koneps + bonus_kaiac, 100)

    print("\n" + "="*85)
    print("📄 [1] AI 워싱 검증 이유 보고서 (Detail Report)")
    print("="*85)

    sources = [
        ('DART 공시 실적', 'DART'), ('KIPRIS 특허 실적', 'KIPRIS'), 
        ('RRA 전파인증', 'RRA'), ('TTA/GS 인증', 'TTA'),
        ('AI솔루션 공급기업', 'AI공급'), ('조달청 디지털서비스몰', '조달몰'),
        ('나라장터 낙찰정보', '나라장터'), ('한국인공지능인증센터', 'KAIAC')
    ]

    for title, key in sources:
        res = final_results.get(key, {})
        print(f"\n📌 [{title} 분석]")
        if res.get('error'): 
            print(f"└ ❌ 에러: {res['error']}")
        else: 
            print(f"└ {res.get('detail', '내역 없음')}")

    print("\n" + "="*85)
    print("📊 [2] 최종 점수 및 종합 결과 보고서 (Score Report)")
    print("="*85)
    print(f"🏢 타겟 법인: {official_company} | 📦 제품/모델: {official_model}")
    print("-" * 85)

    print("\n[코어 지표]")
    print(f"▶ DART 공시 실적   : {score_dart:02d}점 / 40")
    print(f"▶ KIPRIS 특허 실적 : {score_kipris:02d}점 / 25")
    print(f"▶ RRA 전파인증     : {score_rra:02d}점 / 20")
    print(f"▶ TTA/GS 인증      : {score_tta:02d}점 / 05")
    print(f"▶ AI솔루션 공급기업: {score_nipa:02d}점 / 10")
    
    print("\n[가산점 지표]")
    print(f"▶ 조달청 등록 가점 : {bonus_pps:02d}점 / +5")
    print(f"▶ 나라장터 낙찰 가점: {bonus_koneps:02d}점 / +15")
    print(f"▶ AI인증센터 가점  : {bonus_kaiac:02d}점 / +5")
    print("-" * 85)
    
    print(f"⭐ 최종 AI 신뢰도 점수 : {final_score} / 100 점")
    
    if final_score >= 70:
        print("🟢 판정: AI 기술 실체 확인 (워싱 위험 매우 낮음)")
    elif final_score >= 50:
        print("🟡 판정: 일부 AI 기술 확인 (추가 검증 필요)")
    else:
        print("🔴 판정: AI 기술 근거 부족 (AI 워싱 강력 의심군!)")
    print("="*85 + "\n")

if __name__ == "__main__":
    url = "https://prod.danawa.com/info/?pcode=82630370&keyword=lg+ai&cate=10239280"
    run_full_pipeline(url)