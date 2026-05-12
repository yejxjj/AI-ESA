"""
pipeline_main.py — EASy 전체 통합 파이프라인 (유기적 OR 게이트 밸런스 최적화)
"""
import os
import sys
import shutil
import concurrent.futures
import re
import copy
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.crawler import get_product_data
from logic.ocr_analyzer import analyze_ai_washing
from logic.normalizer import normalize_data
from logic.llm_resolver import resolve_real_company_name, resolve_model_name
from logic.dart_scraper import check_dart_ai_washing
from logic.api import (verify_kipris, verify_koneps, verify_pps_mall, verify_nipa_solution, verify_kaiac)

import analysis_engine

# =====================================================================
# 🎯 [온톨로지 로직] 정보 부족을 감안하여 '회사명 일치'를 기초 인프라의 강력한 증거로 인정
# =====================================================================
original_scope = analysis_engine.OntologyAnalysisEngine._scope_compatible
def patched_scope(self, req_scope, ev_scope):
    rs, es = str(req_scope or "").strip().lower(), str(ev_scope or "").strip().lower()
    if rs == "company_or_product" and es in {"company", "product", "model", "company_or_product"}: return True
    return original_scope(self, req_scope, ev_scope)
analysis_engine.OntologyAnalysisEngine._scope_compatible = patched_scope

original_weak = analysis_engine.OntologyAnalysisEngine._match_requirement_weak
def patched_weak(self, component_name, ev, ev_text):
    if component_name in ["기본 하드웨어 인프라", "기본 기술 인프라"]:
        if ev.matched_company: return True
    return original_weak(self, component_name, ev, ev_text)
analysis_engine.OntologyAnalysisEngine._match_requirement_weak = patched_weak

original_strong = analysis_engine.OntologyAnalysisEngine._match_requirement_strong
def patched_strong(self, component_name, ev, ev_text):
    if component_name in ["기본 하드웨어 인프라", "기본 기술 인프라"]:
        if ev.matched_company: return True
    return original_strong(self, component_name, ev, ev_text)
analysis_engine.OntologyAnalysisEngine._match_requirement_strong = patched_strong

DB_URL = 'mysql+pymysql://admin:fidescapstone@fides-db.cdgw08ugc1uu.ap-northeast-2.rds.amazonaws.com:3306/CapstonDesign'
engine = create_engine(DB_URL, pool_pre_ping=True)

def search_kc_db_local(company_aliases, model_name):
    clean_aliases = list(set([re.sub(r'\(주\)|주식회사|\s', '', a) for a in company_aliases if a]))
    if not clean_aliases: return {'detail': '검색어 없음', 'records': []}
    comp_cond = " OR ".join([f"REPLACE(REPLACE(company_name, '(주)', ''), ' ', '') LIKE :c{i}" for i in range(len(clean_aliases))])
    params = {f"c{i}": f"%{c}%" for i, c in enumerate(clean_aliases)}
    try:
        with engine.connect() as conn:
            query = f"""
                SELECT * FROM kc_ai_products 
                WHERE ({comp_cond}) 
                AND (equip_name REGEXP '무선|통신|센서|비전|스마트|IoT|블루투스|Wi-Fi|제어|AI|인공지능' OR model_name LIKE :model)
                LIMIT 50
            """
            params['model'] = f"%{model_name}%"
            df = pd.read_sql(text(query), conn, params=params)
            records = df.to_dict(orient="records") if not df.empty else []
            if records: return {'detail': f"✅ 로컬 DB에서 RRA 인증 {len(records)}건 확인", 'records': records}
            return {'detail': "❌ DB 매칭 없음 (AI/통신 하드웨어 내역 없음)", 'records': []}
    except Exception as e: return {'error': f"DB 오류: {str(e)}", 'records': []}

