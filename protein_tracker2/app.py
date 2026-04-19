import os, json, uuid, secrets
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session
from PIL import Image
from google import genai
from google.genai import types
import psycopg2
from psycopg2.extras import RealDictCursor
import sqlite3

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "protein-tracker-secret-2024")
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL")
SQLITE_DB_PATH = "food_nutrition.db"
# API 키는 환경변수에서만 읽음 — 사용자가 변경 불가
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")


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
        multiplier REAL DEFAULT 1.5
    )""")

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

    conn.commit()
    cur.close()
    conn.close()

init_db()


# ─────────────────────────────────────────
# 음식 DB 검색 (food_nutrition SQLite 유지)
# ─────────────────────────────────────────
def find_food_in_db(food_name):
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        conn.row_factory = sqlite3.Row
        q = f"%{food_name}%"
        row = conn.execute("""
            SELECT * FROM food_nutrition
            WHERE name LIKE ? OR synm LIKE ? OR synm2 LIKE ? OR srch_keyword LIKE ?
            LIMIT 1
        """, (q, q, q, q)).fetchone()
        if row:
            conn.close()
            return dict(row)
        for cut in [food_name[:len(food_name)//2+1], food_name[:3], food_name[:2]]:
            if len(cut) < 2:
                break
            p = f"%{cut}%"
            row = conn.execute("""
                SELECT * FROM food_nutrition
                WHERE name LIKE ? OR synm LIKE ? OR synm2 LIKE ? OR srch_keyword LIKE ?
                LIMIT 1
            """, (p, p, p, p)).fetchone()
            if row:
                conn.close()
                return dict(row)
        conn.close()
    except Exception:
        pass
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
        return {"error": "서버에 API 키가 설정되지 않았습니다. 관리자에게 문의하세요."}
    try:
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((1024, 1024))
            img.save(image_path, "JPEG", quality=85)
        with open(image_path, "rb") as f:
            image_data = f.read()
        prompt = """이미지 속 음식을 분석해서 한국어 JSON으로만 답해.
