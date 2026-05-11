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
# [유틸리티] 회사명 정규화
# =====================================================================
def clean_name(name):
    """(주), 주식회사, (유) 등을 제거하고 대문자로 통일"""
    if not name: return ""
    name = re.sub(r'\(주\)|주식회사|\(유\)|주\s|' , '', name)
    return name.strip().upper()

# =====================================================================
# [DB 그룹] RRA, TTA
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
        except:
            continue
    return {"score": 0, "detail": "RRA 전파인증 DB 내역 없음", "evidence": None}

def verify_tta(company_aliases: list) -> dict:
    if not engine: return {"score": 0, "error": "DB 연결 실패"}
    for alias in company_aliases:
        name = clean_name(alias)
        try:
            query = f"SELECT 1 FROM tta_cert_list WHERE company_name LIKE '%%{name}%%' LIMIT 1"
            df = pd.read_sql(query, engine)
            if not df.empty:
                return {"score": 20, "evidence": "TTA GS인증 보유", "detail": f"TTA 소프트웨어 품질 인증 명단({alias}) 확인되었습니다."}
        except:
            continue
    return {"score": 0, "detail": "TTA/GS 인증 내역 없음", "evidence": None}

# =====================================================================
# [API 그룹] KIPRIS, 나라장터, 조달몰, NIPA
# =====================================================================
def verify_kipris(company_aliases: list, product_keyword: str = "") -> dict:
    if not KIPRIS_KEY: return {"score": 0, "error": "KIPRIS 키 미설정"}
    if not get_company_patent_data: return {"score": 0, "error": "patent_scraper 모듈 없음"}
    
    try:
        count, df, search_type = get_company_patent_data(
            company_aliases=company_aliases, 
            product_keyword=product_keyword, 
            service_key=KIPRIS_KEY
        )
        if count > 0:
            title = df.iloc[0]['발명의명칭(한글)'] if not df.empty else "특허 내역"
            return {"score": 30, "evidence": f"특허 확인: {title} 등 {count}건", "detail": f"KIPRIS 조회 결과, AI 관련 특허 역량이 확인되었습니다."}
    except Exception as e: 
        print(f"[KIPRIS 에러] {e}")
        return {"score": 0, "error": f"KIPRIS 통신 실패: {e}"}
    
    return {"score": 0, "detail": "AI 특허 내역 없음", "evidence": None}

def verify_koneps(company_aliases: list) -> dict:
    """나라장터 낙찰정보 - 1개월 단위로 쪼개서 과거 1년치 역순 검색"""
    if not COMMON_DATAGO_KEY: return {"score": 0, "error": "API 키 미설정"}
    
    url = "http://apis.data.go.kr/1230000/ScsbidInfoService/getScsbidListSttusServc01"
    today = datetime.today()
    
    search_names = list(set([clean_name(name) for name in company_aliases if len(clean_name(name)) > 1]))

    for name in search_names:
        # 💡 핵심 개선: 최근 1달부터 과거로 1달씩, 총 12번(1년치) 거슬러 올라가며 검색합니다.
        for month_offset in range(12):
            end_dt = today - timedelta(days=(30 * month_offset))
            start_dt = end_dt - timedelta(days=30)
            
            try:
                params = {
                    'serviceKey': urllib.parse.unquote(COMMON_DATAGO_KEY), 
                    'numOfRows': '100', 
                    'inqryBgnDt': start_dt.strftime('%Y%m%d%H%M'), 
                    'inqryEndDt': end_dt.strftime('%Y%m%d%H%M'), 
                    'type': 'json'
                }
                response = requests.get(url, params=params, timeout=10)
                
                # API 서버 에러 시 해당 달은 건너뛰고 다음 달로 넘어갑니다.
                if response.status_code != 200:
                    continue

                items = response.json().get('response', {}).get('body', {}).get('items', [])
                for item in items:
                    entrps_nm = item.get('scsbidEntrpsNm', '')
                    if name in entrps_nm:
                        title = item.get('bidNtceNm', '')
                        if any(kw in title.upper() for kw in ['AI', '인공지능', '빅데이터', '소프트웨어', '시스템', '구축', '유지보수', '개발']):
                            # 💡 실적을 찾는 즉시 15점을 부여하고 전체 반복문을 완전히 종료합니다! (속도 최적화)
                            return {
                                "score": 15, 
                                "evidence": f"낙찰: {title}", 
                                "detail": f"나라장터 조회 결과, [{name}] 명의의 공공 사업 실적(최근 1년 내)이 확인되었습니다."
                            }
            except Exception as e:
                print(f"[나라장터 에러] {name} {month_offset}개월 전 조회 중 예외 발생: {e}")
                continue
            
    return {"score": 0, "detail": "최근 1년간 나라장터 낙찰 실적 없음", "evidence": None}


