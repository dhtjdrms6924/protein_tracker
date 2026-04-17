import os, json, uuid, sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, render_template, session
from PIL import Image
from google import genai
from google.genai import types

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "protein-tracker-secret-2024")
UPLOAD_FOLDER = os.path.join("static", "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
DB_PATH = "food_nutrition.db"


# ─────────────────────────────────────────
# DB 초기화 (앱 시작 시 테이블 보장)
# ─────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        food_cd TEXT, food_name TEXT NOT NULL,
        emoji TEXT DEFAULT '🍽️',
        amount TEXT, weight_g REAL,
        protein_g REAL NOT NULL,
        energy_kcal REAL, fat_g REAL, carb_g REAL,
        image_path TEXT, created_at TEXT
    )""")
    # 기존 meals 테이블에 user_id 없으면 추가
    try:
        conn.execute("ALTER TABLE meals ADD COLUMN user_id INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    conn.commit()
    conn.close()

init_db()

# ─────────────────────────────────────────
# 헬퍼
# ─────────────────────────────────────────
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}

def save_setting(key, value):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()

def find_food_in_db(conn, food_name):
    q = f"%{food_name}%"
    row = conn.execute("""
        SELECT * FROM food_nutrition
        WHERE name LIKE ? OR synm LIKE ? OR synm2 LIKE ? OR srch_keyword LIKE ?
        LIMIT 1
    """, (q, q, q, q)).fetchone()
    if row:
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
            return dict(row)
    return None

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
    if not username or not password:
        return jsonify({"status": "error", "message": "아이디와 비밀번호를 입력해주세요."}), 400
    conn = get_conn()
    try:
        conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"status": "error", "message": "이미 존재하는 아이디입니다."}), 400
    conn.close()
    return jsonify({"status": "success"})

@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    conn = get_conn()
    user = conn.execute(
        "SELECT id, username FROM users WHERE username=? AND password=?", (username, password)
    ).fetchone()
    conn.close()
    if user:
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return jsonify({"status": "success", "username": user["username"]})
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
    return jsonify({"logged_in": True, "username": session.get("username")}), 200

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
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM food_nutrition
        WHERE name LIKE ? OR synm LIKE ? OR synm2 LIKE ? OR srch_keyword LIKE ?
        LIMIT 15
    """, (f"%{q}%",)*4).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

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
    conn = get_conn()
    for food in result.get("foods", []):
        db_match = find_food_in_db(conn, food.get("name", ""))
        if db_match:
            food["db_match"] = db_match
            food["protein_g"] = db_match["protein_g"]
    conn.close()
    result["image_path"] = fname
    return jsonify(result)

@app.route("/api/meals", methods=["GET", "POST", "DELETE"])
def api_meals():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    conn = get_conn()
    if request.method == "POST":
        d = request.json
        conn.execute(
            "INSERT INTO meals(user_id,date,food_cd,food_name,emoji,amount,weight_g,protein_g,energy_kcal,fat_g,carb_g,image_path,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, d.get('date'), d.get('food_cd'), d['food_name'],
             d.get('emoji','🍽️'), d.get('amount'), d.get('weight_g'),
             d['protein_g'], d.get('energy_kcal'), d.get('fat_g'), d.get('carb_g'),
             d.get('image_path'), datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    if request.method == "DELETE":
        meal_id = request.args.get("id")
        conn.execute("DELETE FROM meals WHERE id=? AND user_id=?", (meal_id, user_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    rows = conn.execute(
        "SELECT * FROM meals WHERE date=? AND user_id=? ORDER BY created_at",
        (request.args.get("date"), user_id)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/stats/monthly")
def api_stats_monthly():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "로그인 필요"}), 401
    month = request.args.get("month")
    conn = get_conn()
    rows = conn.execute("""
        SELECT date, SUM(protein_g) as total
        FROM meals WHERE date LIKE ? AND user_id=?
        GROUP BY date
    """, (f"{month}%", user_id)).fetchall()
    conn.close()
    settings = get_settings()
    w = float(settings.get('weight', 0) or 0)
    m = float(settings.get('multiplier', 0) or 0)
    return jsonify({"goal": w * m, "data": [dict(r) for r in rows]})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, threaded=True)
