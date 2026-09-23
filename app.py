import csv
import getpass
import hashlib
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


app.jinja_env.filters["dt"] = fmt_dt


def parse_day(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        abort(400)


# ---------------------------------------------------------------- 점검 항목(트리)

ITEM_COLS = ("parent_id", "sort", "title", "is_group", "owner_id", "active", "retired_on")
OWNER_RE = re.compile(r"^(.*?)\s*\(([^()]*)\)$")


def load_items(db, active_only=True):
    """항목을 트리 순서로. 각 dict에 depth(깊이)와 path(상위 구분 이름 목록)를 붙인다.
    active_only면 미사용 항목과 그 하위는 뺀다."""
    rows = [dict(r) for r in db.execute("""SELECT i.*, u.name AS owner_name, u.login_id AS owner_login
                                           FROM items i LEFT JOIN users u ON u.id = i.owner_id""")]
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    out = []

    def walk(parent_id, depth, path):
        for r in sorted(kids.get(parent_id, []), key=lambda r: (r["sort"], r["id"])):
            if active_only and not r["active"]:
                continue
            r["depth"], r["path"] = depth, path
            out.append(r)
            walk(r["id"], depth + 1, path + [r["title"]])

    walk(None, 0, [])
    return out


def subtree_ids(db, item_id):
    """item_id와 그 모든 하위 항목 id."""
    kids = {}
    for r in db.execute("SELECT id, parent_id FROM items"):
        kids.setdefault(r["parent_id"], []).append(r["id"])
    ids, todo = set(), [item_id]
    while todo:
        i = todo.pop()
        if i not in ids:
            ids.add(i)
            todo.extend(kids.get(i, []))
    return ids


def next_sort(db, parent_id):
    return db.execute("SELECT COALESCE(MAX(sort), -1) + 1 FROM items WHERE parent_id IS ?", (parent_id,)).fetchone()[0]


def item_form(db, row=None):
    """항목 폼 검증. (값 dict, None) 또는 (None, 오류 메시지).
    수정(row 있음)일 때 바꾸지 않은 상위 구분·담당자는 미사용이어도 그대로 둔다."""
    f = request.form
    title = f.get("title", "").strip()[:200]
    is_group = int(f.get("is_group") == "1")
    owner_id = None if is_group else f.get("owner_id", type=int)
    parent_id = f.get("parent_id", type=int)
    if not title:
        return None, "항목명을 입력하세요."
    if parent_id is not None and not (row and parent_id == row["parent_id"]):
        parent = db.execute("SELECT * FROM items WHERE id = ?", (parent_id,)).fetchone()
        if parent is None or not parent["is_group"] or not parent["active"]:
            return None, "하위 항목은 사용 중인 구분 아래에만 둘 수 있습니다."
    if not is_group:
        if owner_id is None:
            return None, "점검 항목은 담당자를 지정해야 합니다."
        unchanged = row and owner_id == row["owner_id"]
        if not unchanged and db.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (owner_id,)).fetchone() is None:
            return None, "사용 중인 사용자만 담당자로 지정할 수 있습니다."
    return {"parent_id": parent_id, "title": title, "is_group": is_group, "owner_id": owner_id}, None


def parse_indented(text, logins):
    """들여쓰기 목록 → ([{title, is_group, owner_id, parent}], [오류]).
    parent는 목록 안 상위 줄의 순번(없으면 None). 줄 끝 (담당자ID)가 있으면 점검 항목, 없으면 구분."""
    lines = [(n, line.expandtabs(4).rstrip()) for n, line in enumerate(text.splitlines(), 1) if line.strip()]
    base = min((len(l) - len(l.lstrip()) for _, l in lines), default=0)
    rows, errors, stack = [], [], []  # stack: (들여쓰기 폭, rows 순번)
    for n, line in lines:
        indent = max(len(line) - len(line.lstrip()) - base, 0)
        body = line.strip()
        m = OWNER_RE.match(body)
        title, login = (m.group(1).strip(), m.group(2).strip()) if m else (body, None)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1] if stack else None
        if parent is not None and not rows[parent]["is_group"]:
            errors.append(f"{n}번째 줄: 점검 항목(담당자가 있는 줄) 아래에는 하위 항목을 둘 수 없습니다.")
        elif not title:
            errors.append(f"{n}번째 줄: 항목명이 비어 있습니다.")
        elif login is not None and login not in logins:
            errors.append(f"{n}번째 줄: 담당자 ID '{login}'가 없습니다.")
        else:
            stack.append((indent, len(rows)))
            rows.append({"title": title[:200], "is_group": int(login is None),
                         "owner_id": logins[login] if login is not None else None, "parent": parent})
    return rows, errors


