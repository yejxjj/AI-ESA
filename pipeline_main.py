"""
pipeline_main.py — EASy 전체 통합 파이프라인 (온톨로지 분석 엔진 연동 버전)
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

from analysis_engine import analyze_feature_scraper_bundle

def generate_search_terms(raw_name, scraping_aliases):
    base_list = scraping_aliases if scraping_aliases and len(scraping_aliases) > 1 else [raw_name]
    extended_list = list(base_list)
    for name in base_list:
        if not name: continue
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
    print("🚀 [EASy] 실시간 AI 워싱 검증 파이프라인 가동 (온톨로지 분석)")
    print("="*85)
    
    # 1. 데이터 수집 및 정규화
    scraped_item = get_product_data(url)
    if not scraped_item: return
    img_path = scraped_item.get("screenshot_path", "")
    ocr_result = analyze_ai_washing(img_path) if img_path else {}
    ocr_text = ocr_result.get("extracted_text", "")
    
    norm_result = normalize_data(scraped_item)
    official_company = resolve_real_company_name(norm_result.get("raw_company", ""), scraped_item.get("model_name", ""))
    official_model = resolve_model_name(scraped_item.get("model_name", ""), ocr_text) or norm_result.get("final_norm_model", "미확인")
    product_category = scraped_item.get("category", "") if isinstance(scraped_item.get("category"), str) else ""

    llm_aliases = scraped_item.get("aliases", [])
    search_aliases = generate_search_terms(official_company, llm_aliases)
    
    if official_company not in search_aliases:
        search_aliases.insert(0, official_company)

    # =====================================================================
    # TEST_MODE 설정 (필요 시 수정)
    TEST_MODE = True
    if TEST_MODE:
        official_company = "LG전자"
        search_aliases = ["LG전자", "엘지전자", "엘지전자 주식회사", "(주)엘지전자"]
        print(f"\n⚠️ [TEST MODE ON] API 생존 검증을 위해 타겟을 '{official_company}'(으)로 고정합니다.")
    # =====================================================================

    print(f"\n🔍 분석 대상: {official_company} / {official_model}")
    print(f"📡 활용 별칭(Aliases): {search_aliases}") 
    print("⏳ 다중 API 통신 및 로컬 DB를 병렬로 긁어옵니다...")

    # 2. 병렬 데이터 수집
    final_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
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

    # ---------------------------------------------------------
    # 🔥 [중요] 3. 온톨로지 분석 엔진 호출 (임시 점수 계산 완전 대체)
    # ---------------------------------------------------------
    print("\n🧠 온톨로지 기반 AI Claim Credibility Score(ACCS) 산출 중...")
    
    # 온톨로지 파일 경로 설정 (구조에 맞게)
    ontology_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ontology")
    
    # 엔진에 넘길 제품 주장 텍스트 구성
    product_json = {
        "name": official_model,
        "description": scraped_item.get("description", "") or scraped_item.get("product_name", ""),
        "ocr_text": ocr_text,
        "specs": scraped_item.get("specs", {})
    }

    # 분석 엔진 실행 (수집된 모든 데이터를 어댑터 함수로 전달)
    analysis_result = analyze_feature_scraper_bundle(
        ontology_dir=ontology_path,
        product_json=product_json,
        db_results=[final_results.get('RRA')],  # 하드웨어 근거(HES)
        jodale_result=final_results.get('조달몰') or final_results.get('나라장터'), # 조달/공공(CES)
        tipa_result=final_results.get('AI공급'), # AI 솔루션 공급(CES)
        patent_items_df=final_results.get('KIPRIS'), # 특허 기술(TES)
        cert_results=[final_results.get('TTA'), final_results.get('KAIAC')], # 품질 인증(CES)
        dart_result=final_results.get('DART'), # 공시 기술(TES)
        target_company_name=official_company,
        model_param=official_model
    )

    # ---------------------------------------------------------
    # 📊 4. 최종 온톨로지 기반 리포트 출력
    # ---------------------------------------------------------
    print("\n" + "="*85)
    print("📄 [1] AI 워싱 검증 상세 근거 (Evidence Detail)")
    print("="*85)

    sources = [
        ('DART 공시 실적', 'DART'), ('KIPRIS 특허 실적', 'KIPRIS'), 
        ('RRA 전파인증', 'RRA'), ('TTA/GS 인증', 'TTA'),
        ('AI솔루션 공급기업', 'AI공급'), ('조달/나라장터', '조달몰'),
        ('한국인공지능인증센터', 'KAIAC')
    ]

    for title, key in sources:
        res = final_results.get(key, {})
        print(f"\n📌 [{title} 분석]")
        if res.get('error'): print(f"└ ❌ 에러: {res['error']}")
        else: print(f"└ {res.get('detail', '내역 없음')}")

    print("\n" + "="*85)
    print("📊 [2] 온톨로지 기반 AI 신뢰도 종합 보고서")
    print("="*85)
    print(f"🏢 타겟 법인: {official_company} | 📦 제품/모델: {official_model}")
    print("-" * 85)

    print("\n[지표별 점수 (100점 만점 기준)]")
    print(f"▶ HES (하드웨어 실체성): {analysis_result.hes:05.2f}점")
    print(f"▶ TES (기술적 근거성)  : {analysis_result.tes:05.2f}점")
    print(f"▶ CES (인증/공공 신뢰성): {analysis_result.ces:05.2f}점")
    print(f"▶ ECS (근거 채널 다양성): {analysis_result.ecs:05.2f}점")
    print(f"▶ CONF (분석 신뢰도)    : {analysis_result.conf:05.2f}점")
    print("-" * 85)
    
    print(f"⭐ 최종 AI 주장 신뢰도 (ACCS) : {analysis_result.accs:05.2f} / 100 점")
    
    # 판정 결과 출력
    verdict_icon = "🟢" if "신뢰" in analysis_result.verdict else "🟡" if "검토" in analysis_result.verdict else "🔴"
    print(f"{verdict_icon} 최종 판정 : {analysis_result.verdict} (위험도: {analysis_result.risk_level})")
    
    print("\n[온톨로지 분석 이유]")
    for reason in analysis_result.reasons:
        print(f" - {reason}")
    print("="*85 + "\n")

if __name__ == "__main__":
    url = "https://prod.danawa.com/info/?pcode=82630370&keyword=lg+ai&cate=10239280"
    run_full_pipeline(url)