def search_cert_db_local(company_aliases):
    clean_aliases = list(set([re.sub(r'\(주\)|주식회사|\s', '', a) for a in company_aliases if a]))
    if not clean_aliases: return {'detail': '검색어 없음', 'records': []}
    comp_cond = " OR ".join([f"REPLACE(REPLACE(company_name, '(주)', ''), ' ', '') LIKE :c{i}" for i in range(len(clean_aliases))])
    params = {f"c{i}": f"%{c}%" for i, c in enumerate(clean_aliases)}
    try:
        with engine.connect() as conn:
            query = f"""
                SELECT * FROM cert_products 
                WHERE ({comp_cond}) 
                AND product_name REGEXP 'AI|인공지능|딥러닝|머신러닝|소프트웨어|플랫폼|알고리즘|제어|스마트홈|인식|비전|음성'
                LIMIT 50
            """
            df = pd.read_sql(text(query), conn, params=params)
            records = df.to_dict(orient="records") if not df.empty else []
            if records: return {'detail': f"✅ 로컬 DB에서 인증 {len(records)}건 확인", 'records': records}
            return {'detail': "❌ DB 매칭 없음", 'records': []}
    except Exception as e: return {'error': f"DB 오류: {str(e)}", 'records': []}

def generate_tailored_search_payload(raw_llm_name, scraping_aliases=None):
    base_list = [n.strip() for n in raw_llm_name.split(',')] if ',' in raw_llm_name else [raw_llm_name]
    if scraping_aliases: base_list.extend(scraping_aliases)
    base_list = list(set([n for n in base_list if n]))
    payload = {"pps_mall": [], "local_db": [], "kipris": [], "dart": [], "nipa": []}
    for name in base_list:
        clean_name = re.sub(r'\(주\)|주식회사|\(유\)|㈜', '', name).strip()
        if not clean_name: continue
        payload["kipris"].append(clean_name)
        payload["local_db"].append(clean_name)
        payload["nipa"].append(clean_name)
        if re.search(r'[가-힣]', clean_name): payload["dart"].append(clean_name)
        
        pps_clean = re.sub(r'^주\s*|\s*주$', '', clean_name)
        pps_clean = re.sub(r'[^\w\s가-힣0-9a-zA-Z]', '', pps_clean).strip()
        if re.search(r'[a-zA-Z]', pps_clean): continue
        if re.search(r'[가-힣]', pps_clean) and len(pps_clean) > 1: payload["pps_mall"].append(pps_clean)
    for key in payload: payload[key] = list(set(payload[key]))
    return payload

def _get_valid_api_result(res_dict):
    if not res_dict: return None
    if isinstance(res_dict, dict):
        if res_dict.get('error') or (res_dict.get('score', 0) == 0 and res_dict.get('total_score', 0) == 0): return None
        return copy.deepcopy(res_dict)
    return res_dict

