import csv
import getpass
import io
import os
import re
import secrets
import sqlite3
import sys
from datetime import date, datetime
from functools import wraps

from flask import Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db as dbm
from db import audit, get_db, now

BASE = os.path.dirname(os.path.abspath(__file__))


def load_secret():
    path = os.path.join(BASE, "secret.key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.config.update(
    DATABASE=os.environ.get("CHECKLIST_DB", os.path.join(BASE, "checklist.db")),
    SECRET_KEY=load_secret(),
    SESSION_COOKIE_SAMESITE="Lax",
)
app.teardown_appcontext(dbm.close_db)


def today():
    return app.config.get("TODAY") or date.today().isoformat()


MAX_FAIL = 5
ROLE_NAMES = {"member": "부서원", "leader": "팀장", "admin": "관리자"}
ROLE_RANK = {"member": 1, "leader": 2, "admin": 3}


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ? AND active = 1", (uid,)).fetchone()


@app.before_request
def csrf_protect():
    if request.method == "POST":
        token = session.get("csrf")
        if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
            abort(400)


def login_required(role="member"):
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if g.user is None:
                return redirect(url_for("login"))
            if g.user["must_change_pw"] and request.endpoint != "change_password":
                return redirect(url_for("change_password"))
            if ROLE_RANK[g.user["role"]] < ROLE_RANK[role]:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return deco


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE login_id = ?", (request.form.get("login_id", "").strip(),)
        ).fetchone()
        if user and user["active"] and user["fail_count"] >= MAX_FAIL:
            error = "계정이 잠겼습니다. 관리자에게 문의하세요."
        elif user and user["active"] and check_password_hash(user["pw_hash"], request.form.get("password", "")):
            db.execute("UPDATE users SET fail_count = 0 WHERE id = ?", (user["id"],))
            db.commit()
            session.clear()
            session["uid"] = user["id"]
            return redirect(url_for("index"))
        else:
            if user:
                db.execute("UPDATE users SET fail_count = fail_count + 1 WHERE id = ?", (user["id"],))
                db.commit()
            error = "아이디 또는 비밀번호가 틀렸습니다."
    return render_template("login.html", error=error)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required()
def change_password():
    error = None
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if not check_password_hash(g.user["pw_hash"], request.form.get("current_password", "")):
            error = "현재 비밀번호가 틀렸습니다."
        elif len(new) < 8:
            error = "새 비밀번호는 8자 이상이어야 합니다."
        elif new != request.form.get("new_password2"):
            error = "새 비밀번호 두 개가 서로 다릅니다."
        else:
            db = get_db()
            db.execute(
                "UPDATE users SET pw_hash = ?, must_change_pw = 0 WHERE id = ?",
                (generate_password_hash(new), g.user["id"]),
            )
            audit(db, g.user["id"], "user", g.user["id"], "change_password")
            db.commit()
            flash("비밀번호를 변경했습니다.")
            return redirect(url_for("index"))
    return render_template("password.html", error=error)


def fmt_dt(value):
    """저장값 '2026-09-23T16:01:55' → 화면용 '2026-09-23 16:01'."""
    return value.replace("T", " ")[:16] if value else "-"


CODE_RE = re.compile(r"^\d+(-\d+)*$")


def norm_code(value):
    """'1-1-1.' → '1-1-1'. 형식이 틀리면 None."""
    code = (value or "").strip().rstrip(".").strip()
    return code if CODE_RE.match(code) else None


def code_key(code):
    return tuple(int(p) for p in code.split("-"))


app.jinja_env.filters["dt"] = fmt_dt
app.jinja_env.filters["depth"] = lambda code: code.count("-")


def parse_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        abort(400)


# ---------------------------------------------------------------- 점검 항목

ITEM_COLS = ("code", "title", "is_group", "owner_id", "active")


def load_items(db, active_only=True):
    sql = """SELECT i.*, u.name AS owner_name, u.login_id AS owner_login
             FROM items i LEFT JOIN users u ON u.id = i.owner_id"""
    if active_only:
        sql += " WHERE i.active = 1"
    return sorted((dict(r) for r in db.execute(sql)), key=lambda r: code_key(r["code"]))