인사말이나 백틱(```) 없이 오직 { } 데이터만 출력해.
형식:
{"foods": [{"name": "음식명", "estimated_amount": "1인분", "weight_g": 200, "protein_g": 15.0}]}
- name: 한국 일반적인 음식명 (예: 닭가슴살, 흰쌀밥, 삶은달걀)
- estimated_amount: 눈대중 분량
- weight_g: 예상 중량(g)
- protein_g: 예상 단백질(g), 확실하지 않으면 0"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=image_data, mime_type="image/jpeg")
            ]
        )
        t = response.text.strip()
        start = t.find('{')
        end = t.rfind('}') + 1
        if start == -1:
            return {"error": "AI 응답 파싱 실패. 다시 시도해주세요."}
        return json.loads(t[start:end])
    except Exception as e:
        return {"error": str(e)}

def analyze_nutrition_label(image_path):
    """영양성분표 이미지 분석 → 프로틴 제품 정보 추출"""
    client = get_gemini_client()
    if not client:
        return {"error": "서버에 API 키가 설정되지 않았습니다."}
    try:
        with Image.open(image_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((1024, 1024))
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
            model="gemini-2.5-flash",
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
        "SELECT id, username, nickname, display_name, weight, multiplier FROM users WHERE username=%s AND password=%s",
        (username, password)
    )
    user = cur.fetchone()
    cur.close()
    conn.close()
    if user:
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return jsonify({
            "status": "success",
            "username": user["username"],
            "nickname": user["nickname"] or user["username"],
            "display_name": user["display_name"] or user["nickname"] or user["username"],
            "weight": user["weight"],
            "multiplier": user["multiplier"]
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
    cur.execute("SELECT username, nickname, display_name, weight, multiplier FROM users WHERE id=%s", (user_id,))
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
        "multiplier": user["multiplier"]
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
    return jsonify(search_food_in_db(q))

@app.route("/api/search-ai", methods=["POST"])
def api_search_ai():
    if not session.get("user_id"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    food_name = request.json.get("food_name", "").strip()
    if not food_name:
        return jsonify({"error": "음식명을 입력해주세요."}), 400
    client = get_gemini_client()
    if not client:
        return jsonify({"error": "서버에 API 키가 설정되지 않았습니다."}), 500
    try:
        prompt = f"""'{food_name}'의 영양 정보를 JSON으로만 답해.
인사말이나 백틱(```) 없이 오직 {{ }} 데이터만 출력해.
형식:
{{"name": "음식명", "protein_g": 20.0, "energy_kcal": 250, "fat_g": 5.0, "carb_g": 30.0, "std_unit": "1인분(200g)"}}
- 모든 수치는 1인분(일반적인 1회 제공량) 기준
- 확실하지 않은 값은 0으로"""
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Part.from_text(text=prompt)]
        )
        t = response.text.strip()
        start = t.find('{')
        end = t.rfind('}') + 1
        if start == -1:
            return jsonify({"error": "AI 응답 파싱 실패"}), 500
        result = json.loads(t[start:end])
        result["ai_generated"] = True
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    if not session.get("user_id"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    if "image" not in request.files:
        return jsonify({"error": "이미지가 없습니다."})
    file = request.files["image"]
    fname = f"{uuid.uuid4().hex}.jpg"
    path = os.path.join(UPLOAD_FOLDER, fname)
    file.save(path)
    result = analyze_image_with_gemini(path)
    if "error" in result:
        return jsonify(result)
    for food in result.get("foods", []):
        db_match = find_food_in_db(food.get("name", ""))
        if db_match:
            food["db_match"] = db_match
            food["protein_g"] = db_match["protein_g"]
    result["image_path"] = fname
    return jsonify(result)

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
    result["image_path"] = fname
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
    """QR코드에서 읽은 토큰으로 기기 등록"""
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    token = request.json.get("token", "").strip()
    if not token:
        return jsonify({"error": "토큰이 없습니다."}), 400
    conn = get_conn()
    cur = conn.cursor()
    # 토큰이 이미 다른 유저에게 등록됐는지 확인
    cur.execute("SELECT id, user_id FROM devices WHERE token=%s", (token,))
    existing = cur.fetchone()
    if existing:
        if existing["user_id"] != user_id:
            cur.close()
            conn.close()
            return jsonify({"error": "이미 다른 계정에 등록된 기기입니다."}), 400
        # 이미 내 기기면 OK
        cur.close()
        conn.close()
        return jsonify({"status": "already_registered"})
    cur.execute("""
        INSERT INTO devices(user_id, token, name, created_at)
        VALUES(%s,%s,%s,%s)
    """, (user_id, token, "내 디스펜서", datetime.now().isoformat()))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"status": "ok"})

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
    """
    하드웨어(ESP32)가 호출하는 엔드포인트.
    토큰으로 인증 → 오늘 부족한 단백질량 + 활성 제품 정보 반환
    """
    token = request.headers.get("X-Device-Token") or request.args.get("token")
    if not token:
        return jsonify({"error": "토큰이 없습니다."}), 401

    conn = get_conn()
    cur = conn.cursor()

    # 토큰으로 유저 찾기
    cur.execute("SELECT user_id FROM devices WHERE token=%s", (token,))
    device = cur.fetchone()
    if not device:
        cur.close()
        conn.close()
        return jsonify({"error": "등록되지 않은 기기입니다."}), 403

    user_id = device["user_id"]
    today = datetime.now().strftime("%Y-%m-%d")

    # last_seen 업데이트
    cur.execute("UPDATE devices SET last_seen=%s WHERE token=%s", (datetime.now().isoformat(), token))

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

    # 활성 프로틴 제품 (가장 최근 등록된 것)
    cur.execute("""
        SELECT name, protein_per_scoop, scoop_weight_g
        FROM protein_products WHERE user_id=%s AND is_active=TRUE
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    product = cur.fetchone()

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
        "goal_g": goal,
        "intake_g": intake,
        "shortage_g": shortage,
        "product": dict(product) if product else None,
        "scoops_needed": scoops_needed,
        "grams_needed": grams_needed
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
