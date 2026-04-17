"""
식이_영양평가_진단모델 DB → SQLite 변환
처음 한 번만 실행하면 됩니다.
"""
import sqlite3, os, sys

SEP = bytes.fromhex('5e43635f5f43635e')  # ^Cc__Cc^
REC = bytes.fromhex('5e52725f5f52725e')  # ^Rr__Rr^
DAT = os.path.join("DB", "altibase", "altibaseDump")
DB  = "food_nutrition.db"

def parse(filepath):
    with open(filepath, "rb") as f:
        raw = f.read()
    return [
        [c.decode("euc-kr", errors="replace").strip() for c in rec.strip().split(SEP)]
        for rec in raw.split(REC)
        if rec.strip()
    ]

def sf(v):
    try: return float(v) if v else None
    except: return None

def build():
    if not os.path.isdir(DAT):
        sys.exit(f"ERR: '{DAT}' 폴더 없음. protein_tracker 폴더 안에서 실행하세요.")

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    print("DB 변환 시작...")

    c.execute("DROP TABLE IF EXISTS food")
    c.execute("""CREATE TABLE food(
        food_cd TEXT PRIMARY KEY, food_nm TEXT NOT NULL,
        grp_cd TEXT, grp_nm TEXT, std_wgt REAL, std_vol REAL,
        std_unit TEXT, avg_itk REAL, srch_keyword TEXT, synm TEXT, synm2 TEXT)""")
    rows = parse(os.path.join(DAT, "DDAM_TB_FOOD.dat"))
    c.executemany("INSERT OR IGNORE INTO food VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
        (r[0],r[1],r[2] if len(r)>2 else None,r[3] if len(r)>3 else None,
         sf(r[4]) if len(r)>4 else None,sf(r[5]) if len(r)>5 else None,
         r[6] if len(r)>6 else None,sf(r[7]) if len(r)>7 else None,
         r[10] if len(r)>10 else None,r[11] if len(r)>11 else None,r[12] if len(r)>12 else None)
        for r in rows if len(r)>=2])
    print(f"  food: {conn.execute('SELECT COUNT(*) FROM food').fetchone()[0]}개")

    c.execute("DROP TABLE IF EXISTS ingredients")
    c.execute("""CREATE TABLE ingredients(
        igr_cd TEXT PRIMARY KEY, igr_nm TEXT, igr_grp TEXT, igr_grp_nm TEXT,
        cnvs_factor REAL, energy_kcal REAL, protein_g REAL, lipid_g REAL,
        carb_g REAL, calcium_mg REAL, iron_mg REAL, sodium_mg REAL)""")
    rows = parse(os.path.join(DAT, "DDAM_TB_INGREDIENTS.dat"))
    c.executemany("INSERT OR IGNORE INTO ingredients VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", [
        (r[0],r[1],r[9] if len(r)>9 else None,r[10] if len(r)>10 else None,
         sf(r[11]) if len(r)>11 else None,sf(r[12]) if len(r)>12 else None,
         sf(r[13]) if len(r)>13 else None,sf(r[14]) if len(r)>14 else None,
         sf(r[15]) if len(r)>15 else None,sf(r[21]) if len(r)>21 else None,
         sf(r[24]) if len(r)>24 else None,sf(r[23]) if len(r)>23 else None)
        for r in rows if len(r)>=2])
    print(f"  ingredients: {conn.execute('SELECT COUNT(*) FROM ingredients').fetchone()[0]}개")

    c.execute("DROP TABLE IF EXISTS recipes")
    c.execute("CREATE TABLE recipes(food_cd TEXT, igr_cd TEXT, igr_rc_wgt REAL, igr_rc_vol REAL)")
    rows = parse(os.path.join(DAT, "DDAM_TB_RECIPES.dat"))
    c.executemany("INSERT INTO recipes VALUES(?,?,?,?)", [
        (r[0],r[1],sf(r[2]) if len(r)>2 else None,sf(r[3]) if len(r)>3 else None)
        for r in rows if len(r)>=3])
    print(f"  recipes: {conn.execute('SELECT COUNT(*) FROM recipes').fetchone()[0]}개")

    c.execute("DROP VIEW IF EXISTS food_nutrition")
    c.execute("""CREATE VIEW food_nutrition AS
        SELECT f.food_cd, f.food_nm AS name, f.grp_nm AS category,
               f.synm, f.synm2, f.srch_keyword, f.std_wgt, f.std_unit,
               ROUND(SUM(r.igr_rc_wgt * i.protein_g),   2) AS protein_g,
               ROUND(SUM(r.igr_rc_wgt * i.energy_kcal), 1) AS energy_kcal,
               ROUND(SUM(r.igr_rc_wgt * i.lipid_g),     2) AS fat_g,
               ROUND(SUM(r.igr_rc_wgt * i.carb_g),      2) AS carb_g
        FROM food f
        JOIN recipes r     ON f.food_cd = r.food_cd
        JOIN ingredients i ON r.igr_cd  = i.igr_cd
        GROUP BY f.food_cd""")

    c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        INSERT OR IGNORE INTO settings VALUES('weight_kg','70');
        INSERT OR IGNORE INTO settings VALUES('goal_g','105');
        INSERT OR IGNORE INTO settings VALUES('scoop_protein_g','25');
        INSERT OR IGNORE INTO settings VALUES('api_key','');
        CREATE TABLE IF NOT EXISTS meals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, food_cd TEXT, food_name TEXT NOT NULL,
            emoji TEXT DEFAULT '🍽️', amount TEXT, weight_g REAL,
            protein_g REAL NOT NULL, energy_kcal REAL, fat_g REAL, carb_g REAL,
            image_path TEXT, note TEXT, created_at TEXT);
        CREATE INDEX IF NOT EXISTS idx_meals_date ON meals(date);
        CREATE INDEX IF NOT EXISTS idx_food_nm    ON food(food_nm);
        CREATE INDEX IF NOT EXISTS idx_igr_cd     ON ingredients(igr_cd);
        CREATE INDEX IF NOT EXISTS idx_rec_food   ON recipes(food_cd);
    """)
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM food_nutrition").fetchone()[0]
    print(f"  food_nutrition 뷰: {total}개 식품")

    # 테스트
    rows = conn.execute(
        "SELECT name, protein_g, std_unit FROM food_nutrition WHERE name LIKE '%닭%' LIMIT 5"
    ).fetchall()
    print("\n테스트 — 닭 검색:")
    for r in rows: print(f"  {r[0]}: {r[1]}g 단백질 / {r[2]}")
    conn.close()

    size = os.path.getsize(DB)//1024
    print(f"\n완료! food_nutrition.db ({size}KB)")

if __name__ == "__main__":
    build()
