"""
normalizer.py — 크롤링 데이터 정규화

크롤링된 상품 데이터에서 회사명과 모델번호를 추출·정규화합니다.
정규화된 데이터는 KC DB 검색, 공공 API 조회, 특허 검색의 입력으로 사용됩니다.

주요 기능:
    - 회사명 추출 (스펙표 > OCR 패턴 > 상품명 내 매핑 > 상품명 첫 단어 순)
    - 회사명 정규화 및 company_map.json에 자동 저장 (누적 학습)
    - 법인 표기 변형 생성 (주식회사, (주) 등 → DB 검색 커버리지 확대)
    - 기술적 모델번호 정규식 추출 및 오탐 필터링
"""

import re
import json
import os

# 회사명 정규화 사전을 영구 저장하는 파일 (실행 중 자동 업데이트됨)
MAP_FILE = 'company_map.json'


def is_valid_model_number(model):
    """하이픈으로 구분된 2개 이상 세그먼트를 가진 모델번호인지 판단 (예: SM-R640 → True, WI-FI → False)"""
    if not model or len(model) < 4:
        return False
    if '-' in model:
        parts = model.split('-')
        if len(parts) >= 2 and all(len(p) >= 2 for p in parts):
            return True
    return False


def strip_company_suffix(name):
    """법인 suffix 제거 (예: '퀀텀테크엔시큐 주식회사' → '퀀텀테크엔시큐')"""
    name = name.strip()
    name = re.sub(r'\s*주식회사$', '', name)
    name = re.sub(r'^주식회사\s*', '', name)
    name = re.sub(r'\(주\)$', '', name)
    name = re.sub(r'^\(주\)', '', name)
    name = re.sub(r'\s*유한회사$', '', name)
    return name.strip()


def expand_company_aliases(name):
    """
    하나의 회사명에서 가능한 모든 표기 변형을 생성합니다.
    DB 검색 시 누락 없이 다양한 표기를 커버하기 위해 사용합니다.
    예: '삼성전자' → ['삼성전자', '삼성전자 주식회사', '주식회사 삼성전자', ...]
    """
    core = strip_company_suffix(name)
    return list(set([
        core,
        f"{core} 주식회사",
        f"주식회사 {core}",
        f"{core}(주)",
        f"(주){core}",
        name,
    ]))


