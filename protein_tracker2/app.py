import os, json, uuid, secrets
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session
from PIL import Image
from google import genai
from google.genai import types
import psycopg2
from psycopg2.extras import RealDictCursor
import sqlite3
import cloudinary
import cloudinary.uploader

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "protein-tracker-secret-2024")
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_DB_PATH = "food_nutrition.db"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Cloudinary 설정
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key=os.environ.get("CLOUDINARY_API_KEY"),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET")
)

def upload_to_cloudinary(file_path, folder="meals"):
    """파일을 Cloudinary에 업로드하고 URL 반환. 실패 시 None 반환"""
    try:
        result = cloudinary.uploader.upload(
            file_path,
            folder=folder,
            resource_type="image"
        )
        return result.get("secure_url")
    except Exception as e:
        print(f"Cloudinary 업로드 실패: {e}")
        return None


# ─────────────────────────────────────────
# DB 연결
# ─────────────────────────────────────────
def get_conn():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return conn


# ─────────────────────────────────────────
# DB 초기화
# ─────────────────────────────────────────
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        nickname TEXT DEFAULT '',
        display_name TEXT DEFAULT '',
        weight REAL DEFAULT 0,
        multiplier REAL DEFAULT 1.5,
        is_admin BOOLEAN DEFAULT FALSE
    )""")

    # 기존 테이블에 is_admin 없으면 추가
    try:
        cur.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE")
        conn.commit()
    except Exception:
        conn.rollback()

    cur.execute("""CREATE TABLE IF NOT EXISTS meals (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        food_cd TEXT,
        food_name TEXT NOT NULL,
        emoji TEXT DEFAULT '🍽️',
        amount TEXT,
        weight_g REAL,
        protein_g REAL NOT NULL,
        energy_kcal REAL,
        fat_g REAL,
        carb_g REAL,
        image_path TEXT,
        created_at TEXT
    )""")

    # 유저별 프로틴 제품 테이블
    cur.execute("""CREATE TABLE IF NOT EXISTS protein_products (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        protein_per_scoop REAL NOT NULL,
        scoop_weight_g REAL DEFAULT 0,
        energy_kcal REAL DEFAULT 0,
        image_path TEXT,
        created_at TEXT,
        is_active BOOLEAN DEFAULT TRUE
    )""")

    # 하드웨어 기기 토큰 테이블
    cur.execute("""CREATE TABLE IF NOT EXISTS devices (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        name TEXT DEFAULT '내 디스펜서',
        created_at TEXT,
        last_seen TEXT
    )""")

    # AI 텍스트 검색 캐시 테이블
    cur.execute("""CREATE TABLE IF NOT EXISTS ai_food_cache (
        id SERIAL PRIMARY KEY,
        food_name TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        protein_g REAL DEFAULT 0,
        energy_kcal REAL DEFAULT 0,
        fat_g REAL DEFAULT 0,
        carb_g REAL DEFAULT 0,
        std_unit TEXT DEFAULT '',
        search_count INTEGER DEFAULT 1,
        created_at TEXT
    )""")

    # 관리자가 승격시킨 커스텀 음식 DB
    cur.execute("""CREATE TABLE IF NOT EXISTS custom_foods (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL,
        protein_g REAL DEFAULT 0,
        energy_kcal REAL DEFAULT 0,
        fat_g REAL DEFAULT 0,
        carb_g REAL DEFAULT 0,
        std_unit TEXT DEFAULT '',
        category TEXT DEFAULT 'AI추가',
        created_at TEXT
    )""")

    # 위젯 전용 토큰 (세션 없이 인증)
    cur.execute("""CREATE TABLE IF NOT EXISTS widget_tokens (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        token TEXT UNIQUE NOT NULL,
        created_at TEXT
    )""")

    conn.commit()
    cur.close()
    conn.close()

init_db()


# ─────────────────────────────────────────
# 음식 DB 검색 (food_nutrition SQLite 유지)
# ─────────────────────────────────────────
def find_food_in_db(search_keyword):
    """
    AI가 추출한 search_keyword를 바탕으로 DB에서 가장 유사한 음식을 찾습니다.
    """
    if not search_keyword:
        return None
        
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        
        # 1순위: 이름이 정확히 일치하는지 확인
        row = conn.execute("SELECT * FROM food_nutrition WHERE name = ? LIMIT 1", (search_keyword,)).fetchone()
        
        # 2순위: 일치하는 게 없다면 LIKE 검색 (유사 검색)
        if not row:
            q = f"%{search_keyword}%"
            row = conn.execute("""
                SELECT * FROM food_nutrition 
                WHERE name LIKE ? OR synm LIKE ? OR synm2 LIKE ? OR srch_keyword LIKE ? 
                ORDER BY length(name) ASC LIMIT 1
            """, (q, q, q, q)).fetchone()
        
        conn.close()
        return dict(row) if row else None
        
    except Exception as e:
        print(f"DB 검색 오류: {e}")
        return None

def search_food_in_db(q, limit=15):
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM food_nutrition
            WHERE name LIKE ? OR synm LIKE ? OR synm2 LIKE ? OR srch_keyword LIKE ?
            LIMIT ?
        """, (f"%{q}%",)*4 + (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ─────────────────────────────────────────
# Gemini 헬퍼
# ─────────────────────────────────────────
def get_gemini_client():
    key = GEMINI_API_KEY
    if not key:
        return None
    return genai.Client(api_key=key)

def analyze_image_with_gemini(image_path):
    client = get_gemini_client()
    if not client:
        return {"error": "서버에 API 키가 설정되지 않았습니다."}
    
    try:
        # 이미지 처리 (512px 최적화 유지)
        with Image.open(image_path) as img:
            if img.mode != 'RGB': img = img.convert('RGB')
            img.thumbnail((512, 512))
            img.save(image_path, "JPEG", quality=85)
        
        with open(image_path, "rb") as f:
            image_data = f.read()

        # 한국어 답변을 강력하게 지시하는 프롬프트 추가
        instruction = """
        당신은 한국인 전문 영양사입니다. 
        반드시 모든 음식의 이름('name')은 한국어로만 답변하세요.
        에너지('energy_kcal'), 단백질('protein_g') 등 영양 성분은 1인분 기준으로 추정하세요.
        """

        response = client.models.generate_content(
            model="gemini-flash-lite-latest", # 최신 모델명 권장
            contents=[
                instruction,
                types.Part.from_bytes(data=image_data, mime_type="image/jpeg")
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "foods": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "name": {"type": "STRING", "description": "음식의 한국어 이름"},
                                    "energy_kcal": {"type": "NUMBER"},
                                    "protein_g": {"type": "NUMBER"},
                                    "fat_g": {"type": "NUMBER"},
                                    "carb_g": {"type": "NUMBER"}
                                },
                                "required": ["name", "energy_kcal", "protein_g"]
                            }
                        }
                    }
                }
            )
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"Gemini 분석 에러: {e}")
        return {"error": str(e)}

