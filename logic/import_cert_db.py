"""
import_cert_db.py — GS인증·NEP 인증 데이터 DB 적재 스크립트

중소벤처기업부 기술개발제품 인증현황 공공 API에서 데이터를 수집하여
MySQL cert_products 테이블에 저장합니다.

용도: 최초 설정 또는 인증 데이터 갱신 시 1회 실행
실행: python logic/import_cert_db.py

수집 대상:
    - GS인증 (소프트웨어 품질인증)
    - NEP (신제품 인증)
    - NET (신기술 인증)
    등 중기부 기술개발제품 인증 전체

저장 테이블: cert_products
    - cert_type: 인증 구분 (GS인증/NEP 등)
    - cert_no: 인증 번호
    - product_name: 인증 제품명
    - company_name: 업체명
    - cert_date / expire_date: 인증일자 / 만료일자
"""

import requests
import sys
from sqlalchemy import create_engine, text

sys.path.insert(0, '.')
try:
    from config import OPEN_DATA_KEY
except ImportError:
    OPEN_DATA_KEY = ""

DB_URL = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
engine = create_engine(DB_URL)

# 최신순으로 나열된 API endpoint 목록
API_ENDPOINTS = [
    "https://api.odcloud.kr/api/3033913/v1/uddi:834e8428-51b3-420b-9fd4-aaee942e4916",  # 2025.05.12 최신
    "https://api.odcloud.kr/api/3033913/v1/uddi:27bb6889-e56d-4cdc-a222-9f02900c81e7",  # 2023.11.30
    "https://api.odcloud.kr/api/3033913/v1/uddi:39673df9-60f2-4c81-9e72-fa37d5045dfd",  # 2021.12.15
]
PER_PAGE = 1000

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS cert_products (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    cert_type     VARCHAR(100)  COMMENT '인증구분 (GS인증/NEP/NET 등)',
    cert_no       VARCHAR(100)  COMMENT '인증번호',
    product_name  VARCHAR(500)  COMMENT '인증제품명',
    company_name  VARCHAR(200)  COMMENT '업체명',
    biz_no        VARCHAR(20)   COMMENT '사업자등록번호',
    representative VARCHAR(100) COMMENT '대표자',
    cert_date     VARCHAR(20)   COMMENT '인증일자',
    expire_date   VARCHAR(20)   COMMENT '만료일자',
    INDEX idx_company  (company_name),
    INDEX idx_type     (cert_type),
    INDEX idx_product  (product_name(100))
) CHARACTER SET utf8mb4;
"""

INSERT_SQL = """
INSERT INTO cert_products
    (cert_type, cert_no, product_name, company_name, biz_no, representative, cert_date, expire_date)
VALUES
    (:cert_type, :cert_no, :product_name, :company_name, :biz_no, :representative, :cert_date, :expire_date)
"""

# API 응답 필드명 → DB 컬럼명 매핑 (API 버전마다 필드명이 다를 수 있어 후보 목록으로 관리)
KEY_MAP = {
    "cert_type":      ["인증구분", "certType"],
    "cert_no":        ["인증번호", "certNo"],
    "product_name":   ["인증제품명", "productName", "제품명"],
    "company_name":   ["업체명", "companyName", "기업명"],
    "biz_no":         ["사업자등록번호", "bizNo"],
    "representative": ["대표자", "representative"],
    "cert_date":      ["인증일자", "certDate"],
    "expire_date":    ["만료일자", "expireDate"],
}


def pick(row, candidates):
    """API 응답 row에서 후보 필드명 중 값이 있는 첫 번째 것을 반환합니다."""
    for key in candidates:
        if key in row and row[key]:
            return str(row[key]).strip()
    return ""


def fetch_all():
    """API에서 전체 데이터를 페이징으로 수집합니다. 동작하는 endpoint를 최신순으로 자동 선택합니다."""
    import math

    params_base = {
        "serviceKey": OPEN_DATA_KEY,
        "page": 1,
        "perPage": PER_PAGE,
        "returnType": "json",
    }

    api_url = None
    for endpoint in API_ENDPOINTS:
        print(f"🌐 연결 시도: {endpoint.split('uddi:')[1][:8]}...")
        try:
            resp = requests.get(endpoint, params=params_base, timeout=15)
            if resp.status_code == 200 and "data" in resp.json():
                api_url = endpoint
                first_data = resp.json()
                print(f"   ✅ 연결 성공")
                break
            else:
                print(f"   ❌ 응답 오류 ({resp.status_code})")
        except Exception as e:
            print(f"   ❌ {e}")

    if not api_url:
        print("\n❌ 동작하는 endpoint가 없습니다.")
        print("   Swagger에서 최신 endpoint 확인 후 API_ENDPOINTS 맨 앞에 추가하세요:")
        print("   https://infuser.odcloud.kr/oas/docs?namespace=3033913/v1")
        return None

    total = first_data.get("totalCount", first_data.get("matchCount", 0))
    print(f"   전체 {total}건 확인, 수집 시작...")

    all_rows = list(first_data["data"])
    total_pages = math.ceil(total / PER_PAGE)

    for page in range(2, total_pages + 1):
        params_base["page"] = page
        resp = requests.get(api_url, params=params_base, timeout=30)
        resp.raise_for_status()
        chunk = resp.json().get("data", [])
        all_rows.extend(chunk)
        print(f"   {page}/{total_pages}페이지 완료 ({len(all_rows)}/{total}건)")

    return all_rows


def save_to_db(rows):
    """수집한 인증 데이터를 MySQL cert_products 테이블에 저장합니다. 실행 시 기존 데이터를 전체 교체합니다."""
    with engine.connect() as conn:
        conn.execute(text(CREATE_TABLE))
        conn.commit()
        conn.execute(text("TRUNCATE TABLE cert_products"))
        conn.commit()

        batch = []
        for row in rows:
            batch.append({
                "cert_type":      pick(row, KEY_MAP["cert_type"]),
                "cert_no":        pick(row, KEY_MAP["cert_no"]),
                "product_name":   pick(row, KEY_MAP["product_name"]),
                "company_name":   pick(row, KEY_MAP["company_name"]),
                "biz_no":         pick(row, KEY_MAP["biz_no"]),
                "representative": pick(row, KEY_MAP["representative"]),
                "cert_date":      pick(row, KEY_MAP["cert_date"]),
                "expire_date":    pick(row, KEY_MAP["expire_date"]),
            })
            if len(batch) >= 500:
                conn.execute(text(INSERT_SQL), batch)
                conn.commit()
                batch = []
        if batch:
            conn.execute(text(INSERT_SQL), batch)
            conn.commit()

    # 저장 결과 통계 출력
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT cert_type, COUNT(*) FROM cert_products GROUP BY cert_type ORDER BY COUNT(*) DESC"
        ))
        print("\n📊 인증구분별 건수:")
        for r in result:
            print(f"   {r[0] or '(없음)'}: {r[1]}건")


def main():
    rows = fetch_all()
    if rows is None:
        sys.exit(1)

    print(f"\n💾 DB 저장 중... ({len(rows)}건)")
    save_to_db(rows)
    print(f"✅ 완료: {len(rows)}건 → cert_products 테이블")


if __name__ == "__main__":
    main()
