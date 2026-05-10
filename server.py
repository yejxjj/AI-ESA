"""
server.py — Fides 메인 서버 (온톨로지 분석 엔진 연동 완료)

FastAPI 기반 웹 서버. 다나와 URL을 입력받아 AI 워싱 여부를 분석하고 결과를 스트리밍으로 반환합니다.

분석 파이프라인 (run_analysis):
    1. 크롤링      — crawler.py로 다나와 상품 페이지 스크래핑 + 스크린샷
    2. OCR         — ocr_analyzer.py로 상세 이미지에서 텍스트 추출
    3. Gemini 정제 — OCR 텍스트 오타 교정 및 회사명·모델명 추출
    4. 정규화      — normalizer.py로 회사명·모델명 정규화 및 동의어 확장
    5. 병렬 검색   — KC DB, 조달청, TIPA, KORAIA, DART 등 동시 조회
    6. 특허·인증   — 특허(KIPRIS) 수, GS/NEP 인증 조회
    7. 🧠 온톨로지 — analysis_engine.py 호출하여 ACCS 신뢰도 및 판정(verdict) 산출
"""

import os
import sys
import json
import uuid
import asyncio
import concurrent.futures
import re
import urllib.parse
from datetime import datetime
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, text
import pandas as pd
from google import genai
from google.genai import types as genai_types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logic'))

from crawler import get_product_data
from ocr_analyzer import analyze_ai_washing
from normalizer import normalize_data, expand_company_aliases, is_valid_model_number
from patent_scraper import get_company_patent_data
from llm_resolver import resolve_real_company_name, resolve_model_name
from dart_scraper import check_dart_ai_washing  # 🔥 DART 스크래퍼 추가

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
    DATA_GO_KR_KEY = urllib.parse.unquote(config.DATA_GO_KR_KEY)
    gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
except Exception:
    DATA_GO_KR_KEY = ""
    gemini_client = None


try:
    from analysis_engine import analyze_feature_scraper_bundle
except ImportError:
    print("⚠️ analysis_engine.py를 찾을 수 없습니다. 루트 디렉토리에 있는지 확인하세요.")

DB_URL = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
engine = create_engine(DB_URL, pool_pre_ping=True)

app = FastAPI(title="Fides API")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 진행 상황 + 결과 저장소 (메모리)
_tasks: dict[str, dict] = {}

# ══════════════════════════════════════════
# 요청 모델
# ══════════════════════════════════════════
class AnalyzeRequest(BaseModel):
    url: str

# ══════════════════════════════════════════
# 유틸 함수 (기존 로직 유지)
# ══════════════════════════════════════════
def load_whitelist(filename: str) -> list[str]:
    base = os.path.dirname(os.path.abspath(__file__))
    for path in [os.path.join(base, filename), os.path.join(base, 'logic', filename), filename]:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
    return []

def check_jodale_mall(model_name: str) -> dict:
    import requests as req
    if not DATA_GO_KR_KEY or not model_name:
        return {"status": "스킵", "spec": "", "cert": ""}
    url = "http://apis.data.go.kr/1230000/ShoppingMallPrdInfoService03/getManufacturerItemInfo02"
    params = {'serviceKey': DATA_GO_KR_KEY, 'type': 'json', 'modelNm': model_name, 'numOfRows': '1', 'pageNo': '1'}
    try:
        res = req.get(url, params=params, timeout=5)
        if res.status_code == 200:
            body = res.json().get('response', {}).get('body', {})
            if body.get('items'):
                item = body['items'][0]
                return {"status": "등록됨", "spec": item.get('cntrctSpec', ''), "cert": item.get('certInfo', '')}
        return {"status": "미등록", "spec": "", "cert": ""}
    except Exception as e:
        return {"status": "에러", "spec": "", "cert": str(e)}