def item_form(db):
    """항목 폼 검증. (값 dict, None) 또는 (None, 오류 메시지)."""
    f = request.form
    code = norm_code(f.get("code"))
    title = f.get("title", "").strip()[:200]
    is_group = int(f.get("is_group") == "1")
    owner_id = None if is_group else f.get("owner_id", type=int)
    if not code:
        return None, "번호는 1, 1-1, 1-1-1 처럼 숫자와 - 로 입력하세요."
    if not title:
        return None, "항목명을 입력하세요."
    if not is_group:
        if owner_id is None:
            return None, "점검 항목은 담당자를 지정해야 합니다."
        if db.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (owner_id,)).fetchone() is None:
            return None, "사용 중인 사용자만 담당자로 지정할 수 있습니다."
    return {"code": code, "title": title, "is_group": is_group, "owner_id": owner_id}, None


def parse_bulk(text, logins, existing_codes):
    """'번호 | 항목명 | 담당자ID' 줄들을 해석한다. 담당자가 없으면 구분(제목줄)."""
    rows, errors, seen = [], [], set(existing_codes)
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) not in (2, 3):
            errors.append(f"{n}번째 줄: '번호 | 항목명 | 담당자ID' 형식이 아닙니다.")
            continue
        code, title = norm_code(parts[0]), parts[1][:200]
        login = parts[2] if len(parts) == 3 else ""
        if not code:
            errors.append(f"{n}번째 줄: 번호 '{parts[0]}' 형식이 틀렸습니다.")
        elif code in seen:
            errors.append(f"{n}번째 줄: 이미 있는 번호 {code} 입니다.")
        elif not title:
            errors.append(f"{n}번째 줄: 항목명이 비어 있습니다.")
        elif login and login not in logins:
            errors.append(f"{n}번째 줄: 담당자 ID '{login}'가 없습니다.")
        else:
            seen.add(code)
            rows.append({"code": code, "title": title, "is_group": int(not login),
                         "owner_id": logins[login] if login else None})
    return rows, errors


@app.route("/items")
@login_required()
def items_page():
    db = get_db()
    return render_template("items.html", items=load_items(db, active_only=False),
                           users=db.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall())


@app.post("/items")
@login_required()
def add_item():
    db = get_db()
    values, error = item_form(db)
    if error:
        flash(error)
        return redirect(url_for("items_page"))
    try:
        item_id = db.execute(
            "INSERT INTO items(code, title, is_group, owner_id) VALUES(:code, :title, :is_group, :owner_id)", values
        ).lastrowid
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"이미 있는 번호 {values['code']} 입니다.")
        return redirect(url_for("items_page"))
    audit(db, g.user["id"], "item", item_id, "add", after=values)
    db.commit()
    flash(f"{values['code']} 항목을 추가했습니다.")
    return redirect(url_for("items_page"))


@app.post("/items/<int:item_id>")
@login_required()
def update_item(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    values, error = item_form(db)
    if error:
        flash(f"{row['code']}: {error}")
        return redirect(url_for("items_page"))
    values["active"] = int(request.form.get("active") == "1")
    try:
        db.execute("UPDATE items SET code = :code, title = :title, is_group = :is_group, owner_id = :owner_id,"
                   " active = :active WHERE id = :id", {**values, "id": item_id})
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"이미 있는 번호 {values['code']} 입니다.")
        return redirect(url_for("items_page"))
    audit(db, g.user["id"], "item", item_id, "update", before={k: row[k] for k in ITEM_COLS}, after=values)
    db.commit()
    flash(f"{values['code']} 항목을 저장했습니다.")
    return redirect(url_for("items_page"))


@app.post("/items/bulk")
@login_required()
def bulk_items():
    db = get_db()
    logins = {r["login_id"]: r["id"] for r in db.execute("SELECT id, login_id FROM users WHERE active = 1")}
    codes = {r["code"] for r in db.execute("SELECT code FROM items")}
    rows, errors = parse_bulk(request.form.get("text", ""), logins, codes)
    if errors:
        for e in errors:
            flash(e)
        flash("오류가 있어 아무것도 등록하지 않았습니다. 고쳐서 다시 붙여넣으세요.")
        return redirect(url_for("items_page"))
    for values in rows:
        item_id = db.execute(
            "INSERT INTO items(code, title, is_group, owner_id) VALUES(:code, :title, :is_group, :owner_id)", values
        ).lastrowid
        audit(db, g.user["id"], "item", item_id, "add", after=values)
    db.commit()
    flash(f"{len(rows)}개 항목을 등록했습니다.")
    return redirect(url_for("items_page"))


@app.route("/")
@login_required()
def index():
    return render_template("index.html")  # Task 2에서 교체


# ---------------------------------------------------------------- 사용자 관리

