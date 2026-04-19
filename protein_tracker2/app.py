import os, json, uuid, shutil
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
    cur.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
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
    conn.commit()
    cur.close()
    conn.close()

init_db()


# ─────────────────────────────────────────
# 설정 헬퍼
# ─────────────────────────────────────────
def get_settings():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def save_setting(key, value):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO settings (key, value) VALUES (%s, %s)
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
    """, (key, str(value)))
    conn.commit()
    cur.close()
    conn.close()


# ─────────────────────────────────────────
# 음식 DB 검색 (food_nutrition은 SQLite 유지)
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
# Gemini 이미지 분석
# ─────────────────────────────────────────
def analyze_image_with_gemini(image_path, api_key):
    if not api_key:
        return {"error": "사이드바에서 API 키를 먼저 저장해주세요."}
    try:
        client = genai.Client(api_key=api_key)
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
        session["nickname"] = user["nickname"] or user["username"]
        session["display_name"] = user["display_name"] or user["nickname"] or user["username"]
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
    cur.execute(
        "SELECT username, nickname, display_name, weight, multiplier FROM users WHERE id=%s", (user_id,)
    )
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
    meals = cur.fetchall()
    for m in meals:
        if m["image_path"]:
            path = os.path.join(UPLOAD_FOLDER, m["image_path"])
            if os.path.exists(path):
                os.remove(path)
    cur.execute("DELETE FROM meals WHERE user_id=%s", (user_id,))
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
        UPDATE users SET nickname=%s, display_name=%s, weight=%s, multiplier=%s
        WHERE id=%s
    """, (data.get("nickname"), data.get("display_name"), data.get("weight"), data.get("multiplier"), user_id))
    conn.commit()
    cur.close()
    conn.close()
    session["nickname"] = data.get("nickname")
    session["display_name"] = data.get("display_name")
    return jsonify({"status": "ok"})


# ─────────────────────────────────────────
# 메인 라우트
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        for k, v in request.json.items():
            save_setting(k, v)
        return jsonify({"status": "ok"})
    return jsonify(get_settings())

@app.route("/api/search")
def api_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    return jsonify(search_food_in_db(q))
@app.route("/api/search-ai", methods=["POST"])
def api_search_ai():
    """DB에 없는 음식을 AI로 단백질 추정"""
    if not session.get("user_id"):
        return jsonify({"error": "로그인이 필요합니다."}), 401
    food_name = request.json.get("food_name", "").strip()
    if not food_name:
        return jsonify({"error": "음식명을 입력해주세요."}), 400
    api_key = get_settings().get("api_key")
    if not api_key:
        return jsonify({"error": "API 키를 먼저 저장해주세요."}), 400
    try:
        client = genai.Client(api_key=api_key)
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
    api_key = get_settings().get("api_key")
    result = analyze_image_with_gemini(path, api_key)
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
        FROM meals WHERE date LIKE %s AND user_id=%s
        GROUP BY date
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
    cur.execute(
        "SELECT * FROM meals WHERE date=%s AND user_id=%s ORDER BY created_at",
        (date, user_id)
    )
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
        GROUP BY image_path, date
        ORDER BY MIN(created_at) DESC
    """, (user_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([dict(r) for r in rows])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)