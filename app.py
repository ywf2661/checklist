import csv
import getpass
import io
import os
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


STATUS_NAMES = {None: "미점검", "draft": "작성중", "submitted": "제출", "approved": "확인완료"}


def status_label(status, has_issue=0):
    return STATUS_NAMES[status] + (" · 이상" if has_issue else "")


def status_class(status, has_issue=0):
    """상태 배지 색(CSS class). 확인 전 이상 건만 빨강으로 강조한다."""
    if status == "submitted" and has_issue:
        return "st-issue"
    return {None: "st-none", "draft": "st-draft", "submitted": "st-submitted", "approved": "st-approved"}[status]


def fmt_dt(value):
    """저장값 '2026-09-23T16:01:55' → 화면용 '2026-09-23 16:01'."""
    return value.replace("T", " ")[:16] if value else "-"


app.jinja_env.globals.update(status_label=status_label, status_class=status_class)
app.jinja_env.filters["dt"] = fmt_dt


def parse_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        abort(400)


@app.route("/")
@login_required()
def index():
    rows = get_db().execute(
        """SELECT s.id, s.name, s.owner_id, o.name AS owner_name, c.status, c.has_issue
           FROM systems s
           LEFT JOIN users o ON o.id = s.owner_id
           LEFT JOIN checks c ON c.system_id = s.id AND c.date = ?
           WHERE s.active = 1
           ORDER BY s.owner_id IS NOT ?, s.sort, s.name""",
        (today(), g.user["id"]),
    ).fetchall()
    return render_template("index.html", rows=rows, today=today())


def load_sheet(db, system_id, day):
    """제출된 점검은 저장된 스냅샷을, 그 외에는 현재 사용 중인 항목을 돌려준다."""
    check = db.execute("SELECT * FROM checks WHERE date = ? AND system_id = ?", (day, system_id)).fetchone()
    if check and check["status"] != "draft":
        items = db.execute(
            "SELECT item_id, item_text, checked FROM check_items WHERE check_id = ? ORDER BY rowid", (check["id"],)
        )
        return check, [dict(r) for r in items]
    saved = {}
    if check:
        saved = {
            r["item_id"]: r["checked"]
            for r in db.execute("SELECT item_id, checked FROM check_items WHERE check_id = ?", (check["id"],))
        }
    items = db.execute(
        "SELECT id, text FROM items WHERE system_id = ? AND active = 1 ORDER BY sort, id", (system_id,)
    )
    return check, [{"item_id": r["id"], "item_text": r["text"], "checked": saved.get(r["id"], 0)} for r in items]


def snapshot(remark, items):
    return {"remark": remark, "items": [[it["item_text"], it["checked"]] for it in items]}


def write_items(db, check_id, items):
    db.execute("DELETE FROM check_items WHERE check_id = ?", (check_id,))
    db.executemany(
        "INSERT INTO check_items(check_id, item_id, item_text, checked) VALUES(?, ?, ?, ?)",
        [(check_id, it["item_id"], it["item_text"], it["checked"]) for it in items],
    )


def apply_form(items):
    """폼에서 받은 체크 값을 items에 덮어쓰고 (비고, 이상여부)를 돌려준다."""
    posted = set(request.form.getlist("item"))
    for it in items:
        it["checked"] = int(str(it["item_id"]) in posted)
    return request.form.get("remark", "").strip(), int(any(not it["checked"] for it in items))


def edit_sheet(db, check, items):
    """제출(또는 확인)된 점검을 사유와 함께 수정. 확인은 풀리고 이력이 남는다."""
    if check is None or check["status"] == "draft":
        return "제출된 점검만 수정할 수 있습니다."
    reason = request.form.get("reason", "").strip()
    if not reason:
        return "수정 사유를 입력하세요."
    before = snapshot(check["remark"], items)
    remark, has_issue = apply_form(items)
    if has_issue and not remark:
        return "체크하지 않은 항목이 있으면 비고를 입력해야 합니다."
    db.execute(
        "UPDATE checks SET remark = ?, has_issue = ?, status = 'submitted', approved_by = NULL, approved_at = NULL"
        " WHERE id = ?",
        (remark, has_issue, check["id"]),
    )
    write_items(db, check["id"], items)
    audit(db, g.user["id"], "check", check["id"], "edit",
          before=before, after=snapshot(remark, items), reason=reason)
    db.commit()
    return None


