"""
llm_resolver.py — 로컬 LLM(Ollama) 및 마스터 맵 기반 엔티티 리졸루션
"""
import os
import json
import requests

# 📂 로컬 캐시 파일 (한번 찾은건 다시 안묻게 저장)
CACHE_FILE = 'llm_company_cache.json'

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_cache(data):
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

def _ask_local_llm(prompt):
    """내 컴퓨터에서 돌아가는 Ollama 서버(localhost:11434)에 요청을 보냅니다."""
    url = "http://localhost:11434/api/generate"
    data = {
        "model": "gemma2:2b", # 방금 다운로드한 모델
        "prompt": prompt,
        "stream": False
    }
    try:
        # CPU 연산 속도를 고려해 타임아웃을 60초로 넉넉히 설정
        response = requests.post(url, json=data, timeout=60)
        return response.json().get('response', '').strip()
    except Exception as e:
        print(f"   ⚠️ 로컬 LLM 응답 실패: {e}")
        return ""

def resolve_real_company_name(brand_name, product_name=""):
    """쇼핑몰 브랜드명을 KIPRIS 검색용 공식 한국 법인명으로 변환합니다."""
    if not brand_name or brand_name in ["미확인", "없음", ""]:
        return brand_name

    # 🎯 [전략 1] 마스터 맵: LG/삼성 등 대기업은 즉시 반환 (API 한도 절약)
    master_map = {
        "LG": "엘지전자,LG전자",
        "LG전자": "엘지전자,LG전자",
        "삼성": "삼성전자",
        "삼성전자": "삼성전자"
    }
    key = brand_name.upper().replace(" ", "")
    if key in master_map:
        return master_map[key]

    # 🎯 [전략 2] 로컬 캐시 확인
    cache = load_cache()
    if brand_name in cache:
        print(f"   🗄️ [캐시 적중] '{brand_name}' -> '{cache[brand_name]}'")
        return cache[brand_name]
    
    # 🎯 [전략 3] 로컬 LLM 분석
    print(f"🧠 [로컬 LLM] '{brand_name}' 법인명 추론 중... (CPU 연산)")
    prompt = f"브랜드명 '{brand_name}'의 한국 공식 법인명(제조사/수입사)을 쉼표로만 나열해. 이름만 출력해."
    result = _ask_local_llm(prompt)

    if result:
        cache[brand_name] = result
        save_cache(cache)
        print(f"   👉 결과 저장: [{result}]")
        return result

    return brand_name

def resolve_model_name(product_title, specs_text=""):
    """모델번호 추출도 로컬 LLM으로 안전하게 수행합니다."""
    prompt = f"제품명 '{product_title}'에서 전파인증 검색에 필요한 공식 모델번호만 출력해."
    return _ask_local_llm(prompt)