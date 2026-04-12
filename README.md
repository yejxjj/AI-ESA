# Fides — AI 워싱 탐지 시스템

다나와 상품 URL을 입력하면 해당 제품의 **AI 워싱(AI Washing)** 여부를 자동으로 분석합니다.

> **AI 워싱**이란? 실제 AI 기술이 적용되지 않았음에도 마케팅 목적으로 'AI', '인공지능' 등의 표현을 남발하는 행위

---

## 작동 방식

상품 페이지를 크롤링하여 이미지에서 텍스트를 추출하고, Gemini AI로 제조사·모델명을 정제한 뒤 6개 공공기관 데이터와 교차 검증합니다. 검증 결과를 텍스트·검증·연관성 3개 차원으로 합산해 신뢰도 점수(0~1)를 산출합니다.

| 판정 | 점수 |
| --- | --- |
| 신뢰 가능 | 0.60 이상 |
| 불확실 | 0.35 ~ 0.59 |
| AI Washing | 0.35 미만 |

교차 검증 대상: KC 전파인증 · 조달청 · TIPA 제조AI솔루션 · KORAIA · KIPRIS 특허 · GS인증·NEP

---

## 기술 스택

- **Backend**: FastAPI, Selenium, EasyOCR, Gemini API
- **DB**: MySQL
- **기타**: SSE 기반 실시간 결과 스트리밍

---

## Dependencies
이 프로젝트는 Python 기반으로 작성되었으며, 원활한 실행을 위해 아래 라이브러리 설치가 필요합니다.
### Web Server
fastapi
uvicorn

### Database & Data
sqlalchemy
pymysql
cryptography
pandas

### AI & API
google-genai
OpenDartReader

### Crawling & OCR
undetected-chromedriver
selenium
easyocr
opencv-python

## 설치 방법
pip install fastapi uvicorn sqlalchemy pymysql cryptography pandas \
            google-genai OpenDartReader undetected-chromedriver \
            selenium easyocr opencv-python

> 현재 개발 진행 중입니다.
