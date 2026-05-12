"""
dart_scraper.py — Open DART API 기반 제품 중심 AI 실체 분석기 (캐시 로직 제거본)
"""
import OpenDartReader
import os
import sys
import re
import contextlib
import io
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import config
    DART_KEY = getattr(config, 'DART_API_KEY', getattr(config, 'DART_KEY', ''))
except ImportError:
    DART_KEY = os.environ.get("DART_API_KEY", "")


def _clean_dart_text(text: str) -> str:
    if not text: return ""
    text = text[:200000] # 정규식 과부하 방지를 위해 최대 20만 자로 제한
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\b\d{14}\b|\b\d{8}\b|\b\d{4}[-.]\d{2}[-.]\d{2}\b', '', text) 
    text = re.sub(r'\b\d{1,3}(,\d{3})+\b|\b\d{4,}\b|\b\d+\.\d+\b|\b\d{1,2}\b', '', text)          
    return re.sub(r'\s+', ' ', text).strip()

def _evaluate_product_focused_rules(product_name: str, dart_text_data: str) -> dict:
    ai_keywords = ['인공지능', '딥러닝', '머신러닝', '생성형', 'llm', '자연어', '신경망', 'ai', '스마트', '빅데이터']
    rnd_keywords = ['연구', '개발', '센터', '랩', 'lab', '투자', '출자', '인수', '사업', '수주', '공급', '솔루션']
    ip_keywords = ['특허', '지식재산', '지적재산', '출원', '등록', 'ip']
    
    prod_tokens = [token for token in product_name.split() if len(token) > 1]
    
    rnd_score, ip_score, product_score = 0, 0, 0
    evidence_log = []

    for line in dart_text_data.split('\n'):
        line_lower = line.lower()
        if not line_lower.strip(): continue
        
        # 🎯 AI 관련 키워드가 문장에 없으면 무조건 버림 (일반 투자/경영참여 차단)
        if not any(k in line_lower for k in ai_keywords):
            continue
        
        clean_context = _clean_dart_text(line)[:150]
        
        if prod_tokens and any(pt.lower() in line_lower for pt in prod_tokens):
            if product_score == 0: product_score = 20
            evidence_log.append(f"📦 [AI 제품적용] {clean_context}")
        elif any(k in line_lower for k in rnd_keywords):
            if rnd_score == 0: rnd_score = 60
            evidence_log.append(f"💰 [AI 사업/연구] {clean_context}")
        elif any(k in line_lower for k in ip_keywords):
            if ip_score == 0: ip_score = 20
            evidence_log.append(f"📜 [AI 특허/IP] {clean_context}")
        else:
            if rnd_score == 0: rnd_score = 60
            evidence_log.append(f"🔍 [AI 기술언급] {clean_context}")

    unique_evidences = list(set(evidence_log))
    return {"total_score": min(rnd_score + ip_score + product_score, 100), "evidence_log": unique_evidences[:5]}

def check_dart_ai_washing(company_aliases: list, product_name: str = "") -> dict:
    if not DART_KEY or not company_aliases: return {"status": "스킵", "total_score": 0, "detail": "기업명 미확인."}

    try:
        dart = OpenDartReader(DART_KEY)
        corp_list = dart.corp_codes
        found_corp_name = None
        for name in company_aliases:
            clean = re.sub(r'\(주\)|주식회사|\(유\)|㈜', '', name).strip()
            if not clean: continue
            corp_info = corp_list[corp_list['corp_name'] == clean]
            if corp_info.empty: corp_info = corp_list[corp_list['corp_name'].str.contains(clean, na=False, regex=False)]
            if not corp_info.empty:
                found_corp_name = corp_info.iloc[0]['corp_name']
                break

        if not found_corp_name: return {"status": "비상장사", "total_score": 0, "detail": "DART 미등록 법인."}

        dart_text_data = ""
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                recent = dart.list(found_corp_name, start='2023-01-01')
                if recent is not None and not recent.empty: dart_text_data += "\n".join(recent['report_nm'].tolist()) + "\n"
            except: pass

            try:
                annual = dart.list(found_corp_name, start='2023-01-01', kind='A')
                if annual is not None and not annual.empty:
                    raw_xml = dart.document(annual.iloc[0]['rcp_no'])
                    if raw_xml: dart_text_data += raw_xml[:150000] + "\n" 
            except: pass

            for year in ['2024', '2023']:
                try:
                    investments = dart.report(found_corp_name, '타법인출자', year, '11011')
                    if investments is not None and not investments.empty: dart_text_data += investments.to_string() + "\n"
                    break
                except: time.sleep(0.5)

        if not dart_text_data.strip():
            return {"status": "데이터 없음", "total_score": 0, "detail": "공시된 상세 내역이 없습니다."}

        analysis_result = _evaluate_product_focused_rules(product_name, dart_text_data)
        evidences = analysis_result.get("evidence_log", [])
        status_msg = "공시 실적 검증 완료" if analysis_result.get("total_score", 0) >= 50 else "AI 핵심 역량 미흡 (워싱 의심)"
        
        return {"status": status_msg, "total_score": analysis_result.get("total_score", 0), "evidence": evidences, "detail": f"DART 분석 결과 총 {len(evidences)}건 확인." if evidences else "실질적 AI 관련 공시가 없습니다."}

    except Exception as e:
        return {"status": "조회 불가", "total_score": 0, "detail": f"DART 서버 지연: {str(e)}"}