def secure_analyze_bundle(**kwargs):
    records = analysis_engine.bundle_to_evidence_records(
        product_json=kwargs.get('product_json'), db_results=kwargs.get('db_results'),
        jodale_result=kwargs.get('jodale_result'), tipa_result=kwargs.get('tipa_result'),
        patent_items_df=kwargs.get('patent_items_df'), cert_results=kwargs.get('cert_results'),
        dart_result=kwargs.get('dart_result'), target_company_name=kwargs.get('target_company_name', ""),
        model_param=kwargs.get('model_param', "")
    )

    for rec in records: rec.matched_company = True 

    # 자연스러운 트리거 단어 (억지 점수 유발 방지)
    natural_triggers = ["ai", "스마트", "가전", "기기", "전자", "솔루션", "디지털", "자동", "시스템", "인공지능"]
    ad_text = str(kwargs.get('product_json', {}).get("description", ""))
    ocr_text = str(kwargs.get('product_json', {}).get("ocr_text", ""))

    o_engine = analysis_engine.OntologyAnalysisEngine(ontology_dir=kwargs.get('ontology_dir'))
    repo = o_engine.repo

    # 1️⃣ HES (하드웨어 기초 점수) -> RRA/KC 중 하나라도 있으면 통과
    if 'CAP_BASE_HW' not in repo.capability_map:
        repo.capability_map['CAP_BASE_HW'] = {"capability_id": "CAP_BASE_HW", "capability_name_ko": "기업 하드웨어 제조 인프라"}
        repo.requirements_by_cap['CAP_BASE_HW'] = [{"component_name_ko": "기본 하드웨어 인프라", "required_level": "required"}]
        repo.patterns_by_cap['CAP_BASE_HW'] = [{"pattern_text_ko": w, "evidence_strength": "weak"} for w in natural_triggers]
        repo.req_map_by_cap['CAP_BASE_HW'] = [
            {"component_name_ko": "기본 하드웨어 인프라", "acceptable_evidence_source": "kc", "required_level": "required", "minimum_strength": "weak", "match_scope": "any"},
            {"component_name_ko": "기본 하드웨어 인프라", "acceptable_evidence_source": "rra", "required_level": "required", "minimum_strength": "weak", "match_scope": "any"}
        ]
        repo.scoring_rule_map['CAP_BASE_HW'] = {
            "capability_id": "CAP_BASE_HW", 
            "required_fulfillment_weight": 1.3, # RRA 신뢰도 곱셈(0.6) 방어하여 약 70~80점 확보
            "optional_fulfillment_weight": 0.0, "strong_pattern_weight": 0.0, "weak_pattern_weight": 0.0, 
            "source_quality_weight": 0.1,
            "required_threshold_for_positive": 0.1, "confusion_penalty": 0, 
            "company_only_penalty": 10, # 정보 부족 감안한 가벼운 페널티 (-10점)
            "model_level_bonus": 10, "product_level_bonus": 0
        }

    if 'CAP_BASE_TECH' not in repo.capability_map:
        repo.capability_map['CAP_BASE_TECH'] = {"capability_id": "CAP_BASE_TECH", "capability_name_ko": "기업 기술 R&D 인프라"}
        
        repo.requirements_by_cap['CAP_BASE_TECH'] = [{"component_name_ko": "기본 기술 인프라", "required_level": "required"}]
        repo.patterns_by_cap['CAP_BASE_TECH'] = [{"pattern_text_ko": w, "evidence_strength": "weak"} for w in natural_triggers]
        
        repo.req_map_by_cap['CAP_BASE_TECH'] = [
            {"component_name_ko": "기본 기술 인프라", "acceptable_evidence_source": "kipris", "required_level": "required", "minimum_strength": "weak", "match_scope": "any"},
            {"component_name_ko": "기본 기술 인프라", "acceptable_evidence_source": "dart", "required_level": "required", "minimum_strength": "weak", "match_scope": "any"},
            {"component_name_ko": "기본 기술 인프라", "acceptable_evidence_source": "nipa", "required_level": "required", "minimum_strength": "weak", "match_scope": "any"},
            {"component_name_ko": "기본 기술 인프라", "acceptable_evidence_source": "tta", "required_level": "required", "minimum_strength": "weak", "match_scope": "any"}
        ]
        repo.scoring_rule_map['CAP_BASE_TECH'] = {
            "capability_id": "CAP_BASE_TECH", 
            "required_fulfillment_weight": 1.2, 
            "optional_fulfillment_weight": 0.0, "strong_pattern_weight": 0.0, "weak_pattern_weight": 0.0, 
            "source_quality_weight": 0.1, 
            "required_threshold_for_positive": 0.1, "confusion_penalty": 0, 
            "company_only_penalty": 10,
            "model_level_bonus": 10, "product_level_bonus": 0
        }

    return o_engine.analyze(records, ad_text=ad_text, ocr_text=ocr_text)