@app.route("/items")
@login_required()
def items_page():
    db = get_db()
    items = load_items(db, active_only=False)
    edit = request.args.get("edit", type=int)
    blocked = subtree_ids(db, edit) if edit else set()
    groups = [i for i in items if i["is_group"] and i["active"] and i["id"] not in blocked]
    current = next((i for i in items if i["id"] == edit), None)
    if current and current["parent_id"] and current["parent_id"] not in {p["id"] for p in groups}:
        groups.append(next(i for i in items if i["id"] == current["parent_id"]))
    return render_template(
        "items.html", items=items, add=request.args.get("add"), edit=edit, groups=groups,
        users=db.execute("SELECT * FROM users ORDER BY active DESC, name").fetchall())


@app.post("/items")
@login_required()
def add_item():
    db = get_db()
    values, error = item_form(db)
    if error:
        flash(error)
        return redirect(url_for("items_page"))
    values.update(sort=next_sort(db, values["parent_id"]), created_on=today())
    item_id = db.execute("""INSERT INTO items(parent_id, sort, title, is_group, owner_id, created_on)
                            VALUES(:parent_id, :sort, :title, :is_group, :owner_id, :created_on)""", values).lastrowid
    audit(db, g.user["id"], "item", item_id, "add", after=values)
    db.commit()
    flash(f"'{values['title']}' 항목을 추가했습니다.")
    return redirect(url_for("items_page"))


def cascade_group(db, group_id, active):
    """구분을 미사용하면 사용 중인 하위 항목도 오늘 날짜로 미사용 처리하고, 당일 되돌리면 함께 되돌린다."""
    for sid in subtree_ids(db, group_id) - {group_id}:
        if active:
            cur = db.execute("UPDATE items SET active = 1, retired_on = NULL WHERE id = ? AND active = 0 AND retired_on = ?",
                             (sid, today()))
        else:
            cur = db.execute("UPDATE items SET active = 0, retired_on = ? WHERE id = ? AND active = 1", (today(), sid))
        if cur.rowcount:
            audit(db, g.user["id"], "item", sid, "reactivate" if active else "deactivate",
                  reason="상위 구분 사용 여부 변경에 따름")


