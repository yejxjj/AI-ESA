"""
pipeline_main.py — EASy 전체 통합 파이프라인 (로컬 DB 기반 RRA/TTA 검색 최적화)
"""
import os
import sys
import shutil
import concurrent.futures
import re
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.crawler import get_product_data
from logic.ocr_analyzer import analyze_ai_washing
from logic.normalizer import normalize_data
from logic.llm_resolver import resolve_real_company_name, resolve_model_name
from logic.dart_scraper import check_dart_ai_washing

# 통합 API 모듈 임포트 (RRA, TTA는 로컬 DB로 대체되었으므로 제외)
from logic.api import (
    verify_kipris, verify_koneps, verify_pps_mall, 
    verify_nipa_solution, verify_kaiac
)

from analysis_engine import analyze_feature_scraper_bundle

# =====================================================================
# 🗄️ 로컬 DB 연결 설정 (RRA / TTA 검색용)
# =====================================================================
DB_URL = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
engine = create_engine(DB_URL, pool_pre_ping=True)

def search_kc_db_local(company_aliases, model_name):
    """RRA (전파인증) 로컬 DB 검색 - 공백 및 (주) 무시 로직 적용"""
    clean_aliases = list(set([re.sub(r'\(주\)|주식회사|\s', '', a) for a in company_aliases if a]))
    if not clean_aliases: return {'detail': '검색어 없음', 'records': []}
    
    # DB 컬럼의 (주)와 공백을 모두 제거한 후 검색어와 매칭 (강력한 매칭)
    comp_cond = " OR ".join([f"REPLACE(REPLACE(company_name, '(주)', ''), ' ', '') LIKE :c{i}" for i in range(len(clean_aliases))])
    params = {f"c{i}": f"%{c}%" for i, c in enumerate(clean_aliases)}
    
    try:
        with engine.connect() as conn:
            query = f"SELECT * FROM kc_ai_products WHERE ({comp_cond}) LIMIT 50"
            df = pd.read_sql(text(query), conn, params=params)
            records = df.to_dict(orient="records") if not df.empty else []
            if records:
                return {'detail': f"✅ 로컬 DB에서 {len(records)}건 확인 (예: {records[0].get('equip_name')})", 'records': records}
            return {'detail': "❌ DB 매칭 없음 (데이터 없음)", 'records': []}
    except Exception as e:
        return {'error': f"DB 오류: {str(e)}", 'records': []}

def search_cert_db_local(company_aliases):
    """TTA / GS / NEP (품질인증) 로컬 DB 검색 - 공백 및 (주) 무시 로직 적용"""
    clean_aliases = list(set([re.sub(r'\(주\)|주식회사|\s', '', a) for a in company_aliases if a]))
    if not clean_aliases: return {'detail': '검색어 없음', 'records': []}
    
    comp_cond = " OR ".join([f"REPLACE(REPLACE(company_name, '(주)', ''), ' ', '') LIKE :c{i}" for i in range(len(clean_aliases))])
    params = {f"c{i}": f"%{c}%" for i, c in enumerate(clean_aliases)}
    
    try:
        with engine.connect() as conn:
            query = f"SELECT * FROM cert_products WHERE ({comp_cond}) LIMIT 50"
            df = pd.read_sql(text(query), conn, params=params)
            records = df.to_dict(orient="records") if not df.empty else []
            if records:
                return {'detail': f"✅ 로컬 DB에서 {len(records)}건 확인 (예: {records[0].get('product_name')})", 'records': records}
            return {'detail': "❌ DB 매칭 없음 (데이터 없음)", 'records': []}
    except Exception as e:
        return {'error': f"DB 오류: {str(e)}", 'records': []}


