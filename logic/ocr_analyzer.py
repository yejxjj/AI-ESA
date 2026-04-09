"""
ocr_analyzer.py — 상세 이미지 OCR 텍스트 추출

crawler.py가 캡처한 상세 이미지에서 텍스트를 추출합니다.
추출된 텍스트는 Gemini 정제 → AI 워싱 클레임 분석에 사용됩니다.

주요 처리:
    - 한글 경로 지원: numpy로 파일 읽기 후 OpenCV 디코딩
    - 흑백 변환으로 OCR 정확도 향상
    - 2000px 단위 청크 분할 스캔으로 메모리 효율 확보
"""

import easyocr
import cv2
import numpy as np
import os

# 모듈 로드 시 EasyOCR 리더를 한 번만 초기화 (한국어 + 영어)
reader = easyocr.Reader(['ko', 'en'])


def analyze_ai_washing(image_path):
    """
    이미지 파일에서 텍스트를 OCR로 추출하여 반환합니다.

    Args:
        image_path: 분석할 이미지 파일 경로

    Returns:
        {"extracted_text": str} — 추출된 전체 텍스트 (실패 또는 이미지 없으면 빈 문자열)
    """
    if not image_path or not os.path.exists(image_path):
        return {"extracted_text": ""}

    print("🤖 OCR 엔진 가동! (흑백 변환 + 청크 분할 스캔...)")
    try:
        # 한글 경로 지원: numpy로 읽어서 CV2로 디코딩
        img_array = np.fromfile(image_path, np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        height = gray_img.shape[0]

        chunk_size = 2000
        all_texts = []

        for y in range(0, height, chunk_size):
            chunk = gray_img[y:y + chunk_size, :]
            texts = reader.readtext(chunk, detail=0, paragraph=True)
            if texts:
                all_texts.extend(texts)
            current_y = min(y + chunk_size, height)
            print(f"   청크 스캔 중... ({y}px ~ {current_y}px) / 총 {height}px")

        return {"extracted_text": " ".join(all_texts)}

    except Exception as e:
        print(f"❌ OCR 실패: {e}")
        return {"extracted_text": ""}
