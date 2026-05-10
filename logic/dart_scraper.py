"""
dart_scraper.py — Open DART API 기반 제품 중심 AI 실체 분석기
(숫자 노이즈 제거 + 사업보고서 원문 파싱 및 SW 기업 대응 버전)
"""
import OpenDartReader
import json
import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    DART_KEY = getattr(config, 'DART_API_KEY', getattr(config, 'DART_KEY', ''))
except ImportError:
    DART_KEY = os.environ.get("DART_API_KEY", "")

def _clean_dart_text(text: str) -> str:
    """날짜, 금액, 주식수, DART 접수번호 등 불필요한 숫자 노이즈를 제거하여 문맥만 남깁니다."""
    if not text: return ""
    
    text = re.sub(r'\b\d{14}\b', '', text) 
    text = re.sub(r'\b\d{8}\b', '', text)  
    text = re.sub(r'\b\d{4}[-.]\d{2}[-.]\d{2}\b', '', text) 
    
    text = re.sub(r'\b\d{1,3}(,\d{3})+\b', '', text) 
    text = re.sub(r'\b\d{4,}\b', '', text)           
    text = re.sub(r'\b\d+\.\d+\b', '', text)         
    text = re.sub(r'\b\d{1,2}\b', '', text)          
    
    text = re.sub(r'[-=]{2,}', '', text)             
    text = re.sub(r'\b[A-Za-z]\b', '', text)         
    
    return re.sub(r'\s+', ' ', text).strip()

def _evaluate_product_focused_rules(product_name: str, dart_text_data: str) -> dict:
    ai_keywords = ['인공지능', '딥러닝', '머신러닝', '생성형', 'llm', '자연어', '신경망', ' ai ', '(ai)', ' ai,', ' ai.']
    rnd_keywords = ['연구', '개발', '센터', '랩', 'lab', '투자', '출자', '인수', '사업', '수주', '공급']
    ip_keywords = ['특허', '지식재산', '지적재산', '출원', '등록', 'ip']

    prod_tokens = [token for token in product_name.split() if len(token) > 1]
    
    rnd_score, ip_score, product_score = 0, 0, 0
    evidence_log = []

    for line in dart_text_data.split('\n'):
        line_lower = line.lower()
        if not any(k in line_lower for k in ai_keywords): continue
        
        clean_context = _clean_dart_text(line)
        if len(clean_context) < 15: continue 
        
        # [A] 제품 직접 언급 (20점)
        exact_prod_match = [pt for pt in prod_tokens if pt.lower() in line_lower]
        if exact_prod_match:
            if product_score == 0: product_score = 20
            evidence_log.append(f"📦 [제품적용] {clean_context}")

        # [B] 사업보고서/연구/투자 (60점) - '사업' 및 '수주' 키워드 추가 대응
        rnd_match = [k for k in rnd_keywords if k in line_lower]
        if rnd_match:
            if rnd_score == 0: rnd_score = 60
            evidence_log.append(f"💰 [사업/연구역량] {clean_context}")

        # [C] 특허/IP (20점)
        ip_match = [k for k in ip_keywords if k in line_lower]
        if ip_match:
            if ip_score == 0: ip_score = 20
            evidence_log.append(f"📜 [특허/IP] {clean_context}")

    unique_evidences = []
    seen = set()
    for ev in evidence_log:
        prefix = ev[:20]
        if prefix not in seen:
            unique_evidences.append(ev)
            seen.add(prefix)
    
    final_evidences = unique_evidences[:5]

    return {
        "total_score": min(rnd_score + ip_score + product_score, 100),
        "rnd_score": rnd_score, 
        "ip_score": ip_score, 
        "product_score": product_score,
        "evidence_log": final_evidences
    }

