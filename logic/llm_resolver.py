"""
llm_resolver.py — Gemini 기반 동적 엔티티 리졸루션 (429/503 에러 완벽 방어 및 우회 버전)
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
# from sqlalchemy import create_engine, text # DB 비활성화

def _extract_text_from_response(response, fallback=""):
    """Gemini 응답에서 텍스트를 안전하게 추출합니다."""
    try:
        if response.text:
            return response.text
    except Exception:
        pass
    try:
        for candidate in (response.candidates or []):
            content = candidate.content
            if not content:
                continue
            for part in (content.parts or []):
                t = getattr(part, 'text', None)
                if t:
                    return t
    except Exception:
        pass
    return fallback

def _get_from_cache(brand_name):
    """DB 캐시 조회를 비활성화하고 항상 None을 반환합니다."""
    print(f"   🗄️ [캐시 건너뛰기] '{brand_name}'")
    return None

def _verify_against_db(company_names):
    """DB 검증을 건너뛰고 빈 목록을 반환합니다."""
    return []

def _ask_gemini(prompt):
    """서버 과부하(503) 시 대기, 할당량 초과(429) 시 즉시 우회합니다."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            # 🌟 모델명 2.0으로 통일
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            return _extract_text_from_response(response, fallback="").strip().replace(".", "").replace("\n", "").replace("**", "").strip()
        
        except Exception as e:
            error_msg = str(e)
            # 503 에러 (서버 지연)인 경우만 재시도
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2
                    print(f"⚠️ 서버 부하 감지(503). {wait_time}초 후 다시 시도합니다... ({attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
            
            # 🌟 429 에러(할당량 초과) 등 복구 불가능한 에러는 즉시 빈 문자열 반환 (우회 트리거)
            if "429" in error_msg or "RESOURCE_EXHAUSTED" in error_msg:
                print(f"⚠️ [API 한도 초과] 구글 API 일일 무료 할당량이 모두 소진되었습니다 (429 에러).")
            else:
                print(f"⚠️ 제미나이 API 호출 최종 실패: {e}")
            return ""

def _save_to_cache(brand_name, resolved_company):
    """DB 저장을 수행하지 않고 넘어갑니다."""
    pass

try:
    import config
    _api_key = config.GEMINI_API_KEY
except ImportError:
    _api_key = os.environ.get("GEMINI_API_KEY", "")

client = genai.Client(api_key=_api_key)

def resolve_real_company_name(brand_name, product_name=""):
    """쇼핑몰 브랜드명으로 실제 한국 법인명을 역추적합니다."""
    if not brand_name or brand_name in ["미확인", "없음", ""]:
        return brand_name

    cached = _get_from_cache(brand_name)
    
    print(f"🧠 [동적 엔티티 탐색] '{brand_name}'의 법인명 구글링 중...")

    try:
        result1 = _ask_gemini(f"""
            한국 전파인증(KC) DB와 특허청(KIPRIS)에서 '{brand_name}' 브랜드 제품 '{product_name}'을 찾으려 해.
            이 브랜드와 관련된 한국 법인명(수입사, 제조사, 특허출원인 등)을 모두 찾아줘.
            [출력규칙] 핵심 법인명만 쉼표로 구분해서 나열. 주식회사/(주) 제외. 설명 금지. 못찾으면 '{brand_name}'만 출력.
        """)
        
        # 🌟 API가 뻗어서 빈 값이 돌아오면, 원본 브랜드명을 그대로 넘겨서 파이프라인 생존!
        if not result1:
            print(f"   👉 [우회 모드 작동] API 한도 초과로 원본 브랜드명 '{brand_name}'을(를) 법인명으로 사용합니다.")
            return brand_name

        candidates = [c.strip() for c in result1.split(',') if c.strip()]
        rra_verified = _verify_against_db(candidates)

        if not rra_verified:
            result2 = _ask_gemini(f"""
                '{product_name}' 제품의 한국 KC 전파인증 수입사 또는 책임자 법인명을 찾아줘.
                [출력규칙] 핵심 법인명만 쉼표로 구분. 주식회사/(주) 제외. 설명 금지.
            """)
            extra = [c.strip() for c in result2.split(',') if c.strip()]
            candidates = list(set(candidates + extra))

        final = ','.join(candidates) if candidates else brand_name
        print(f"   👉 최종 법인명: [{final}]")
        return final

    except Exception as e:
        # 안전망: 최악의 에러가 나도 원본을 뱉어냅니다.
        print(f"   👉 [우회 모드 작동] 예기치 않은 오류로 원본 브랜드명 '{brand_name}'을(를) 사용합니다.")
        return brand_name

def resolve_model_name(product_title, specs_text=""):
    """Gemini를 이용해 공식 모델번호를 찾습니다."""
    if not product_title:
        return ""

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=f"""
            너는 전파인증(RRA) DB 검색 전문가야.
            상품명 '{product_title}'의 공식 기술 모델명만 출력해.
            """,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )
        model_name = _extract_text_from_response(response, fallback="")
        return model_name.strip()
        
    except Exception as e:
        # 🌟 모델명 추출 시 429 에러가 나도 조용히 빈 문자열을 뱉어, 메인 파이프라인이 정규화된 모델명을 쓰게 합니다.
        return ""