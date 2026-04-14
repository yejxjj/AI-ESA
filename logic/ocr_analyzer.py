import cv2
import numpy as np
import easyocr
import os

def analyze_ai_washing(image_path):
    # 1. 엔진 초기화
    reader = easyocr.Reader(['ko', 'en'], gpu=False)
    
    # [수정 포인트] cv2.imread(image_path) 대신 한글 경로 지원 방식으로 변경
    try:
        # 이미지를 바이너리로 읽어서 넘파이 배열로 변환
        img_array = np.fromfile(image_path, np.uint8)
        img = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    except Exception as e:
        print(f"❌ 이미지 로드 실패: {e}")
        return {"extracted_text": ""}

    if img is None: 
        print(f"❌ 이미지를 불러올 수 없습니다. 경로를 확인하세요: {image_path}")
        return {"extracted_text": ""}

    h, w = img.shape[:2]
    
    # 2. 가로 폭 제한 (1200px)
    target_w = 1200
    if w > target_w:
        ratio = target_w / w
        img = cv2.resize(img, (target_w, int(h * ratio)))
        h, w = img.shape[:2]

    # 3. 청크 설정 (Overlap 적용)
    chunk_h = 3000
    overlap = 200
    all_text = []

    print(f"🧐 [OCR 엔진] 총 {h}px 정밀 스캔 시작 (한글 경로 호환 모드)")

    start_y = 0
    while start_y < h:
        end_y = min(start_y + chunk_h, h)
        chunk = img[start_y:end_y, 0:w]
        
        # 4. 빈 공간 스킵 로직
        edges = cv2.Canny(chunk, 30, 100)
        if np.count_nonzero(edges) / edges.size < 0.002:
            start_y += (chunk_h - overlap)
            continue

        print(f" 🔍 {start_y}px ~ {end_y}px 스캔 중.../{h}px")
        results = reader.readtext(chunk, detail=0)
        all_text.extend(results)
        
        if end_y == h: break
        start_y += (chunk_h - overlap)

    final_text = list(dict.fromkeys(all_text))
    return {"extracted_text": " ".join(final_text)}