def save_sheet(db, system_id, day, check, items):
    """폼 내용을 저장한다. 성공하면 None, 실패하면 오류 메시지를 돌려준다."""
    action = request.form.get("action")
    if action == "edit":
        return edit_sheet(db, check, items)
    if action not in ("save", "submit"):
        abort(400)
    if day != today():
        return "오늘 점검만 작성할 수 있습니다."
    if not db.execute("SELECT active FROM systems WHERE id = ?", (system_id,)).fetchone()["active"]:
        return "사용하지 않는 시스템입니다. 새 점검을 작성할 수 없습니다."
    if check and check["status"] != "draft":
        return "다른 사용자가 먼저 제출했습니다."
    # 다른 사람의 작성중 점검표는 화면에서 그 내용을 본 뒤(draft_user 일치)에만 이어받는다
    if check and check["user_id"] != g.user["id"] and request.form.get("draft_user") != str(check["user_id"]):
        owner = db.execute("SELECT name FROM users WHERE id = ?", (check["user_id"],)).fetchone()["name"]
        return f"{owner}님이 작성 중인 점검표입니다. 새로고침해서 작성 내용을 확인한 뒤 이어서 작성하세요."
    remark, has_issue = apply_form(items)
    if action == "submit":
        if not items:
            return "점검 항목이 없습니다. 항목을 먼저 추가하세요."
        if has_issue and not remark:
            return "체크하지 않은 항목이 있으면 비고를 입력해야 합니다."
    status = "submitted" if action == "submit" else "draft"
    values = (g.user["id"], status, remark, has_issue if status == "submitted" else 0,
              now() if status == "submitted" else None)
    try:
        if check is None:
            check_id = db.execute(
                "INSERT INTO checks(user_id, status, remark, has_issue, submitted_at, date, system_id)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                values + (day, system_id),
            ).lastrowid
        else:
            check_id = check["id"]
            cur = db.execute(
                "UPDATE checks SET user_id = ?, status = ?, remark = ?, has_issue = ?, submitted_at = ?"
                " WHERE id = ? AND status = 'draft'",
                values + (check_id,),
            )
            if cur.rowcount == 0:
                db.rollback()
                return "다른 사용자가 먼저 제출했습니다."
    except sqlite3.IntegrityError:
        db.rollback()
        return "다른 사용자가 먼저 작성을 시작했습니다. 새로고침 후 다시 시도하세요."
    write_items(db, check_id, items)
    if status == "submitted":
        audit(db, g.user["id"], "check", check_id, "submit", after=snapshot(remark, items))
    db.commit()
    return None


SAVED_MESSAGES = {"save": "임시저장했습니다.", "submit": "제출했습니다.", "edit": "수정했습니다."}


@app.route("/check/<int:system_id>/<day>", methods=["GET", "POST"])
@login_required()
def check_sheet(system_id, day):
    day = parse_day(day)
    db = get_db()
    system = db.execute("SELECT * FROM systems WHERE id = ?", (system_id,)).fetchone()
    if system is None:
        abort(404)
    check, items = load_sheet(db, system_id, day)
    remark = check["remark"] if check else ""
    error = None
    if request.method == "POST":
        error = save_sheet(db, system_id, day, check, items)
        if error is None:
            flash(SAVED_MESSAGES[request.form["action"]])
            return redirect(url_for("check_sheet", system_id=system_id, day=day))
        remark = request.form.get("remark", "")
    editing = bool(check and check["status"] != "draft"
                   and (request.args.get("edit") == "1" or request.form.get("action") == "edit"))
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM users")}
    return render_template("sheet.html", system=system, day=day, today=today(), check=check,
                           items=items, remark=remark, error=error, names=names, editing=editing)


