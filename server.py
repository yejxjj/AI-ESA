"""
server.py

Fides 메인 FastAPI 서버.
- 다나와 URL 입력
- 크롤링 / OCR / 정규화 / 외부근거 수집
- analysis_engine.py 기반 온톨로지 분석 수행
- SSE(Server-Sent Events)로 진행 상황 스트리밍

전제 파일 구조:
project_root/
├─ server.py
├─ analysis_engine.py
├─ config.py
├─ static/
│  └─ index.html
├─ logic/
│  ├─ crawler.py
│  ├─ ocr_analyzer.py
│  ├─ normalizer.py
│  ├─ llm_resolver.py
│  ├─ patent_scraper.py
│  └─ ...
└─ ontology/
   ├─ ai_capability_master.csv
   ├─ capability_requirement_master.csv
   ├─ confusion_rule_master.csv
   ├─ evidence_pattern_master.csv
   ├─ requirement_evidence_map_master.csv
   ├─ source_credibility_master.csv
   ├─ negative_pattern_master.csv
   └─ capability_scoring_rule_master.csv
"""

from __future__ import annotations

import os
import re
import sys
import json
import uuid
import asyncio
import urllib.parse
import traceback
import concurrent.futures
from datetime import datetime
from typing import Any, AsyncGenerator

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import create_engine, text

# -----------------------------------------------------------------------------
# 경로 설정
# -----------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGIC_DIR = os.path.join(BASE_DIR, "logic")
STATIC_DIR = os.path.join(BASE_DIR, "static")
ONTOLOGY_DIR = os.path.join(BASE_DIR, "ontology")

if LOGIC_DIR not in sys.path:
    sys.path.insert(0, LOGIC_DIR)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# -----------------------------------------------------------------------------
# 외부 / 내부 모듈 import
# -----------------------------------------------------------------------------
from analysis_engine import analyze_feature_scraper_bundle

from crawler import get_product_data
from ocr_analyzer import analyze_ai_washing
from normalizer import normalize_data, expand_company_aliases, is_valid_model_number
from patent_scraper import get_company_patent_data
from llm_resolver import resolve_real_company_name, resolve_model_name

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None
    genai_types = None

try:
    import config

    DATA_GO_KR_KEY = urllib.parse.unquote(getattr(config, "DATA_GO_KR_KEY", ""))
    GEMINI_API_KEY = getattr(config, "GEMINI_API_KEY", "")
    DB_URL = getattr(
        config,
        "DB_URL",
        "mysql+pymysql://root:1234@localhost:3306/CapstonDesign",
    )
except Exception:
    DATA_GO_KR_KEY = ""
    GEMINI_API_KEY = ""
    DB_URL = "mysql+pymysql://root:1234@localhost:3306/CapstonDesign"

try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY) if (genai and GEMINI_API_KEY) else None
except Exception:
    gemini_client = None

engine = create_engine(DB_URL, pool_pre_ping=True)

# -----------------------------------------------------------------------------
# 앱 설정
# -----------------------------------------------------------------------------
app = FastAPI(title="Fides API")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 메모리 작업 저장소
_tasks: dict[str, dict[str, Any]] = {}


# -----------------------------------------------------------------------------
# 요청 모델
# -----------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    url: str


# -----------------------------------------------------------------------------
# 공통 유틸
# -----------------------------------------------------------------------------
def push_event(task_id: str, stage: str, message: str, data: dict | None = None) -> None:
    payload = {
        "type": "progress",
        "stage": stage,
        "message": message,
        "timestamp": datetime.now().isoformat(),
    }
    if data is not None:
        payload["data"] = data
    _tasks[task_id]["events"].append(payload)


def push_error(task_id: str, message: str, detail: str = "") -> None:
    _tasks[task_id]["events"].append(
        {
            "type": "error",
            "message": message,
            "detail": detail,
            "timestamp": datetime.now().isoformat(),
        }
    )


def push_result(task_id: str, result: dict) -> None:
    _tasks[task_id]["result"] = result
    _tasks[task_id]["done"] = True
    _tasks[task_id]["events"].append(
        {
            "type": "result",
            "timestamp": datetime.now().isoformat(),
            "data": result,
        }
    )


