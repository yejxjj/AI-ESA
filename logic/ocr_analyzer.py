"""
ocr_analyzer.py — Gemini Vision 기반 텍스트 추출

crawler.py가 캡처한 상세 이미지를 Gemini Vision API로 전송해
OCR 텍스트를 추출합니다. EasyOCR 대비 CPU 부하 없이 빠르게 동작합니다.

주요 처리:
    - 전송 전 이미지를 JPEG 85%로 압축해 파일 크기 최소화
    - 이미지가 너무 크면 최대 폭 1920px로 리사이즈
    - 실패 시 빈 문자열 반환 (downstream 에러 전파 방지)
"""

import os
import io
import cv2
import numpy as np
from google import genai
from google.genai import types as genai_types

try:
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import config as _config
    GEMINI_API_KEY = getattr(_config, "GEMINI_API_KEY", "") or os.getenv("GEMINI_API_KEY", "")
except Exception:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

_PROMPT = (
    "이 이미지는 온라인 쇼핑몰의 제품 상세페이지 스크린샷입니다. "
    "이미지 안에 보이는 모든 텍스트를 빠짐없이 추출해 주세요. "
    "마케팅 문구, AI 관련 주장, 기술 스펙, 인증 정보, 제품 설명 등 "
    "모든 글자를 원문 그대로 나열해 주세요. "
    "설명이나 해석은 필요 없고, 텍스트만 출력해 주세요."
)

_MAX_WIDTH = 1920


def _load_and_compress(image_path: str) -> bytes:
    """이미지를 읽어 필요시 리사이즈 후 JPEG bytes로 반환"""
    img_array = np.fromfile(image_path, np.uint8)
    img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)

    h, w = img.shape[:2]
    if w > _MAX_WIDTH:
        scale = _MAX_WIDTH / w
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        print(f"   이미지 리사이즈: {w}×{h} → {img.shape[1]}×{img.shape[0]}")

    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


def analyze_ai_washing(image_path: str) -> dict:
    """
    Gemini Vision으로 이미지에서 텍스트를 추출합니다.

    Args:
        image_path: 분석할 이미지 파일 경로

    Returns:
        {"extracted_text": str}
    """
    if not image_path or not os.path.exists(image_path):
        return {"extracted_text": ""}

    if not _client:
        print("❌ GEMINI_API_KEY가 설정되지 않았습니다.")
        return {"extracted_text": ""}

    print("🤖 Gemini Vision OCR 시작...")
    try:
        jpeg_bytes = _load_and_compress(image_path)
        size_kb = len(jpeg_bytes) / 1024
        print(f"   전송 이미지 크기: {size_kb:.0f} KB")

        response = _client.models.generate_content(
            model="gemini-2.0-flash",
            contents=[
                genai_types.Part(
                    inline_data=genai_types.Blob(data=jpeg_bytes, mime_type="image/jpeg")
                ),
                genai_types.Part(text=_PROMPT),
            ],
        )

        extracted = response.text.strip() if response.text else ""
        print(f"✅ Gemini Vision OCR 완료 — {len(extracted)}자 추출")
        return {"extracted_text": extracted}

    except Exception as e:
        print(f"❌ Gemini Vision OCR 실패: {e}")
        return {"extracted_text": ""}