USER_AUDIT_COLS = ("login_id", "name", "role", "active", "fail_count")


def user_view(db, user_id):
    """감사로그용 사용자 정보(비밀번호 해시 제외)."""
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        abort(404)
    return {k: row[k] for k in USER_AUDIT_COLS}


@app.route("/admin")
@login_required("admin")
def admin():
    users = get_db().execute("SELECT * FROM users ORDER BY active DESC, name").fetchall()
    return render_template("admin.html", roles=ROLE_NAMES, max_fail=MAX_FAIL, users=users)


@app.post("/admin/users")
@login_required("admin")
def admin_add_user():
    f = request.form
    login_id, name, role, pw = f.get("login_id", "").strip(), f.get("name", "").strip(), f.get("role"), f.get("password", "")
    if not login_id or not name or role not in ROLE_NAMES or len(pw) < 8:
        flash("ID, 이름, 역할, 8자 이상 임시 비밀번호를 입력하세요.")
        return redirect(url_for("admin"))
    db = get_db()
    try:
        user_id = db.execute(
            "INSERT INTO users(login_id, name, pw_hash, role) VALUES(?, ?, ?, ?)",
            (login_id, name, generate_password_hash(pw), role),
        ).lastrowid
    except sqlite3.IntegrityError:
        db.rollback()
        flash("이미 있는 ID입니다.")
        return redirect(url_for("admin"))
    audit(db, g.user["id"], "user", user_id, "add", after=user_view(db, user_id))
    db.commit()
    flash(f"{name} 사용자를 추가했습니다. 첫 로그인 때 비밀번호를 바꿔야 합니다.")
    return redirect(url_for("admin"))


@app.post("/admin/users/<int:user_id>")
@login_required("admin")
def admin_update_user(user_id):
    db = get_db()
    action = request.form.get("action")
    before = user_view(db, user_id)
    if user_id == g.user["id"] and action in ("role", "deactivate"):
        flash("본인 계정의 역할이나 사용 여부는 바꿀 수 없습니다.")
        return redirect(url_for("admin"))
    if action == "role":
        role = request.form.get("role")
        if role not in ROLE_NAMES:
            abort(400)
        db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    elif action == "reset_pw":
        pw = request.form.get("password", "")
        if len(pw) < 8:
            flash("임시 비밀번호는 8자 이상이어야 합니다.")
            return redirect(url_for("admin"))
        db.execute("UPDATE users SET pw_hash = ?, must_change_pw = 1, fail_count = 0 WHERE id = ?",
                   (generate_password_hash(pw), user_id))
    elif action == "unlock":
        db.execute("UPDATE users SET fail_count = 0 WHERE id = ?", (user_id,))
    elif action in ("deactivate", "activate"):
        db.execute("UPDATE users SET active = ? WHERE id = ?", (int(action == "activate"), user_id))
    else:
        abort(400)
    audit(db, g.user["id"], "user", user_id, action, before=before, after=user_view(db, user_id))
    db.commit()
    flash("저장했습니다.")
    return redirect(url_for("admin"))


def backup(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"checklist-{datetime.now():%Y%m%d-%H%M%S}.db")
    src, dst = sqlite3.connect(app.config["DATABASE"]), sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.close()
    return dest


def main(argv):
    dbm.init_db(app.config["DATABASE"])
    if argv[:1] == ["init-admin"]:
        login_id = input("관리자 ID: ").strip()
        name = input("이름: ").strip()
        pw = getpass.getpass("임시 비밀번호(8자 이상): ")
        if not login_id or not name or len(pw) < 8:
            sys.exit("입력값을 확인하세요.")
        con = sqlite3.connect(app.config["DATABASE"])
        with con:
            con.execute(
                "INSERT INTO users(login_id, name, pw_hash, role) VALUES(?, ?, ?, 'admin')",
                (login_id, name, generate_password_hash(pw)),
            )
        con.close()
        print("관리자를 만들었습니다. 첫 로그인 때 비밀번호를 바꿔야 합니다.")
    elif argv[:1] == ["backup"]:
        print(backup(argv[1] if len(argv) > 1 else os.path.join(BASE, "backup")))
    else:
        from waitress import serve
        host = os.environ.get("CHECKLIST_HOST", "0.0.0.0")
        port = int(os.environ.get("CHECKLIST_PORT", "8080"))
        print(f"http://{host}:{port} 에서 실행 중")
        serve(app, host=host, port=port)


if __name__ == "__main__":
    main(sys.argv[1:])