# =====================================================================
# 🚀 메인 파이프라인
# =====================================================================
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
    print("🚀 [EASy] 실시간 AI 워싱 검증 파이프라인 가동 (로컬 DB 적용판)")
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
    TEST_MODE = True
    if TEST_MODE:
        official_company = "LG전자"
        search_aliases = ["LG전자", "엘지전자", "엘지전자 주식회사", "(주)엘지전자"]
        print(f"\n⚠️ [TEST MODE ON] 검색을 위해 타겟을 '{official_company}'(으)로 고정합니다.")
    # =====================================================================

    print(f"\n🔍 분석 대상: {official_company} / {official_model}")
    print(f"📡 활용 별칭(Aliases): {search_aliases}") 
    print("⏳ 로컬 DB 및 다중 API 통신을 병렬로 진행합니다...")

    # 2. 병렬 데이터 수집 (🔥 RRA, TTA는 이제 로컬 DB 함수를 호출합니다)
    final_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(check_dart_ai_washing, official_company, scraped_item.get("model_name")): 'DART',
            executor.submit(verify_kipris, search_aliases, product_category): 'KIPRIS',
            executor.submit(search_kc_db_local, search_aliases, official_model): 'RRA',       # 🔥 로컬 DB 호출
            executor.submit(search_cert_db_local, search_aliases): 'TTA',                     # 🔥 로컬 DB 호출
            executor.submit(verify_nipa_solution, search_aliases): 'AI공급',
            executor.submit(verify_pps_mall, search_aliases): '조달몰',
            executor.submit(verify_koneps, search_aliases): '나라장터',
            executor.submit(verify_kaiac, search_aliases): 'KAIAC'
        }
        for future in concurrent.futures.as_completed(futures):
            final_results[futures[future]] = future.result()

    # 3. 온톨로지 분석 엔진 호출
    print("\n🧠 온톨로지 기반 AI Claim Credibility Score(ACCS) 산출 중...")
    
    ontology_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ontology")
    
    product_json = {
        "name": official_model,
        "description": scraped_item.get("description", "") or scraped_item.get("product_name", ""),
        "ocr_text": ocr_text,
        "specs": scraped_item.get("specs", {})
    }

    # 🔥 수정: 로컬 DB 함수의 리턴값에서 'records' 리스트만 안전하게 빼내어 엔진에 전달
    analysis_result = analyze_feature_scraper_bundle(
        ontology_dir=ontology_path,
        product_json=product_json,
        db_results=final_results.get('RRA', {}).get('records', []),   # HES 근거
        jodale_result=final_results.get('조달몰') or final_results.get('나라장터'),
        tipa_result=final_results.get('AI공급'),
        patent_items_df=final_results.get('KIPRIS'),
        cert_results=final_results.get('TTA', {}).get('records', []), # CES 근거
        dart_result=final_results.get('DART'),
        target_company_name=official_company,
        model_param=official_model
    )

    # 4. 리포트 출력
    print("\n" + "="*85)
    print("📄 [1] AI 워싱 검증 상세 근거 (Evidence Detail)")
    print("="*85)

    sources = [
        ('DART 공시 실적', 'DART'), ('KIPRIS 특허 실적', 'KIPRIS'), 
        ('RRA 전파인증 (로컬DB)', 'RRA'), ('TTA/GS 인증 (로컬DB)', 'TTA'),
        ('AI솔루션 공급기업', 'AI공급'), ('조달/나라장터', '조달몰'),
        ('한국인공지능인증센터', 'KAIAC')
    ]

    for title, key in sources:
        res = final_results.get(key, {})
        print(f"\n📌 [{title} 분석]")
        if isinstance(res, dict):
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
    
    verdict_icon = "🟢" if "신뢰" in analysis_result.verdict else "🟡" if "검토" in analysis_result.verdict else "🔴"
    print(f"{verdict_icon} 최종 판정 : {analysis_result.verdict} (위험도: {analysis_result.risk_level})")
    
    print("\n[온톨로지 분석 이유]")
    for reason in analysis_result.reasons:
        print(f" - {reason}")
    print("="*85 + "\n")

if __name__ == "__main__":
    url = "https://prod.danawa.com/info/?pcode=82630370&keyword=lg+ai&cate=10239280"
    run_full_pipeline(url)