"""
server.py — Fides 메인 서버

FastAPI 기반 웹 서버. 다나와 URL을 입력받아 AI 워싱 여부를 분석하고 결과를 스트리밍으로 반환합니다.

분석 파이프라인 (run_analysis):
    1. 크롤링      — crawler.py로 다나와 상품 페이지 스크래핑 + 스크린샷
    2. OCR         — ocr_analyzer.py로 상세 이미지에서 텍스트 추출
    3. Gemini 정제 — OCR 텍스트 오타 교정 및 회사명·모델명 추출
    4. 정규화      — normalizer.py로 회사명·모델명 정규화 및 동의어 확장
    5. 병렬 검색   — KC DB, 조달청 API, TIPA API, KORAIA 화이트리스트 동시 조회
    6. 특허·인증   — patent_scraper.py로 AI 특허 수, GS/NEP 인증 조회
    7. 점수 계산   — 텍스트·검증·연관성 3개 차원 점수 합산 → 신뢰도 판정

엔드포인트:
    GET  /                  — index.html 반환
    POST /api/analyze       — 분석 작업 생성, task_id 반환
    GET  /api/stream/{id}   — SSE(Server-Sent Events)로 진행 상황 및 결과 스트리밍
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

try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config
    DATA_GO_KR_KEY = urllib.parse.unquote(config.DATA_GO_KR_KEY)
    gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
except Exception:
    DATA_GO_KR_KEY = ""
    gemini_client = None

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
# 유틸 함수
# ══════════════════════════════════════════
def load_whitelist(filename: str) -> list[str]:
    """로컬 텍스트 파일에서 인증 화이트리스트를 줄 단위로 읽어 반환합니다."""
    base = os.path.dirname(os.path.abspath(__file__))
    for path in [os.path.join(base, filename), os.path.join(base, 'logic', filename), filename]:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
    return []


def check_jodale_mall(model_name: str) -> dict:
    """
    조달청 쇼핑몰 API로 모델명 등록 여부를 조회합니다.
    등록된 경우 규격(spec)과 인증 정보(cert)를 함께 반환합니다.
    """
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
    """
    TIPA(한국산업기술진흥원) API로 제조AI 솔루션 공급기업 인증 여부를 조회합니다.
    인증 기업인 경우 AI 솔루션명(solution_name)을 함께 반환합니다.
    """
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
    """
    로컬 koraia_list.txt 화이트리스트와 대조하여 한국AI인증센터 인증 여부를 반환합니다.
    API 없이 사전에 수집한 인증 기업 목록을 파일로 관리합니다.
    """
    whitelist = load_whitelist("koraia_list.txt")
    if not whitelist:
        return {"status": "목록 없음"}
    return {"status": "인증기업"} if any(c in company_name for c in whitelist) else {"status": "미등록"}


def clean_ocr_text_with_gemini(product_data: dict) -> dict | None:
    """
    Gemini로 OCR 텍스트를 정제합니다.
    - company_name: 제조사/브랜드명 추출
    - exact_model_name: 전파인증 검색용 공식 모델번호 추출
    - cleaned_text: OCR 오타 교정 및 정돈된 텍스트
    OCR 결과가 없으면 None을 반환합니다.
    """
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
    """
    로컬 MySQL DB(kc_ai_products)에서 KC 전파인증 내역을 검색합니다.
    검색 우선순위: ① 법인명 → ② 정규화된 모델번호 → ③ Gemini로 추출한 모델명 → ④ 상품명 첫 단어
    """
    try:
        if has_real_company:
            real_names = [n.strip() for n in target_company_name.split(',') if n.strip()]
            real_cores = list(set([n.replace('주식회사', '').replace('(주)', '').strip() for n in real_names]))
            comp_cond = " OR ".join([f"company_name LIKE :c{i}" for i, _ in enumerate(real_cores)])
            params = {f"c{i}": f"%{c}%" for i, c in enumerate(real_cores)}
            with engine.connect() as conn:
                result = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({comp_cond}) LIMIT 50"), conn, params=params)
            if not result.empty:
                return result

        model_candidates = [m for m in norm_info.get('extracted_tech_models', []) if '-' in m]
        if is_valid_model_number(model_param) and model_param not in model_candidates:
            model_candidates.insert(0, model_param)
        if model_candidates:
            model_cond = " OR ".join([f"model_name LIKE :m{i}" for i, _ in enumerate(model_candidates)])
            params = {f"m{i}": f"%{m}%" for i, m in enumerate(model_candidates)}
            with engine.connect() as conn:
                result = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({model_cond}) LIMIT 50"), conn, params=params)
            if not result.empty:
                return result

        gemini_model = resolve_model_name(product_json.get('model_name', ''), product_json.get('raw_specs', ''))
        if gemini_model:
            g_models = [m.strip() for m in gemini_model.split(',') if m.strip()]
            model_cond = " OR ".join([f"model_name LIKE :g{i}" for i, _ in enumerate(g_models)])
            params = {f"g{i}": f"%{m}%" for i, m in enumerate(g_models)}
            with engine.connect() as conn:
                result = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({model_cond}) LIMIT 50"), conn, params=params)
            if not result.empty:
                return result

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
    """
    로컬 MySQL DB(cert_products)에서 GS인증·NEP 등 기술개발 인증 내역을 검색합니다.
    회사명 동의어 목록(aliases) 전체를 OR 조건으로 검색합니다.
    """
    if not company_aliases:
        return pd.DataFrame()
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
    """신뢰도 점수(0~1)에 따라 프론트엔드 표시용 색상 코드를 반환합니다. (초록/노랑/빨강)"""
    if v >= 0.60: return "#c8ff4a"
    if v >= 0.35: return "#f0c040"
    return "#ff5d4b"


# ══════════════════════════════════════════
# 분석 파이프라인 (동기, 스레드에서 실행)
# ══════════════════════════════════════════
def run_analysis(task_id: str, url: str):
    """
    전체 분석 파이프라인을 동기적으로 실행합니다.
    진행 상황은 _tasks[task_id]["events"]에 순차적으로 쌓이며,
    event_stream()이 SSE로 클라이언트에 전달합니다.
    """
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
            product_json["ocr_extracted_text"] = ocr_result["extracted_text"]

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

        # Step 4: 공공 API + DB 병렬 검색
        push(4, "전파인증 DB + 공공 API 병렬 검색 중")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            f_db = executor.submit(search_kc_db, norm_info, product_json, has_real_company, target_company_name, model_param)
            f_jodale = executor.submit(check_jodale_mall, model_param)
            f_tipa = executor.submit(check_tipa_ai, api_company)
            f_koraia = executor.submit(check_koraia, api_company)
            db_results = f_db.result()
            jodale_result = f_jodale.result()
            tipa_result = f_tipa.result()
            koraia_result = f_koraia.result()

        # Step 5: 특허 + GS 인증
        push(5, "특허 및 GS·NEP 인증 검색 중")
        raw_specs_str = product_json.get('raw_specs', '')
        product_category = raw_specs_str.split('/')[0].strip() if raw_specs_str else ""
        patent_count, patent_items_df, patent_search_type = get_company_patent_data(company_aliases, product_category)
        cert_results = search_cert_db(company_aliases)

        # 신뢰도 점수 계산
        # - text_score: OCR로 추출된 텍스트 양 (AI 주장 근거 텍스트 존재 여부)
        # - verify_score: KC·조달청·TIPA·KORAIA 공공 인증 합산
        # - relation_score: AI 특허 수 + GS인증 수 기반 기술력 점수
        kc_ok = not db_results.empty
        jodale_ok = jodale_result.get('status') == '등록됨'
        tipa_ok = tipa_result.get('status') == '인증기업'
        koraia_ok = koraia_result.get('status') == '인증기업'
        gs_count = len(cert_results[cert_results['cert_type'].str.contains('GS', na=False)]) if not cert_results.empty else 0

        ocr_text = product_json.get("ocr_extracted_text", "")
        text_score = round(min(0.85, max(0.15, len(ocr_text) / 1500)), 2) if ocr_text else 0.15
        verify_pts = (0.50 if kc_ok else 0) + (0.20 if jodale_ok else 0) + (0.20 if tipa_ok else 0) + (0.10 if koraia_ok else 0)
        verify_score = round(min(1.0, verify_pts), 2)
        rel_pts = min(0.70, patent_count * 0.07) + min(0.30, gs_count * 0.15)
        relation_score = round(min(1.0, rel_pts), 2)
        trust_score = round((text_score + verify_score + relation_score) / 3, 2)

        if trust_score >= 0.60:
            verdict_cls, verdict_text = "genuine", "신뢰 가능"
        elif trust_score >= 0.35:
            verdict_cls, verdict_text = "uncertain", "불확실"
        else:
            verdict_cls, verdict_text = "washing", "AI Washing"

        brand = norm_info.get('raw_company', '미확인')
        prod_name = product_json.get('model_name', '')

        # AI 관련 클레임 문장 추출 및 분류
        # level: high(AI주장+모호표현), medium(AI주장만), low(일반)
        AI_KEYWORDS = ['AI', '인공지능', '딥러닝', '머신러닝', '스마트', '자동', '최적화', '뉴럴', '학습']
        VAGUE_WORDS = ['최고', '최대', '최적', '완벽', '독자적', '혁신', '세계 최초', '업계 최고']
        sentences = [s.strip() for s in re.split(r'[.。\n]', ocr_text) if len(s.strip()) > 20][:12] if ocr_text else []
        claims = []
        for sent in sentences:
            has_ai = any(k in sent for k in AI_KEYWORDS)
            has_vague = any(k in sent for k in VAGUE_WORDS)
            level = "high" if (has_ai and has_vague) else ("medium" if has_ai else "low")
            flags = []
            if has_ai: flags.append({"label": "AI 주장", "type": "ai"})
            if has_vague: flags.append({"label": "모호한 표현", "type": "vague"})
            if not flags: flags.append({"label": "일반 설명", "type": "ok"})
            claims.append({"text": sent, "level": level, "flags": flags})

        # KC DB 결과 요약
        best = db_results.iloc[0] if not db_results.empty else None
        kc_detail = (
            f"{best['company_name']} / {best.get('model_name','')} — 인증번호 {best.get('cert_no','')}"
            if best is not None else "KC 전파인증 DB에서 해당 모델을 찾지 못했습니다."
        )

        # 특허 목록 (최대 15건)
        patents = []
        if not patent_items_df.empty:
            for _, row in patent_items_df.head(15).iterrows():
                patents.append({
                    "title": str(row.get('발명의명칭(한글)', '제목 없음'))[:60],
                    "applicant": str(row.get('출원인', '—'))[:20],
                    "date": str(row.get('출원일자', '—')),
                    "status": str(row.get('등록상태', '—')),
                })

        # GS·NEP 인증 목록
        certs = []
        if not cert_results.empty:
            for _, row in cert_results.iterrows():
                certs.append({
                    "type": str(row.get('cert_type', '')),
                    "no": str(row.get('cert_no', '')),
                    "name": str(row.get('product_name', ''))[:50],
                    "expire": str(row.get('expire_date', '')),
                })

        j_detail = jodale_result.get('spec', '') or jodale_result.get('cert', '') or '해당 없음'
        t_detail = tipa_result.get('solution_name', '') or '해당 없음'
        specs = [{"key": k, "value": v} for k, v in product_json.get('specs', {}).items()]

        result = {
            "product_name": prod_name[:60] + ('...' if len(prod_name) > 60 else ''),
            "brand": brand,
            "url": url,
            "timestamp": datetime.now().strftime("%Y.%m.%d %H:%M"),
            "trust_score": trust_score,
            "verdict_cls": verdict_cls,
            "verdict_text": verdict_text,
            "text_score": text_score,
            "verify_score": verify_score,
            "relation_score": relation_score,
            "text_color": calc_dim_color(text_score),
            "verify_color": calc_dim_color(verify_score),
            "relation_color": calc_dim_color(relation_score),
            "text_desc": f"Gemini OCR 정제 완료 — 텍스트 {len(ocr_text)}자 추출" if ocr_text else "이미지 텍스트 없음",
            "verify_desc": " / ".join(filter(None, [
                "전파인증 확인" if kc_ok else "",
                "조달청 등록" if jodale_ok else "",
                "TIPA 인증" if tipa_ok else "",
                "KORAIA 인증" if koraia_ok else "",
            ])) or "공공 인증 미확인",
            "relation_desc": f"AI 특허 {patent_count}건" + (f" / GS인증 {gs_count}건" if gs_count > 0 else "") if patent_count > 0 else "특허·인증 미확인",
            "claims": claims,
            "verification": {
                "kc": {"ok": kc_ok, "detail": kc_detail},
                "jodale": {"status": jodale_result['status'], "cls": "pass" if jodale_ok else ("warn" if jodale_result['status'] == '스킵' else "fail"), "detail": j_detail[:80]},
                "tipa": {"status": tipa_result['status'], "cls": "pass" if tipa_ok else "fail", "detail": t_detail[:80]},
                "koraia": {"status": koraia_result['status'], "cls": "pass" if koraia_ok else ("warn" if koraia_result['status'] == '목록 없음' else "fail")},
                "gs": {"count": gs_count, "detail": f"GS인증 {gs_count}건 포함 총 {len(cert_results)}건 확인" if not cert_results.empty else "인증 기록 없음", "cls": "pass" if gs_count > 0 else "warn"},
                "patent": {"count": patent_count, "search_type": patent_search_type, "brand": brand, "cls": "pass" if patent_count > 0 else "warn"},
            },
            "patent_count": patent_count,
            "gs_count": gs_count,
            "relation_score_val": relation_score,
            "patent_search_type": patent_search_type.split()[0] if patent_search_type else "—",
            "patents": patents,
            "certs": certs,
            "specs": specs,
        }

        _tasks[task_id]["events"].append({"type": "result", "data": result})
        _tasks[task_id]["done"] = True

    except Exception as e:
        _tasks[task_id]["events"].append({"type": "error", "message": str(e)})
        _tasks[task_id]["done"] = True


# ══════════════════════════════════════════
# SSE 스트리머
# ══════════════════════════════════════════
async def event_stream(task_id: str) -> AsyncGenerator[str, None]:
    """
    _tasks 딕셔너리를 0.3초 간격으로 폴링하며 새 이벤트를 SSE 형식으로 yield합니다.
    run_analysis가 done=True로 설정하면 스트림을 종료합니다.
    """
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

        if task.get("done") and sent_idx >= len(events):
            return

        await asyncio.sleep(0.3)


# ══════════════════════════════════════════
# 엔드포인트
# ══════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def index():
    """정적 index.html을 반환합니다."""
    with open(os.path.join("static", "index.html"), encoding="utf-8") as f:
        return f.read()


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    """
    분석 작업을 생성하고 task_id를 반환합니다.
    실제 분석은 별도 스레드(run_analysis)에서 비동기로 실행됩니다.
    현재 다나와(danawa.com) URL만 지원합니다.
    """
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL이 비어 있습니다.")
    if "danawa.com" not in url:
        raise HTTPException(status_code=400, detail="현재 다나와(danawa.com) URL만 지원합니다.")

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {"events": [], "done": False}

    loop = asyncio.get_event_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    loop.run_in_executor(executor, run_analysis, task_id, url)

    return {"task_id": task_id}


@app.get("/api/stream/{task_id}")
async def stream(task_id: str):
    """
    SSE(Server-Sent Events) 스트림을 반환합니다.
    클라이언트는 이 엔드포인트를 구독해 진행 상황(progress)과 최종 결과(result)를 실시간으로 수신합니다.
    """
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="task not found")
    return StreamingResponse(
        event_stream(task_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )
