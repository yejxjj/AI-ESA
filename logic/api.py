"""
integrated_api.py — 7개 기관 통합 검증 모듈 (LLM Aliases 리스트 활용 버전)
"""
import os
import sys
import requests
import urllib.parse
import pandas as pd
from sqlalchemy import create_engine
from datetime import datetime, timedelta
import re

# [설정 로드] config.py 경로 강제 지정
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

try:
    import config
    COMMON_DATAGO_KEY = getattr(config, 'DATAGO_API_KEY', '')
    KIPRIS_KEY = getattr(config, 'KIPRIS_KEY', '') 
    NIPA_KEY = getattr(config, 'DATAGO_API_KEY', '') # NIPA도 공공데이터 키 사용
except ImportError:
    print("❌ 에러: config.py 파일을 찾을 수 없습니다.")
    COMMON_DATAGO_KEY = KIPRIS_KEY = NIPA_KEY = ""

# [DB 연결] 로컬 MySQL 설정 (RRA, TTA용)
DB_URL = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
try:
    engine = create_engine(DB_URL)
except:
    engine = None

# 팀원의 KIPRIS 모듈 임포트
try:
    from logic.patent_scraper import get_company_patent_data
except ImportError:
    get_company_patent_data = None

# =====================================================================
# [유틸리티] 회사명 정규화 (괄호, 주식회사 등 제거하여 검색률 극대화)
# =====================================================================
def clean_name(name):
    """(주), 주식회사, 공백 등을 제거하고 대문자로 통일"""
    if not name: return ""
    name = re.sub(r'\(주\)|주식회사|\(유\)|주\s|' , '', name)
    return name.strip().upper()

# =====================================================================
# [DB 그룹] RRA, TTA (리스트 루프 적용)
# =====================================================================
def verify_rra(company_aliases: list, model: str) -> dict:
    if not engine: return {"score": 0, "error": "DB 연결 실패"}
    search_model = model.split()[0] if model else ""
    
    for alias in company_aliases:
        name = clean_name(alias)
        try:
            query = f"SELECT cert_no FROM rra WHERE company_name LIKE '%%{name}%%' AND model_name LIKE '%%{search_model}%%' LIMIT 1"
            df = pd.read_sql(query, engine)
            if not df.empty:
                return {"score": 20, "evidence": f"인증번호: {df.iloc[0]['cert_no']}", "detail": f"RRA 인증 DB에 [{alias}] 명의 실체가 등록되어 있습니다."}
        except: continue
    return {"score": 0, "detail": "RRA 전파인증 DB 내역 없음", "evidence": None}

def verify_tta(company_aliases: list) -> dict:
    if not engine: return {"score": 0, "error": "DB 연결 실패"}
    for alias in company_aliases:
        name = clean_name(alias)
        try:
            query = f"SELECT 1 FROM tta_cert_list WHERE company_name LIKE '%%{name}%%' LIMIT 1"
            df = pd.read_sql(query, engine)
            if not df.empty:
                return {"score": 5, "evidence": "TTA GS인증 보유", "detail": f"TTA 소프트웨어 품질 인증 명단({alias}) 확인되었습니다."}
        except: continue
    return {"score": 0, "detail": "TTA/GS 인증 내역 없음", "evidence": None}

# =====================================================================
# [API 그룹] KIPRIS, 나라장터, 조달몰, NIPA
# =====================================================================

def verify_kipris(company_aliases: list, product_keyword: str = "") -> dict:
    """팀원의 스크래퍼에 별칭 리스트를 통째로 전달합니다."""
    if not KIPRIS_KEY: return {"score": 0, "error": "KIPRIS 키 미설정"}
    if not get_company_patent_data: return {"score": 0, "error": "patent_scraper 모듈 없음"}
    
    try:
        # 팀원 함수는 이미 리스트를 지원하므로 그대로 전달
        count, df, search_type = get_company_patent_data(
            company_aliases=company_aliases, 
            product_keyword=product_keyword, 
            service_key=KIPRIS_KEY
        )
        if count > 0:
            title = df.iloc[0]['발명의명칭(한글)'] if not df.empty else "특허 내역"
            return {"score": 25, "evidence": f"특허 확인: {title} 등 {count}건", "detail": f"KIPRIS 조회 결과, AI 관련 특허 역량이 확인되었습니다."}
    except Exception as e: return {"score": 0, "error": f"KIPRIS 통신 실패: {e}"}
    return {"score": 0, "detail": "AI 특허 내역 없음", "evidence": None}

