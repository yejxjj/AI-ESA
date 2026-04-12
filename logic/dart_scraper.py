"""
dart_scraper.py — Open DART API 기반 제품 중심 AI 실체 분석기
(증거 데이터 5개 추출 및 상세 보고서 자동 생성 버전)
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
    """날짜, DART 접수번호 등 불필요한 노이즈를 제거하여 보고서용 텍스트를 만듭니다."""
    text = re.sub(r'\b\d{14}\b', '', text)
    text = re.sub(r'\b\d{8}\b', '', text)
    text = re.sub(r'\b\d{4}[-.]\d{2}[-.]\d{2}\b', '', text)
    text = re.sub(r'\b[A-Za-z]\b', '', text)
    return re.sub(r'\s+', ' ', text).strip()

def _evaluate_product_focused_rules(product_name: str, dart_text_data: str) -> dict:
    ai_keywords = ['인공지능', '딥러닝', '머신러닝', '생성형', 'llm', '자연어', '신경망', ' ai ', '(ai)', ' ai,', ' ai.']
    
    rnd_keywords = ['연구', '개발', '센터', '랩', 'lab', '투자', '출자', '인수']
    ip_keywords = ['특허', '지식재산', '지적재산', '출원', '등록', 'ip']

    prod_tokens = [token for token in product_name.split() if len(token) > 1]
    
    rnd_score, ip_score, product_score = 0, 0, 0
    evidence_log = []

    for line in dart_text_data.split('\n'):
        line_lower = line.lower()
        if not any(k in line_lower for k in ai_keywords): continue
        
        clean_context = _clean_dart_text(line)
        if len(clean_context) < 15: continue # 너무 짧은 문장은 무시
        
        # [A] 제품 직접 언급 (20점)
        exact_prod_match = [pt for pt in prod_tokens if pt.lower() in line_lower]
        if exact_prod_match:
            if product_score == 0: product_score = 20
            evidence_log.append(f"📦 [제품적용] {clean_context}")

        # [B] 연구/투자 (60점)
        rnd_match = [k for k in rnd_keywords if k in line_lower]
        if rnd_match:
            if rnd_score == 0: rnd_score = 60
            evidence_log.append(f"💰 [자본/연구투자] {clean_context}")

        # [C] 특허/IP (20점)
        ip_match = [k for k in ip_keywords if k in line_lower]
        if ip_match:
            if ip_score == 0: ip_score = 20
            evidence_log.append(f"📜 [특허/IP] {clean_context}")

    # 중복 증거 제거 및 최대 5개 선정
    unique_evidences = []
    seen = set()
    for ev in evidence_log:
        # 문장 유사도 중복 방지를 위해 앞 20자만 체크
        prefix = ev[:20]
        if prefix not in seen:
            unique_evidences.append(ev)
            seen.add(prefix)
    
    final_evidences = unique_evidences[:5] # 🌟 정확히 5개(또는 이하)로 제한

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

        years_to_try = ['2024', '2023']
        dart_text_data = ""

        for year in years_to_try:
            try:
                investments = dart.report(company_name, '타법인출자', year, '11011')
                employees = dart.report(company_name, '직원', year, '11011')
                
                if investments is not None and not investments.empty:
                    dart_text_data += f"\n{investments.to_string()}"
                if employees is not None and not employees.empty:
                    dart_text_data += f"\n{employees.to_string()}"
                
                if dart_text_data: break
            except: continue

        if not dart_text_data:
            return {"status": "데이터 없음", "total_score": 0, "detail": "최근 2년간 공시된 상세 투자/인력 내역이 존재하지 않습니다."}

        analysis_result = _evaluate_product_focused_rules(product_name, dart_text_data)
        
        # 🌟 상세 이유(detail) 문장 생성 로직
        evidences = analysis_result.get("evidence_log", [])
        if evidences:
            detail_text = f"DART 공시 분석 결과, 총 {len(evidences)}건의 핵심 실적 증거가 확인되었습니다:\n\n"
            for i, ev in enumerate(evidences, 1):
                detail_text += f"      {i}. {ev}\n"
        else:
            detail_text = "공시 데이터 내에서 AI 연구개발이나 자본 투자와 관련된 실질적 키워드가 발견되지 않았습니다. (워싱 위험성 존재)"

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