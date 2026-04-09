 1 +# Fides — AI 워싱 탐지 시스템
        2 +
        3 +다나와 상품 URL을 입력하면 해당 제품의 **AI 워싱(AI Wa
          +shing)** 여부를 자동으로 분석합니다.
        4 +
        5 +> **AI 워싱**이란? 실제 AI 기술이 적용되지 않았음에도
          +마케팅 목적으로 'AI', '인공지능' 등의 표현을 남발하는
          +행위
        6 +
        7 +---
        8 +
        9 +## 주요 기능
       10 +
       11 +- 다나와 상품 페이지 자동 크롤링 및 스크린샷 캡처
       12 +- EasyOCR 기반 상세 이미지 텍스트 추출
       13 +- Gemini AI를 통한 텍스트 정제 및 제조사·모델명 추출
       14 +- 6개 공공기관 데이터 교차 검증
       15 +  - KC 전파인증 DB
       16 +  - 조달청 쇼핑몰 API
       17 +  - TIPA 제조AI 솔루션 인증
       18 +  - KORAIA 한국AI인증센터
       19 +  - KIPRIS 특허청 AI 특허
       20 +  - GS인증·NEP 기술개발제품 인증
       21 +- 신뢰도 점수(0~1) 산출 및 판정 (신뢰 가능 / 불확실 /
          +AI Washing)
       22 +- SSE(Server-Sent Events) 기반 실시간 분석 진행 상황
          +스트리밍
       23 +
       24 +---
       25 +
       26 +## 시스템 구조
       27 +
       28 +```
       29 +Fides/
       30 +├── server.py               # FastAPI 메인 서버 (분석
          +파이프라인, API 엔드포인트)
       31 +├── config.py               # API 키 설정 (git 제외)
       32 +├── static/
       33 +│   └── index.html          # 프론트엔드 UI
       34 +└── logic/
       35 +    ├── crawler.py          # 다나와 크롤러 (Selenium,
          + 스크린샷)
       36 +    ├── ocr_analyzer.py     # OCR 텍스트 추출 (EasyOCR
          +)
       37 +    ├── normalizer.py       # 회사명·모델번호 정규화
       38 +    ├── llm_resolver.py     # Gemini 기반 법인명 역추
          +적
       39 +    ├── patent_scraper.py   # KIPRIS 특허 검색
       40 +    └── import_cert_db.py   # GS·NEP 인증 DB 적재 스크
          +립트 (1회성)
       41 +```
       42 +
       43 +### 분석 파이프라인
       44 +
       45 +```
       46 +URL 입력
       47 +  ↓
       48 +[1] 크롤링        — Selenium으로 다나와 상품 페이지 스
          +크래핑 + 상세 이미지 캡처
       49 +  ↓
       50 +[2] OCR           — EasyOCR로 상세 이미지에서 텍스트
          +추출
       51 +  ↓
       52 +[3] Gemini 정제   — OCR 텍스트 오타 교정, 제조사·모델
          +명 추출
       53 +  ↓
       54 +[4] 정규화        — 회사명 동의어 확장, 기술 모델번호
          +정규식 추출
       55 +  ↓
       56 +[5] 병렬 검증     — KC DB + 조달청 + TIPA + KORAIA 동
          +시 조회
       57 +  ↓
       58 +[6] 특허·인증     — KIPRIS AI 특허, GS·NEP 인증 조회
       59 +  ↓
       60 +[7] 점수 산출     — 텍스트·검증·연관성 3개 차원 합산 →
          + 신뢰도 판정
       61 +```
       62 +
       63 +---
       64 +
       65 +## 시작하기
       66 +
       67 +### 요구사항
       68 +
       69 +- Python 3.10+
       70 +- MySQL 8.0+
       71 +- Google Chrome (크롤러용)
       72 +
       73 +### 설치
       74 +
       75 +```bash
       76 +git clone https://github.com/your-repo/fides.git
       77 +cd fides
       78 +pip install -r requirements.txt
       79 +```
       80 +
       81 +### 설정
       82 +
       83 +프로젝트 루트에 `config.py` 파일을 생성하고 API 키를
          +입력합니다.
       84 +
       85 +```python
       86 +# config.py
       87 +GEMINI_API_KEY   = "your_gemini_api_key"
       88 +KIPRIS_KEY       = "your_kipris_service_key"
       89 +DATA_GO_KR_KEY   = "your_data_go_kr_key"
       90 +OPEN_DATA_KEY    = "your_open_data_key"
       91 +```
       92 +
       93 +| 키 | 발급처 |
       94 +|----|--------|
       95 +| `GEMINI_API_KEY` | [Google AI Studio](https://aistud
          +io.google.com/app/apikey) |
       96 +| `KIPRIS_KEY` | [특허정보검색서비스 KIPRIS+](https://
          +plus.kipris.or.kr) |
       97 +| `DATA_GO_KR_KEY` | [공공데이터포털](https://www.data
          +.go.kr) |
       98 +| `OPEN_DATA_KEY` | [공공데이터포털](https://www.data.
          +go.kr) |
       99 +
      100 +### DB 초기 설정
      101 +
      102 +```bash
      103 +# MySQL에 CapstonDesign 데이터베이스 생성 후
      104 +# GS·NEP 인증 데이터 적재 (최초 1회)
      105 +python logic/import_cert_db.py
      106 +```
      107 +
      108 +KC 전파인증 데이터(`kc_ai_products` 테이블)는 국립전파
          +연구원 CSV를 별도 적재해야 합니다.
      109 +
      110 +### 서버 실행
      111 +
      112 +```bash
      113 +uvicorn server:app --reload
      114 +```
      115 +
      116 +브라우저에서 `http://localhost:8000` 접속 후 다나와 상
          +품 URL을 입력합니다.
      117 +
      118 +---
      119 +
      120 +## 신뢰도 점수 계산
      121 +
      122 +| 차원 | 가중치 | 세부 항목 |
      123 +|------|--------|-----------|
      124 +| 텍스트 점수 | 1/3 | OCR 추출 텍스트 양 (AI 주장 근거
          + 존재 여부) |
      125 +| 검증 점수 | 1/3 | KC 전파인증(50%) + 조달청(20%) + T
          +IPA(20%) + KORAIA(10%) |
      126 +| 연관성 점수 | 1/3 | AI 특허 건수(최대 70%) + GS인증
          +건수(최대 30%) |
      127 +
      128 +| 판정 | 점수 범위 |
      129 +|------|----------|
      130 +| 신뢰 가능 | 0.60 이상 |
      131 +| 불확실 | 0.35 ~ 0.59 |
      132 +| AI Washing | 0.35 미만 |
      133 +
      134 +---
      135 +
      136 +## API 엔드포인트
      137 +
      138 +| 메서드 | 경로 | 설명 |
      139 +|--------|------|------|
      140 +| `POST` | `/api/analyze` | 분석 작업 생성, `task_id`
          +반환 |
      141 +| `GET` | `/api/stream/{task_id}` | SSE로 진행 상황 및
          + 결과 스트리밍 |
      142 +
      143 +### 사용 예시
      144 +
      145 +```bash
      146 +# 분석 요청
      147 +curl -X POST http://localhost:8000/api/analyze \
      148 +  -H "Content-Type: application/json" \
      149 +  -d '{"url": "https://prod.danawa.com/info/?pcode=...
          +"}'
      150 +
      151 +# 결과 스트리밍
      152 +curl http://localhost:8000/api/stream/{task_id}
      153 +```
