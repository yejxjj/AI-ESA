"""
llm_resolver.py — Gemini 기반 동적 엔티티 리졸루션

쇼핑몰 브랜드명(예: CnCare, 오랄비)을 입력받아 Gemini 웹 검색으로
특허/전파인증 검색에 사용할 실제 법인명을 역추적합니다.

조회 결과는 DB(brand_resolver_cache 테이블)에 캐싱되어
동일 브랜드 재조회 시 API 호출 없이 즉시 반환합니다.

주요 함수:
    resolve_real_company_name(brand_name, product_name)
        → 브랜드명으로 법인명을 역추적하여 반환
    resolve_model_name(product_title, specs_text)
        → 상품명/스펙으로 공식 기술 모델번호를 검색하여 반환
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google import genai
from google.genai import types
from sqlalchemy import create_engine, text


def _extract_text_from_response(response, fallback=""):
    """Gemini 응답에서 텍스트를 안전하게 추출합니다. 응답 구조 변화에 대비한 fallback 포함."""
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


# DB 연결 및 캐시 테이블 초기화
DB_URL = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
_engine = create_engine(DB_URL)

with _engine.connect() as conn:
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS brand_resolver_cache (
            brand_name VARCHAR(200) PRIMARY KEY,
            resolved_company VARCHAR(200) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """))
    conn.commit()


def _get_from_cache(brand_name):
    """DB 캐시에서 브랜드명에 대한 기존 리졸루션 결과를 조회합니다."""
    with _engine.connect() as conn:
        row = conn.execute(
            text("SELECT resolved_company FROM brand_resolver_cache WHERE brand_name = :b"),
            {"b": brand_name}
        ).fetchone()
    result = row[0] if row else None
    print(f"   🗄️ [캐시 조회] '{brand_name}' → {result}")
    return result


def _verify_against_db(company_names):
    """
    법인명 목록을 kc_ai_products DB에서 실제로 검증합니다.
    KC 전파인증 DB에 존재하는 이름만 반환합니다. (없는 이름은 특허 검색용으로만 활용)
    """
    rra_verified = []
    for name in company_names:
        name = name.strip()
        if not name:
            continue
        with _engine.connect() as conn:
            row = conn.execute(
                text("SELECT 1 FROM kc_ai_products WHERE company_name LIKE :p LIMIT 1"),
                {"p": f"%{name}%"}
            ).fetchone()
        if row:
            rra_verified.append(name)
            print(f"   ✅ [DB 검증] '{name}' → kc_ai_products 존재 확인")
        else:
            print(f"   ❌ [DB 검증] '{name}' → kc_ai_products 없음 (특허용으로만 사용)")
    return rra_verified


def _ask_gemini(prompt):
    """Google Search가 활성화된 Gemini로 웹 검색을 수행하고 결과 텍스트를 반환합니다."""
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )
    )
    return _extract_text_from_response(response, fallback="").strip().replace(".", "").replace("\n", "").replace("**", "").strip()


def _save_to_cache(brand_name, resolved_company):
    """리졸루션 결과를 DB 캐시에 저장합니다. 이미 존재하면 덮어씁니다."""
    with _engine.connect() as conn:
        conn.execute(
            text("""
                INSERT INTO brand_resolver_cache (brand_name, resolved_company)
                VALUES (:b, :r)
                ON DUPLICATE KEY UPDATE resolved_company = :r
            """),
            {"b": brand_name, "r": resolved_company}
        )
        conn.commit()


try:
    import config
    _api_key = config.GEMINI_API_KEY
except ImportError:
    _api_key = os.environ.get("GEMINI_API_KEY", "")

client = genai.Client(api_key=_api_key)