def load_company_map():
    """JSON 파일에서 회사명 사전을 불러옵니다. 없으면 기본값으로 초기화합니다."""
    if os.path.exists(MAP_FILE):
        with open(MAP_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    default_map = {
        '삼성': 'Samsung', 'samsung': 'Samsung', '삼성전자': 'Samsung',
        '엘지': 'LG', 'lg': 'LG', '엘지전자': 'LG',
        '드리미': 'Dreame', 'dreame': 'Dreame',
        '샤오미': 'Xiaomi', 'xiaomi': 'Xiaomi'
    }
    save_company_map(default_map)
    return default_map


def save_company_map(cmap):
    """업데이트된 사전을 JSON 파일로 저장합니다."""
    with open(MAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(cmap, f, ensure_ascii=False, indent=4)


def normalize_data(scraped_json):
    """
    크롤링된 JSON 데이터에서 회사명과 모델명을 추출·정규화하여 반환합니다.

    Args:
        scraped_json: crawler.py가 반환한 상품 데이터 dict

    Returns:
        dict:
            - raw_company: 원본 회사명
            - norm_company: 정규화된 회사명 (company_map 기준)
            - raw_model: 원본 상품명
            - extracted_tech_models: 정규식으로 추출한 기술 모델번호 목록
            - final_norm_model: 최종 선택된 대표 모델번호
            - company_aliases: DB 검색에 사용할 회사명 동의어 목록
    """
    print("🧹 [데이터 정규화 엔진] 가동...")

    company_map = load_company_map()

    raw_model_name = scraped_json.get("model_name", "")
    raw_specs = scraped_json.get("raw_specs", "")
    ocr_text = scraped_json.get("ocr_extracted_text", "")

    full_text_blob = f"{raw_model_name} {raw_specs} {ocr_text}"

    normalized_result = {
        "raw_company": "미확인",
        "norm_company": "미확인",
        "raw_model": raw_model_name,
        "extracted_tech_models": [],
        "final_norm_model": ""
    }

    # 회사명 추출 (4단계 우선순위)
    potential_company = ""
    specs = scraped_json.get("specs", {})

    # 1순위: 스펙표 '제조회사' 항목
    potential_company = specs.get("제조회사", "")

    # 2순위: OCR 텍스트에서 '제조회사 OOO' 패턴
    if not potential_company:
        match = re.search(r'제조회사\s*([가-힣a-zA-Z0-9]+)', ocr_text)
        if match:
            potential_company = match.group(1)

    # 3순위: 알려진 회사명이 상품명에 포함된 경우
    if not potential_company:
        for known_company in company_map.keys():
            if known_company in raw_model_name:
                potential_company = known_company
                break

    # 4순위: 상품명 첫 단어를 회사로 간주 (다나와 특성)
    if not potential_company and raw_model_name:
        first_word = raw_model_name.split()[0]
        first_word = re.sub(r'[^가-힣a-zA-Z0-9]', '', first_word)
        if first_word:
            potential_company = first_word

    if potential_company:
        normalized_result["raw_company"] = potential_company
        if potential_company in company_map:
            normalized_result["norm_company"] = company_map[potential_company]
        else:
            # 처음 보는 회사명은 capitalize 후 사전에 자동 등록
            new_norm_name = potential_company.capitalize()
            company_map[potential_company] = new_norm_name
            save_company_map(company_map)
            normalized_result["norm_company"] = new_norm_name
            print(f"✨ [새로운 회사 등록 완료] '{potential_company}' -> '{new_norm_name}'")

    # 기술적 모델번호 추출
    # 와이파이·해상도·단위 등 오탐 가능성 높은 패턴을 블랙리스트로 제외
    MODEL_BLACKLIST = {
        'WI-FI', 'WIFI', 'USB', 'HDMI', 'TYPE-C', 'USB-C', 'AC-DC', 'DC-AC',
        'LTE', '5G', '4G', '3G', 'BLE', 'NFC', 'IR', 'IP65', 'IP67', 'IP68',
        'AI', 'APP', 'API', 'SDK', 'IoT', 'IOT', 'HTTP', 'HTTPS',
        'LED', 'LCD', 'OLED', 'RGB', 'HD', 'FHD', 'UHD', '4K', '8K',
        'AC', 'DC', 'Hz', 'HZ', 'DB', 'GB', 'MB', 'TB', 'GHz',
    }
    model_pattern = r'(?<![A-Za-z0-9])(?:[A-Za-z0-9]+-[A-Za-z0-9]+|[A-Za-z]+\d+[A-Za-z0-9]*|\d+[A-Za-z]+[A-Za-z0-9]*)(?![A-Za-z0-9])'

    matches = [m for m in re.findall(model_pattern, full_text_blob)
               if m.upper() not in MODEL_BLACKLIST
               and not re.match(r'^\d+X\d+$', m, re.IGNORECASE)   # 1920X1080 같은 해상도 제외
               and not re.match(r'^\d+[A-Z]{1,2}\d+$', m)         # 123A456 같은 수치 제외
               and not re.match(r'^\d+[A-Za-z]{1,6}$', m)         # 30FPS, 164G 같은 수치+단위 제외
               ]

    tech_models = list(set([m.upper() for m in matches]))
    # 하이픈이 많고 길수록 실제 모델번호일 가능성이 높아 우선 정렬
    tech_models = sorted(tech_models, key=lambda x: (x.count('-'), len(x)), reverse=True)
    normalized_result["extracted_tech_models"] = tech_models

    # 대표 모델번호 선택 (상품명 포함 여부 우선)
    final_model = ""
    for tm in tech_models:
        if '-' in tm and tm in raw_model_name.upper():
            final_model = tm
            break
    if not final_model:
        for tm in tech_models:
            if '-' in tm and tm in full_text_blob.upper():
                final_model = tm
                break
    if not final_model:
        for tm in tech_models:
            if tm in raw_model_name.upper():
                final_model = tm
                break
    if not final_model and tech_models:
        final_model = tech_models[0]

    normalized_result["final_norm_model"] = final_model

    # 회사명 동의어 목록 생성 (DB 검색 커버리지 확대용)
    aliases = []
    if normalized_result["norm_company"] != "미확인":
        raw_aliases = [k for k, v in company_map.items() if v == normalized_result["norm_company"]]
        if normalized_result["norm_company"] not in raw_aliases:
            raw_aliases.append(normalized_result["norm_company"])
        for a in raw_aliases:
            aliases.extend(expand_company_aliases(a))

    normalized_result["company_aliases"] = list(set(aliases))

    print(f"✅ 정규화 완료: 회사[{normalized_result['norm_company']}], 모델[{normalized_result['final_norm_model']}]")
    print(f"🔗 검색용 회사 동의어: {normalized_result['company_aliases']}")

    return normalized_result