@app.post("/items/<int:item_id>")
@login_required()
def update_item(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    values, error = item_form(db, row)
    if not error and values["parent_id"] in subtree_ids(db, item_id):
        error = "자기 자신이나 하위 항목 밑으로는 옮길 수 없습니다."
    if not error and values["is_group"] != row["is_group"]:
        if db.execute("SELECT 1 FROM results WHERE item_id = ?", (item_id,)).fetchone():
            error = "점검 기록이 있는 항목은 종류(구분/점검)를 바꿀 수 없습니다. 미사용 처리 후 새로 추가하세요."
        elif row["created_on"] != today():
            error = "종류(구분/점검)는 오늘 추가한 항목만 바꿀 수 있습니다. 미사용 처리 후 새로 추가하세요."
        elif db.execute("SELECT 1 FROM items WHERE parent_id = ?", (item_id,)).fetchone():
            error = "하위 항목이 있는 구분은 점검 항목으로 바꿀 수 없습니다."
    if not error and request.form.get("active") == "1" and not row["active"] and row["retired_on"] != today():
        error = "지난 날짜에 미사용 처리한 항목은 다시 사용할 수 없습니다(지난 기록이 바뀌기 때문). 새로 추가하세요."
    if error:
        flash(f"'{row['title']}': {error}")
        return redirect(url_for("items_page", edit=item_id))
    values["active"] = int(request.form.get("active") == "1")
    if values["active"] != row["active"]:
        values["retired_on"] = None if values["active"] else today()
    else:
        values["retired_on"] = row["retired_on"]
    values["sort"] = row["sort"] if values["parent_id"] == row["parent_id"] else next_sort(db, values["parent_id"])
    db.execute("""UPDATE items SET parent_id = :parent_id, sort = :sort, title = :title, is_group = :is_group,
                  owner_id = :owner_id, active = :active, retired_on = :retired_on WHERE id = :id""",
               {**values, "id": item_id})
    audit(db, g.user["id"], "item", item_id, "update", before={k: row[k] for k in ITEM_COLS}, after=values)
    if row["is_group"] and values["active"] != row["active"]:
        cascade_group(db, item_id, values["active"])
    db.commit()
    flash(f"'{values['title']}' 항목을 저장했습니다.")
    return redirect(url_for("items_page"))


@app.post("/items/<int:item_id>/move")
@login_required()
def move_item(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM items WHERE parent_id IS ? ORDER BY sort, id", (row["parent_id"],))]
    i = ids.index(item_id)
    j = i - 1 if request.form.get("direction") == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for n, sid in enumerate(ids):
            db.execute("UPDATE items SET sort = ? WHERE id = ?", (n, sid))
        audit(db, g.user["id"], "item", item_id, "move", after={"direction": request.form.get("direction")})
        db.commit()
    return redirect(url_for("items_page"))


@app.post("/items/bulk")
@login_required()
def bulk_items():
    db = get_db()
    logins = {r["login_id"]: r["id"] for r in db.execute("SELECT id, login_id FROM users WHERE active = 1")}
    rows, errors = parse_indented(request.form.get("text", ""), logins)
    if errors:
        for e in errors:
            flash(e)
        flash("오류가 있어 아무것도 등록하지 않았습니다. 고쳐서 다시 붙여넣으세요.")
        return redirect(url_for("items_page"))
    ids = []
    for r in rows:
        parent_id = ids[r["parent"]] if r["parent"] is not None else None
        values = {"parent_id": parent_id, "sort": next_sort(db, parent_id), "title": r["title"],
                  "is_group": r["is_group"], "owner_id": r["owner_id"], "created_on": today()}
        ids.append(db.execute("""INSERT INTO items(parent_id, sort, title, is_group, owner_id, created_on)
                                 VALUES(:parent_id, :sort, :title, :is_group, :owner_id, :created_on)""",
                              values).lastrowid)
        audit(db, g.user["id"], "item", ids[-1], "add", after=values)
    db.commit()
    flash(f"{len(rows)}개 항목을 등록했습니다.")
    return redirect(url_for("items_page"))


# ---------------------------------------------------------------- 오늘 점검


def result_token(res):
    """화면을 연 시점의 줄 상태. 저장할 때 달라졌으면 다른 사람이 먼저 손댄 것."""
    if not res:
        return ""
    raw = f"{res['status']}|{res['checker_id']}|{res['issue']}|{res['remark']}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def row_badge(res):
    if res is None:
        return "미입력", "st-none"
    if res["status"] == "draft":
        return "작성중", "st-draft"
    return ("이상 유", "st-issue") if res["issue"] else ("이상 무", "st-approved")


app.jinja_env.globals.update(result_token=result_token, row_badge=row_badge)


def existed(it, day):
    """그날 점검 대상이던 항목인가(추가한 날부터 미사용 처리한 날 전까지)."""
    if it["created_on"] and day < it["created_on"]:
        return False
    if not it["active"]:
        return bool(it["retired_on"]) and day < it["retired_on"]
    return True


def sheet_rows(db, day):
    """그날 표의 줄(트리 순서). 점검 줄에는 'result'를 붙이고, 제출된 줄은 제출 당시 항목명·담당자·구분 경로를 보여준다.
    그날 대상이 아닌 항목(추가 전·미사용 후)은 제출 기록이 있을 때만 나온다. 구분을 미사용하면 하위도 함께
    미사용 처리되므로(cascade_group) 대상 여부는 항목 자신의 기간만 본다."""
    results = {r["item_id"]: dict(r) for r in db.execute(
        "SELECT r.*, u.name AS checker_name FROM results r JOIN users u ON u.id = r.checker_id WHERE r.date = ?",
        (day,))}
    rows = []
    for it in load_items(db, active_only=False):
        it["path_text"] = " > ".join(it["path"])
        it["moved_from"] = None
        res = results.get(it["id"])
        if it["is_group"]:
            rows.append(it)  # 제목줄은 keep_with_groups가 하위 줄이 있는 것만 남긴다
            continue
        if res and res["status"] == "submitted":
            if res["path"] != it["path_text"]:
                it["moved_from"] = res["path"]
            it.update(title=res["title"], owner_name=res["owner_name"], path_text=res["path"])
        elif not existed(it, day):
            continue
        it["result"] = res
        rows.append(it)
    return rows


def keep_with_groups(rows, keep):
    """keep()을 통과한 점검 줄과, 그 줄들의 상위 구분 줄만 남긴다."""
    parent = {r["id"]: r["parent_id"] for r in rows}
    ids = set()
    for r in rows:
        if not r["is_group"] and keep(r):
            i = r["id"]
            while i is not None and i not in ids:
                ids.add(i)
                i = parent.get(i)
    return [r for r in rows if r["id"] in ids]


def submitted(r):
    return bool(r.get("result") and r["result"]["status"] == "submitted")


def day_info(db, day):
    return db.execute("""SELECT d.*, u.name AS approver_name FROM days d
                         LEFT JOIN users u ON u.id = d.approved_by WHERE d.date = ?""", (day,)).fetchone()


CONFLICT = "다른 사람이 먼저 입력한 항목이 있습니다. 화면을 새로 불러왔으니 확인한 뒤 다시 저장하세요."


def unapprove(db, day, reason):
    """그날 확인이 되어 있으면 풀고 감사로그를 남긴다(commit은 호출한 쪽)."""
    d = day_info(db, day)
    if d and d["approved_by"]:
        db.execute("UPDATE days SET approved_by = NULL, approved_at = NULL WHERE date = ?", (day,))
        audit(db, g.user["id"], "day", None, "unapprove",
              before={"date": day, "approved_by": d["approved_by"]}, reason=reason)


def posted_issue(item_id):
    v = request.form.get(f"issue_{item_id}")
    return int(v) if v in ("0", "1") else None


def save_rows(db, day, action):
    """임시저장/제출. (오류 메시지, 성공 메시지) 중 하나를 채워 돌려준다."""
    if day != today():
        return "오늘 점검만 입력할 수 있습니다.", None
    items = {r["id"]: r for r in load_items(db) if not r["is_group"]}
    existing = {r["item_id"]: r for r in db.execute("SELECT * FROM results WHERE date = ?", (day,))}
    changes = []
    for raw in request.form.getlist("row"):
        it = items.get(int(raw)) if raw.isdigit() else None
        if it is None:
            continue
        res = existing.get(it["id"])
        if request.form.get(f"base_{it['id']}", "") != result_token(res):
            return CONFLICT, None
        if res and res["status"] == "submitted":
            continue
        issue = posted_issue(it["id"])
        remark = request.form.get(f"remark_{it['id']}", "").strip()[:500]
        if issue is None and not remark and res is None:
            continue
        submit = action == "submit" and issue is not None
        if submit and issue == 1 and not remark:
            return f"'{it['title']}' 항목: 이상 '유'는 비고를 입력해야 합니다.", None
        changes.append((it, res, issue, remark, submit))
    if action == "submit" and not any(c[4] for c in changes):
        return "제출할 항목이 없습니다. 이상 유/무를 선택하세요.", None
    try:
        for it, res, issue, remark, submit in changes:
            vals = {"date": day, "item_id": it["id"], "path": " > ".join(it["path"]), "title": it["title"],
                    "owner_name": it["owner_name"], "checker_id": g.user["id"], "issue": issue, "remark": remark,
                    "status": "submitted" if submit else "draft", "submitted_at": now() if submit else None}
            if res is None:
                db.execute("""INSERT INTO results(date, item_id, path, title, owner_name, checker_id, issue, remark,
                              status, submitted_at) VALUES(:date, :item_id, :path, :title, :owner_name, :checker_id,
                              :issue, :remark, :status, :submitted_at)""", vals)
            else:
                cur = db.execute("""UPDATE results SET path = :path, title = :title, owner_name = :owner_name,
                              checker_id = :checker_id, issue = :issue, remark = :remark, status = :status,
                              submitted_at = :submitted_at
                              WHERE date = :date AND item_id = :item_id AND status = 'draft'""", vals)
                if cur.rowcount == 0:  # 그 사이 다른 사람이 제출함
                    db.rollback()
                    return CONFLICT, None
            if submit:
                audit(db, g.user["id"], "result", it["id"], "submit",
                      after={"date": day, "title": it["title"], "issue": issue, "remark": remark})
    except sqlite3.IntegrityError:
        db.rollback()
        return CONFLICT, None
    if any(c[4] for c in changes):
        unapprove(db, day, "확인 후 새 항목 제출로 확인 해제")
    db.commit()
    if action == "save":
        return None, "임시저장했습니다."
    left = sum(1 for r in sheet_rows(db, day)
               if not r["is_group"] and r["owner_id"] == g.user["id"] and not submitted(r))
    return None, f"{sum(1 for c in changes if c[4])}건 제출했습니다. 남은 내 항목 {left}건."


def edit_row(db, day, item_id):
    """제출된 줄을 사유와 함께 수정. 그날 확인이 되어 있었으면 풀린다."""
    res = db.execute("SELECT * FROM results WHERE date = ? AND item_id = ?", (day, item_id)).fetchone()
    if res is None or res["status"] != "submitted":
        return "제출된 항목만 수정할 수 있습니다.", None
    reason = request.form.get("reason", "").strip()
    if not reason:
        return "수정 사유를 입력하세요.", None
    issue = posted_issue(item_id)
    remark = request.form.get(f"remark_{item_id}", "").strip()[:500]
    if issue is None:
        return "이상 유/무를 선택하세요.", None
    if issue == 1 and not remark:
        return "이상 '유'는 비고를 입력해야 합니다.", None
    db.execute("UPDATE results SET issue = ?, remark = ? WHERE id = ?", (issue, remark, res["id"]))
    audit(db, g.user["id"], "result", item_id, "edit",
          before={"date": day, "title": res["title"], "issue": res["issue"], "remark": res["remark"]},
          after={"date": day, "title": res["title"], "issue": issue, "remark": remark}, reason=reason)
    unapprove(db, day, "항목 수정으로 확인 해제")
    db.commit()
    return None, "수정했습니다."


@app.route("/", methods=["GET", "POST"])
@login_required()
def index():
    day = parse_day(request.args.get("day") or today())
    view = "all" if request.args.get("view") == "all" else "mine"
    db = get_db()
    error = None
    editing = request.args.get("edit", type=int)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "edit":
            editing = request.form.get("item_id", type=int)
            error, msg = edit_row(db, day, editing)
        elif action in ("save", "submit"):
            error, msg = save_rows(db, day, action)
        else:
            abort(400)
        if error is None or error == CONFLICT:
            flash(msg or error)
            return redirect(url_for("index", day=day, view=view))
    rows = sheet_rows(db, day)
    posted = set(request.form.getlist("row")) if error else set()
    for r in rows:
        if r["is_group"]:
            continue
        res = r["result"]
        r["draft_issue"] = res["issue"] if res else None
        r["draft_remark"] = res["remark"] if res else ""
        if str(r["id"]) in posted:
            r["draft_issue"] = posted_issue(r["id"])
            r["draft_remark"] = request.form.get(f"remark_{r['id']}", "")
    mine = [r for r in rows if not r["is_group"] and r["owner_id"] == g.user["id"]]
    if view == "mine":
        rows = keep_with_groups(rows, lambda r: r["owner_id"] == g.user["id"])
    else:
        rows = keep_with_groups(rows, lambda r: True)
    return render_template("index.html", rows=rows, day=day, today=today(), view=view, error=error,
                           editing=editing, day_row=day_info(db, day),
                           my_total=len(mine), my_done=sum(1 for r in mine if submitted(r)))


# ---------------------------------------------------------------- 현황판


def day_status(db, day):
    rows = [r for r in sheet_rows(db, day) if not r["is_group"]]
    owners = {}
    for r in rows:
        o = owners.setdefault(r["owner_name"] or "-", [0, 0])
        o[1] += 1
        o[0] += submitted(r)
    return {
        "rows": rows,
        "total": len(rows),
        "missing": [r for r in rows if not submitted(r)],
        "issues": [r for r in rows if submitted(r) and r["result"]["issue"] == 1],
        "owners": [(name, done, total) for name, (done, total) in sorted(owners.items())],
        "checkers": {r["result"]["checker_id"] for r in rows if submitted(r)} | {
            r["user_id"] for r in db.execute("""SELECT user_id FROM audit_log WHERE target = 'result'
                                                AND action = 'edit' AND json_extract(before, '$.date') = ?""", (day,))},
        "approved": day_info(db, day),
    }


@app.route("/dashboard")
@login_required("leader")
def dashboard():
    day = parse_day(request.args.get("day") or today())
    return render_template("dashboard.html", day=day, s=day_status(get_db(), day))


@app.post("/approve/<day>")
@login_required("leader")
def approve_day(day):
    day = parse_day(day)
    db = get_db()
    s = day_status(db, day)
    if s["approved"] and s["approved"]["approved_by"]:
        flash("이미 확인한 날짜입니다.")
    elif not s["total"] or s["missing"]:
        flash("모든 점검 항목이 제출돼야 확인할 수 있습니다.")
    elif g.user["id"] in s["checkers"]:
        flash("본인이 제출하거나 수정한 항목이 있는 날은 확인할 수 없습니다. 다른 팀장이나 관리자가 확인해야 합니다.")
    else:
        db.execute("""INSERT INTO days(date, approved_by, approved_at) VALUES(?, ?, ?)
                      ON CONFLICT(date) DO UPDATE SET approved_by = excluded.approved_by,
                      approved_at = excluded.approved_at""", (day, g.user["id"], now()))
        audit(db, g.user["id"], "day", None, "approve",
              after={"date": day, "total": s["total"], "issues": len(s["issues"])})
        db.commit()
        flash(f"{day} 점검을 확인했습니다.")
    return redirect(url_for("dashboard", day=day))


# ---------------------------------------------------------------- 이력·출력

CSV_HEADER = ["날짜", "구분", "점검항목", "담당자", "점검자", "이상", "비고", "상태", "제출시각", "확인자", "확인시각"]


def csv_cell(v):
    """엑셀이 수식으로 해석하지 않도록 위험한 첫 글자 앞에 ' 를 붙인다."""
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


@app.route("/history")
@login_required()
def history():
    start = parse_day(request.args.get("start") or today())
    end = parse_day(request.args.get("end") or start)
    if start > end:
        start, end = end, start
    db = get_db()
    dates = [r["date"] for r in db.execute(
        "SELECT DISTINCT date FROM results WHERE date BETWEEN ? AND ? ORDER BY date", (start, end))]
    days = [{"date": d, **day_status(db, d)} for d in dates]
    fmt = request.args.get("format")
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(CSV_HEADER)
        for d in days:
            ap = d["approved"]
            for r in d["rows"]:
                res = r["result"]
                w.writerow([csv_cell(v) for v in (
                    d["date"], r["path_text"], r["title"], r["owner_name"],
                    res["checker_name"] if res else "",
                    ("유" if res["issue"] else "무") if res and res["issue"] is not None else "",
                    res["remark"] if res else "",
                    "제출" if submitted(r) else ("작성중" if res else "미입력"),
                    fmt_dt(res["submitted_at"]) if res and res["submitted_at"] else "",
                    ap["approver_name"] if ap else "", fmt_dt(ap["approved_at"]) if ap and ap["approved_at"] else "")])
        return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=checklist_{start}_{end}.csv"})
    if fmt == "print":
        for d in days:
            d["sheet"] = keep_with_groups(sheet_rows(db, d["date"]), lambda r: True)
        return render_template("print.html", days=days, start=start, end=end)
    return render_template("history.html", days=days, start=start, end=end)


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