def save_ai_cache(data):
    """AI가 분석한 영양 성분 결과를 DB 캐시 테이블에 저장합니다."""
    try:
        conn = get_conn() # 기존에 작성하신 DB 연결 함수
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_cache (food_name, calories, protein, fat, carbs)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (food_name) DO UPDATE SET
                calories = EXCLUDED.calories,
                protein = EXCLUDED.protein,
                fat = EXCLUDED.fat,
                carbs = EXCLUDED.carbs
        """, (
            data['food_name'], data['calories'], 
            data['protein'], data['fat'], data['carbs']
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Cache Save Error: {e}")

def analyze_nutrition_label(image_path):
    """영양성분표 이미지 분석 → 프로틴 제품 정보 추출"""
    client = get_gemini_client()
    if not client:
        return {"error": "서버에 API 키가 설정되지 않았습니다."}
    try:
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((512, 512))
            img.save(image_path, "JPEG", quality=85)
        with open(image_path, "rb") as f:
            image_data = f.read()
        prompt = """이 이미지는 프로틴 보충제의 영양성분표야.
분석해서 JSON으로만 답해. 백틱(```) 없이 오직 { } 데이터만 출력해.
형식:
{"name": "제품명", "protein_per_scoop": 25.0, "scoop_weight_g": 33.0, "energy_kcal": 130.0}
- name: 제품명 (없으면 "프로틴 보충제")
- protein_per_scoop: 1스쿱당 단백질(g)
- scoop_weight_g: 1스쿱 중량(g), 없으면 0
- energy_kcal: 1스쿱당 칼로리(kcal), 없으면 0
영양성분표가 아니면: {"error": "영양성분표를 찾을 수 없어요."}"""
        response = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=image_data, mime_type="image/jpeg")
            ]
        )
        t = response.text.strip()
        start = t.find('{')
        end = t.rfind('}') + 1
        if start == -1:
            return {"error": "AI 응답 파싱 실패"}
        return json.loads(t[start:end])
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────
# 인증 라우트
# ─────────────────────────────────────────
@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    nickname = data.get("nickname", "").strip()
    display_name = data.get("display_name", "").strip()
    weight = data.get("weight", 0)
    multiplier = data.get("multiplier", 1.5)
    if not username or not password:
        return jsonify({"status": "error", "message": "아이디와 비밀번호를 입력해주세요."}), 400
    if not nickname:
        nickname = username
    if not display_name:
        display_name = nickname
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO users (username, password, nickname, display_name, weight, multiplier) VALUES (%s, %s, %s, %s, %s, %s)",
            (username, password, nickname, display_name, weight, multiplier)
        )
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({"status": "error", "message": "이미 존재하는 아이디입니다."}), 400
    cur.close()
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, username, nickname, display_name, weight, multiplier, is_admin FROM users WHERE username=%s AND password=%s",
        (username, password)
    )
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user:
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["is_admin"] = bool(user["is_admin"])

        # ── 위젯 토큰 발급 (없으면 생성, 있으면 재사용) ──
        widget_token = None
        try:
            conn2 = get_conn()
            cur2 = conn2.cursor()
            cur2.execute("SELECT token FROM widget_tokens WHERE user_id=%s", (user["id"],))
            existing_token = cur2.fetchone()
            if existing_token:
                widget_token = existing_token["token"]
            else:
                widget_token = secrets.token_hex(32)
                cur2.execute(
                    "INSERT INTO widget_tokens (user_id, token, created_at) VALUES (%s, %s, %s)",
                    (user["id"], widget_token, datetime.now().isoformat())
                )
                conn2.commit()
            cur2.close()
            conn2.close()
        except Exception as e:
            print(f"위젯 토큰 발급 실패: {e}")

        return jsonify({
            "status": "success",
            "username": user["username"],
            "nickname": user["nickname"] or user["username"],
            "display_name": user["display_name"] or user["nickname"] or user["username"],
            "weight": user["weight"],
            "multiplier": user["multiplier"],
            "is_admin": bool(user["is_admin"]),
            "widget_token": widget_token  # ← Android 위젯에 전달
        })
    return jsonify({"status": "error", "message": "아이디 또는 비밀번호가 틀렸습니다."}), 401

@app.route("/api/logout")
def api_logout():
    session.clear()
    return jsonify({"status": "success"})

@app.route("/api/check-login")
def api_check_login():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"logged_in": False}), 200
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username, nickname, display_name, weight, multiplier, is_admin FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    if not user:
        return jsonify({"logged_in": False}), 200
    return jsonify({
        "logged_in": True,
        "username": user["username"],
        "nickname": user["nickname"] or user["username"],
        "display_name": user["display_name"] or user["nickname"] or user["username"],
        "weight": user["weight"],
        "multiplier": user["multiplier"],
        "is_admin": bool(user["is_admin"])
    }), 200

@app.route("/api/account", methods=["DELETE"])
def api_delete_account():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT image_path FROM meals WHERE user_id=%s", (user_id,))
    for m in cur.fetchall():
        if m["image_path"]:
            path = os.path.join(UPLOAD_FOLDER, m["image_path"])
            if os.path.exists(path):
                os.remove(path)
    cur.execute("SELECT image_path FROM protein_products WHERE user_id=%s", (user_id,))
    for p in cur.fetchall():
        if p["image_path"]:
            path = os.path.join(UPLOAD_FOLDER, p["image_path"])
            if os.path.exists(path):
                os.remove(path)
    cur.execute("DELETE FROM meals WHERE user_id=%s", (user_id,))
    cur.execute("DELETE FROM protein_products WHERE user_id=%s", (user_id,))
    cur.execute("DELETE FROM devices WHERE user_id=%s", (user_id,))
    cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
    conn.commit()
    cur.close()
    conn.close()
    session.clear()
    return jsonify({"status": "success"})

@app.route("/api/user/profile", methods=["POST"])
def api_update_profile():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    data = request.json
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE users SET nickname=%s, display_name=%s, weight=%s, multiplier=%s WHERE id=%s
    """, (data.get("nickname"), data.get("display_name"), data.get("weight"), data.get("multiplier"), user_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────
# 메인 라우트
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    # SQLite DB 검색
    results = search_food_in_db(q)
    # custom_foods도 추가 검색
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT name, protein_g, energy_kcal, fat_g, carb_g, std_unit, category,
                   NULL as food_cd, NULL as synm
            FROM custom_foods WHERE name ILIKE %s LIMIT 5
        """, (f"%{q}%",))
        customs = cur.fetchall()
        cur.close()
        conn.close()
        for c in customs:
            results.insert(0, dict(c))
    except Exception as e:
        print(f"custom_foods 검색 실패: {e}")
    return jsonify(results)

@app.route("/api/search-ai", methods=["POST"])
def api_search_ai():
    if not session.get("user_id"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    food_name = request.json.get("food_name", "").strip()
    if not food_name:
        return jsonify({"error": "음식명을 입력해주세요."}), 400

    conn = get_conn()
    cur = conn.cursor()

    # ① custom_foods 먼저 조회
    cur.execute("SELECT * FROM custom_foods WHERE name ILIKE %s LIMIT 1", (f"%{food_name}%",))
    custom = cur.fetchone()
    if custom:
        cur.execute("UPDATE ai_food_cache SET search_count = search_count + 1 WHERE food_name = %s", (food_name,))
        conn.commit()
        cur.close()
        conn.close()
        result = dict(custom)
        result["ai_generated"] = True
        result["from_cache"] = True
        return jsonify(result)

    # ② 캐시 조회
    cur.execute("""
        SELECT name, protein_g, energy_kcal, fat_g, carb_g, std_unit
        FROM ai_food_cache WHERE food_name = %s
    """, (food_name,))
    cached = cur.fetchone()
    if cached:
        cur.execute("UPDATE ai_food_cache SET search_count = search_count + 1 WHERE food_name = %s", (food_name,))
        conn.commit()
        cur.close()
        conn.close()
        result = dict(cached)
        result["ai_generated"] = True
        result["from_cache"] = True
        return jsonify(result)

    cur.close()
    conn.close()

    # ③ AI 호출
    client = get_gemini_client()
    if not client:
        return jsonify({"error": "서버에 API 키가 설정되지 않았습니다."}), 500
    try:
        prompt = f"""'{food_name}'의 영양 정보를 JSON으로만 답해. 이때 반드시 모든 음식명(name)은 한국어로 작성해.
인사말이나 백틱(```) 없이 오직 {{ }} 데이터만 출력해.
형식:
{{"name": "음식명", "protein_g": 20.0, "energy_kcal": 250, "fat_g": 5.0, "carb_g": 30.0, "std_unit": "1인분(200g)"}}
- 반드시 모든 음식명(name)은 한국어로 작성
- 모든 수치는 1인분(일반적인 1회 제공량) 기준
- 확실하지 않은 값은 0으로"""
        response = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents=[types.Part.from_text(text=prompt)]
        )
        t = response.text.strip()
        start = t.find('{')
        end = t.rfind('}') + 1
        if start == -1:
            return jsonify({"error": "AI 응답 파싱 실패"}), 500
        result = json.loads(t[start:end])
        result["ai_generated"] = True

        # ④ 캐시에 저장
        try:
            conn2 = get_conn()
            cur2 = conn2.cursor()
            cur2.execute("""
                INSERT INTO ai_food_cache
                    (food_name, name, protein_g, energy_kcal, fat_g, carb_g, std_unit, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (food_name) DO NOTHING
            """, (
                food_name,
                result.get("name", food_name),
                result.get("protein_g", 0),
                result.get("energy_kcal", 0),
                result.get("fat_g", 0),
                result.get("carb_g", 0),
                result.get("std_unit", ""),
                datetime.now().isoformat()
            ))
            conn2.commit()
            cur2.close()
            conn2.close()
        except Exception as cache_err:
            print(f"캐시 저장 실패: {cache_err}")

        return jsonify(result)
    except Exception as e:
        err_msg = str(e)
        if "429" in err_msg:
            return jsonify({"error": "AI 요청 한도 초과. 잠시 후 다시 시도해주세요."}), 429
        return jsonify({"error": err_msg}), 500

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    # 0. 세션 및 이미지 체크
    if not session.get("user_id"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    
    if "image" not in request.files:
        return jsonify({"error": "이미지가 없습니다."}), 400

    path = None # 에러 발생 시 파일 삭제를 위한 경로 변수 초기화
    
    try:
        user_id = session.get("user_id")
        file = request.files["image"]
        fname = f"{uuid.uuid4().hex}.jpg"
        path = os.path.join(UPLOAD_FOLDER, fname)
        file.save(path)

        # 1. AI 분석 수행
        result = analyze_image_with_gemini(path)
        
        if not result or "error" in result:
            if path and os.path.exists(path): os.remove(path)
            return jsonify(result if result else {"error": "AI 분석 결과가 없습니다."}), 500

        # 2. 결과 가공 및 캐시 저장 (텍스트 검색 캐시 테이블 공유)
        processed_foods = []
        for food in result.get("foods", []):
            # AI가 뽑아준 이름을 기준으로 DB 및 캐시 확인
            food_name = food.get("name", "").strip()
            
            # DB(커스텀+캐시)에서 검색
            db_match = find_food_in_db(food_name)
            
            if db_match:
                # DB 데이터 우선 적용
                food["protein_g"] = db_match.get("protein_g", 0)
                food["calories"] = db_match.get("energy_kcal", 0)
                food["carbs"] = db_match.get("carb_g", 0)
                food["fat"] = db_match.get("fat_g", 0)
                food["is_db_match"] = True
            else:
                # DB에 없는 새로운 음식이라면 ai_food_cache에 저장 (텍스트 검색 캐시와 동일한 테이블)
                try:
                    conn_c = get_conn()
                    cur_c = conn_c.cursor()
                    cur_c.execute("""
                        INSERT INTO ai_food_cache 
                            (food_name, name, protein_g, energy_kcal, fat_g, carb_g, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (food_name) DO NOTHING
                    """, (
                        food_name,  # 검색 키워드로 사용될 이름
                        food_name,  # 표시용 이름
                        food.get("protein_g", 0),
                        food.get("calories", 0),
                        food.get("fat", 0),
                        food.get("carbs", 0),
                        datetime.now().isoformat()
                    ))
                    conn_c.commit()
                    cur_c.close()
                    conn_c.close()
                except Exception as cache_err:
                    print(f"AI 결과 캐시 저장 중 오류: {cache_err}")
                
                food["is_db_match"] = False
            
            processed_foods.append(food)
        
        result["foods"] = processed_foods

        # 3. Cloudinary 업로드 (이미지 URL은 분석 결과와 별개로 처리)
        cloud_url = upload_to_cloudinary(path, folder="meals")
        
        if cloud_url:
            result["image_path"] = cloud_url
            if path and os.path.exists(path): os.remove(path)
        else:
            if path and os.path.exists(path): os.remove(path)
            return jsonify({"error": "이미지 서버 업로드 실패. Cloudinary 설정을 확인하세요."}), 500

        # 최종 성공 응답
        return jsonify(result)

    except Exception as e:
        if path and os.path.exists(path): os.remove(path)
        print(f"CRITICAL SERVER ERROR: {str(e)}")
        return jsonify({
            "error": "서버 내부 처리 오류가 발생했습니다.",
            "details": str(e)
        }), 500

@app.route("/api/meals", methods=["GET", "POST", "DELETE"])
def api_meals():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    conn = get_conn()
    cur = conn.cursor()
    if request.method == "POST":
        d = request.json
        cur.execute(
            "INSERT INTO meals(user_id,date,food_cd,food_name,emoji,amount,weight_g,protein_g,energy_kcal,fat_g,carb_g,image_path,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (user_id, d.get('date'), d.get('food_cd'), d['food_name'],
             d.get('emoji','🍽️'), d.get('amount'), d.get('weight_g'),
             d['protein_g'], d.get('energy_kcal'), d.get('fat_g'), d.get('carb_g'),
             d.get('image_path'), datetime.now().isoformat())
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    if request.method == "DELETE":
        meal_id = request.args.get("id")
        cur.execute("DELETE FROM meals WHERE id=%s AND user_id=%s", (meal_id, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})
    cur.execute(
        "SELECT * FROM meals WHERE date=%s AND user_id=%s ORDER BY created_at",
        (request.args.get("date"), user_id)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/stats/monthly")
def api_stats_monthly():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401
    month = request.args.get("month")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT date, SUM(protein_g) as total
        FROM meals WHERE date LIKE %s AND user_id=%s GROUP BY date
    """, (f"{month}%", user_id))
    rows = cur.fetchall()
    cur.execute("SELECT weight, multiplier FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()
    w = float(user["weight"] or 0) if user else 0
    m = float(user["multiplier"] or 0) if user else 0
    return jsonify({"goal": w * m, "data": [dict(r) for r in rows]})

@app.route("/api/day-detail")
def api_day_detail():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401
    date = request.args.get("date")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM meals WHERE date=%s AND user_id=%s ORDER BY created_at", (date, user_id))
    meals_list = [dict(m) for m in cur.fetchall()]
    cur.close()
    conn.close()
    total_protein = sum(float(m["protein_g"] or 0) for m in meals_list)
    seen = set()
    photos = []
    for m in meals_list:
        if m["image_path"] and m["image_path"] not in seen:
            seen.add(m["image_path"])
            photos.append(m["image_path"])
    return jsonify({"meals": meals_list, "photos": photos, "total_protein": total_protein})

@app.route("/api/album")
def api_album():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT image_path, date, MIN(created_at) as created_at
        FROM meals WHERE user_id=%s AND image_path IS NOT NULL AND image_path != ''
        GROUP BY image_path, date ORDER BY MIN(created_at) DESC
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────
# 프로틴 제품 라우트
# ─────────────────────────────────────────
@app.route("/api/protein-product/analyze", methods=["POST"])
def api_analyze_protein_label():
    """영양성분표 사진 → 제품 정보 추출"""
    if not session.get("user_id"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    if "image" not in request.files:
        return jsonify({"error": "이미지가 없습니다."}), 400
    file = request.files["image"]
    fname = f"product_{uuid.uuid4().hex}.jpg"
    path = os.path.join(UPLOAD_FOLDER, fname)
    file.save(path)
    result = analyze_nutrition_label(path)
    if "error" in result:
        os.remove(path)
        return jsonify(result), 400
    cloud_url = upload_to_cloudinary(path, folder="products")
    if cloud_url:
        result["image_path"] = cloud_url
        os.remove(path)
    else:
        result["image_path"] = f"/static/uploads/{fname}"
    return jsonify(result)

@app.route("/api/protein-product", methods=["GET", "POST", "DELETE"])
def api_protein_product():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    conn = get_conn()
    cur = conn.cursor()

    if request.method == "GET":
        cur.execute("""
            SELECT * FROM protein_products WHERE user_id=%s AND is_active=TRUE ORDER BY created_at DESC
        """, (user_id,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return jsonify([dict(r) for r in rows])

    if request.method == "POST":
        d = request.json
        cur.execute("""
            INSERT INTO protein_products(user_id, name, protein_per_scoop, scoop_weight_g, energy_kcal, image_path, created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s)
        """, (user_id, d["name"], d["protein_per_scoop"], d.get("scoop_weight_g", 0),
              d.get("energy_kcal", 0), d.get("image_path"), datetime.now().isoformat()))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})

    if request.method == "DELETE":
        product_id = request.args.get("id")
        cur.execute("UPDATE protein_products SET is_active=FALSE WHERE id=%s AND user_id=%s", (product_id, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "ok"})


# ─────────────────────────────────────────
# 하드웨어 기기 라우트
# ─────────────────────────────────────────
@app.route("/api/device/register", methods=["POST"])
def api_device_register():
    user_id = session.get("user_id")
    
    if not user_id:
        data = request.json
        user_id = data.get("user_id")
        token = data.get("token")
    else:
        data = request.json
        token = data.get("token")

    if not user_id or not token:
        return jsonify({"error": "로그인 필요"}), 401

    conn = get_conn()
    cur = conn.cursor()

    # 이미 등록됐는지 확인
    cur.execute("SELECT id FROM devices WHERE token = %s AND user_id = %s", (token, user_id))
    existing = cur.fetchone()

    if existing:
        cur.close()
        conn.close()
        return jsonify({"status": "already_registered"})

    # 처음 등록이면 is_current = True, 아니면 False
    cur.execute("SELECT id FROM devices WHERE token = %s", (token,))
    any_existing = cur.fetchone()
    is_current = not any_existing

    cur.execute("""
        INSERT INTO devices (user_id, token, name, created_at, status, is_current)
        VALUES (%s, %s, %s, NOW(), 'active', %s)
    """, (user_id, token, "프로틴 디스펜서", is_current))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})

    # active 상태에서 이미 이 유저가 등록했는지 확인
    cur.execute("SELECT id FROM devices WHERE token = %s AND user_id = %s AND status = 'active'", (token, user_id))
    existing = cur.fetchone()

    if existing:
        cur.close()
        conn.close()
        return jsonify({"status": "already_registered"})

    # 새로 삽입
    cur.execute("""
        INSERT INTO devices (user_id, token, name, created_at, status)
        VALUES (%s, %s, %s, NOW(), 'active')
    """, (user_id, token, "프로틴 디스펜서"))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})

