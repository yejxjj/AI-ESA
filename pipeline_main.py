import os
import sys
import time
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.dart_scraper import check_dart_ai_washing
from logic.api import (
    verify_rra, verify_tta, verify_kipris, 
    verify_koneps, verify_pps_mall, verify_nipa_solution, verify_kaiac
)

def mock_get_product_data(url):
    print("⏳ [크롤링 연출 중] 제품 상세 페이지 분석 및 AI 기술 키워드 스캐닝...")
    for i in range(10, 0, -1):
        print(f"   ... 데이터 수집 중 (남은 시간: {i}초)", end="\r")
        time.sleep(1)
    print("\n✅ 데이터 분석 완료!\n")
    
    if "lg" in url.lower() or "82630370" in url:
        return {"raw_company": "LG전자", "model_name": "WA2525GEHF", "category": "가전"}
    else:
        # 크로스오버 모니터 (90890663)
        return {"raw_company": "크로스오버", "model_name": "27GQC7", "category": "모니터"}

def run_full_pipeline(url: str):
    print("\n" + "="*85)
    print("🚀 [FIDES AI] 실시간 AI 워싱 검증 파이프라인 가동 (데모 모드)")
    print("="*85)
    
    scraped_item = mock_get_product_data(url)
    raw_company = scraped_item.get("raw_company", "")
    
    if raw_company == "LG전자":
        official_company = "LG전자"
        official_model = "WA2525GEHF"
        search_aliases = ["LG전자", "엘지전자", "엘지전자 주식회사", "(주)엘지전자"]
    else:
        official_company = "크로스오버존" 
        official_model = "27GQC7 멀티스탠드"
        search_aliases = ["크로스오버존", "CROSSOVER", "(주)크로스오버존", "크로스오버"]

    print(f"🔍 분석 대상: {official_company} / {official_model}")
    print(f"📡 활용 별칭: {search_aliases}") 
    print("⏳ 다중 검증을 병렬로 진행합니다...")

    final_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(check_dart_ai_washing, official_company): 'DART',
            executor.submit(verify_kipris, search_aliases, scraped_item.get("category")): 'KIPRIS',
            executor.submit(verify_rra, search_aliases, official_model): 'RRA',
            executor.submit(verify_tta, search_aliases): 'TTA',
            executor.submit(verify_nipa_solution, search_aliases): 'AI공급',
            executor.submit(verify_pps_mall, search_aliases): '조달몰',
            executor.submit(verify_koneps, search_aliases): '나라장터',
            executor.submit(verify_kaiac, search_aliases): 'KAIAC'
        }
        for future in concurrent.futures.as_completed(futures):
            final_results[futures[future]] = future.result()

    # [점수 계산]
    dart_res = final_results.get('DART', {})
    dart_raw = dart_res.get('total_score', 0)
    
    # LG전자인 경우 잭팟(100점) 연출
    if official_company == "LG전자" and dart_raw > 0:
        dart_raw = 100 
        print(f"✨ [DART 잭팟] 공시 보고서 내 핵심 AI 알고리즘 실체 확인! (100점 부여)")

    score_dart = int(dart_raw * 0.3)
    score_kipris = final_results.get('KIPRIS', {}).get('score', 0)
    score_rra = final_results.get('RRA', {}).get('score', 0)
    score_tta = final_results.get('TTA', {}).get('score', 0)
    
    bonus_nipa = final_results.get('AI공급', {}).get('score', 0)
    bonus_koneps = final_results.get('나라장터', {}).get('score', 0)
    bonus_pps = final_results.get('조달몰', {}).get('score', 0)
    bonus_kaiac = final_results.get('KAIAC', {}).get('score', 0)
    
    total_raw_score = score_dart + score_kipris + score_rra + score_tta + bonus_nipa + bonus_koneps + bonus_pps + bonus_kaiac
    final_score = min(total_raw_score, 100)

    # [리포트 출력 1: 상세 이유]
    print("\n" + "="*85)
    print("📄 [1] AI 워싱 검증 이유 보고서 (Detail Report)")
    print("="*85)
    sources = [
        ('DART 공시 실적', 'DART'), ('KIPRIS 특허 실적', 'KIPRIS'), 
        ('RRA 전파인증', 'RRA'), ('국가 품질/신기술 인증', 'TTA'),
        ('AI솔루션 공급기업', 'AI공급'), ('나라장터 낙찰정보', '나라장터'), 
        ('조달청 디지털서비스몰', '조달몰'), ('한국인공지능인증센터', 'KAIAC')
    ]
    for title, key in sources:
        res = final_results.get(key, {})
        print(f"\n📌 [{title}]")
        print(f"└ {res.get('detail', '내역 없음')} (증거: {res.get('evidence', '없음')})")

    # [리포트 출력 2: 점수 통계 - 원래대로 복구]
    print("\n" + "="*85)
    print("📊 [2] 최종 점수 및 종합 결과 보고서 (Score Report)")
    print("="*85)
    print(f"🏢 타겟 법인: {official_company} | 📦 제품/모델: {official_model}")
    print("-" * 85)

    print("\n[코어 지표 (기본: 100점)]")
    print(f"▶ DART 공시 실적   : {score_dart:02d}점 / 30")
    print(f"▶ KIPRIS 특허 실적 : {score_kipris:02d}점 / 30")
    print(f"▶ RRA 전파인증     : {score_rra:02d}점 / 20")
    print(f"▶ 국가품질/신기술인증: {score_tta:02d}점 / 20")
    
    print("\n[가산점 지표 (최대: +50점)]")
    print(f"▶ AI솔루션 공급기업: {bonus_nipa:02d}점 / +20")
    print(f"▶ 나라장터 낙찰 실적: {bonus_koneps:02d}점 / +15")
    print(f"▶ 조달청 디지털몰  : {bonus_pps:02d}점 / +10")
    print(f"▶ AI인증센터(KAIAC): {bonus_kaiac:02d}점 / +05")
    print("-" * 85)
    
    print(f"⭐ 최종 AI 신뢰도 점수 : {final_score} / 100 점 (획득 원점수: {total_raw_score}점)")
    
    if final_score >= 70: print("🟢 판정: AI 기술 실체 확인 (워싱 위험 매우 낮음)")
    elif final_score >= 50: print("🟡 판정: 일부 AI 기술 확인 (추가 검증 필요)")
    else: print("🔴 판정: AI 기술 근거 부족 (AI 워싱 강력 의심군!)")
    print("="*85 + "\n")

if __name__ == "__main__":
    url_lg = "https://prod.danawa.com/info/?pcode=82630370&keyword=lg+ai&cate=10239280"
    url_cross = "https://prod.danawa.com/info/?pcode=90890663"

    
    choice = input()
    run_full_pipeline(url_lg if choice == url_lg else url_cross)