@app.post("/system/<int:system_id>/items")
@login_required()
def add_item(system_id):
    db = get_db()
    if db.execute("SELECT 1 FROM systems WHERE id = ? AND active = 1", (system_id,)).fetchone() is None:
        abort(404)
    text = request.form.get("text", "").strip()[:200]
    if not text:
        flash("추가할 항목 내용을 입력하세요.")
    else:
        sort = db.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM items WHERE system_id = ?", (system_id,)).fetchone()[0]
        item_id = db.execute(
            "INSERT INTO items(system_id, text, sort) VALUES(?, ?, ?)", (system_id, text, sort)
        ).lastrowid
        audit(db, g.user["id"], "item", item_id, "add", after={"system_id": system_id, "text": text})
        db.commit()
        flash("점검 항목을 추가했습니다.")
    return redirect(url_for("check_sheet", system_id=system_id, day=today()))


@app.post("/item/<int:item_id>/deactivate")
@login_required()
def deactivate_item(item_id):
    db = get_db()
    item = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        abort(404)
    db.execute("UPDATE items SET active = 0 WHERE id = ?", (item_id,))
    audit(db, g.user["id"], "item", item_id, "deactivate",
          before={"text": item["text"], "active": item["active"]}, after={"active": 0})
    db.commit()
    flash("점검 항목을 미사용 처리했습니다. 필요하면 관리자가 다시 사용으로 바꿀 수 있습니다.")
    return redirect(url_for("check_sheet", system_id=item["system_id"], day=today()))


@app.post("/approve/<int:check_id>")
@login_required("leader")
def approve(check_id):
    db = get_db()
    check = db.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    if check is None:
        abort(404)
    if check["user_id"] == g.user["id"]:
        flash("본인이 제출한 점검은 확인할 수 없습니다. 다른 팀장이나 관리자가 확인해야 합니다.")
        return redirect(url_for("dashboard", day=check["date"]))
    cur = db.execute(
        "UPDATE checks SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ? AND status = 'submitted'",
        (g.user["id"], now(), check_id),
    )
    if cur.rowcount:
        audit(db, g.user["id"], "check", check_id, "approve")
        db.commit()
        flash("확인 처리했습니다.")
    else:
        flash("제출 상태인 점검만 확인할 수 있습니다.")
    return redirect(url_for("dashboard", day=check["date"]))


@app.route("/dashboard")
@login_required("leader")
def dashboard():
    day = parse_day(request.args.get("day") or today())
    rows = get_db().execute(
        """SELECT s.id AS system_id, s.name, o.name AS owner_name, c.id AS check_id, c.status,
                  c.has_issue, c.user_id AS checker_id, u.name AS checker_name, c.submitted_at
           FROM systems s
           LEFT JOIN users o ON o.id = s.owner_id
           LEFT JOIN checks c ON c.system_id = s.id AND c.date = ?
           LEFT JOIN users u ON u.id = c.user_id
           WHERE s.active = 1 OR c.id IS NOT NULL
           ORDER BY s.sort, s.name""",
        (day,),
    ).fetchall()
    return render_template(
        "dashboard.html", day=day, rows=rows,
        missing=sum(r["status"] is None for r in rows),
        issues=sum(bool(r["has_issue"]) for r in rows),
        waiting=sum(r["status"] == "submitted" for r in rows),
    )


CSV_HEADER = ["날짜", "시스템", "점검자", "제출시각", "상태", "확인자", "확인시각", "점검항목", "체크", "비고"]


def query_history(start, end, system_id=None):
    sql = """SELECT c.*, s.name AS system_name, u.name AS checker_name, a.name AS approver_name
             FROM checks c
             JOIN systems s ON s.id = c.system_id
             JOIN users u ON u.id = c.user_id
             LEFT JOIN users a ON a.id = c.approved_by
             WHERE c.date BETWEEN ? AND ?"""
    args = [start, end]
    if system_id:
        sql += " AND c.system_id = ?"
        args.append(system_id)
    sql += " ORDER BY c.date, s.sort, s.name"
    db = get_db()
    rows = [dict(r) for r in db.execute(sql, args)]
    for r in rows:
        r["lines"] = [dict(i) for i in db.execute(
            "SELECT item_text, checked FROM check_items WHERE check_id = ? ORDER BY rowid", (r["id"],))]
    return rows