@app.route("/api/check-login")
def check_login():
    user_id = session.get("user_id")
    if user_id:
        return jsonify({"logged_in": True, "user_id": user_id})
    return jsonify({"logged_in": False})

@app.route("/api/device/list")
def api_device_list():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, token, name, created_at, last_seen FROM devices WHERE user_id=%s", (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/device/user-change", methods=["POST"])
def api_device_user_change():
    data = request.json
    token = data.get("token")
    if not token:
        return jsonify({"error": "토큰 없음"}), 400

    conn = get_conn()
    cur = conn.cursor()

    # 등록된 모든 유저 목록
    cur.execute("""
        SELECT id, user_id FROM devices 
        WHERE token = %s 
        ORDER BY created_at ASC
    """, (token,))
    users = cur.fetchall()

    if not users:
        cur.close()
        conn.close()
        return jsonify({"error": "등록된 유저 없음"}), 404

    # 현재 유저 찾기
    cur.execute("SELECT id FROM devices WHERE token = %s AND is_current = TRUE", (token,))
    current = cur.fetchone()

    # 다음 유저로 순환
    ids = [u["id"] for u in users]
    if current and current["id"] in ids:
        next_idx = (ids.index(current["id"]) + 1) % len(ids)
    else:
        next_idx = 0

    next_id = ids[next_idx]

    # is_current 업데이트
    cur.execute("UPDATE devices SET is_current = FALSE WHERE token = %s", (token,))
    cur.execute("UPDATE devices SET is_current = TRUE WHERE id = %s", (next_id,))
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok"})

