# 💪 단백질 트래커

식이영양평가 DB 기반 음식 검색 + Claude AI 사진 인식 + 날짜별 식단 기록 + 프로틴 파우더 계산

---

## 🚀 처음 설치 (한 번만)

### 1. 이 폴더를 원하는 위치에 압축 해제
```
protein_tracker/
├── app.py
├── build_db.py
├── requirements.txt
├── DB/                ← 식이영양평가 원본 DB 폴더
├── templates/
└── static/
```

### 2. Python 가상환경 만들기
```bash
cd protein_tracker

python -m venv venv

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate
```

### 3. 패키지 설치
```bash
pip install -r requirements.txt
```

### 4. DB 변환 (한 번만)
```bash
python build_db.py
```
→ `food_nutrition.db` 파일이 생성됩니다 (식품 1,415개 + 영양소 데이터)

---

## ▶️ 매일 실행

```bash
cd protein_tracker

# Windows
venv\Scripts\activate

# Mac/Linux
source venv/bin/activate

python app.py
```

→ 브라우저에서 **http://localhost:5000** 접속

---

## ⚙️ 설정

앱 왼쪽 사이드바에서:
- **몸무게** 입력 → 권장 단백질 목표 자동 계산
- **일일 목표** 직접 조정 가능
- **Claude API 키** 입력 → 사진으로 음식 인식 가능
  - API 키 없으면 음식 이름 검색만 사용 가능
  - 발급: https://console.anthropic.com

---

## 🔧 하드웨어 연동 (디스펜서)

앱이 실행 중일 때 라즈베리파이에서 호출:

```
GET http://<PC_IP>:5000/api/dispenser/today
```

응답:
```json
{
  "date": "2024-01-15",
  "total_protein_g": 45.2,
  "goal_g": 105,
  "remaining_g": 59.8,
  "scoops_needed": 2.39,
  "should_dispense": true
}
```

앱 내 **하드웨어 연동** 탭에서 예시 코드 확인 가능.

---

## 📁 파일 설명

| 파일 | 설명 |
|------|------|
| `build_db.py` | 원본 DB → SQLite 변환 (최초 1회) |
| `app.py` | Flask 서버 |
| `food_nutrition.db` | SQLite DB (build_db.py 실행 후 생성) |
| `templates/index.html` | 웹 UI |
| `static/uploads/` | 업로드된 음식 사진 저장 |
"# protein_tracker" 