def csv_cell(v):
    """엑셀이 수식으로 해석하지 않도록 위험한 첫 글자 앞에 ' 를 붙인다."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def csv_response(rows, start, end):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_HEADER)
    for r in rows:
        for it in r["lines"] or [{"item_text": "", "checked": 0}]:
            w.writerow([csv_cell(v) for v in (
                r["date"], r["system_name"], r["checker_name"], r["submitted_at"],
                status_label(r["status"], r["has_issue"]), r["approver_name"], r["approved_at"],
                it["item_text"], "O" if it["checked"] else "X", r["remark"])])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=checklist_{start}_{end}.csv"})


@app.route("/history")
@login_required()
def history():
    start = parse_day(request.args.get("start") or today())
    end = parse_day(request.args.get("end") or start)
    if start > end:
        start, end = end, start
    system_id = request.args.get("system_id", type=int)
    rows = query_history(start, end, system_id)
    fmt = request.args.get("format")
    if fmt == "csv":
        return csv_response(rows, start, end)
    if fmt == "print":
        return render_template("print.html", rows=rows, start=start, end=end)
    systems = get_db().execute("SELECT id, name FROM systems ORDER BY sort, name").fetchall()
    return render_template("history.html", rows=rows, start=start, end=end,
                           system_id=system_id, systems=systems)


USER_AUDIT_COLS = ("login_id", "name", "role", "active", "fail_count")


def user_view(db, user_id):
    """감사로그용 사용자 정보(비밀번호 해시 제외)."""
    row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        abort(404)
    return {k: row[k] for k in USER_AUDIT_COLS}


def system_form():
    f = request.form
    return {"name": f.get("name", "").strip(), "owner_id": f.get("owner_id", type=int),
            "sort": f.get("sort", 0, type=int), "active": int(f.get("active") == "1")}


@app.route("/admin")
@login_required("admin")
def admin():
    db = get_db()
    return render_template(
        "admin.html", roles=ROLE_NAMES, max_fail=MAX_FAIL,
        users=db.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall(),
        systems=db.execute("SELECT * FROM systems ORDER BY active DESC, sort, name").fetchall(),
        items=db.execute("""SELECT i.*, s.name AS system_name FROM items i JOIN systems s ON s.id = i.system_id
                            ORDER BY s.sort, s.name, i.sort, i.id""").fetchall(),
    )


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


@app.post("/admin/systems")
@login_required("admin")
def admin_add_system():
    s = system_form()
    if not s["name"]:
        flash("시스템 이름을 입력하세요.")
        return redirect(url_for("admin"))
    db = get_db()
    system_id = db.execute(
        "INSERT INTO systems(name, owner_id, sort, active) VALUES(:name, :owner_id, :sort, :active)", s
    ).lastrowid
    audit(db, g.user["id"], "system", system_id, "add", after=s)
    db.commit()
    flash("시스템을 추가했습니다.")
    return redirect(url_for("admin"))


@app.post("/admin/systems/<int:system_id>")
@login_required("admin")
def admin_update_system(system_id):
    db = get_db()
    row = db.execute("SELECT * FROM systems WHERE id = ?", (system_id,)).fetchone()
    if row is None:
        abort(404)
    s = system_form()
    if not s["name"]:
        flash("시스템 이름을 입력하세요.")
        return redirect(url_for("admin"))
    db.execute("UPDATE systems SET name = :name, owner_id = :owner_id, sort = :sort, active = :active WHERE id = :id",
               {**s, "id": system_id})
    audit(db, g.user["id"], "system", system_id, "update", before={k: row[k] for k in s}, after=s)
    db.commit()
    flash("저장했습니다.")
    return redirect(url_for("admin"))


@app.post("/admin/items/<int:item_id>")
@login_required("admin")
def admin_update_item(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    after = {"sort": request.form.get("sort", 0, type=int), "active": int(request.form.get("active") == "1")}
    db.execute("UPDATE items SET sort = :sort, active = :active WHERE id = :id", {**after, "id": item_id})
    audit(db, g.user["id"], "item", item_id, "update",
          before={"sort": row["sort"], "active": row["active"]}, after=after)
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
