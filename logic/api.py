from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from sqlalchemy import create_engine
import pandas as pd
from typing import Optional

app = FastAPI(title="전파인증 API", description="RRA 데이터를 조회하는 API입니다.")

db_connection_str = 'mysql+pymysql://root:1234@localhost:3306/CapstonDesign'
engine = create_engine(db_connection_str)

# ----------------------------------------------------
#  띄어쓰기로 여러 단어를 검색할 수 있는  API
# ----------------------------------------------------
@app.get("/api/search")
def search_products(
    company: Optional[str] = None, 
    equip: Optional[str] = None,   
    model: Optional[str] = None,   
    format: str = "json"
):
    query = "SELECT * FROM rra WHERE 1=1"
    
    #  함수: 이제 '쉼표(,)'를 기준으로 단어를 쪼갭니다!
    def build_condition(column_name, search_text):
        if not search_text:
            return ""
            
        # 1. 쉼표(,)를 기준으로 쪼개고, 단어 앞뒤에 실수로 들어간 띄어쓰기(거품)를 싹 빼줍니다(strip)
        keywords = [kw.strip() for kw in search_text.split(',')]
        
        # 빈 단어 제거 (예: "삼성, , LG" 처럼 실수로 쉼표를 두 번 쳤을 때 방어)
        keywords = [kw for kw in keywords if kw]
        
        if not keywords:
            return ""

        # 2. 쪼갠 단어들 각각에 LIKE 조건을 씌움
        conditions = [f"{column_name} LIKE '%%{kw}%%'" for kw in keywords]
        
        # 3. OR로 묶고 괄호를 쳐서 반환
        return " AND (" + " OR ".join(conditions) + ")"
    
    # 사용자가 입력한 칸만 찾아서 찰칵찰칵 조립
    query += build_condition("company_name", company)
    query += build_condition("equip_name", equip)
    query += build_condition("model_name", model)
        
    # 방어 코드: 세 칸 다 비워뒀을 때
    if not company and not equip and not model:
        if format == "text":
            return PlainTextResponse("검색어를 최소 한 칸 이상 입력해 주세요.")
        return {"message": "검색어를 최소 한 칸 이상 입력해 주세요.", "data": []}

    query += " LIMIT 100"
    
    # DB에 쿼리 날리기
    try:
        df = pd.read_sql(query, engine)
    except Exception as e:
        return {"error": f"DB 검색 중 에러 발생: {e}"}
    
    if df.empty:
        if format == "text":
            return PlainTextResponse("조건에 맞는 검색 결과가 없습니다.")
        return {"message": "검색 결과가 없습니다.", "data": []}

    if format == "json":
        return df.to_dict(orient='records')
    
    elif format == "text":
        search_info = []
        if company: search_info.append(f"상호명:'{company}'")
        if equip: search_info.append(f"기기명:'{equip}'")
        if model: search_info.append(f"모델명:'{model}'")
        
        text_result = f"=== [{', '.join(search_info)}] 검색 결과 (총 {len(df)}건) ===\n\n"
        for index, row in df.iterrows():
            text_result += f"[{index+1}] 상호: {row['company_name']} | 기기명: {row['equip_name']} | 모델명: {row['model_name']} | 인증번호: {row['cert_no']}\n"
        return PlainTextResponse(text_result)
        
    else:
        return {"error": "지원하지 않는 포맷입니다."}