"""
llm_resolver.py — Gemini 기반 동적 엔티티 리졸루션 (DB 비활성화 버전)
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from sqlalchemy import create_engine, text

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

# --- [DB 연결 및 초기화 부분 주석 처리] ---
# DB_URL = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
# _engine = create_engine(DB_URL)

# with _engine.connect() as conn:
#     conn.execute(text("""
#         CREATE TABLE IF NOT EXISTS brand_resolver_cache (
#             brand_name VARCHAR(200) PRIMARY KEY,
#             resolved_company VARCHAR(200) NOT NULL,
#             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
#         )
#     """))
#     conn.commit()
# ------------------------------------------

def _get_from_cache(brand_name):
    """DB 캐시 조회를 비활성화하고 항상 None을 반환합니다."""
    # with _engine.connect() as conn:
    #     row = conn.execute(
    #         text("SELECT resolved_company FROM brand_resolver_cache WHERE brand_name = :b"),
    #         {"b": brand_name}
    #     ).fetchone()
    # result = row[0] if row else None
    print(f"   🗄️ [캐시 건너뛰기] '{brand_name}'")
    return None

def _verify_against_db(company_names):
    """DB 검증을 건너뛰고 빈 목록을 반환하여 Gemini 검색을 유도합니다."""
    rra_verified = []
    # for name in company_names:
    #     name = name.strip()
    #     if not name:
    #         continue
    #     with _engine.connect() as conn:
    #         row = conn.execute(
    #             text("SELECT 1 FROM kc_ai_products WHERE company_name LIKE :p LIMIT 1"),
    #             {"p": f"%{name}%"}
    #         ).fetchone()
    #     if row:
    #         rra_verified.append(name)
    #         print(f"   ✅ [DB 검증] '{name}' → 존재 확인")
    #     else:
    #         print(f"   ❌ [DB 검증] '{name}' → 존재하지 않음")
    return rra_verified

def _ask_gemini(prompt):
    """서버 과부하(503) 발생 시 잠시 대기 후 최대 3번까지 재시도합니다."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )
            return _extract_text_from_response(response, fallback="").strip().replace(".", "").replace("\n", "").replace("**", "").strip()
        
        except Exception as e:
            # 503 에러인 경우 재시도 수행
            if "503" in str(e) or "UNAVAILABLE" in str(e):
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # 2초, 4초 순으로 대기 시간 증가
                    print(f"⚠️ 서버 부하 감지(503). {wait_time}초 후 다시 시도합니다... ({attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
            
            # 그 외의 에러이거나 재시도 횟수를 초과한 경우
            print(f"⚠️ 제미나이 API 호출 최종 실패: {e}")
            return ""

def _save_to_cache(brand_name, resolved_company):
    """DB 저장을 수행하지 않고 넘어갑니다."""
    # with _engine.connect() as conn:
    #     conn.execute(
    #         text("""
    #             INSERT INTO brand_resolver_cache (brand_name, resolved_company)
    #             VALUES (:b, :r)
    #             ON DUPLICATE KEY UPDATE resolved_company = :r
    #         """),
    #         {"b": brand_name, "r": resolved_company}
    #     )
    #     conn.commit()
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

    # DB 캐시 확인 단계 (항상 None 반환)
    cached = _get_from_cache(brand_name)
    
    print(f"🧠 [동적 엔티티 탐색] '{brand_name}'의 법인명 구글링 중...")

    try:
        # 1단계: 브랜드 소유·특허 출원 법인명 검색
        result1 = _ask_gemini(f"""
            한국 전파인증(KC) DB와 특허청(KIPRIS)에서 '{brand_name}' 브랜드 제품 '{product_name}'을 찾으려 해.
            이 브랜드와 관련된 한국 법인명(수입사, 제조사, 특허출원인 등)을 모두 찾아줘.
            [출력규칙] 핵심 법인명만 쉼표로 구분해서 나열. 주식회사/(주) 제외. 설명 금지. 못찾으면 '{brand_name}'만 출력.
        """)
        candidates = [c.strip() for c in result1.split(',') if c.strip()]
        
        # 2단계: DB 검증 (비활성화 상태이므로 rra_verified는 항상 빈 리스트)
        rra_verified = _verify_against_db(candidates)

        # 3단계: DB 매칭이 없으므로 항상 재질문 수행
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
        print(f"⚠️ 제미나이 API 검색 실패: {e}")
        return brand_name

def resolve_model_name(product_title, specs_text=""):
    """Gemini를 이용해 공식 모델번호를 찾습니다."""
    if not product_title:
        return ""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
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
    except Exception:
        return ""