def check_tipa_ai(company_name: str) -> dict:
    import requests as req
    if not DATA_GO_KR_KEY or not company_name or company_name == "미확인":
        return {"status": "스킵", "solution_name": ""}
    url = "http://apis.data.go.kr/1352000/TIPA_MnfctAI_Sol_Sply_Entps_Stat/getMnfctAI_Sol_Sply_Entps_Stat"
    params = {'serviceKey': DATA_GO_KR_KEY, 'type': 'json', 'entpsNm': company_name, 'numOfRows': '1', 'pageNo': '1'}
    try:
        res = req.get(url, params=params, timeout=5)
        if res.status_code == 200:
            body = res.json().get('response', {}).get('body', {})
            if body.get('totalCount', 0) > 0 and body.get('items'):
                item = body['items'][0]
                return {"status": "인증기업", "solution_name": item.get('aiSolNm', '')}
        return {"status": "미등록", "solution_name": ""}
    except Exception as e:
        return {"status": "에러", "solution_name": str(e)}

def check_koraia(company_name: str) -> dict:
    whitelist = load_whitelist("koraia_list.txt")
    if not whitelist:
        return {"status": "목록 없음"}
    return {"status": "인증기업"} if any(c in company_name for c in whitelist) else {"status": "미등록"}

def clean_ocr_text_with_gemini(product_data: dict) -> dict | None:
    ocr_str = product_data.get("ocr_extracted_text", "")
    if not ocr_str.strip():
        return None
    product_name = product_data.get("model_name", "알 수 없는 제품")
    specs_str = json.dumps(product_data.get("specs", {}), ensure_ascii=False)
    system_instruction = (
        "너는 이커머스 상세페이지의 데이터 정제 전문가야. "
        "다음 작업을 수행하고 무조건 JSON 형식으로만 답변해:\n"
        "1. company_name: 제조사/브랜드 이름.\n"
        "2. exact_model_name: 전파인증 검색용 모델명.\n"
        "3. cleaned_text: OCR 텍스트 오타 교정 및 정돈.\n"
        "출력 JSON 키는 company_name, exact_model_name, cleaned_text 세 개만."
    )
    user_prompt = f"[제품명]: {product_name}\n[스펙 표]:\n{specs_str}\n[OCR 텍스트]:\n{ocr_str}"
    try:
        if not gemini_client:
            return None
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"⚠️ Gemini OCR 정제 실패: {e}")
        return None

def search_kc_db(norm_info: dict, product_json: dict, has_real_company: bool, target_company_name: str, model_param: str) -> pd.DataFrame:
    try:
        if has_real_company:
            real_names = [n.strip() for n in target_company_name.split(',') if n.strip()]
            real_cores = list(set([n.replace('주식회사', '').replace('(주)', '').strip() for n in real_names]))
            comp_cond = " OR ".join([f"company_name LIKE :c{i}" for i, _ in enumerate(real_cores)])
            params = {f"c{i}": f"%{c}%" for i, c in enumerate(real_cores)}
            with engine.connect() as conn:
                result = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({comp_cond}) LIMIT 50"), conn, params=params)
            if not result.empty: return result

        model_candidates = [m for m in norm_info.get('extracted_tech_models', []) if '-' in m]
        if is_valid_model_number(model_param) and model_param not in model_candidates:
            model_candidates.insert(0, model_param)
        if model_candidates:
            model_cond = " OR ".join([f"model_name LIKE :m{i}" for i, _ in enumerate(model_candidates)])
            params = {f"m{i}": f"%{m}%" for i, m in enumerate(model_candidates)}
            with engine.connect() as conn:
                result = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({model_cond}) LIMIT 50"), conn, params=params)
            if not result.empty: return result

        if product_json.get('model_name'):
            kw = product_json['model_name'].split()[0]
            with engine.connect() as conn:
                return pd.read_sql(
                    text("SELECT * FROM kc_ai_products WHERE model_name LIKE :kw OR equip_name LIKE :kw LIMIT 50"),
                    conn, params={"kw": f"%{kw}%"}
                )
    except Exception as e:
        print(f"⚠️ KC DB 검색 오류: {e}")
    return pd.DataFrame()

def search_cert_db(company_aliases: list[str]) -> pd.DataFrame:
    if not company_aliases: return pd.DataFrame()
    try:
        cond = " OR ".join([f"company_name LIKE :a{i}" for i, _ in enumerate(company_aliases)])
        params = {f"a{i}": f"%{a}%" for i, a in enumerate(company_aliases)}
        with engine.connect() as conn:
            return pd.read_sql(
                text(f"SELECT cert_type, cert_no, product_name, company_name, cert_date, expire_date FROM cert_products WHERE ({cond}) LIMIT 30"),
                conn, params=params
            )
    except Exception as e:
        print(f"⚠️ 인증 DB 검색 오류: {e}")
        return pd.DataFrame()