def verify_koneps(company_aliases: list) -> dict:
    """나라장터 낙찰정보 - 리스트 내 모든 이름을 순차적으로 검색"""
    if not COMMON_DATAGO_KEY: return {"score": 0, "error": "API 키 미설정"}
    
    url = "http://apis.data.go.kr/1230000/ScsbidInfoService/getScsbidListSttusServc01"
    today = datetime.today()
    year_ago = today - timedelta(days=365)
    
    # 별칭 리스트에서 중복 제거한 핵심 이름들만 추출
    search_names = list(set([clean_name(name) for name in company_aliases if len(clean_name(name)) > 1]))

    for name in search_names:
        try:
            params = {
                'serviceKey': COMMON_DATAGO_KEY, 
                'numOfRows': '50', 
                'inqryBgnDt': year_ago.strftime('%Y%m%d%H%M'), 
                'inqryEndDt': today.strftime('%Y%m%d%H%M'), 
                'type': 'json'
            }
            # API 호출 (나라장터는 기업명 필터링 기능이 약해 전체를 받아와서 코드에서 비교)
            response = requests.get(url, params=params, timeout=5)
            if response.status_code == 200:
                items = response.json().get('response', {}).get('body', {}).get('items', [])
                for item in items:
                    entrps_nm = item.get('scsbidEntrpsNm', '')
                    if name in entrps_nm:
                        title = item.get('bidNtceNm', '')
                        if any(kw in title.upper() for kw in ['AI', '인공지능', '빅데이터', '소프트웨어']):
                            return {"score": 15, "evidence": f"낙찰: {title}", "detail": f"나라장터 조회 결과, [{name}] 명의의 공공 사업 실적이 확인되었습니다."}
        except: continue
    return {"score": 0, "detail": "최근 1년간 나라장터 낙찰 실적 없음", "evidence": None}

def verify_pps_mall(company_aliases: list) -> dict:
    """조달청 디지털서비스몰 - 리스트 내 이름을 찔러보기"""
    if not COMMON_DATAGO_KEY: return {"score": 0, "error": "API 키 미설정"}
    url = "http://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService/getShoppingMallPrdctInfoList01"
    search_names = list(set([clean_name(name) for name in company_aliases]))

    for name in search_names:
        try:
            params = {'serviceKey': COMMON_DATAGO_KEY, 'prdctClsNm': '인공지능', 'entrpsNm': name, 'type': 'json'}
            response = requests.get(url, params=params, timeout=5)
            if response.status_code == 200:
                items = response.json().get('response', {}).get('body', {}).get('items', [])
                if items:
                    return {"score": 5, "evidence": f"몰 등록: {name}", "detail": "조달청 디지털서비스몰에 정식 AI 상품 등록 확인."}
        except: continue
    return {"score": 0, "detail": "디지털서비스몰 등록 내역 없음", "evidence": None}

def verify_nipa_solution(company_aliases: list) -> dict:
    """NIPA 제조AI 솔루션 공급기업 - 헤더 인증 방식 적용 버전"""
    key = "QnGWF0isjBPG/EXxLVhwkts/GtuhtD3cAEf3bEzXPvt73kfBsPflla8lVoK8VtBQLaTw1rhvMpiMHjIFoX6Pew=="
    url = "https://api.odcloud.kr/api/15089204/v1/uddi:4d85feed-91b3-4774-bb76-ce7fd277c990"
    
    # [성공 공식] 헤더에 인증키를 넣습니다.
    headers = {"Authorization": f"Infuser {key}"}
    
    try:
        # 한 번에 500개 정도 넉넉하게 가져와서 로컬에서 비교 (속도 최적화)
        params = {"page": 1, "perPage": 500, "returnType": "JSON"}
        res = requests.get(url, headers=headers, params=params, timeout=10, verify=False)
        
        if res.status_code == 200:
            items = res.json().get('data', [])
            # 가져온 데이터 전체를 문자열로 합쳐서 별칭이 있는지 검색
            full_text_db = str(items)
            
            for alias in company_aliases:
                # 괄호 등을 제거한 순수 이름으로 검색 (검색 성공률 극대화)
                clean_target = re.sub(r'\(주\)|주식회사', '', alias).strip()
                if clean_target in full_text_db:
                    return {
                        "score": 10, 
                        "evidence": f"NIPA 등록 확인: {alias}", 
                        "detail": f"NIPA 제조AI 공급기업 명단에서 [{alias}] 실체가 확인되었습니다."
                    }
    except Exception as e:
        return {"score": 0, "error": f"NIPA 통신 실패: {e}"}
        
    return {"score": 0, "detail": "NIPA 공급기업 명단 내역 없음", "evidence": None}

def verify_kaiac(company: str) -> dict:
    return {"score": 0, "detail": "KAIAC 품질 인증 내역 미확인", "evidence": None}