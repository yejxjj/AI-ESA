"""
patent_scraper.py — KIPRIS 특허 검색

특허청 KIPRIS API를 호출하여 기업의 AI 관련 특허 건수와 목록을 조회합니다.
조회 결과는 신뢰도 점수(relation_score) 계산에 활용됩니다.

검색 전략 (Fallback):
    1차: '인공지능 {제품 카테고리}' 키워드로 정밀 검색
    2차: 1차 결과가 0건이면 '인공지능'으로 일반 검색
    회사명 동의어(aliases) 각각에 대해 검색 후 최대 건수 결과를 채택

반환값:
    (patent_count, df_items, search_type)
    - patent_count: 총 특허 건수
    - df_items: 특허 목록 DataFrame (발명명칭, 출원일자, 출원인, 등록상태)
    - search_type: 어떤 검색 방식으로 결과를 얻었는지 설명 문자열
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
import xml.etree.ElementTree as ET
import urllib.parse
import pandas as pd

try:
    import config
    KIPRIS_SERVICE_KEY = config.KIPRIS_KEY
except ImportError:
    KIPRIS_SERVICE_KEY = os.environ.get("KIPRIS_KEY", "")


def get_company_patent_data(company_aliases, product_keyword="", service_key=KIPRIS_SERVICE_KEY):
    """
    KIPRIS API로 기업의 AI 관련 특허를 검색합니다.

    Args:
        company_aliases: 검색할 회사명 목록 (동의어 포함)
        product_keyword: 제품 카테고리 키워드 (예: '무선이어폰') — 정밀 검색에 사용
        service_key: KIPRIS API 서비스 키

    Returns:
        (int, DataFrame, str): 특허 건수, 특허 목록, 검색 방식 설명
    """
    if not service_key:
         return (0, pd.DataFrame(), "키 없음")

    if isinstance(company_aliases, str):
        company_aliases = [company_aliases]

    if not company_aliases or company_aliases == ["미확인"]:
        return (0, pd.DataFrame(), "미확인")

    max_count = 0
    best_items = []
    search_type = "일반 AI"

    base_url = "http://plus.kipris.or.kr/kipo-api/kipi/patUtiModInfoSearchSevice/getAdvancedSearch"

    for alias in company_aliases:
        # 1차 시도: 인공지능 + 제품 카테고리 키워드
        search_word = f"인공지능 {product_keyword}".strip() if product_keyword else "인공지능"

        params = {
            "applicant": alias,
            "word": search_word,
            "patent": "true",
            "numOfRows": "50",
            "ServiceKey": service_key
        }

        try:
            query_string = "&".join([f"{k}={urllib.parse.quote(str(v))}" if k != "ServiceKey" else f"{k}={v}" for k, v in params.items()])
            resp = requests.get(f"{base_url}?{query_string}", timeout=10)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            count = int(root.findtext(".//count/totalCount", default="0"))

            # 2차 시도 (Fallback): 1차 결과가 0건이면 '인공지능'으로만 재검색
            if count == 0 and product_keyword:
                print(f"⚠️ '{alias}'의 '{product_keyword}' 연관 특허 0건. 일반 AI 특허로 재검색합니다.")
                params["word"] = "인공지능"
                query_string = "&".join([f"{k}={urllib.parse.quote(str(v))}" if k != "ServiceKey" else f"{k}={v}" for k, v in params.items()])
                resp = requests.get(f"{base_url}?{query_string}", timeout=10)
                root = ET.fromstring(resp.text)
                count = int(root.findtext(".//count/totalCount", default="0"))
                current_search_type = "일반 AI"
            else:
                current_search_type = f"'{product_keyword}' 연관 AI" if product_keyword else "일반 AI"

            # 가장 많은 결과를 낸 alias의 데이터를 채택
            if count >= max_count and count > 0:
                max_count = count
                search_type = current_search_type

                items_xml = root.findall(".//items/item")
                current_alias_items = []
                for item in items_xml:
                    current_alias_items.append({
                        "일련번호": item.findtext("indexNo", default="-").strip(),
                        "발명의명칭(한글)": item.findtext("inventionTitle", default="제목없음").strip(),
                        "출원일자": item.findtext("applicationDate", default="-").strip(),
                        "출원인": item.findtext("applicantName", default="-").strip(),
                        "등록상태": item.findtext("registerStatus", default="-").strip()
                    })
                best_items = current_alias_items

        except Exception:
            continue

    df_items = pd.DataFrame(best_items)
    # 출원일자 포맷 변환: 20060616 → 2006-06-16
    if not df_items.empty and '출원일자' in df_items.columns:
         df_items['출원일자'] = df_items['출원일자'].apply(lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(str(d))==8 else d)

    return (max_count, df_items, search_type)