def calc_dim_color(v: float) -> str:
    if v >= 60.0: return "#c8ff4a"
    if v >= 35.0: return "#f0c040"
    return "#ff5d4b"

# ══════════════════════════════════════════
# 분석 파이프라인 (비동기 스레드 실행)
# ══════════════════════════════════════════
def run_analysis(task_id: str, url: str):
    def push(step: int, message: str):
        _tasks[task_id]["events"].append({"type": "progress", "step": step, "message": message})

    def fail(message: str):
        _tasks[task_id]["events"].append({"type": "error", "message": message})
        _tasks[task_id]["done"] = True

    try:
        # Step 0: 크롤링
        push(0, "URL 크롤링 및 OCR 처리 중")
        product_json = get_product_data(url)
        if not product_json:
            fail("크롤링 실패 — 다나와 제품 URL을 확인하세요.")
            return

        # Step 1: OCR
        push(1, "이미지 OCR 분석 중")
        image_path = product_json.get("screenshot_path")
        if image_path and os.path.exists(image_path):
            ocr_result = analyze_ai_washing(image_path)
            product_json["ocr_extracted_text"] = ocr_result.get("extracted_text", "")

        # Step 2: Gemini 정제
        push(2, "Gemini 텍스트 정제 중")
        if product_json.get("ocr_extracted_text", "").strip():
            cleaned = clean_ocr_text_with_gemini(product_json)
            if cleaned and cleaned.get("cleaned_text"):
                product_json["ocr_extracted_text"] = cleaned["cleaned_text"]

        # Step 3: 정규화 + 제조사 추적
        push(3, "데이터 정규화 및 제조사 추적 중")
        norm_info = normalize_data(product_json)
        raw_brand = norm_info.get("norm_company", "")
        target_company_name = resolve_real_company_name(raw_brand, product_json.get('model_name', ''))
        has_real_company = bool(target_company_name and target_company_name != raw_brand)

        for name in target_company_name.split(','):
            name = name.strip()
            for variant in expand_company_aliases(name):
                if variant not in norm_info['company_aliases']:
                    norm_info['company_aliases'].append(variant)

        company_aliases = norm_info.get('company_aliases', [])
        model_param = norm_info.get('final_norm_model', '')
        api_company = target_company_name.split(',')[0].strip() if target_company_name else raw_brand

        # Step 4: 공공 API + DB + DART 병렬 검색
        push(4, "외부 증거(인증/특허/공시) 병렬 수집 중")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            f_db = executor.submit(search_kc_db, norm_info, product_json, has_real_company, target_company_name, model_param)
            f_jodale = executor.submit(check_jodale_mall, model_param)
            f_tipa = executor.submit(check_tipa_ai, api_company)
            f_koraia = executor.submit(check_koraia, api_company)
            f_dart = executor.submit(check_dart_ai_washing, api_company, model_param) # 🔥 DART 추가
            
            db_results_df = f_db.result()
            jodale_result = f_jodale.result()
            tipa_result = f_tipa.result()
            koraia_result = f_koraia.result()
            dart_result = f_dart.result()

        # Step 5: 특허 + GS 인증
        raw_specs_str = product_json.get('raw_specs', '')
        product_category = raw_specs_str.split('/')[0].strip() if raw_specs_str else ""
        patent_count, patent_items_df, patent_search_type = get_company_patent_data(company_aliases, product_category)
        cert_results_df = search_cert_db(company_aliases)


        # 🧠 Step 6: 온톨로지 기반 최종 분석 엔진 호출 🧠
        push(5, "온톨로지 엔진 기반 AI 주장 신뢰도(ACCS) 산출 중")
        
        ontology_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ontology")
        
        # 엔진 입력용 product_json 재구성
        product_for_ontology = {
            "name": product_json.get('product_name') or product_json.get('model_name'),
            "description": product_json.get('description', ''),
            "ocr_text": product_json.get('ocr_extracted_text', ''),
            "specs": product_json.get('specs', {})
        }

        # DataFrame을 dict list로 변환하여 어댑터에 안전하게 전달
        db_records = db_results_df.to_dict(orient="records") if not db_results_df.empty else []
        cert_records = cert_results_df.to_dict(orient="records") if not cert_results_df.empty else []

        analysis_result = analyze_feature_scraper_bundle(
            ontology_dir=ontology_dir,
            product_json=product_for_ontology,
            db_results=db_records,
            jodale_result=jodale_result.get('spec', '') or jodale_result.get('cert', ''),
            tipa_result=tipa_result.get('solution_name', '') if tipa_result.get('status') == '인증기업' else None,
            koraia_result="인증됨" if koraia_result.get('status') == '인증기업' else None,
            patent_items_df=patent_items_df,
            cert_results=cert_records,
            dart_result=dart_result.get('detail', '') if dart_result else None,
            target_company_name=api_company,
            model_param=model_param
        )

        # 📊 Step 7: 프론트엔드용 JSON 결과 조립
        push(6, "분석 완료 및 결과 반환")

        brand = norm_info.get('raw_company', '미확인')
        prod_name = product_json.get('model_name', '')

        result_payload = {
            "product_name": prod_name[:60] + ('...' if len(prod_name) > 60 else ''),
            "company_name": brand,
            "url": url,
            "timestamp": datetime.now().strftime("%Y.%m.%d %H:%M"),
            
            # 🔥 온톨로지 결과 매핑 (프론트엔드 스펙에 맞춤)
            "ontology_scores": {
                "accs": analysis_result.accs,
                "raw_accs": analysis_result.raw_accs,
                "hes": analysis_result.hes,
                "tes": analysis_result.tes,
                "ces": analysis_result.ces,
                "ecs": analysis_result.ecs,
                "conf": analysis_result.conf,
            },
            "ontology_verdict": analysis_result.verdict,
            "ontology_risk_level": analysis_result.risk_level,
            "top_capabilities": analysis_result.top_capabilities,
            "ontology_reasons": analysis_result.reasons,
            
            # (기존 UI 호환성을 위한 일부 필드 보존)
            "trust_score": analysis_result.accs / 100, # 0~1 스케일 변환 (색상 바닥용)
            "verdict_cls": "genuine" if "신뢰" in analysis_result.verdict else "washing" if "의심" in analysis_result.verdict else "uncertain",
            "text_color": calc_dim_color(analysis_result.tes),
            "verify_color": calc_dim_color(analysis_result.hes),
            "relation_color": calc_dim_color(analysis_result.ces),
            
            "patent_count": patent_count,
            "gs_count": len(cert_records),
            "specs": [{"key": k, "value": v} for k, v in product_json.get('specs', {}).items()]
        }

        _tasks[task_id]["events"].append({"type": "result", "data": result_payload})
        _tasks[task_id]["done"] = True

    except Exception as e:
        import traceback
        traceback.print_exc()
        _tasks[task_id]["events"].append({"type": "error", "message": f"서버 오류 발생: {str(e)}"})
        _tasks[task_id]["done"] = True