def read_text_file_lines(filename: str) -> list[str]:
    candidates = [
        os.path.join(BASE_DIR, filename),
        os.path.join(LOGIC_DIR, filename),
        filename,
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
    return []


def safe_df_records(df: pd.DataFrame | None) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.fillna("").to_dict(orient="records")


def calc_display_color(score_100: float) -> str:
    if score_100 >= 75:
        return "#c8ff4a"
    if score_100 >= 50:
        return "#f0c040"
    return "#ff5d4b"


def is_danawa_url(url: str) -> bool:
    return "danawa.com" in (url or "")


# -----------------------------------------------------------------------------
# 외부 조회 함수들
# -----------------------------------------------------------------------------
def check_jodale_mall(model_name: str) -> dict[str, Any]:
    """조달청 쇼핑몰 API 조회"""
    import requests as req

    if not DATA_GO_KR_KEY or not model_name:
        return {"status": "스킵", "spec": "", "cert": ""}

    url = "http://apis.data.go.kr/1230000/ShoppingMallPrdInfoService03/getManufacturerItemInfo02"
    params = {
        "serviceKey": DATA_GO_KR_KEY,
        "type": "json",
        "modelNm": model_name,
        "numOfRows": "5",
        "pageNo": "1",
    }

    try:
        res = req.get(url, params=params, timeout=8)
        if res.status_code == 200:
            body = res.json().get("response", {}).get("body", {})
            items = body.get("items") or []
            if items:
                item = items[0]
                return {
                    "status": "등록됨",
                    "spec": item.get("cntrctSpec", ""),
                    "cert": item.get("certInfo", ""),
                    "item": item,
                }
        return {"status": "미등록", "spec": "", "cert": ""}
    except Exception as e:
        return {"status": "에러", "spec": "", "cert": str(e)}



def check_tipa_ai(company_name: str) -> dict[str, Any]:
    """TIPA 제조AI 솔루션 공급기업 조회"""
    import requests as req

    if not DATA_GO_KR_KEY or not company_name or company_name == "미확인":
        return {"status": "스킵", "solution_name": ""}

    url = "http://apis.data.go.kr/1352000/TIPA_MnfctAI_Sol_Sply_Entps_Stat/getMnfctAI_Sol_Sply_Entps_Stat"
    params = {
        "serviceKey": DATA_GO_KR_KEY,
        "type": "json",
        "entpsNm": company_name,
        "numOfRows": "5",
        "pageNo": "1",
    }

    try:
        res = req.get(url, params=params, timeout=8)
        if res.status_code == 200:
            body = res.json().get("response", {}).get("body", {})
            items = body.get("items") or []
            total = body.get("totalCount", 0)
            if total and items:
                item = items[0]
                return {
                    "status": "인증기업",
                    "solution_name": item.get("aiSolNm", ""),
                    "item": item,
                }
        return {"status": "미등록", "solution_name": ""}
    except Exception as e:
        return {"status": "에러", "solution_name": str(e)}



def check_koraia(company_name: str) -> dict[str, Any]:
    """로컬 화이트리스트 기반 KORAIA 여부 확인"""
    whitelist = read_text_file_lines("koraia_list.txt")
    if not whitelist:
        return {"status": "목록 없음"}
    return {"status": "인증기업"} if any(w in company_name for w in whitelist) else {"status": "미등록"}



def clean_ocr_text_with_gemini(product_data: dict[str, Any]) -> dict[str, Any] | None:
    """OCR 텍스트 정제 + 회사명 / 모델명 추출"""
    ocr_str = product_data.get("ocr_extracted_text", "")
    if not str(ocr_str).strip() or not gemini_client:
        return None

    product_name = product_data.get("model_name", "알 수 없는 제품")
    specs_str = json.dumps(product_data.get("specs", {}), ensure_ascii=False)

    system_instruction = (
        "너는 이커머스 상세페이지 데이터 정제 전문가다. "
        "반드시 JSON만 반환한다."
        "company_name, exact_model_name, cleaned_text 세 키만 반환하라."
    )
    user_prompt = (
        f"[제품명]\n{product_name}\n\n"
        f"[스펙표]\n{specs_str}\n\n"
        f"[OCR 텍스트]\n{ocr_str}"
    )

    try:
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
    except Exception:
        return None



def search_kc_db(
    norm_info: dict[str, Any],
    product_json: dict[str, Any],
    has_real_company: bool,
    target_company_name: str,
    model_param: str,
) -> pd.DataFrame:
    """로컬 KC DB 검색"""
    try:
        # 1) 법인명 기준
        if has_real_company and target_company_name:
            real_names = [n.strip() for n in target_company_name.split(",") if n.strip()]
            real_cores = list({n.replace("주식회사", "").replace("(주)", "").strip() for n in real_names})
            if real_cores:
                cond = " OR ".join([f"company_name LIKE :c{i}" for i, _ in enumerate(real_cores)])
                params = {f"c{i}": f"%{name}%" for i, name in enumerate(real_cores)}
                with engine.connect() as conn:
                    df = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({cond}) LIMIT 50"), conn, params=params)
                    if not df.empty:
                        return df

        # 2) 정규화 모델명 기준
        model_candidates = [m for m in norm_info.get("extracted_tech_models", []) if "-" in str(m)]
        if is_valid_model_number(model_param) and model_param not in model_candidates:
            model_candidates.insert(0, model_param)
        if model_candidates:
            cond = " OR ".join([f"model_name LIKE :m{i}" for i, _ in enumerate(model_candidates)])
            params = {f"m{i}": f"%{name}%" for i, name in enumerate(model_candidates)}
            with engine.connect() as conn:
                df = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({cond}) LIMIT 50"), conn, params=params)
                if not df.empty:
                    return df

        # 3) Gemini 추론 모델명
        gemini_model = resolve_model_name(product_json.get("model_name", ""), product_json.get("raw_specs", ""))
        if gemini_model:
            models = [m.strip() for m in str(gemini_model).split(",") if m.strip()]
            if models:
                cond = " OR ".join([f"model_name LIKE :g{i}" for i, _ in enumerate(models)])
                params = {f"g{i}": f"%{name}%" for i, name in enumerate(models)}
                with engine.connect() as conn:
                    df = pd.read_sql(text(f"SELECT * FROM kc_ai_products WHERE ({cond}) LIMIT 50"), conn, params=params)
                    if not df.empty:
                        return df

        # 4) 상품명 첫 토큰 fallback
        if product_json.get("model_name"):
            kw = str(product_json["model_name"]).split()[0]
            with engine.connect() as conn:
                return pd.read_sql(
                    text(
                        "SELECT * FROM kc_ai_products "
                        "WHERE model_name LIKE :kw OR equip_name LIKE :kw LIMIT 50"
                    ),
                    conn,
                    params={"kw": f"%{kw}%"},
                )
    except Exception as e:
        print(f"⚠️ KC DB 검색 오류: {e}")

    return pd.DataFrame()



def search_cert_db(company_aliases: list[str]) -> pd.DataFrame:
    """GS/NEP 등 인증 DB 검색"""
    if not company_aliases:
        return pd.DataFrame()
    try:
        cond = " OR ".join([f"company_name LIKE :a{i}" for i, _ in enumerate(company_aliases)])
        params = {f"a{i}": f"%{name}%" for i, name in enumerate(company_aliases)}
        with engine.connect() as conn:
            return pd.read_sql(
                text(
                    "SELECT cert_type, cert_no, product_name, company_name, cert_date, expire_date "
                    f"FROM cert_products WHERE ({cond}) LIMIT 30"
                ),
                conn,
                params=params,
            )
    except Exception as e:
        print(f"⚠️ 인증 DB 검색 오류: {e}")
        return pd.DataFrame()


# -----------------------------------------------------------------------------
# 메인 분석 파이프라인
# -----------------------------------------------------------------------------
def run_analysis(task_id: str, url: str) -> None:
    import time
    _t = {}  # 단계별 소요시간 기록

    def tick(label: str):
        _t[label] = time.time()

    def tock(label: str) -> float:
        elapsed = round(time.time() - _t.get(label, time.time()), 2)
        print(f"⏱  [{label}] {elapsed}s")
        return elapsed

    try:
        push_event(task_id, "validate", "URL 검증 중")
        if not is_danawa_url(url):
            raise ValueError("다나와 상품 URL만 지원합니다.")

        # 1. 크롤링
        push_event(task_id, "crawl", "상품 페이지 크롤링 중")
        tick("crawl")
        product_json = get_product_data(url)
        if not isinstance(product_json, dict) or not product_json:
            raise RuntimeError("상품 정보를 가져오지 못했습니다.")

        push_event(
            task_id,
            "crawl_done",
            "크롤링 완료",
            {
                "product_name": product_json.get("model_name", ""),
                "spec_count": len(product_json.get("specs", {}) or {}),
                "elapsed": tock("crawl"),
            },
        )

        # 2. OCR
        push_event(task_id, "ocr", "OCR 분석 중")
        tick("ocr")
        image_path = product_json.get("screenshot_path") or product_json.get("detail_image_path") or ""
        ocr_result = analyze_ai_washing(image_path)

        ocr_text = ""
        if isinstance(ocr_result, dict):
            ocr_text = (
                ocr_result.get("extracted_text")
                or ocr_result.get("ocr_extracted_text")
                or ocr_result.get("text")
                or ""
            )
        elif isinstance(ocr_result, str):
            ocr_text = ocr_result

        product_json["ocr_extracted_text"] = ocr_text
        push_event(task_id, "ocr_done", "OCR 완료", {"ocr_length": len(ocr_text), "elapsed": tock("ocr")})

        # 3. Gemini OCR 정제
        push_event(task_id, "gemini", "텍스트 정제 및 회사명/모델명 추출 중")
        tick("gemini")
        gemini_cleaned = clean_ocr_text_with_gemini(product_json)
        if gemini_cleaned:
            product_json["gemini_cleaned"] = gemini_cleaned
            refined_text = gemini_cleaned.get("cleaned_text", "")
        else:
            refined_text = ocr_text

        push_event(
            task_id,
            "gemini_done",
            "정제 완료",
            {
                "company_name": (gemini_cleaned or {}).get("company_name", ""),
                "exact_model_name": (gemini_cleaned or {}).get("exact_model_name", ""),
                "elapsed": tock("gemini"),
            },
        )

        # 4. 정규화
        push_event(task_id, "normalize", "회사명/모델명 정규화 중")
        tick("normalize")
        norm_info = normalize_data(product_json)

        company_name_guess = (gemini_cleaned or {}).get("company_name") or norm_info.get("company_name") or ""
        tick("llm_resolver")
        raw_resolved = resolve_real_company_name(
            company_name_guess,
            product_json.get("model_name", ""),
        ) if company_name_guess else company_name_guess

        # llm_resolver가 설명 문장을 섞어 반환하는 경우가 있어서
        # 짧고 공백이 적은 세그먼트만 법인명으로 추출
        def _clean_company_names(raw: str) -> str:
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            clean = [p for p in parts if len(p) <= 20 and p.count(" ") <= 1]
            return ",".join(clean) if clean else (parts[0] if parts else raw)

        target_company_name = _clean_company_names(raw_resolved)

        model_param = (gemini_cleaned or {}).get("exact_model_name") or norm_info.get("normalized_model_name") or ""
        has_real_company = bool(target_company_name and target_company_name != "미확인")

        # 회사명 변형(주식회사, (주) 등) 전부 생성
        company_aliases = []
        for name in target_company_name.split(","):
            name = name.strip()
            if name:
                company_aliases.extend(expand_company_aliases(name))
        if not company_aliases:
            company_aliases = expand_company_aliases(company_name_guess)

        push_event(
            task_id,
            "normalize_done",
            "정규화 완료",
            {
                "target_company_name": target_company_name,
                "model_param": model_param,
                "aliases": company_aliases[:10],
                "elapsed_normalize": tock("normalize"),
                "elapsed_llm_resolver": tock("llm_resolver"),
            },
        )

        # 5. 병렬 근거 검색
        push_event(task_id, "search", "외부 근거 병렬 검색 중")
        tick("search")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            f_kc = executor.submit(
                search_kc_db,
                norm_info,
                product_json,
                has_real_company,
                target_company_name,
                model_param,
            )
            f_jodale = executor.submit(check_jodale_mall, model_param)
            f_tipa = executor.submit(check_tipa_ai, target_company_name or company_name_guess)
            f_koraia = executor.submit(check_koraia, target_company_name or company_name_guess)
            f_cert = executor.submit(search_cert_db, company_aliases)

            db_results = f_kc.result()
            jodale_result = f_jodale.result()
            tipa_result = f_tipa.result()
            koraia_result = f_koraia.result()
            cert_results = f_cert.result()

        push_event(
            task_id,
            "search_done",
            "외부 근거 검색 완료",
            {
                "kc_count": 0 if db_results is None else len(db_results),
                "jodale_status": jodale_result.get("status"),
                "tipa_status": tipa_result.get("status"),
                "koraia_status": koraia_result.get("status"),
                "cert_count": 0 if cert_results is None else len(cert_results),
                "elapsed": tock("search"),
            },
        )

        # 6. 특허 검색
        push_event(task_id, "patent", "특허 근거 검색 중")
        tick("patent")
        patent_items_df = pd.DataFrame()
        patent_search_type = ""
        try:
            patent_result = get_company_patent_data(company_aliases or [target_company_name or company_name_guess])
            if isinstance(patent_result, tuple) and len(patent_result) == 3:
                _, patent_items_df, patent_search_type = patent_result
            elif isinstance(patent_result, pd.DataFrame):
                patent_items_df = patent_result
        except Exception as e:
            print(f"⚠️ 특허 검색 오류: {e}")

        push_event(task_id, "patent_done", "특허 검색 완료", {"patent_count": len(patent_items_df), "elapsed": tock("patent")})

        # 7. 온톨로지 분석
        push_event(task_id, "analysis", "온톨로지 기반 분석 중")
        tick("analysis")

        # product_json 키 정규화 (엔진이 기대하는 키명으로 추가)
        engine_product_json = dict(product_json)
        engine_product_json.setdefault("name", product_json.get("model_name", ""))
        engine_product_json.setdefault("ocr_text", product_json.get("ocr_extracted_text", ""))
        engine_product_json.setdefault("refined_text", refined_text)

        # DataFrame → list of dicts 변환
        db_records = db_results.to_dict(orient="records") if isinstance(db_results, pd.DataFrame) and not db_results.empty else []
        cert_records = cert_results.to_dict(orient="records") if isinstance(cert_results, pd.DataFrame) and not cert_results.empty else []

        ar = analyze_feature_scraper_bundle(
            ontology_dir=ONTOLOGY_DIR,
            product_json=engine_product_json,
            norm_info=norm_info,
            db_results=db_records,
            jodale_result=jodale_result,
            tipa_result=tipa_result,
            koraia_result=koraia_result,
            patent_items_df=patent_items_df,
            cert_results=cert_records,
            dart_result=None,
            target_company_name=target_company_name,
            model_param=model_param,
        )

        tock("analysis")
        ontology_scores = {
            "accs":     ar.accs,
            "raw_accs": ar.raw_accs,
            "hes":      ar.hes,
            "tes":      ar.tes,
            "ces":      ar.ces,
            "ecs":      ar.ecs,
            "conf":     ar.conf,
        }
        top_capabilities = ar.top_capabilities
        reasons          = ar.reasons
        verdict          = ar.verdict
        risk_level       = ar.risk_level

        # 프론트 호환 계산
        _accs  = ar.accs  / 100.0
        _tes   = ar.tes   / 100.0
        _ces   = ar.ces   / 100.0
        _hes   = ar.hes   / 100.0

        _VERDICT_CLS = {
            "신뢰 가능":       "genuine",
            "추가 검토 필요":  "uncertain",
            "근거 부족":       "uncertain",
            "AI Washing 의심": "washing",
            "불확실":          "uncertain",
        }
        verdict_cls = _VERDICT_CLS.get(verdict, "uncertain")

        _color = calc_display_color  # 0-100 스케일 함수 재사용
        kc_ok      = isinstance(db_results, pd.DataFrame) and not db_results.empty
        jodale_ok  = jodale_result.get("status") == "등록됨"
        tipa_ok    = tipa_result.get("status") == "인증기업"
        koraia_ok  = koraia_result.get("status") == "인증기업"
        gs_count   = (
            len(cert_results[cert_results["cert_type"].str.contains("GS", na=False)])
            if isinstance(cert_results, pd.DataFrame) and not cert_results.empty else 0
        )
        patent_count = len(patent_items_df)

        # claims 파싱 (OCR 텍스트에서 AI 주장 문장 추출)
        AI_KEYWORDS  = ["AI", "인공지능", "딥러닝", "머신러닝", "스마트", "자동", "최적화", "뉴럴", "학습"]
        VAGUE_WORDS  = ["최고", "최대", "최적", "완벽", "독자적", "혁신", "세계 최초", "업계 최고"]
        sentences = [s.strip() for s in re.split(r"[.。\n]", ocr_text) if len(s.strip()) > 20][:12]
        claims = []
        for sent in sentences:
            has_ai    = any(k in sent for k in AI_KEYWORDS)
            has_vague = any(k in sent for k in VAGUE_WORDS)
            level = "high" if (has_ai and has_vague) else ("medium" if has_ai else "low")
            flags = []
            if has_ai:    flags.append({"label": "AI 주장",    "type": "ai"})
            if has_vague: flags.append({"label": "모호한 표현", "type": "vague"})
            if not flags: flags.append({"label": "일반 설명",   "type": "ok"})
            claims.append({"text": sent, "level": level, "flags": flags})

        best_kc = db_results.iloc[0] if kc_ok else None

        result = {
            # 기본 정보
            "requested_url": url,
            "product_name":  product_json.get("model_name", ""),
            "brand":         target_company_name or company_name_guess,
            "company_name":  target_company_name or company_name_guess,
            "model_name":    model_param,
            "timestamp":     datetime.now().strftime("%Y.%m.%d %H:%M"),
            "url":           url,
            # 판정
            "verdict":      verdict,
            "verdict_text": verdict,
            "verdict_cls":  verdict_cls,
            "risk_level":   risk_level,
            "risk_color":   _color(ar.accs),
            # 점수 (0-1 스케일, 프론트 호환)
            "trust_score":    round(_accs, 2),
            "text_score":     round(_tes,  2),
            "verify_score":   round(_ces,  2),
            "relation_score": round(_hes,  2),
            # 색상
            "text_color":     _color(ar.tes),
            "verify_color":   _color(ar.ces),
            "relation_color": _color(ar.hes),
            # 설명
            "text_desc":     f"기술 근거 점수 {ar.tes:.1f}점 (특허·DART 기반)",
            "verify_desc":   " / ".join(filter(None, [
                "전파인증 확인" if kc_ok    else "",
                "조달청 등록"   if jodale_ok else "",
                "TIPA 인증"    if tipa_ok   else "",
                "KORAIA 인증"  if koraia_ok else "",
            ])) or "공공 인증 미확인",
            "relation_desc": (
                f"AI 특허 {patent_count}건" + (f" / GS인증 {gs_count}건" if gs_count else "")
                if patent_count else "특허·인증 미확인"
            ),
            # 텍스트 분석
            "claims": claims,
            # 검증 항목
            "verification": {
                "kc": {
                    "ok":     kc_ok,
                    "detail": (
                        f"{best_kc['company_name']} / {best_kc.get('model_name','')} — 인증번호 {best_kc.get('cert_no','')}"
                        if best_kc is not None else "KC 전파인증 DB에서 해당 모델을 찾지 못했습니다."
                    ),
                },
                "jodale": {
                    "status": jodale_result["status"],
                    "cls":    "pass" if jodale_ok else ("warn" if jodale_result["status"] == "스킵" else "fail"),
                    "detail": jodale_result.get("spec") or jodale_result.get("cert") or "해당 없음",
                },
                "tipa": {
                    "status": tipa_result["status"],
                    "cls":    "pass" if tipa_ok else "fail",
                    "detail": tipa_result.get("solution_name") or "해당 없음",
                },
                "koraia": {
                    "status": koraia_result["status"],
                    "cls":    "pass" if koraia_ok else ("warn" if koraia_result["status"] == "목록 없음" else "fail"),
                },
                "gs": {
                    "count":  gs_count,
                    "detail": f"GS인증 {gs_count}건 포함 총 {len(cert_results)}건 확인" if isinstance(cert_results, pd.DataFrame) and not cert_results.empty else "인증 기록 없음",
                    "cls":    "pass" if gs_count > 0 else "warn",
                },
                "patent": {
                    "count":       patent_count,
                    "search_type": patent_search_type,
                    "brand":       target_company_name or company_name_guess,
                    "cls":         "pass" if patent_count > 0 else "warn",
                },
            },
            # 구 프론트 호환 필드
            "patent_count":       patent_count,
            "gs_count":           gs_count,
            "relation_score_val": round(_hes, 2),
            "patent_search_type": patent_search_type,
            "patents": [
                {
                    "title":     str(row.get("발명의명칭(한글)", "제목 없음"))[:60],
                    "applicant": str(row.get("출원인", "—"))[:20],
                    "date":      str(row.get("출원일자", "—")),
                    "status":    str(row.get("등록상태", "—")),
                }
                for _, row in patent_items_df.head(15).iterrows()
            ] if not patent_items_df.empty else [],
            "certs": [
                {
                    "type":   str(row.get("cert_type", "")),
                    "no":     str(row.get("cert_no", "")),
                    "name":   str(row.get("product_name", ""))[:50],
                    "expire": str(row.get("expire_date", "")),
                }
                for _, row in cert_results.iterrows()
            ] if isinstance(cert_results, pd.DataFrame) and not cert_results.empty else [],
            "specs": [
                {"key": k, "value": v}
                for k, v in (product_json.get("specs") or {}).items()
            ],
            # 온톨로지 원본
            "reasons":          reasons,
            "ontology_scores":  ontology_scores,
            "top_capabilities": top_capabilities,
            "capability_scores": ar.capability_scores,
            "claim_text":       ar.details.get("claim_text", refined_text),
            # 원본 근거
            "evidence_summary": {
                "kc_count":      len(db_results) if isinstance(db_results, pd.DataFrame) else 0,
                "patent_count":  patent_count,
                "cert_count":    len(cert_results) if isinstance(cert_results, pd.DataFrame) else 0,
                "jodale_status": jodale_result.get("status"),
                "tipa_status":   tipa_result.get("status"),
                "koraia_status": koraia_result.get("status"),
            },
            "raw_data": {
                "product_json":  product_json,
                "normalized":    norm_info,
                "kc_results":    safe_df_records(db_results),
                "patent_items":  safe_df_records(patent_items_df),
                "cert_results":  safe_df_records(cert_results),
                "jodale_result": jodale_result,
                "tipa_result":   tipa_result,
                "koraia_result": koraia_result,
            },
        }

        push_event(
            task_id,
            "analysis_done",
            "최종 분석 완료",
            {
                "accs": ontology_scores.get("accs", 0.0),
                "verdict": verdict,
                "risk_level": risk_level,
            },
        )
        push_result(task_id, result)

    except Exception as e:
        trace = traceback.format_exc()
        print(trace)
        _tasks[task_id]["done"] = True
        push_error(task_id, str(e), trace)


# -----------------------------------------------------------------------------
# SSE 스트림
# -----------------------------------------------------------------------------
async def event_stream(task_id: str) -> AsyncGenerator[str, None]:
    sent = 0
    while True:
        task = _tasks.get(task_id)
        if not task:
            yield f"data: {json.dumps({'type': 'error', 'message': 'task not found'}, ensure_ascii=False)}\n\n"
            return

        events = task.get("events", [])
        while sent < len(events):
            payload = events[sent]
            sent += 1
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if task.get("done") and sent >= len(events):
            return

        await asyncio.sleep(0.3)


# -----------------------------------------------------------------------------
# 라우트
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    if not req.url.strip():
        raise HTTPException(status_code=400, detail="URL이 비어 있습니다.")

    task_id = str(uuid.uuid4())
    _tasks[task_id] = {
        "events": [],
        "done": False,
        "result": None,
        "created_at": datetime.now().isoformat(),
    }

    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, run_analysis, task_id, req.url.strip())
    return {"task_id": task_id}


@app.get("/api/stream/{task_id}")
async def stream(task_id: str):
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="task_id를 찾을 수 없습니다.")
    return StreamingResponse(event_stream(task_id), media_type="text/event-stream")


@app.get("/api/result/{task_id}")
def get_result(task_id: str):
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_id를 찾을 수 없습니다.")
    return {
        "done": task.get("done", False),
        "result": task.get("result"),
        "event_count": len(task.get("events", [])),
    }


# -----------------------------------------------------------------------------
# 로컬 실행
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