def check_dart_ai_washing(company_name: str, product_name: str = "") -> dict:
    if not DART_KEY or not company_name or company_name in ["미확인", "없음"]:
        return {"status": "스킵", "total_score": 0, "detail": "기업명 미확인으로 분석을 스킵합니다."}

    try:
        dart = OpenDartReader(DART_KEY)
        corp_list = dart.corp_codes
        corp_info = corp_list[corp_list['corp_name'] == company_name]
        
        if corp_info.empty:
            return {"status": "비상장사", "total_score": 0, "detail": f"DART 미등록 법인({company_name})은 공시 기반 실적 확인이 불가합니다."}

        dart_text_data = ""

        # =========================================================================
        # 🚀 [업그레이드 1] AI/SW 전문 기업의 핵심 실적인 '공시 제목' 전체 수집
        # (특허권 취득, 단일판매/공급계약체결 등에 '인공지능' 키워드가 들어가는지 확인)
        # =========================================================================
        try:
            recent_disclosures = dart.list(company_name, start='2023-01-01')
            if recent_disclosures is not None and not recent_disclosures.empty:
                titles = recent_disclosures['report_nm'].tolist()
                dart_text_data += "\n" + "\n".join(titles)
        except Exception as e:
            pass

        # =========================================================================
        # 🚀 [업그레이드 2] 사업보고서 원문(XML) 강제 파싱
        # (솔트룩스 등 본업이 AI인 회사들의 'II. 사업의 내용' 본문 텍스트 추출)
        # =========================================================================
        try:
            # kind='A'는 정기공시(사업보고서 등)
            annual_reports = dart.list(company_name, start='2023-01-01', kind='A')
            if annual_reports is not None and not annual_reports.empty:
                latest_rcp = annual_reports.iloc[0]['rcp_no']
                # 사업보고서 원문 XML 다운로드
                raw_xml = dart.document(latest_rcp)
                if raw_xml:
                    # 복잡한 XML/HTML 태그를 정규식으로 모두 벗겨내고 순수 텍스트만 추출
                    clean_doc = re.sub(r'<[^>]+>', ' ', raw_xml)
                    dart_text_data += f"\n{clean_doc}"
        except Exception as e:
            pass

        # =========================================================================
        # [기존 로직] 대기업/하드웨어 기업의 타법인출자(투자) 및 직원 현황 유지
        # =========================================================================
        years_to_try = ['2024', '2023']
        for year in years_to_try:
            try:
                investments = dart.report(company_name, '타법인출자', year, '11011')
                if investments is not None and not investments.empty:
                    dart_text_data += f"\n{investments.to_string()}"
            except: pass

        if not dart_text_data.strip():
            return {"status": "데이터 없음", "total_score": 0, "detail": "최근 2년간 공시된 상세 투자/사업 내역이 존재하지 않습니다."}

        analysis_result = _evaluate_product_focused_rules(product_name, dart_text_data)
        
        evidences = analysis_result.get("evidence_log", [])
        if evidences:
            detail_text = f"DART 공시 분석 결과, 총 {len(evidences)}건의 핵심 실적 증거가 확인되었습니다:\n\n"
            for i, ev in enumerate(evidences, 1):
                detail_text += f"      {i}. {ev}\n"
        else:
            detail_text = "공시 원문(사업내용, 계약/특허 공시, 투자 등) 내에서 AI 연구개발이나 자본 투자와 관련된 실질적 키워드가 발견되지 않았습니다."

        status_msg = "공시 실적 검증 완료"
        if analysis_result.get("total_score", 0) < 50:
            status_msg = "AI 핵심 역량 미흡 (워싱 의심)"

        return {
            "status": status_msg,
            "total_score": analysis_result.get("total_score", 0),
            "scores": {
                "rnd": analysis_result.get("rnd_score", 0),
                "ip": analysis_result.get("ip_score", 0),
                "product": analysis_result.get("product_score", 0)
            },
            "evidence": evidences,
            "detail": detail_text
        }
    except Exception as e:
        return {"status": "조회 불가", "total_score": 0, "detail": f"DART API 오류: {str(e)}"}