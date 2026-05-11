"""
dart_scraper.py — Open DART API 기반 제품 중심 AI 실체 분석기
"""
import OpenDartReader
import json
import os
import sys
import re
import contextlib
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    DART_KEY = getattr(config, 'DART_API_KEY', getattr(config, 'DART_KEY', ''))
except ImportError:
    DART_KEY = os.environ.get("DART_API_KEY", "")

def _clean_dart_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'\b\d{14}\b|\b\d{8}\b|\b\d{4}[-.]\d{2}[-.]\d{2}\b', '', text) 
    text = re.sub(r'\b\d{1,3}(,\d{3})+\b|\b\d{4,}\b|\b\d+\.\d+\b|\b\d{1,2}\b', '', text)          
    text = re.sub(r'[-=]{2,}|\b[A-Za-z]\b', '', text)             
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
        
        if [pt for pt in prod_tokens if pt.lower() in line_lower]:
            if product_score == 0: product_score = 20
            evidence_log.append(f"📦 [제품적용] {clean_context}")
        if [k for k in rnd_keywords if k in line_lower]:
            if rnd_score == 0: rnd_score = 60
            evidence_log.append(f"💰 [사업/연구역량] {clean_context}")
        if [k for k in ip_keywords if k in line_lower]:
            if ip_score == 0: ip_score = 20
            evidence_log.append(f"📜 [특허/IP] {clean_context}")

    unique_evidences = []
    seen = set()
    for ev in evidence_log:
        prefix = ev[:20]
        if prefix not in seen:
            unique_evidences.append(ev)
            seen.add(prefix)
            
    return {
        "total_score": min(rnd_score + ip_score + product_score, 100),
        "rnd_score": rnd_score, "ip_score": ip_score, "product_score": product_score,
        "evidence_log": unique_evidences[:5]
    }

# 🎯 [핵심 수정] 단일 문자열 대신 리스트를 받고, 라이브러리의 print 출력을 강제 차단합니다.
def check_dart_ai_washing(company_aliases: list, product_name: str = "") -> dict:
    if not DART_KEY or not company_aliases:
        return {"status": "스킵", "total_score": 0, "detail": "기업명 미확인으로 분석을 스킵합니다."}

    try:
        dart = OpenDartReader(DART_KEY)
        corp_list = dart.corp_codes

        found_corp_name = None
        # 여러 별칭을 돌면서 DART에 등록된 진짜 이름을 찾습니다. (예: LG전자 -> 실패, 엘지전자 -> 성공)
        for name in company_aliases:
            clean = re.sub(r'\(주\)|주식회사|\(유\)|㈜', '', name).strip()
            if not clean: continue
            
            corp_info = corp_list[corp_list['corp_name'] == clean]
            if corp_info.empty:
                corp_info = corp_list[corp_list['corp_name'].str.contains(clean, na=False, regex=False)]
            
            if not corp_info.empty:
                found_corp_name = corp_info.iloc[0]['corp_name']
                break

        if not found_corp_name:
            return {"status": "비상장사", "total_score": 0, "detail": f"DART 미등록 법인({company_aliases[0]} 등)은 확인 불가."}

        dart_text_data = ""

        # 🎯 [입막음 처리] OpenDartReader가 멋대로 뱉는 013 에러 print를 가상의 공간으로 보내버립니다.
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                recent_disclosures = dart.list(found_corp_name, start='2023-01-01')
                if recent_disclosures is not None and not recent_disclosures.empty:
                    dart_text_data += "\n" + "\n".join(recent_disclosures['report_nm'].tolist())
            except: pass

            try:
                annual_reports = dart.list(found_corp_name, start='2023-01-01', kind='A')
                if annual_reports is not None and not annual_reports.empty:
                    raw_xml = dart.document(annual_reports.iloc[0]['rcp_no'])
                    if raw_xml: dart_text_data += f"\n{re.sub(r'<[^>]+>', ' ', raw_xml)}"
            except: pass

            for year in ['2024', '2023']:
                try:
                    investments = dart.report(found_corp_name, '타법인출자', year, '11011')
                    if investments is not None and not investments.empty:
                        dart_text_data += f"\n{investments.to_string()}"
                except: pass

        if not dart_text_data.strip():
            return {"status": "데이터 없음", "total_score": 0, "detail": "최근 2년간 공시된 상세 내역이 없습니다."}

        analysis_result = _evaluate_product_focused_rules(product_name, dart_text_data)
        evidences = analysis_result.get("evidence_log", [])
        
        detail_text = f"DART 분석 결과, 총 {len(evidences)}건의 핵심 실적이 확인되었습니다.\n" if evidences else "공시 원문 내에서 실질적 AI 키워드가 발견되지 않았습니다."
        status_msg = "공시 실적 검증 완료" if analysis_result.get("total_score", 0) >= 50 else "AI 핵심 역량 미흡 (워싱 의심)"

        return {
            "status": status_msg, "total_score": analysis_result.get("total_score", 0),
            "evidence": evidences, "detail": detail_text
        }
    except Exception as e:
        return {"status": "조회 불가", "total_score": 0, "detail": f"DART 오류: {str(e)}"}