def resolve_real_company_name(brand_name, product_name=""):
    """
    쇼핑몰 브랜드명으로 실제 한국 법인명을 역추적합니다.

    조회 순서:
        1. DB 캐시 확인 → 캐시된 법인명이 kc_ai_products에 존재하면 즉시 반환
        2. Gemini 웹 검색 1단계: KC DB·KIPRIS 기준 법인명 후보 검색
        3. 후보를 kc_ai_products DB에서 검증
        4. DB 매칭 없으면 Gemini 2단계: KC 전파인증 수입사/책임자 재질문
        5. 최종 법인명을 캐시에 저장 후 반환

    Returns:
        쉼표로 구분된 법인명 문자열 (예: "삼성전자,삼성SDS") 또는 원본 brand_name
    """
    if not brand_name or brand_name in ["미확인", "없음", ""]:
        return brand_name

    # DB 캐시 먼저 확인 + 검증
    cached = _get_from_cache(brand_name)
    if cached:
        cached_names = [c.strip() for c in cached.split(',') if c.strip()]
        rra_ok = _verify_against_db(cached_names)
        if rra_ok:
            print(f"   💾 [DB 캐시 히트 + 검증OK] '{brand_name}' -> '{cached}'")
            return cached
        else:
            print(f"   ⚠️ [캐시 무효화] '{cached}' → kc_ai_products에 없음. 재검색...")
            _save_to_cache(brand_name, brand_name)

    print(f"🧠 [동적 엔티티 탐색] '{brand_name}'의 법인명 구글링 중...")

    try:
        # 1단계: 브랜드 소유·특허 출원 법인명 검색
        result1 = _ask_gemini(f"""
            한국 전파인증(KC) DB와 특허청(KIPRIS)에서 '{brand_name}' 브랜드 제품 '{product_name}'을 찾으려 해.
            이 브랜드와 관련된 한국 법인명(수입사, 제조사, 특허출원인 등)을 모두 찾아줘.
            [출력규칙] 핵심 법인명만 쉼표로 구분해서 나열. 주식회사/(주) 제외. 설명 금지. 못찾으면 '{brand_name}'만 출력.
        """)
        candidates = [c.strip() for c in result1.split(',') if c.strip()]
        print(f"   1단계 후보: {candidates}")

        # 2단계: DB 검증 - kc_ai_products에 실제로 있는지 확인
        rra_verified = _verify_against_db(candidates)

        # 3단계: DB에 없는 경우 KC 전파인증 수입사 전용으로 재질문
        if not rra_verified:
            print(f"   ⚠️ DB 매칭 없음 → KC 전파인증 수입사/책임자 재질문...")
            result2 = _ask_gemini(f"""
                '{product_name}' 제품의 한국 KC 전파인증(적합성평가) 수입사 또는 책임자 법인명을 찾아줘.
                국립전파연구원(RRA) 또는 KC 인증 DB에 등록된 수입사명이야.
                [출력규칙] 핵심 법인명만 쉼표로 구분. 주식회사/(주) 제외. 설명 금지.
            """)
            extra = [c.strip() for c in result2.split(',') if c.strip()]
            print(f"   2단계 추가 후보: {extra}")
            candidates = list(set(candidates + extra))
            rra_verified = _verify_against_db(extra)

        # 최종: DB 검증된 이름 + 검증 안된 이름(특허용) 모두 합쳐서 저장
        all_names = list(set(rra_verified + [c for c in candidates if c != brand_name]))
        final = ','.join(all_names) if all_names else brand_name

        print(f"   👉 최종 법인명: [{final}]")
        _save_to_cache(brand_name, final)
        return final

    except Exception as e:
        print(f"⚠️ 제미나이 API 검색 실패: {e}")
        return brand_name


def resolve_model_name(product_title, specs_text=""):
    """
    상품명·스펙 텍스트에서 모델번호를 찾지 못했을 때 Gemini 웹 검색으로 공식 모델번호를 찾습니다.

    Args:
        product_title: 다나와 상품명
        specs_text: 스펙 원문 텍스트 (선택)

    Returns:
        'SDT-PTJ-4M' 형태의 모델번호 문자열, 못 찾으면 빈 문자열
    """
    if not product_title:
        return ""

    print(f"🔍 [모델명 탐색] '{product_title}' 모델명 검색 중...")

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
            너는 전파인증(RRA) DB 검색 전문가야.
            다나와 쇼핑몰의 상품명은 '{product_title}'이고, 스펙 정보는 다음과 같아:
            {specs_text}

            전파인증 DB에서 검색할 수 있는 공식 기술 모델명(예: SDT-PTJ-4M, SM-R640 같은 영숫자 코드)을 찾아줘.
            보통 제조사가 부여한 알파벳+숫자 조합이며, 하이픈(-)으로 구분된 경우가 많아.

            [출력 절대 규칙]
            1. 모델명 코드만 출력해 (예: SDT-PTJ-4M).
            2. 여러 개면 쉼표로 구분해 (예: SDT-PTJ-4M, SDT-PTJ-2M).
            3. 설명, 마침표, 인사말 절대 금지.
            4. 정 못 찾겠으면 빈 문자열만 출력해.
            """,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
        )

        model_name = _extract_text_from_response(response, fallback="")
        model_name = model_name.strip().replace(".", "").replace("\n", "").replace("**", "").strip()
        print(f"   👉 모델명 탐색 완료: [{model_name}]")
        return model_name

    except Exception as e:
        print(f"⚠️ 모델명 탐색 실패: {e}")
        return ""