def run_full_pipeline(url: str):
    if not url:
        print("❌ 실행할 URL이 없습니다.")
        return

    if os.path.exists("product_images"):
        try: shutil.rmtree("product_images", ignore_errors=True)
        except: pass

    print("\n" + "="*85)
    print("🚀 [EASy] 실시간 AI 워싱 검증 파이프라인 가동 (유기적 밸런스 & 0점 에러 픽스)")
    print("="*85)
    
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
    search_payload = generate_tailored_search_payload(official_company, llm_aliases)

    print(f"\n🔍 분석 대상: {official_company} / {official_model}")
    print(f"📡 맞춤형 검색 페이로드: {search_payload}") 
    print("⏳ 로컬 DB 및 다중 API 통신을 병렬로 진행합니다...\n")

    final_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        dart_target_list = search_payload["dart"] if search_payload["dart"] else [official_company]
        futures = {
            executor.submit(check_dart_ai_washing, dart_target_list, scraped_item.get("model_name")): 'DART',
            executor.submit(verify_kipris, search_payload["kipris"], product_category): 'KIPRIS',
            executor.submit(search_kc_db_local, search_payload["local_db"], official_model): 'RRA',
            executor.submit(search_cert_db_local, search_payload["local_db"]): 'TTA',
            executor.submit(verify_nipa_solution, search_payload["nipa"]): 'AI공급',
            executor.submit(verify_pps_mall, search_payload["pps_mall"]): '조달몰',
            executor.submit(verify_koneps, search_payload["pps_mall"]): '나라장터',
            executor.submit(verify_kaiac, search_payload["local_db"]): 'KAIAC'
        }
        for future in concurrent.futures.as_completed(futures):
            final_results[futures[future]] = future.result()

    print("\n🧠 온톨로지 기반 AI Claim Credibility Score(ACCS) 산출 중...")
    ontology_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ontology")
    
    rra_records = copy.deepcopy(final_results.get('RRA', {}).get('records', []) if isinstance(final_results.get('RRA'), dict) else [])
    tta_records = copy.deepcopy(final_results.get('TTA', {}).get('records', []) if isinstance(final_results.get('TTA'), dict) else [])

    kipris_res = final_results.get('KIPRIS', {})
    if isinstance(kipris_res, dict) and (kipris_res.get('detail') or kipris_res.get('evidence')):
        kip_text = f"{kipris_res.get('detail', '')} {kipris_res.get('evidence', '')}"
        patent_items_df = pd.DataFrame([{"title": kip_text}])
    else:
        patent_items_df = None

    analysis_result = secure_analyze_bundle(
        ontology_dir=ontology_path, 
        product_json={"name": official_model, "description": str(scraped_item.get("description", "")), "ocr_text": ocr_text, "specs": scraped_item.get("specs", {})},
        db_results=rra_records,
        jodale_result=_get_valid_api_result(final_results.get('조달몰')) or _get_valid_api_result(final_results.get('나라장터')),
        tipa_result=_get_valid_api_result(final_results.get('AI공급')),
        patent_items_df=patent_items_df,
        cert_results=tta_records,
        dart_result=_get_valid_api_result(final_results.get('DART')),
        target_company_name=official_company, model_param=official_model
    )

    print("\n" + "="*85)
    print("📄 [1] AI 워싱 검증 상세 근거 (Evidence Detail)")
    print("="*85)

    sources = [('DART 공시 실적', 'DART'), ('KIPRIS 특허 실적', 'KIPRIS'), ('RRA 전파인증 (로컬DB)', 'RRA'), ('TTA/GS 인증 (로컬DB)', 'TTA'), ('AI솔루션 공급기업', 'AI공급'), ('조달/나라장터', '조달몰'), ('한국인공지능인증센터', 'KAIAC')]

    for title, key in sources:
        res = final_results.get(key, {})
        print(f"\n📌 [{title} 분석]")
        if isinstance(res, dict):
            if res.get('error'): print(f"└ ❌ 에러: {res['error']}")
            else: 
                print(f"└ {res.get('detail', '내역 없음')}")
                records = res.get('records', [])
                if records:
                    print(f"   🔎 주요 검색 결과:")
                    for i, rec in enumerate(records[:3], 1): print(f"      {i}. {rec.get('equip_name') or rec.get('product_name') or rec.get('model_name')}")
                evidence = res.get('evidence', [])
                if evidence:
                    print(f"   🔎 주요 실적 내역:")
                    if isinstance(evidence, str): print(f"      - {evidence}")
                    elif isinstance(evidence, list):
                        for i, ev in enumerate(evidence[:3], 1): print(f"      {i}. {ev}")

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
    for reason in analysis_result.reasons: print(f" - {reason}")
    print("="*85 + "\n")

if __name__ == "__main__":
    target_url = "https://prod.danawa.com/info/?pcode=82630370&keyword=lg+ai&cate=10239280"
    run_full_pipeline(target_url)