def verify_pps_mall(company_aliases: list) -> dict:
    if not COMMON_DATAGO_KEY: return {"score": 0, "error": "API 키 미설정"}
    url = "http://apis.data.go.kr/1230000/ShoppingMallPrdctInfoService/getShoppingMallPrdctInfoList"

    search_names = []
    for raw_name in company_aliases:
        split_names = raw_name.split(',') if ',' in raw_name else [raw_name]
        for s_name in split_names:
            clean = re.sub(r'\(주\)|주식회사|\(유\)|㈜|^주\s*|\s*주$', '', s_name)
            clean = re.sub(r'[^\w\s가-힣0-9a-zA-Z]', '', clean).strip()
            # 한글이 포함되어 있고 길이가 2 이상인 경우만 추가
            if re.search(r'[가-힣]', clean) and len(clean) > 1:
                search_names.append(clean)

    search_names = list(set(search_names))
    safe_key = urllib.parse.unquote(COMMON_DATAGO_KEY)

    for name in search_names:
        try:
            params = {'serviceKey': safe_key, 'pageNo': '1', 'numOfRows': '10', 'type': 'json', 'entrpsNm': name}
            response = requests.get(url, params=params, timeout=15)
            
            if response.status_code == 500:
                print(f"⚠️ [조달몰] '{name}' 검색 불가 (조달청 서버 500 에러)")
                continue
            elif response.status_code == 200:
                # 🎯 성공 로그 출력
                print(f"✅ [조달몰] '{name}' 서버 통신 성공 (데이터 분석 중...)")
                res_json = response.json()
                items = res_json.get('response', {}).get('body', {}).get('items', [])
                if isinstance(items, dict): items = items.get('item', [])
                
                if items:
                    target = items[0] if isinstance(items, list) else items
                    return {"score": 5, "evidence": f"몰 등록: {name}", "detail": f"조달청 디지털서비스몰 상품({target.get('prdctNm', '등록 상품')} 등) 등록 확인."}
        except Exception as e:
            continue
            
    return {"score": 0, "detail": "조달청 디지털서비스몰 등록 내역 없음", "evidence": None}

def verify_nipa_solution(company_aliases: list) -> dict:
    key = "QnGWF0isjBPG/EXxLVhwkts/GtuhtD3cAEf3bEzXPvt73kfBsPflla8lVoK8VtBQLaTw1rhvMpiMHjIFoX6Pew=="
    url = "https://api.odcloud.kr/api/15089204/v1/uddi:4d85feed-91b3-4774-bb76-ce7fd277c990"
    headers = {"Authorization": f"Infuser {key}"}
    
    try:
        # 데이터가 500개를 넘어갈 수 있으므로 perPage를 2000으로 대폭 상향
        params = {"page": 1, "perPage": 2000, "returnType": "JSON"}
        res = requests.get(url, headers=headers, params=params, timeout=10, verify=False)
        
        if res.status_code == 200:
            items = res.json().get('data', [])
            
            # 검색 정확도를 높이기 위해 받아온 데이터의 모든 띄어쓰기를 없앱니다.
            full_text_db_nospace = str(items).replace(" ", "")
            
            for alias in company_aliases:
                # 타겟 검색어 역시 불필요한 단어와 모든 띄어쓰기를 없앱니다. (예: "에이아이 닷컴" -> "에이아이닷컴")
                clean_target = clean_name(alias).replace(" ", "")
                if not clean_target: continue
                
                if clean_target in full_text_db_nospace:
                    return {
                        "score": 20, 
                        "evidence": f"NIPA 등록 확인: {alias}", 
                        "detail": f"NIPA 제조AI 공급기업 명단에서 [{alias}] 실체가 확인되었습니다."
                    }
        else:
            print(f"[NIPA 에러] 응답코드: {res.status_code}")
            
    except Exception as e:
        print(f"[NIPA 예외 발생] {e}")
        return {"score": 0, "error": f"NIPA 통신 실패: {e}"}
        
    return {"score": 0, "detail": "NIPA 공급기업 명단 내역 없음", "evidence": None}

def verify_kaiac(company: str) -> dict:
    return {"score": 0, "detail": "KAIAC 품질 인증 내역 미확인", "evidence": None}