@app.route("/api/device/unlink", methods=["DELETE"])
def api_device_unlink():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    device_id = request.args.get("id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM devices WHERE id=%s AND user_id=%s", (device_id, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/device/status")
def api_device_status():
    token = request.headers.get("X-Device-Token") or request.args.get("token")
    if not token:
        return jsonify({"error": "토큰이 없습니다."}), 401

    conn = get_conn()
    cur = conn.cursor()

    # is_current 유저 찾기
    cur.execute("""
        SELECT user_id FROM devices 
        WHERE token=%s AND is_current=TRUE
        LIMIT 1
    """, (token,))
    device = cur.fetchone()

    if not device:
        # is_current 없으면 첫번째 유저
        cur.execute("SELECT user_id FROM devices WHERE token=%s LIMIT 1", (token,))
        device = cur.fetchone()

    if not device:
        cur.close()
        conn.close()
        return jsonify({"error": "등록되지 않은 기기입니다."}), 403

    user_id = device["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")

    # last_seen 업데이트
    cur.execute("UPDATE devices SET last_seen=%s WHERE token=%s AND is_current=TRUE", (datetime.now().isoformat(), token))

    # 오늘 섭취량
    cur.execute("""
        SELECT COALESCE(SUM(protein_g), 0) as total
        FROM meals WHERE user_id=%s AND date=%s
    """, (user_id, today))
    intake = float(cur.fetchone()["total"])

    # 목표량
    cur.execute("SELECT weight, multiplier FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    goal = float(user["weight"] or 0) * float(user["multiplier"] or 0) if user else 0

    # 활성 프로틴 제품
    cur.execute("""
        SELECT name, protein_per_scoop, scoop_weight_g
        FROM protein_products WHERE user_id=%s AND is_active=TRUE
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    product = cur.fetchone()

    # username 가져오기
    cur.execute("SELECT username FROM users WHERE id=%s", (user_id,))
    username_row = cur.fetchone()
    username = username_row["username"] if username_row else "unknown"

    conn.commit()
    cur.close()
    conn.close()

    shortage = max(0, goal - intake)
    scoops_needed = 0
    grams_needed = 0

    if product and shortage > 0:
        protein_per_scoop = float(product["protein_per_scoop"])
        scoop_weight = float(product["scoop_weight_g"] or 0)
        if protein_per_scoop > 0:
            scoops_needed = round(shortage / protein_per_scoop, 1)
            grams_needed = round(scoops_needed * scoop_weight, 1) if scoop_weight > 0 else 0

    return jsonify({
        "today": today,
        "username": username,
        "goal_g": goal,
        "intake_g": intake,
        "shortage_g": shortage,
        "product": dict(product) if product else None,
        "scoops_needed": scoops_needed,
        "grams_needed": grams_needed
    })


# ─────────────────────────────────────────
# 관리자 라우트
# ─────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin1234")

@app.route("/admin")
def admin_page():
    """관리자 페이지 — 캐시 목록 및 custom_foods 관리"""
    pw = request.args.get("pw", "")
    if pw != ADMIN_PASSWORD:
        return """
        <html><body style="font-family:sans-serif;padding:40px;background:#1e1e2e;color:#cdd6f4;">
        <h2>🔐 관리자 로그인</h2>
        <form method="get">
            <input name="pw" type="password" placeholder="비밀번호" style="padding:8px;border-radius:6px;border:1px solid #45475a;background:#313244;color:#cdd6f4;">
            <button type="submit" style="padding:8px 16px;background:#cba6f7;color:#1e1e2e;border:none;border-radius:6px;cursor:pointer;font-weight:700;">확인</button>
        </form>
        </body></html>
        """, 401

    conn = get_conn()
    cur = conn.cursor()

    # 캐시 목록 (검색 횟수 순)
    cur.execute("""
        SELECT id, food_name, name, protein_g, energy_kcal, fat_g, carb_g, std_unit, search_count, created_at
        FROM ai_food_cache ORDER BY search_count DESC
    """)
    caches = cur.fetchall()

    # custom_foods 목록
    cur.execute("SELECT * FROM custom_foods ORDER BY created_at DESC")
    customs = cur.fetchall()

    cur.close()
    conn.close()

    cache_rows = ""
    for c in caches:
        cache_rows += f"""
        <tr>
            <td>{c['food_name']}</td>
            <td>{c['protein_g']}g</td>
            <td>{c['energy_kcal']}kcal</td>
            <td>{c['fat_g']}g</td>
            <td>{c['carb_g']}g</td>
            <td>{c['std_unit']}</td>
            <td><strong>{c['search_count']}</strong></td>
            <td>
                <form method="post" action="/admin/promote?pw={pw}" style="display:inline">
                    <input type="hidden" name="cache_id" value="{c['id']}">
                    <button type="submit" style="background:#a6e3a1;color:#1e1e2e;border:none;padding:4px 10px;border-radius:5px;cursor:pointer;font-weight:700;">DB 추가</button>
                </form>
                <form method="post" action="/admin/cache/delete?pw={pw}" style="display:inline">
                    <input type="hidden" name="cache_id" value="{c['id']}">
                    <button type="submit" style="background:#f38ba8;color:#1e1e2e;border:none;padding:4px 10px;border-radius:5px;cursor:pointer;font-weight:700;">삭제</button>
                </form>
            </td>
        </tr>"""

    custom_rows = ""
    for c in customs:
        custom_rows += f"""
        <tr>
            <td>{c['name']}</td>
            <td>{c['protein_g']}g</td>
            <td>{c['energy_kcal']}kcal</td>
            <td>{c['fat_g']}g</td>
            <td>{c['carb_g']}g</td>
            <td>{c['std_unit']}</td>
            <td>
                <form method="post" action="/admin/custom/delete?pw={pw}" style="display:inline">
                    <input type="hidden" name="custom_id" value="{c['id']}">
                    <button type="submit" style="background:#f38ba8;color:#1e1e2e;border:none;padding:4px 10px;border-radius:5px;cursor:pointer;font-weight:700;">삭제</button>
                </form>
            </td>
        </tr>"""

    return f"""
    <html><head><meta charset="utf-8">
    <style>
        body {{font-family:sans-serif;padding:30px;background:#1e1e2e;color:#cdd6f4;}}
        h2 {{color:#cba6f7;margin-bottom:16px;}}
        h3 {{color:#89dceb;margin:24px 0 10px;}}
        table {{width:100%;border-collapse:collapse;margin-bottom:30px;}}
        th {{background:#313244;padding:8px 12px;text-align:left;color:#a6adc8;font-size:0.82rem;}}
        td {{padding:7px 12px;border-bottom:1px solid #313244;font-size:0.85rem;}}
        tr:hover td {{background:#313244;}}
    </style>
    </head><body>
    <h2>🛠️ 관리자 페이지</h2>

    <h3>📦 AI 검색 캐시 목록 ({len(caches)}개)</h3>
    <table>
        <tr><th>검색어</th><th>단백질</th><th>칼로리</th><th>지방</th><th>탄수화물</th><th>단위</th><th>검색횟수</th><th>액션</th></tr>
        {cache_rows if cache_rows else '<tr><td colspan="8" style="color:#6c7086">캐시 없음</td></tr>'}
    </table>

    <h3>✅ 커스텀 음식 DB ({len(customs)}개)</h3>
    <table>
        <tr><th>음식명</th><th>단백질</th><th>칼로리</th><th>지방</th><th>탄수화물</th><th>단위</th><th>액션</th></tr>
        {custom_rows if custom_rows else '<tr><td colspan="7" style="color:#6c7086">등록된 항목 없음</td></tr>'}
    </table>
    </body></html>
    """

@app.route("/admin/promote", methods=["POST"])
def admin_promote():
    """캐시 → custom_foods 승격"""
    pw = request.args.get("pw", "")
    if pw != ADMIN_PASSWORD:
        return jsonify({"error": "권한 없음"}), 403
    cache_id = request.form.get("cache_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ai_food_cache WHERE id=%s", (cache_id,))
    c = cur.fetchone()
    if c:
        cur.execute("""
            INSERT INTO custom_foods (name, protein_g, energy_kcal, fat_g, carb_g, std_unit, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET
                protein_g=EXCLUDED.protein_g,
                energy_kcal=EXCLUDED.energy_kcal,
                fat_g=EXCLUDED.fat_g,
                carb_g=EXCLUDED.carb_g,
                std_unit=EXCLUDED.std_unit
        """, (c['name'], c['protein_g'], c['energy_kcal'], c['fat_g'], c['carb_g'], c['std_unit'], datetime.now().isoformat()))
        conn.commit()
    cur.close()
    conn.close()
    return f'<script>location.href="/admin?pw={pw}"</script>'

@app.route("/admin/cache/delete", methods=["POST"])
def admin_cache_delete():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PASSWORD:
        return jsonify({"error": "권한 없음"}), 403
    cache_id = request.form.get("cache_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ai_food_cache WHERE id=%s", (cache_id,))
    conn.commit()
    cur.close()
    conn.close()
    return f'<script>location.href="/admin?pw={pw}"</script>'

@app.route("/admin/custom/delete", methods=["POST"])
def admin_custom_delete():
    pw = request.args.get("pw", "")
    if pw != ADMIN_PASSWORD:
        return jsonify({"error": "권한 없음"}), 403
    custom_id = request.form.get("custom_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM custom_foods WHERE id=%s", (custom_id,))
    conn.commit()
    cur.close()
    conn.close()
    return f'<script>location.href="/admin?pw={pw}"</script>'

# ─────────────────────────────────────────
# 관리자 API
# ─────────────────────────────────────────
def require_admin():
    if not session.get("user_id"):
        return False
    return session.get("is_admin", False)

@app.route("/api/admin/cache")
def api_admin_cache():
    if not require_admin():
        return jsonify({"error": "관리자 권한이 필요합니다."}), 403
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, food_name, name, protein_g, energy_kcal, fat_g, carb_g, std_unit, search_count, created_at
        FROM ai_food_cache ORDER BY search_count DESC
    """)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/custom")
def api_admin_custom():
    if not require_admin():
        return jsonify({"error": "관리자 권한이 필요합니다."}), 403
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM custom_foods ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/promote", methods=["POST"])
def api_admin_promote():
    if not require_admin():
        return jsonify({"error": "관리자 권한이 필요합니다."}), 403
    cache_id = request.json.get("cache_id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM ai_food_cache WHERE id=%s", (cache_id,))
    c = cur.fetchone()
    if not c:
        cur.close()
        conn.close()
        return jsonify({"error": "캐시 항목을 찾을 수 없습니다."}), 404
    cur.execute("""
        INSERT INTO custom_foods (name, protein_g, energy_kcal, fat_g, carb_g, std_unit, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (name) DO UPDATE SET
            protein_g=EXCLUDED.protein_g, energy_kcal=EXCLUDED.energy_kcal,
            fat_g=EXCLUDED.fat_g, carb_g=EXCLUDED.carb_g, std_unit=EXCLUDED.std_unit
    """, (c['name'], c['protein_g'], c['energy_kcal'], c['fat_g'], c['carb_g'],
          c['std_unit'], datetime.now().isoformat()))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok", "promoted": c['name']})

@app.route("/api/admin/cache/update", methods=["POST"])
def api_admin_cache_update():
    """캐시 항목 내용 수정"""
    if not require_admin():
        return jsonify({"error": "관리자 권한이 필요합니다."}), 403
    data = request.json
    cache_id = data.get("cache_id")
    name = data.get("name", "").strip()
    protein_g = data.get("protein_g", 0)
    energy_kcal = data.get("energy_kcal", 0)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE ai_food_cache SET name=%s, protein_g=%s, energy_kcal=%s WHERE id=%s
    """, (name, protein_g, energy_kcal, cache_id))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/admin/cache/delete", methods=["DELETE"])
def api_admin_cache_delete():
    if not require_admin():
        return jsonify({"error": "관리자 권한이 필요합니다."}), 403
    cache_id = request.args.get("id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM ai_food_cache WHERE id=%s", (cache_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/admin/custom/delete", methods=["DELETE"])
def api_admin_custom_delete():
    if not require_admin():
        return jsonify({"error": "관리자 권한이 필요합니다."}), 403
    custom_id = request.args.get("id")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM custom_foods WHERE id=%s", (custom_id,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/admin/set-admin", methods=["POST"])
def api_admin_set():
    """최초 1회 — DB에 관리자가 없을 때만 설정 가능"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM users WHERE is_admin=TRUE LIMIT 1")
    existing = cur.fetchone()
    if existing:
        cur.close()
        conn.close()
        return jsonify({"error": "이미 관리자가 존재합니다."}), 403
    username = request.json.get("username")
    cur.execute("UPDATE users SET is_admin=TRUE WHERE username=%s", (username,))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

# ─────────────────────────────────────────
# 위젯 전용 API (토큰 기반 인증)
# ─────────────────────────────────────────
def get_widget_user(token):
    """위젯 토큰으로 user_id 반환. 없으면 None"""
    if not token:
        return None
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM widget_tokens WHERE token=%s", (token,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["user_id"] if row else None

@app.route("/api/widget/token", methods=["POST"])
def api_widget_get_token():
    """로그인된 세션에서 위젯 토큰 발급 (앱 로그인 시 호출)"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    conn = get_conn()
    cur = conn.cursor()
    # 기존 토큰 있으면 반환
    cur.execute("SELECT token FROM widget_tokens WHERE user_id=%s", (user_id,))
    existing = cur.fetchone()
    if existing:
        cur.close()
        conn.close()
        return jsonify({"token": existing["token"]})
    # 새 토큰 생성
    token = secrets.token_hex(32)
    cur.execute(
        "INSERT INTO widget_tokens (user_id, token, created_at) VALUES (%s, %s, %s)",
        (user_id, token, datetime.now().isoformat())
    )
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"token": token})

@app.route("/api/widget/status")
def api_widget_status():
    """위젯 메인 데이터 — 오늘 섭취량, 목표, 최근 식사 목록"""
    token = request.headers.get("X-Widget-Token") or request.args.get("token")
    user_id = get_widget_user(token)
    if not user_id:
        return jsonify({"error": "인증 실패"}), 401

    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT nickname, weight, multiplier FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    goal = float(user["weight"] or 0) * float(user["multiplier"] or 0) if user else 0

    cur.execute("""
        SELECT food_name, protein_g, amount, created_at
        FROM meals WHERE user_id=%s AND date=%s
        ORDER BY created_at DESC LIMIT 10
    """, (user_id, today))
    meals = [dict(m) for m in cur.fetchall()]
    total = sum(float(m["protein_g"]) for m in meals)

    cur.close()
    conn.close()

    return jsonify({
        "today": today,
        "nickname": user["nickname"] if user else "",
        "goal_g": goal,
        "intake_g": total,
        "shortage_g": max(0, goal - total),
        "percent": min(100, round(total / goal * 100)) if goal > 0 else 0,
        "meals": meals
    })

@app.route("/api/widget/quick-add", methods=["POST"])
def api_widget_quick_add():
    """위젯에서 사진 업로드 → AI 분석 → 바로 기록"""
    token = request.headers.get("X-Widget-Token") or request.form.get("token")
    user_id = get_widget_user(token)
    if not user_id:
        return jsonify({"error": "인증 실패"}), 401

    if "image" not in request.files:
        return jsonify({"error": "이미지가 없습니다."}), 400

    file = request.files["image"]
    fname = f"{uuid.uuid4().hex}.jpg"
    path = os.path.join(UPLOAD_FOLDER, fname)
    file.save(path)

    result = analyze_image_with_gemini(path)
    if "error" in result:
        os.remove(path)
        return jsonify(result), 400

    # DB 매칭
    for food in result.get("foods", []):
        db_match = find_food_in_db(food.get("name", ""))
        if db_match:
            food["db_match"] = db_match
            food["protein_g"] = db_match["protein_g"]

    # Cloudinary 업로드
    cloud_url = upload_to_cloudinary(path, folder="meals")
    image_path = cloud_url if cloud_url else fname
    if cloud_url:
        os.remove(path)

    # 모든 음식 자동 기록
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_conn()
    cur = conn.cursor()
    saved = []
    for food in result.get("foods", []):
        db = food.get("db_match")
        cur.execute("""
            INSERT INTO meals(user_id,date,food_name,protein_g,energy_kcal,fat_g,carb_g,amount,image_path,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            user_id, today, food["name"],
            food.get("protein_g", 0),
            db["energy_kcal"] if db else None,
            db["fat_g"] if db else None,
            db["carb_g"] if db else None,
            food.get("estimated_amount", "1인분"),
            image_path, datetime.now().isoformat()
        ))
        saved.append({"name": food["name"], "protein_g": food.get("protein_g", 0)})
    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"status": "ok", "saved": saved, "image_path": image_path})

@app.route("/api/device/wifi-setup", methods=["POST"])
def api_device_wifi_setup():
    """앱 → ESP32로 WiFi 정보 전달 중계"""
    data = request.json
    esp32_ip = data.get("esp32_ip")   # ESP32 AP 모드 IP
    ssid = data.get("ssid")
    password = data.get("password")
    token = data.get("token")

    if not all([esp32_ip, ssid, token]):
        return jsonify({"error": "필수 항목 누락"}), 400

    import requests as req
    try:
        res = req.post(
            f"http://{esp32_ip}/setup",
            data={"ssid": ssid, "pass": password, "token": token},
            timeout=5
        )
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
# ─────────────────────────────────────────
# 단백질 분석 API (PDCAAS 기반)
# ─────────────────────────────────────────

# PDCAAS 점수 DB (식품명 키워드 매핑)
PDCAAS_DB = {
    # 완전단백질 (동물성)
    "닭": 0.92, "계란": 1.00, "달걀": 1.00, "우유": 1.00, "요거트": 1.00, "치즈": 0.95,
    "소고기": 0.92, "돼지고기": 0.91, "참치": 0.94, "연어": 0.94, "생선": 0.90, "새우": 0.88,
    "고등어": 0.91, "명태": 0.90, "오징어": 0.86, "두부": 0.91, "두유": 0.91,
    "프로틴": 1.00, "protein": 1.00, "유청": 1.00, "카제인": 1.00, "whey": 1.00,
    # 불완전단백질 (식물성)
    "쌀": 0.59, "밥": 0.59, "현미": 0.62, "보리": 0.57, "귀리": 0.57,
    "빵": 0.40, "국수": 0.25, "면": 0.25, "파스타": 0.40, "라면": 0.25, "우동": 0.30,
    "밀": 0.40, "밀가루": 0.40, "토스트": 0.40,
    "콩": 0.91, "병아리콩": 0.71, "렌틸콩": 0.52, "검은콩": 0.78,
    "아몬드": 0.48, "땅콩": 0.52, "호두": 0.46,
    "브로콜리": 0.83, "시금치": 0.75, "감자": 0.68,
    "떡": 0.59, "죽": 0.59,
}

def get_pdcaas_score(food_name):
    """음식명으로 PDCAAS 점수 반환 (키워드 매칭)"""
    name_lower = food_name.lower()
    best_score = None
    for keyword, score in PDCAAS_DB.items():
        if keyword in name_lower:
            if best_score is None or score > best_score:
                best_score = score
    return best_score if best_score is not None else 0.75  # 기본값

@app.route("/api/analysis/protein-trend")
def api_protein_trend():
    """최근 N일 단백질 섭취 추이 + 음식별 PDCAAS 유효 단백질 계산"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401

    days = int(request.args.get("days", 30))
    conn = get_conn()
    cur = conn.cursor()

    # 최근 N일 식사 데이터
    cur.execute("""
        SELECT date, food_name, protein_g
        FROM meals
        WHERE user_id=%s
          AND date >= (CURRENT_DATE - INTERVAL '%s days')::TEXT
        ORDER BY date ASC
    """, (user_id, days))
    meals = [dict(r) for r in cur.fetchall()]

    # 유저 정보
    cur.execute("SELECT weight, multiplier FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    cur.close()
    conn.close()

    goal = float(user["weight"] or 0) * float(user["multiplier"] or 0) if user else 0

    # 날짜별 원시/유효 단백질 집계
    from collections import defaultdict
    daily_raw = defaultdict(float)
    daily_effective = defaultdict(float)

    for meal in meals:
        date = meal["date"]
        raw_p = float(meal["protein_g"] or 0)
        score = get_pdcaas_score(meal["food_name"])
        daily_raw[date] += raw_p
        daily_effective[date] += raw_p * score

    # 정렬된 날짜 목록
    all_dates = sorted(set(daily_raw.keys()) | set(daily_effective.keys()))
    trend = []
    for d in all_dates:
        trend.append({
            "date": d,
            "raw": round(daily_raw[d], 1),
            "effective": round(daily_effective[d], 1)
        })

    return jsonify({"trend": trend, "goal": goal})

@app.route("/api/analysis/food-pdcaas")
def api_food_pdcaas():
    """최근 섭취 음식별 PDCAAS 점수 및 단백질 분포 반환"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401

    days = int(request.args.get("days", 30))
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT food_name, SUM(protein_g) as total_protein
        FROM meals
        WHERE user_id=%s
          AND date >= (CURRENT_DATE - INTERVAL '%s days')::TEXT
        GROUP BY food_name
        ORDER BY total_protein DESC
        LIMIT 15
    """, (user_id, days))
    foods = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    result = []
    for f in foods:
        score = get_pdcaas_score(f["food_name"])
        raw = float(f["total_protein"] or 0)
        result.append({
            "food_name": f["food_name"],
            "raw_protein": round(raw, 1),
            "pdcaas_score": score,
            "effective_protein": round(raw * score, 1),
            "lost_protein": round(raw * (1 - score), 1)
        })

    return jsonify(result)

@app.route("/api/analysis/goal-forecast")
def api_goal_forecast():
    """목표 달성 예상 기간 계산 (원시 vs 유효 단백질 기준)"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401

    # 파라미터
    target_weight = float(request.args.get("target_weight", 0))
    target_muscle_kg = float(request.args.get("target_muscle_kg", 0))
    current_weight = float(request.args.get("current_weight", 0))
    daily_raw_avg = float(request.args.get("daily_raw_avg", 0))
    daily_eff_avg = float(request.args.get("daily_eff_avg", 0))

    # 근육 1kg 합성에 필요한 단백질: ~175g (연구 기반 보수적 추정)
    PROTEIN_PER_KG_MUSCLE = 175.0
    # 체중 증가 목표 (지방+근육 혼합 기준, 근육 비율 약 40% 가정)
    MUSCLE_RATIO = 0.4

    results = {}

    if target_muscle_kg > 0:
        needed = target_muscle_kg * PROTEIN_PER_KG_MUSCLE
        results["target_type"] = "muscle"
        results["target_kg"] = target_muscle_kg
        results["needed_protein_g"] = needed
        if daily_raw_avg > 0:
            results["days_raw"] = round(needed / daily_raw_avg)
        if daily_eff_avg > 0:
            results["days_effective"] = round(needed / daily_eff_avg)

    elif target_weight > 0 and current_weight > 0:
        diff = target_weight - current_weight
        muscle_gain = abs(diff) * MUSCLE_RATIO
        needed = muscle_gain * PROTEIN_PER_KG_MUSCLE
        results["target_type"] = "weight"
        results["target_kg"] = target_weight
        results["weight_diff_kg"] = round(diff, 1)
        results["needed_protein_g"] = round(needed, 0)
        if daily_raw_avg > 0:
            results["days_raw"] = round(needed / daily_raw_avg) if diff > 0 else 0
        if daily_eff_avg > 0:
            results["days_effective"] = round(needed / daily_eff_avg) if diff > 0 else 0

    return jsonify(results)


@app.route("/api/device/check", methods=["GET"])
def api_device_check():
    token = request.args.get("token")
    if not token:
        return jsonify({"error": "토큰 없음"}), 400

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM devices WHERE token = %s", (token,))
    existing = cur.fetchone()
    cur.close()
    conn.close()

    return jsonify({"wifi_configured": existing is not None})

# ─────────────────────────────────────────
# 기기 → 앱 WiFi 재설정 트리거
# ─────────────────────────────────────────
from collections import defaultdict
import time

# 토큰별 wifi_setup 요청 시각 저장 (메모리)
_wifi_trigger = {}  # { token: timestamp }

@app.route("/api/device/wifi-trigger", methods=["POST"])
def api_wifi_trigger():
    """ESP32가 버튼 입력을 감지하면 여기로 알림"""
    token = request.json.get("token")
    if not token:
        return jsonify({"error": "토큰 없음"}), 400
    _wifi_trigger[token] = time.time()
    return jsonify({"status": "ok"})

@app.route("/api/device/wifi-trigger/check")
def api_wifi_trigger_check():
    """앱이 주기적으로 폴링해서 WiFi 재설정 필요 여부 확인"""
    token = request.args.get("token")
    if not token:
        return jsonify({"triggered": False})
    ts = _wifi_trigger.get(token)
    if ts and time.time() - ts < 60:  # 60초 이내 요청이면 triggered
        _wifi_trigger.pop(token, None)  # 1회성
        return jsonify({"triggered": True})
    return jsonify({"triggered": False})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)