# ══════════════════════════════════════════
# SSE 스트리머 & 라우터 (기존 유지)
# ══════════════════════════════════════════
async def event_stream(task_id: str) -> AsyncGenerator[str, None]:
    sent_idx = 0
    while True:
        task = _tasks.get(task_id)
        if not task:
            yield "data: {\"type\":\"error\",\"message\":\"task not found\"}\n\n"
            return
        events = task["events"]
        while sent_idx < len(events):
            event = events[sent_idx]
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            sent_idx += 1
        if task.get("done") and sent_idx >= len(events): return
        await asyncio.sleep(0.3)

@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join("static", "index.html"), encoding="utf-8") as f:
        return f.read()

@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    url = req.url.strip()
    if not url: raise HTTPException(status_code=400, detail="URL이 비어 있습니다.")
    if "danawa.com" not in url: raise HTTPException(status_code=400, detail="현재 다나와(danawa.com) URL만 지원합니다.")
    
    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"events": [], "done": False}
    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.run_in_executor(executor, run_analysis, task_id, url)
    return {"task_id": task_id}

@app.get("/api/stream/{task_id}")
async def stream(task_id: str):
    if task_id not in _tasks: raise HTTPException(status_code=404, detail="task not found")
    return StreamingResponse(event_stream(task_id), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.1", port=8000)