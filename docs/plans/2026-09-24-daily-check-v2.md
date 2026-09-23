# 일일 점검 v2 (번호 계층·항목별 담당자) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "시스템 단위 체크박스" 구조를 종이 점검표와 같은 "번호 계층 + 항목별 담당자·이상 유/무·비고" 구조로 바꾼다.

**Architecture:** 기존 Flask 앱(`app.py` 하나, `db.py`, Jinja 템플릿)의 인증·감사로그·백업·디자인은 유지하고, 점검 데이터 모델(`systems/checks/check_items`)을 `items(번호 계층)/results(날짜×항목)/days(하루 확인)`로 교체한다. 계층은 번호 문자열(`1-1-1`)로만 표현하고 정렬·들여쓰기는 파이썬에서 계산한다.

**Tech Stack:** Python 3.10+, Flask 3.x, waitress, 표준 `sqlite3`/`csv`/`re`/`unittest`.

**Spec:** `docs/specs/2026-09-23-daily-check-design.md` (v2)

## Global Constraints

- 외부 패키지는 `flask`, `waitress` 두 개뿐.
- 모든 SQL은 파라미터 바인딩.
- 삭제 기능 없음(`active = 0`만). `audit_log`는 추가만(트리거).
- 모든 POST 폼에 CSRF 토큰.
- 화면 문구는 한국어, 디자인은 기존 `static/style.css`(네이비 금융형) 규칙을 따른다.
- 테스트: `venv/Scripts/python -m unittest` (Windows venv), 파일 `test_app.py` 하나.

## Review Focus

- 화면을 연 뒤 다른 사람이 같은 줄을 먼저 저장·제출하면 덮어쓰지 않고 새로고침을 안내해야 한다(Task 2 `test_stale_page_rejected`).
- 이상 "유"인데 비고가 비어 있으면 제출되면 안 되고, 입력값은 화면에 남아야 한다(Task 2 `test_issue_yes_requires_remark_and_keeps_input`).
- 번호 정렬은 숫자 단위여야 한다(`1-2` < `1-10`)(Task 1 `test_code_helpers`, Task 2 `test_all_view_natural_sort`).
- 일괄 등록에 틀린 줄이 하나라도 있으면 아무것도 등록되지 않아야 한다(Task 1 `test_bulk_all_or_nothing`).
- 미사용 처리한 항목의 과거 제출 기록은 그날 표·출력에 계속 나와야 한다(Task 2 `test_inactive_item_hidden_but_record_kept`).

## 파일 구조

```
db.py                  # 스키마 v2, 구버전 DB 감지
app.py                 # 인증(유지) + 항목 관리 + 오늘 점검 + 현황판 + 이력 + 관리(사용자) + CLI
templates/
  base.html            # 메뉴에 "항목 관리" 추가
  index.html           # 오늘 점검(번호 계층 표) — 전면 교체
  items.html           # 항목 관리(신규)
  dashboard.html       # 현황판 — 전면 교체
  history.html, print.html  # 이력·출력 — 전면 교체
  admin.html           # 사용자 관리만 남김
  sheet.html           # 삭제
static/style.css       # 계층 표·탭 스타일 추가
test_app.py            # 전면 교체(인증·관리자·백업 테스트는 유지)
README.md              # 항목 일괄 등록, 구버전 DB 안내
```

---

### Task 1: 스키마 v2, 번호 유틸, 항목 관리, 사용자 관리 정리

**Files:**
- Modify: `db.py` (SCHEMA, `init_db`)
- Rewrite: `app.py` (아래 전체 내용), `test_app.py` (아래 전체 내용)
- Create: `templates/items.html`
- Rewrite: `templates/index.html`(임시), `templates/admin.html`
- Modify: `templates/base.html` (메뉴)
- Delete: `templates/sheet.html`, `templates/dashboard.html`, `templates/history.html`, `templates/print.html` (Task 3·4에서 새로 만듦)

**Interfaces:**
- Produces:
  - `norm_code(value: str|None) -> str|None`, `code_key(code: str) -> tuple[int,...]`
  - `load_items(db, active_only=True) -> list[dict]` — 번호순, 각 dict에 `owner_name`, `owner_login`
  - `parse_bulk(text: str, logins: dict[str,int], existing_codes: set[str]) -> (rows: list[dict], errors: list[str])`
  - Jinja 필터 `dt`, `depth`
  - 엔드포인트 `items_page`(GET /items), `add_item`(POST /items), `update_item`(POST /items/<id>), `bulk_items`(POST /items/bulk)
  - `admin`, `admin_add_user`, `admin_update_user` (사용자만)
  - `backup(out_dir)`, `main(argv)`

- [ ] **Step 1: 테스트 전면 교체** — `test_app.py`

```python
import csv
import io
import json
import os
import sqlite3
import tempfile
import unittest

from werkzeug.security import generate_password_hash

import app as appmod
from db import init_db

PW = "pw12345678"
PW_HASH = generate_password_hash(PW)
DAY = "2026-09-23"

ITEMS = [  # id, code, title, is_group, owner_id
    (1, "1", "네트워크 및 시스템 점검", 1, None),
    (2, "1-1", "네트워크상태", 1, None),
    (3, "1-1-1", "백본 및 각 층 네트워크 상태", 0, 1),
    (4, "1-1-2", "인터넷 방화벽,스위치 상태", 0, 1),
    (5, "1-2", "시스템 및 업무 서비스", 1, None),
    (6, "1-2-1", "시각동기화 시스템 정상 작동여부", 0, 2),
    (7, "1-10", "전화 및 녹취", 0, 2),
]


class Base(unittest.TestCase):
    """임시 DB + 사용자 4명(m1, m2 부서원 / lead 팀장 / adm 관리자) + 점검 항목 트리."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        appmod.app.config.update(DATABASE=self.path, TESTING=True, TODAY=DAY)
        init_db(self.path)
        con = sqlite3.connect(self.path)
        with con:
            for i, (login_id, role) in enumerate(
                [("m1", "member"), ("m2", "member"), ("lead", "leader"), ("adm", "admin")], 1
            ):
                con.execute(
                    "INSERT INTO users(id, login_id, name, pw_hash, role, must_change_pw) VALUES(?,?,?,?,?,0)",
                    (i, login_id, login_id.upper(), PW_HASH, role),
                )
            con.executemany("INSERT INTO items(id, code, title, is_group, owner_id) VALUES(?,?,?,?,?)", ITEMS)
        con.close()
        self.c = appmod.app.test_client()

    def tearDown(self):
        os.remove(self.path)

    def post(self, url, client=None, **data):
        client = client or self.c
        with client.session_transaction() as s:
            tok = s.setdefault("csrf", "test-token")
        return client.post(url, data={"csrf": tok, **data})

    def login(self, login_id, client=None):
        return self.post("/login", client=client, login_id=login_id, password=PW)

    def row(self, sql, *args):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, args).fetchone()
        finally:
            con.close()

    def exec(self, sql, *args):
        con = sqlite3.connect(self.path)
        try:
            with con:
                con.execute(sql, args)
        finally:
            con.close()

    def text(self, r):
        return r.get_data(as_text=True)


class SchemaTest(Base):
    def test_audit_log_is_append_only(self):
        con = sqlite3.connect(self.path)
        con.execute("INSERT INTO audit_log(at, target, action) VALUES('t', 'x', 'y')")
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("UPDATE audit_log SET action = 'z'")
        with self.assertRaises(sqlite3.DatabaseError):
            con.execute("DELETE FROM audit_log")
        con.close()

    def test_one_result_per_item_per_day(self):
        sql = "INSERT INTO results(date, item_id, code, title, checker_id, status) VALUES(?, 3, '1-1-1', 't', 1, 'draft')"
        self.exec(sql, DAY)
        with self.assertRaises(sqlite3.IntegrityError):
            self.exec(sql, DAY)

    def test_old_version_db_is_detected(self):
        fd, old = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(old)
        con.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, system_id INTEGER, text TEXT)")
        con.close()
        with self.assertRaises(RuntimeError):
            init_db(old)
        os.remove(old)


class AuthTest(Base):
    def test_pages_require_login(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_login_success(self):
        r = self.login("m1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)

    def test_post_without_csrf_rejected(self):
        r = self.c.post("/login", data={"login_id": "m1", "password": PW})
        self.assertEqual(r.status_code, 400)

    def test_five_failures_lock_account(self):
        for _ in range(5):
            self.post("/login", login_id="m1", password="wrong")
        self.assertIn("잠겼습니다", self.text(self.login("m1")))
        self.assertEqual(self.row("SELECT fail_count FROM users WHERE id = 1")["fail_count"], 5)

    def test_inactive_user_cannot_login(self):
        self.exec("UPDATE users SET active = 0 WHERE id = 1")
        self.assertIn("틀렸습니다", self.text(self.login("m1")))

    def test_must_change_password_first(self):
        self.exec("UPDATE users SET must_change_pw = 1 WHERE id = 1")
        self.login("m1")
        r = self.c.get("/")
        self.assertIn("/password", r.headers["Location"])
        r = self.post("/password", current_password=PW, new_password="newpass123", new_password2="newpass123")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.row("SELECT action FROM audit_log")["action"], "change_password")

    def test_change_password_rejects_short_or_wrong_current(self):
        self.login("m1")
        t = self.text(self.post("/password", current_password=PW, new_password="short", new_password2="short"))
        self.assertIn("8자 이상", t)
        t = self.text(self.post("/password", current_password="x", new_password="newpass123", new_password2="newpass123"))
        self.assertIn("현재 비밀번호", t)


class CodeTest(unittest.TestCase):
    def test_code_helpers(self):
        self.assertEqual(appmod.norm_code(" 1-1-1. "), "1-1-1")
        self.assertEqual(appmod.norm_code("1."), "1")
        self.assertIsNone(appmod.norm_code("1-a"))
        self.assertIsNone(appmod.norm_code(""))
        codes = ["1-10", "1-2-1", "1", "1-2", "2", "1-1-1"]
        self.assertEqual(sorted(codes, key=appmod.code_key), ["1", "1-1-1", "1-2", "1-2-1", "1-10", "2"])
        self.assertEqual(appmod.fmt_dt("2026-09-23T16:01:55"), "2026-09-23 16:01")
        self.assertEqual(appmod.fmt_dt(None), "-")

    def test_parse_bulk(self):
        text = "2 | 전화 및 녹취\n\n2-1 | 녹취 서버 상태 | m1\n2-2 | 교환기 | nobody\nx | 틀림\n2-1 | 중복 | m1\n1 | 기존 번호"
        rows, errors = appmod.parse_bulk(text, {"m1": 1}, {"1"})
        self.assertEqual(rows[0], {"code": "2", "title": "전화 및 녹취", "is_group": 1, "owner_id": None})
        self.assertEqual(rows[1], {"code": "2-1", "title": "녹취 서버 상태", "is_group": 0, "owner_id": 1})
        self.assertEqual(len(errors), 4)
        self.assertIn("4번째 줄", errors[0])  # 없는 담당자
        self.assertIn("5번째 줄", errors[1])  # 번호 형식
        self.assertIn("6번째 줄", errors[2])  # 목록 안 중복
        self.assertIn("7번째 줄", errors[3])  # 이미 있는 번호


class ItemsTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")

    def test_member_sees_items_in_code_order(self):
        t = self.text(self.c.get("/items"))
        self.assertLess(t.index("시각동기화"), t.index("전화 및 녹취"))

    def test_member_adds_item_and_it_is_logged(self):
        self.post("/items", code="1-1-3", title="지점 네트워크 상태", is_group="0", owner_id="2")
        it = self.row("SELECT * FROM items WHERE code = '1-1-3'")
        self.assertEqual((it["title"], it["owner_id"], it["is_group"]), ("지점 네트워크 상태", 2, 0))
        a = self.row("SELECT * FROM audit_log WHERE target = 'item'")
        self.assertEqual((a["action"], a["user_id"]), ("add", 1))

    def test_add_rejects_bad_input(self):
        for data, msg in [
            ({"code": "1-a", "title": "x", "is_group": "1"}, "번호는"),
            ({"code": "1-1-1", "title": "x", "is_group": "1"}, "이미 있는 번호"),
            ({"code": "1-1-9", "title": "x", "is_group": "0"}, "담당자"),
            ({"code": "1-1-9", "title": "", "is_group": "1"}, "항목명"),
        ]:
            self.post("/items", **data)
            self.assertIn(msg, self.text(self.c.get("/items")))
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM items")["n"], 7)

    def test_update_item_and_deactivate(self):
        self.post("/items/3", code="1-1-1", title="백본 상태", is_group="0", owner_id="2")
        it = self.row("SELECT * FROM items WHERE id = 3")
        self.assertEqual((it["title"], it["owner_id"], it["active"]), ("백본 상태", 2, 0))
        a = self.row("SELECT * FROM audit_log WHERE action = 'update'")
        self.assertEqual(json.loads(a["before"])["title"], "백본 및 각 층 네트워크 상태")

    def test_bulk_register(self):
        self.post("/items/bulk", text="2 | 전화 및 녹취\n2-1 | 녹취 서버 상태 | m2")
        self.assertEqual(self.row("SELECT owner_id FROM items WHERE code = '2-1'")["owner_id"], 2)
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'add'")["n"], 2)

    def test_bulk_all_or_nothing(self):
        self.post("/items/bulk", text="2 | 전화 및 녹취\n2-1 | 녹취 | nobody")
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM items")["n"], 7)
        self.assertIn("2번째 줄", self.text(self.c.get("/items")))


class AdminTest(Base):
    def setUp(self):
        super().setUp()
        self.login("adm")

    def test_non_admin_forbidden(self):
        lead = appmod.app.test_client()
        self.login("lead", client=lead)
        self.assertEqual(lead.get("/admin").status_code, 403)

    def test_add_user_must_change_password(self):
        self.post("/admin/users", login_id="new1", name="신입", role="member", password="temp12345")
        u = self.row("SELECT * FROM users WHERE login_id = 'new1'")
        self.assertEqual((u["role"], u["must_change_pw"]), ("member", 1))
        self.assertEqual(self.row("SELECT action FROM audit_log WHERE target = 'user'")["action"], "add")

    def test_duplicate_login_id_rejected(self):
        self.post("/admin/users", login_id="m1", name="중복", role="member", password="temp12345")
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM users WHERE login_id = 'm1'")["n"], 1)
        self.assertIn("이미 있는 ID", self.text(self.c.get("/admin")))

    def test_deactivated_user_session_is_blocked(self):
        m1 = appmod.app.test_client()
        self.login("m1", client=m1)
        self.assertEqual(m1.get("/").status_code, 200)
        self.post("/admin/users/1", action="deactivate")
        r = m1.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_admin_cannot_deactivate_or_demote_self(self):
        self.post("/admin/users/4", action="deactivate")
        self.post("/admin/users/4", action="role", role="member")
        u = self.row("SELECT * FROM users WHERE id = 4")
        self.assertEqual((u["active"], u["role"]), (1, "admin"))

    def test_unlock_and_reset_password(self):
        self.exec("UPDATE users SET fail_count = 5 WHERE id = 1")
        self.post("/admin/users/1", action="unlock")
        self.assertEqual(self.login("m1", client=appmod.app.test_client()).status_code, 302)
        self.post("/admin/users/1", action="reset_pw", password="reset12345")
        self.assertEqual(self.row("SELECT must_change_pw FROM users WHERE id = 1")["must_change_pw"], 1)
        a = self.row("SELECT * FROM audit_log WHERE action = 'reset_pw'")
        self.assertNotIn("pw_hash", a["before"] + a["after"])


class BackupTest(Base):
    def test_backup_creates_consistent_copy(self):
        out = tempfile.mkdtemp()
        path = appmod.backup(out)
        con = sqlite3.connect(path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 7)
        con.close()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `venv/Scripts/python -m unittest`
Expected: 다수 ERROR/FAIL (`results` 테이블 없음, `norm_code` 없음, `/items` 404)

- [ ] **Step 3: `db.py`의 SCHEMA와 `init_db` 교체**

`SCHEMA`에서 `systems`, `items`, `checks`, `check_items` 정의를 지우고 아래로 바꾼다(`users`, `audit_log`, 트리거는 그대로):

```sql
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  is_group INTEGER NOT NULL DEFAULT 0,
  owner_id INTEGER REFERENCES users(id),
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  code TEXT NOT NULL,
  title TEXT NOT NULL,
  owner_name TEXT,
  checker_id INTEGER NOT NULL REFERENCES users(id),
  issue INTEGER CHECK(issue IN (0, 1)),
  remark TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('draft', 'submitted')),
  submitted_at TEXT,
  UNIQUE(date, item_id));
CREATE TABLE IF NOT EXISTS days(
  date TEXT PRIMARY KEY,
  approved_by INTEGER REFERENCES users(id),
  approved_at TEXT);
```

```python
def init_db(path):
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    if cols and "code" not in cols:
        con.close()
        raise RuntimeError("checklist.db가 이전 버전 형식입니다. 저장된 점검 기록이 없다면 파일을 지우고 다시 실행하세요.")
    con.executescript(SCHEMA)
    con.close()
```

- [ ] **Step 4: `app.py` 전면 교체**

인증 부분(맨 위 import부터 `change_password` 함수까지)은 기존 코드 그대로 두되 import에 `re`를 추가한다. 그 아래를 모두 지우고 다음으로 채운다:

```python
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
```

그 아래에 기존 `admin_add_user`, `admin_update_user`, `backup`, `main`, `if __name__ == "__main__":` 블록을 **기존 코드 그대로** 붙인다(`admin_add_system`, `admin_update_system`, `admin_update_item` 및 시스템/점검표/현황판/이력 관련 함수는 모두 삭제).

- [ ] **Step 5: 템플릿**

`templates/base.html` 메뉴의 `<a href="/history">이력 조회</a>` 다음 줄에 추가:
```html
  <a href="/items">항목 관리</a>
```

`templates/index.html` (임시):
```html
{% extends "base.html" %}
{% block content %}<h1>오늘 점검</h1>{% endblock %}
```

`templates/items.html`:
```html
{% extends "base.html" %}
{% block content %}
{% macro csrf() %}<input type="hidden" name="csrf" value="{{ csrf_token() }}">{% endmacro %}
{% macro owner_select(form_id, current) %}
<select {% if form_id %}form="{{ form_id }}" {% endif %}name="owner_id"><option value="">-</option>
  {% for u in users %}<option value="{{ u.id }}"{% if u.id == current %} selected{% endif %}>{{ u.name }}{% if not u.active %} (미사용){% endif %}</option>{% endfor %}
</select>
{% endmacro %}
<h1>항목 관리</h1>
<p><small>종이 점검표의 번호 그대로 입력하세요. 번호 순서로 자동 정렬됩니다. <b>구분</b>은 제목줄(입력 없음), <b>점검</b>은 담당자가 이상 유/무를 입력하는 줄입니다.</small></p>
<table class="items">
  <tr><th>번호</th><th>항목명</th><th>종류</th><th>담당자</th><th>사용</th><th></th></tr>
  {% for i in items %}
  <tr class="{{ 'group' if i.is_group }}{{ ' inactive' if not i.active }}">
    <td><input form="it{{ i.id }}" name="code" value="{{ i.code }}" size="7" required></td>
    <td style="padding-left: {{ 0.9 + (i.code|depth) * 1.2 }}rem"><input form="it{{ i.id }}" name="title" value="{{ i.title }}" size="34" required></td>
    <td><select form="it{{ i.id }}" name="is_group">
      <option value="1"{% if i.is_group %} selected{% endif %}>구분</option>
      <option value="0"{% if not i.is_group %} selected{% endif %}>점검</option></select></td>
    <td>{{ owner_select("it" ~ i.id, i.owner_id) }}</td>
    <td><input form="it{{ i.id }}" name="active" type="checkbox" value="1"{% if i.active %} checked{% endif %}></td>
    <td><form id="it{{ i.id }}" method="post" action="{{ url_for('update_item', item_id=i.id) }}">{{ csrf() }}<button class="small">저장</button></form></td>
  </tr>
  {% else %}
  <tr><td colspan="6">등록된 항목이 없습니다. 아래에서 추가하거나 일괄 등록하세요.</td></tr>
  {% endfor %}
</table>

<h2>한 줄 추가</h2>
<form method="post" action="{{ url_for('add_item') }}" class="toolbar">{{ csrf() }}
  <input name="code" placeholder="번호 (예: 1-1-3)" size="12" required>
  <input name="title" placeholder="항목명" size="30" required>
  <select name="is_group"><option value="0">점검</option><option value="1">구분</option></select>
  {{ owner_select(None, None) }}
  <button class="primary">추가</button>
</form>

<h2>일괄 등록</h2>
<form method="post" action="{{ url_for('bulk_items') }}" class="card">{{ csrf() }}
  <p><small>한 줄에 <code>번호 | 항목명 | 담당자ID</code>. 담당자ID를 비우면 구분(제목줄)이 됩니다. 한 줄이라도 틀리면 아무것도 등록되지 않습니다.</small></p>
  <textarea name="text" rows="8" placeholder="1 | 네트워크 및 시스템 점검&#10;1-1 | 네트워크상태&#10;1-1-1 | 백본 및 각 층 네트워크 상태 | kim"></textarea>
  <p class="actions"><button class="primary">일괄 등록</button></p>
</form>
{% endblock %}
```

`templates/admin.html` — 첫 `<h2>사용자</h2>`부터 사용자 추가 폼 `</form>`까지만 남기고, `<h2>시스템</h2>` 이하 시스템·점검 항목 부분을 삭제한다.

`templates/sheet.html`, `dashboard.html`, `history.html`, `print.html` 삭제.

- [ ] **Step 6: 통과 확인**

Run: `venv/Scripts/python -m unittest`
Expected: 전부 OK

- [ ] **Step 7: Commit** — `feat: v2 스키마(번호 계층 항목), 항목 관리·일괄 등록`

---

### Task 2: 오늘 점검 — 번호 계층 표, 이상 유/무·비고 입력, 제출, 수정

**Files:**
- Modify: `app.py` (임시 `index` 교체, `# ---- 사용자 관리` 위에 추가)
- Rewrite: `templates/index.html`
- Modify: `static/style.css` (끝에 추가)
- Test: `test_app.py`

**Interfaces:**
- Consumes: `load_items`, `code_key`, `parse_day`, `audit`, `now`, `today`
- Produces:
  - `sheet_rows(db, day) -> list[dict]` — 번호순. 점검 줄은 `result`(dict|None, `checker_name` 포함)
  - `keep_with_groups(rows, keep) -> list[dict]`
  - `result_token(res) -> str`, `row_badge(res) -> (label, css_class)` (Jinja 전역)
  - `day_info(db, day) -> Row|None` (`approved_by`, `approved_at`, `approver_name`)
  - 폼 필드: `action`(save|submit|edit), `row`(여러 개, item id), `issue_<id>`(0|1), `remark_<id>`, `base_<id>`, 수정 시 `item_id`, `reason`

- [ ] **Step 1: 실패하는 테스트 추가** (`class BackupTest` 위)

```python
def form(**rows):
    """form(r3=("0", "", ""), ...) → 폼 필드. 값은 (issue, remark, base)."""
    data = {"row": []}
    for key, (issue, remark, base) in rows.items():
        i = key[1:]
        data["row"].append(i)
        if issue is not None:
            data[f"issue_{i}"] = issue
        data[f"remark_{i}"] = remark
        data[f"base_{i}"] = base
    return data


class SheetTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")

    def test_mine_view_shows_only_my_items_with_parent_groups(self):
        t = self.text(self.c.get("/"))
        self.assertIn("백본 및 각 층 네트워크 상태", t)
        self.assertIn("네트워크상태", t)
        self.assertNotIn("시각동기화", t)
        self.assertNotIn("시스템 및 업무 서비스", t)
        self.assertIn("0/2", t)

    def test_all_view_natural_sort(self):
        t = self.text(self.c.get("/?view=all"))
        self.assertLess(t.index("시스템 및 업무 서비스"), t.index("시각동기화"))
        self.assertLess(t.index("시각동기화"), t.index("전화 및 녹취"))

    def test_submit_only_selected_rows(self):
        r = self.post("/", action="submit", **form(r3=("0", "", ""), r4=(None, "", "")))
        self.assertEqual(r.status_code, 302)
        res = self.row("SELECT * FROM results WHERE item_id = 3")
        self.assertEqual((res["status"], res["issue"], res["checker_id"], res["title"]), ("submitted", 0, 1, "백본 및 각 층 네트워크 상태"))
        self.assertIsNone(self.row("SELECT * FROM results WHERE item_id = 4"))
        t = self.text(self.c.get("/"))
        self.assertIn("1건 제출", t)
        self.assertIn("남은 내 항목 1건", t)
        self.assertEqual(self.row("SELECT action FROM audit_log")["action"], "submit")

    def test_issue_yes_requires_remark_and_keeps_input(self):
        t = self.text(self.post("/", action="submit", **form(r3=("1", "", ""), r4=("0", "메모", ""))))
        self.assertIn("비고를 입력", t)
        self.assertIn('name="issue_3" value="1" checked', t)
        self.assertIn('value="메모"', t)
        self.assertIsNone(self.row("SELECT * FROM results"))

    def test_save_draft_then_page_shows_values(self):
        self.post("/", action="save", **form(r3=("1", "점검중", "")))
        self.assertEqual(self.row("SELECT status FROM results")["status"], "draft")
        t = self.text(self.c.get("/"))
        self.assertIn('name="issue_3" value="1" checked', t)
        self.assertIn('value="점검중"', t)
        self.assertIn('name="base_3" value="draft:1"', t)

    def test_submitted_row_is_locked(self):
        self.post("/", action="submit", **form(r3=("0", "", "")))
        t = self.text(self.c.get("/"))
        self.assertNotIn('name="issue_3"', t)
        self.assertIn('name="issue_4"', t)

    def test_only_today_can_be_written(self):
        t = self.text(self.post("/?day=2026-09-22", action="save", **form(r3=("0", "", ""))))
        self.assertIn("오늘 점검만", t)
        self.assertIsNone(self.row("SELECT * FROM results"))

    def test_stale_page_rejected(self):
        self.post("/", action="save", **form(r3=("0", "m1", "")))
        other = appmod.app.test_client()
        self.login("m2", client=other)
        t = self.text(self.post("/", client=other, action="save", **form(r3=("1", "m2", ""))))
        self.assertIn("먼저 입력", t)
        self.assertEqual(self.row("SELECT remark FROM results")["remark"], "m1")
        self.post("/", client=other, action="submit", **form(r3=("0", "대무", "draft:1")))
        self.assertEqual(self.row("SELECT checker_id FROM results")["checker_id"], 2)

    def test_proxy_entry_shows_checker(self):
        other = appmod.app.test_client()
        self.login("m2", client=other)
        self.post("/", client=other, action="submit", **form(r3=("0", "", "")))
        self.assertIn("점검: M2", self.text(self.c.get("/")))

    def test_snapshot_kept_after_rename(self):
        self.post("/", action="submit", **form(r3=("0", "", "")))
        self.exec("UPDATE items SET title = '바뀐 문구' WHERE id = 3")
        t = self.text(self.c.get("/"))
        self.assertIn("백본 및 각 층 네트워크 상태", t)
        self.assertNotIn("바뀐 문구", t)

    def test_edit_requires_reason_and_logs(self):
        self.post("/", action="submit", **form(r3=("0", "", "")))
        self.assertIn('name="reason"', self.text(self.c.get("/?edit=3")))
        t = self.text(self.post("/", action="edit", item_id="3", **form(r3=("1", "재부팅", "submitted:1"))))
        self.assertIn("수정 사유", t)
        self.assertEqual(self.row("SELECT issue FROM results")["issue"], 0)
        self.post("/", action="edit", item_id="3", reason="확인 누락", **form(r3=("1", "재부팅", "submitted:1")))
        res = self.row("SELECT * FROM results")
        self.assertEqual((res["issue"], res["remark"], res["checker_id"]), (1, "재부팅", 1))
        a = self.row("SELECT * FROM audit_log WHERE action = 'edit'")
        self.assertEqual((json.loads(a["before"])["issue"], json.loads(a["after"])["issue"], a["reason"]), (0, 1, "확인 누락"))

    def test_inactive_item_hidden_but_record_kept(self):
        self.post("/", action="submit", **form(r3=("0", "", "")))
        self.exec("UPDATE items SET active = 0 WHERE id IN (3, 4)")
        t = self.text(self.c.get("/"))
        self.assertIn("백본 및 각 층 네트워크 상태", t)
        self.assertNotIn("인터넷 방화벽", t)
        appmod.app.config["TODAY"] = "2026-09-24"
        self.assertNotIn("백본 및 각 층 네트워크 상태", self.text(self.c.get("/")))

    def test_bad_date_rejected(self):
        self.assertEqual(self.c.get("/?day=abc").status_code, 400)
```

- [ ] **Step 2: 실패 확인**

Run: `venv/Scripts/python -m unittest`
Expected: SheetTest FAIL/ERROR

- [ ] **Step 3: 구현** — 임시 `index` 라우트를 지우고 아래를 `# ---- 사용자 관리` 위에 넣는다

```python
# ---------------------------------------------------------------- 오늘 점검


def result_token(res):
    """화면을 연 시점의 줄 상태. 저장할 때 달라졌으면 다른 사람이 먼저 손댄 것."""
    return f"{res['status']}:{res['checker_id']}" if res else ""


def row_badge(res):
    if res is None:
        return "미입력", "st-none"
    if res["status"] == "draft":
        return "작성중", "st-draft"
    return ("이상 유", "st-issue") if res["issue"] else ("이상 무", "st-approved")


app.jinja_env.globals.update(result_token=result_token, row_badge=row_badge)


def sheet_rows(db, day):
    """그날 표의 줄(번호순). 점검 줄에는 'result'를 붙이고, 제출된 줄은 제출 당시 번호·항목명·담당자를 보여준다.
    미사용 항목은 그날 기록이 있을 때만 나온다."""
    results = {r["item_id"]: dict(r) for r in db.execute(
        "SELECT r.*, u.name AS checker_name FROM results r JOIN users u ON u.id = r.checker_id WHERE r.date = ?",
        (day,))}
    rows = []
    for it in load_items(db, active_only=False):
        res = results.get(it["id"])
        if it["is_group"]:
            if it["active"]:
                rows.append(it)
            continue
        if not it["active"] and res is None:
            continue
        it["result"] = res
        if res and res["status"] == "submitted":
            it.update(code=res["code"], title=res["title"], owner_name=res["owner_name"])
        rows.append(it)
    rows.sort(key=lambda r: code_key(r["code"]))
    return rows


def keep_with_groups(rows, keep):
    """keep()을 통과한 점검 줄과, 그 줄들의 상위 구분 줄만 남긴다."""
    codes = [r["code"] for r in rows if not r["is_group"] and keep(r)]
    return [r for r in rows
            if (not r["is_group"] and keep(r))
            or (r["is_group"] and any(c.startswith(r["code"] + "-") for c in codes))]


def submitted(r):
    return bool(r.get("result") and r["result"]["status"] == "submitted")


def day_info(db, day):
    return db.execute("""SELECT d.*, u.name AS approver_name FROM days d
                         LEFT JOIN users u ON u.id = d.approved_by WHERE d.date = ?""", (day,)).fetchone()


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
            return "다른 사람이 먼저 입력한 항목이 있습니다. 새로고침해서 확인한 뒤 다시 저장하세요.", None
        if res and res["status"] == "submitted":
            continue
        issue = posted_issue(it["id"])
        remark = request.form.get(f"remark_{it['id']}", "").strip()[:500]
        if issue is None and not remark and res is None:
            continue
        submit = action == "submit" and issue is not None
        if submit and issue == 1 and not remark:
            return f"{it['code']} 항목: 이상 '유'는 비고를 입력해야 합니다.", None
        changes.append((it, res, issue, remark, submit))
    if action == "submit" and not any(c[4] for c in changes):
        return "제출할 항목이 없습니다. 이상 유/무를 선택하세요.", None
    try:
        for it, res, issue, remark, submit in changes:
            vals = {"date": day, "item_id": it["id"], "code": it["code"], "title": it["title"],
                    "owner_name": it["owner_name"], "checker_id": g.user["id"], "issue": issue, "remark": remark,
                    "status": "submitted" if submit else "draft", "submitted_at": now() if submit else None}
            if res is None:
                db.execute("""INSERT INTO results(date, item_id, code, title, owner_name, checker_id, issue, remark,
                              status, submitted_at) VALUES(:date, :item_id, :code, :title, :owner_name, :checker_id,
                              :issue, :remark, :status, :submitted_at)""", vals)
            else:
                db.execute("""UPDATE results SET code = :code, title = :title, owner_name = :owner_name,
                              checker_id = :checker_id, issue = :issue, remark = :remark, status = :status,
                              submitted_at = :submitted_at WHERE date = :date AND item_id = :item_id""", vals)
            if submit:
                audit(db, g.user["id"], "result", it["id"], "submit",
                      after={"date": day, "code": it["code"], "issue": issue, "remark": remark})
    except sqlite3.IntegrityError:
        db.rollback()
        return "다른 사람이 먼저 입력한 항목이 있습니다. 새로고침해서 확인한 뒤 다시 저장하세요.", None
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
          before={"date": day, "code": res["code"], "issue": res["issue"], "remark": res["remark"]},
          after={"date": day, "code": res["code"], "issue": issue, "remark": remark}, reason=reason)
    d = day_info(db, day)
    if d and d["approved_by"]:
        db.execute("UPDATE days SET approved_by = NULL, approved_at = NULL WHERE date = ?", (day,))
        audit(db, g.user["id"], "day", None, "unapprove",
              before={"date": day, "approved_by": d["approved_by"]}, reason="항목 수정으로 확인 해제")
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
        if error is None:
            flash(msg)
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
```

- [ ] **Step 4: `templates/index.html` 전면 교체**

```html
{% extends "base.html" %}
{% block content %}
{% set writable = day == today and not editing %}
<h1>{% if day == today %}오늘 점검{% else %}점검 기록{% endif %} <small>{{ day }}</small></h1>
<div class="toolbar no-print">
  <span>내 항목 <b>{{ my_done }}/{{ my_total }}</b> 제출</span>
  {% if day_row and day_row.approved_at %}<span class="badge st-approved">확인완료 · {{ day_row.approver_name }} {{ day_row.approved_at|dt }}</span>{% endif %}
  <span class="spacer"></span>
  <a class="tab{% if view == 'mine' %} on{% endif %}" href="{{ url_for('index', day=day, view='mine') }}">내 항목</a>
  <a class="tab{% if view == 'all' %} on{% endif %}" href="{{ url_for('index', day=day, view='all') }}">전체</a>
</div>
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post" action="{{ url_for('index', day=day, view=view) }}">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  {% if editing %}<input type="hidden" name="item_id" value="{{ editing }}">{% endif %}
  <table class="sheet">
    <tr><th>번호</th><th>점검 항목</th><th>담당자</th><th>이상</th><th>비고</th><th>상태</th></tr>
    {% for r in rows %}
    {% if r.is_group %}
    <tr class="group"><td class="code">{{ r.code }}</td><td colspan="5" style="padding-left: {{ 0.9 + (r.code|depth) * 1.2 }}rem">{{ r.title }}</td></tr>
    {% else %}
    {% set res = r.result %}
    {% set open = (writable and not (res and res.status == 'submitted')) or editing == r.id %}
    <tr>
      <td class="code">{{ r.code }}</td>
      <td style="padding-left: {{ 0.9 + (r.code|depth) * 1.2 }}rem">{{ r.title }}</td>
      <td>{{ r.owner_name or '-' }}{% if res and res.checker_id != r.owner_id %}<br><small>점검: {{ res.checker_name }}</small>{% endif %}</td>
      {% if open %}
      <td class="issue-pick">
        <input type="hidden" name="row" value="{{ r.id }}">
        <input type="hidden" name="base_{{ r.id }}" value="{{ result_token(res) }}">
        <label><input type="radio" name="issue_{{ r.id }}" value="0"{% if r.draft_issue == 0 %} checked{% endif %}> 무</label>
        <label class="yes"><input type="radio" name="issue_{{ r.id }}" value="1"{% if r.draft_issue == 1 %} checked{% endif %}> 유</label>
      </td>
      <td><input name="remark_{{ r.id }}" value="{{ r.draft_remark }}" maxlength="500" class="remark-input">
        {% if editing == r.id %}<input name="reason" placeholder="수정 사유 (필수)" class="remark-input reason">{% endif %}</td>
      {% else %}
      <td>{% if res and res.issue is not none %}<b class="{{ 'issue' if res.issue }}">{{ '유' if res.issue else '무' }}</b>{% else %}-{% endif %}</td>
      <td class="remark">{{ res.remark if res else '' }}</td>
      {% endif %}
      {% set label, cls = row_badge(res) %}
      <td class="nowrap"><span class="badge {{ cls }}">{{ label }}</span>
        {% if res and res.status == 'submitted' and not editing %} <a class="no-print" href="{{ url_for('index', day=day, view=view, edit=r.id) }}">수정</a>{% endif %}</td>
    </tr>
    {% endif %}
    {% else %}
    <tr><td colspan="6">{% if view == 'mine' %}내 담당 항목이 없습니다. '전체'를 눌러 보세요.{% else %}등록된 점검 항목이 없습니다. '항목 관리'에서 추가하세요.{% endif %}</td></tr>
    {% endfor %}
  </table>
  {% if editing %}
  <p class="actions"><button name="action" value="edit" class="primary">수정 저장</button>
    <a href="{{ url_for('index', day=day, view=view) }}">취소</a></p>
  {% elif writable and rows %}
  <p class="actions"><button name="action" value="save">임시저장</button>
    <button name="action" value="submit" class="primary">제출</button>
    <small>이상 유/무를 고른 항목만 제출됩니다. '유'는 비고가 필요합니다.</small></p>
  {% endif %}
</form>
{% endblock %}
```

- [ ] **Step 5: `static/style.css` 끝(`/* 인쇄` 위)에 추가**

```css
/* 계층 점검표 */
.sheet td.code, .items td.code { color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; width: 4.5rem; }
.sheet tr.group td, .items tr.group td { background: #eef2f9; color: var(--navy); font-weight: bold; }
.items tr.inactive td { opacity: .5; }
.issue-pick { white-space: nowrap; }
.issue-pick label { margin-right: .6rem; cursor: pointer; }
.issue-pick label.yes { color: var(--red); }
.issue-pick input { accent-color: var(--navy); width: 1.1rem; height: 1.1rem; vertical-align: -2px; }
.remark-input { width: 100%; min-width: 10rem; }
.remark-input.reason { margin-top: .4rem; border-color: var(--yellow); }
.nowrap { white-space: nowrap; }
.toolbar .spacer { flex: 1; }
.tab { padding: .3rem .9rem; border-radius: 999px; color: var(--muted); border: 1px solid var(--line); }
.tab.on { background: var(--navy); color: #fff; border-color: var(--navy); }
.tab:hover { text-decoration: none; }
```

- [ ] **Step 6: 통과 확인** — `venv/Scripts/python -m unittest` → 전부 OK
- [ ] **Step 7: Commit** — `feat: 오늘 점검 — 번호 계층 표, 이상 유/무·비고, 제출·수정`

---

### Task 3: 현황판과 하루치 확인

**Files:**
- Modify: `app.py` (`# ---- 사용자 관리` 위에 추가)
- Create: `templates/dashboard.html`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `sheet_rows`, `submitted`, `day_info`, `parse_day`
- Produces: `day_status(db, day) -> dict(rows, total, missing, issues, owners, checkers, approved)`, 엔드포인트 `dashboard`(GET /dashboard?day=), `approve_day`(POST /approve/<day>)

- [ ] **Step 1: 실패하는 테스트 추가** (`class BackupTest` 위)

```python
class DashboardTest(Base):
    def submit_all(self):
        self.login("m1")
        self.post("/", action="submit", **form(r3=("0", "", ""), r4=("0", "", "")))
        m2 = appmod.app.test_client()
        self.login("m2", client=m2)
        self.post("/", client=m2, action="submit", **form(r6=("1", "NTP 3초 지연", ""), r7=("0", "", "")))

    def lead(self):
        c = appmod.app.test_client()
        self.login("lead", client=c)
        return c

    def test_member_forbidden(self):
        self.login("m1")
        self.assertEqual(self.c.get("/dashboard").status_code, 403)

    def test_dashboard_summary(self):
        self.login("m1")
        self.post("/", action="submit", **form(r3=("0", "", "")))
        t = self.text(self.lead().get("/dashboard"))
        self.assertIn("미제출 <b>3</b>", t)
        self.assertIn("M1", t)
        self.assertIn("1/2", t)

    def test_approve_requires_all_submitted(self):
        self.login("m1")
        self.post("/", action="submit", **form(r3=("0", "", "")))
        lead = self.lead()
        self.post(f"/approve/{DAY}", client=lead)
        self.assertIsNone(self.row("SELECT * FROM days"))
        self.assertIn("모든 점검 항목", self.text(lead.get("/dashboard")))

    def test_approve_day_and_issue_list(self):
        self.submit_all()
        lead = self.lead()
        t = self.text(lead.get("/dashboard"))
        self.assertIn("이상 <b>1</b>", t)
        self.assertIn("NTP 3초 지연", t)
        self.post(f"/approve/{DAY}", client=lead)
        self.assertEqual(self.row("SELECT approved_by FROM days")["approved_by"], 3)
        self.assertEqual(self.row("SELECT action FROM audit_log WHERE target = 'day'")["action"], "approve")
        self.assertIn("확인완료", self.text(lead.get("/dashboard")))

    def test_cannot_approve_day_with_own_submission(self):
        self.login("m1")
        self.post("/", action="submit", **form(r3=("0", "", ""), r4=("0", "", "")))
        lead = self.lead()
        self.post("/", client=lead, action="submit", **form(r6=("0", "", ""), r7=("0", "", "")))
        self.post(f"/approve/{DAY}", client=lead)
        self.assertIsNone(self.row("SELECT * FROM days"))

    def test_edit_after_approval_unapproves(self):
        self.submit_all()
        self.post(f"/approve/{DAY}", client=self.lead())
        self.post("/", action="edit", item_id="3", reason="정정", **form(r3=("1", "재부팅", "submitted:1")))
        self.assertIsNone(self.row("SELECT approved_by FROM days")["approved_by"])
```

- [ ] **Step 2: 실패 확인** — DashboardTest FAIL (404)

- [ ] **Step 3: 구현**

```python
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
        "checkers": {r["result"]["checker_id"] for r in rows if submitted(r)},
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
        flash("본인이 제출한 항목이 있는 날은 확인할 수 없습니다. 다른 팀장이나 관리자가 확인해야 합니다.")
    else:
        db.execute("""INSERT INTO days(date, approved_by, approved_at) VALUES(?, ?, ?)
                      ON CONFLICT(date) DO UPDATE SET approved_by = excluded.approved_by,
                      approved_at = excluded.approved_at""", (day, g.user["id"], now()))
        audit(db, g.user["id"], "day", None, "approve",
              after={"date": day, "total": s["total"], "issues": len(s["issues"])})
        db.commit()
        flash(f"{day} 점검을 확인했습니다.")
    return redirect(url_for("dashboard", day=day))
```

- [ ] **Step 4: `templates/dashboard.html`**

```html
{% extends "base.html" %}
{% block content %}
<h1>현황판 <small>{{ day }}</small></h1>
<form method="get" class="no-print toolbar">
  <label>날짜 <input type="date" name="day" value="{{ day }}"></label> <button class="primary">조회</button>
  <span class="spacer"></span>
  <a href="{{ url_for('index', day=day, view='all') }}">그날 전체 표 보기</a>
</form>
<div class="tiles">
  <div class="tile">전체 점검항목 <b>{{ s.total }}</b></div>
  <div class="tile t-missing">미제출 <b>{{ s.missing|length }}</b></div>
  <div class="tile t-issue">이상 <b>{{ s.issues|length }}</b></div>
  <div class="tile t-waiting">확인 {% if s.approved and s.approved.approved_at %}<b>완료</b>{% else %}<b>대기</b>{% endif %}</div>
</div>

<div class="card">
  {% if s.approved and s.approved.approved_at %}
    <span class="badge st-approved">확인완료</span> {{ s.approved.approver_name }} · {{ s.approved.approved_at|dt }}
  {% elif s.total and not s.missing %}
    <form method="post" action="{{ url_for('approve_day', day=day) }}" class="inline">
      <input type="hidden" name="csrf" value="{{ csrf_token() }}">
      <button class="primary">{{ day }} 점검 확인</button>
    </form> <small>모든 항목이 제출되었습니다.</small>
  {% else %}
    <small>모든 점검 항목이 제출되면 확인할 수 있습니다. (미제출 {{ s.missing|length }}건)</small>
  {% endif %}
</div>

<h2>담당자별 진행</h2>
<table>
  <tr><th>담당자</th><th>제출</th></tr>
  {% for name, done, total in s.owners %}
  <tr><td>{{ name }}</td><td><span class="badge {{ 'st-approved' if done == total else 'st-draft' }}">{{ done }}/{{ total }}</span></td></tr>
  {% endfor %}
</table>

{% if s.issues %}
<h2>이상 항목</h2>
<table>
  <tr><th>번호</th><th>항목</th><th>담당자</th><th>점검자</th><th>비고</th></tr>
  {% for r in s.issues %}
  <tr><td class="code">{{ r.code }}</td><td>{{ r.title }}</td><td>{{ r.owner_name }}</td><td>{{ r.result.checker_name }}</td><td class="remark issue">{{ r.result.remark }}</td></tr>
  {% endfor %}
</table>
{% endif %}

{% if s.missing %}
<h2>미제출 항목</h2>
<table>
  <tr><th>번호</th><th>항목</th><th>담당자</th><th>상태</th></tr>
  {% for r in s.missing %}
  {% set label, cls = row_badge(r.result) %}
  <tr><td class="code">{{ r.code }}</td><td>{{ r.title }}</td><td>{{ r.owner_name or '-' }}</td><td><span class="badge {{ cls }}">{{ label }}</span></td></tr>
  {% endfor %}
</table>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: 통과 확인** — 전부 OK
- [ ] **Step 6: Commit** — `feat: 현황판과 하루치 확인`

---

### Task 4: 이력 조회, 인쇄/PDF, CSV

**Files:**
- Modify: `app.py` (`# ---- 사용자 관리` 위에 추가)
- Create: `templates/history.html`, `templates/print.html`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `sheet_rows`, `day_status`, `keep_with_groups`, `fmt_dt`
- Produces: `csv_cell(v) -> str`, 엔드포인트 `history`(GET /history?start=&end=&format=print|csv)

- [ ] **Step 1: 실패하는 테스트 추가** (`class BackupTest` 위)

```python
class HistoryTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")
        self.post("/", action="submit", **form(r3=("1", '=HYPERLINK("http://x")', ""), r4=("0", "", "")))

    def test_history_summary(self):
        t = self.text(self.c.get(f"/history?start={DAY}"))
        self.assertIn(DAY, t)
        self.assertIn("2/4", t)

    def test_csv_escapes_formula_and_has_bom(self):
        r = self.c.get(f"/history?start={DAY}&format=csv")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        t = self.text(r)
        self.assertTrue(t.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(t.lstrip("\ufeff"))))
        self.assertEqual(len(rows), 5)  # 머리글 + 점검 항목 4개
        self.assertEqual(rows[1][1], "1-1-1")
        self.assertEqual(rows[1][6], "'=HYPERLINK(\"http://x\")")
        self.assertEqual(rows[3][7], "미입력")

    def test_print_view_like_paper(self):
        t = self.text(self.c.get(f"/history?start={DAY}&format=print"))
        self.assertIn("네트워크상태", t)
        self.assertIn("백본 및 각 층 네트워크 상태", t)
        self.assertIn("확인자", t)

    def test_reversed_range_still_works(self):
        self.assertIn("2/4", self.text(self.c.get(f"/history?start=2026-09-24&end={DAY}")))
```

- [ ] **Step 2: 실패 확인** — HistoryTest FAIL

- [ ] **Step 3: 구현**

```python
# ---------------------------------------------------------------- 이력·출력

CSV_HEADER = ["날짜", "번호", "점검항목", "담당자", "점검자", "이상", "비고", "상태", "제출시각", "확인자", "확인시각"]


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
                    d["date"], r["code"], r["title"], r["owner_name"],
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
```

- [ ] **Step 4: 템플릿**

`templates/history.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>이력 조회</h1>
<form method="get" class="no-print toolbar">
  <label>시작 <input type="date" name="start" value="{{ start }}"></label>
  <label>종료 <input type="date" name="end" value="{{ end }}"></label>
  <button class="primary">조회</button>
  <button name="format" value="print">인쇄/PDF</button>
  <button name="format" value="csv">엑셀(CSV)</button>
</form>
<table>
  <tr><th>날짜</th><th>제출</th><th>이상</th><th>확인</th></tr>
  {% for d in days %}
  <tr>
    <td><a href="{{ url_for('index', day=d.date, view='all') }}">{{ d.date }}</a></td>
    <td><span class="badge {{ 'st-approved' if not d.missing else 'st-draft' }}">{{ d.total - d.missing|length }}/{{ d.total }}</span></td>
    <td>{% if d.issues %}<span class="badge st-issue">{{ d.issues|length }}건</span>{% else %}-{% endif %}</td>
    <td>{% if d.approved and d.approved.approved_at %}{{ d.approved.approver_name }} · {{ d.approved.approved_at|dt }}{% else %}<span class="badge st-none">대기</span>{% endif %}</td>
  </tr>
  {% else %}
  <tr><td colspan="4">기록이 없습니다.</td></tr>
  {% endfor %}
</table>
{% endblock %}
```

`templates/print.html`:
```html
{% extends "base.html" %}
{% block title %}점검 기록 {{ start }} ~ {{ end }}{% endblock %}
{% block content %}
<p class="no-print"><button onclick="window.print()" class="primary">인쇄 / PDF로 저장</button>
  <small>인쇄 창에서 프린터를 "PDF로 저장"으로 고르면 PDF 파일이 됩니다.</small></p>
{% for d in days %}
<section class="page">
  <h2>일일 시스템 점검표 — {{ d.date }}</h2>
  <table class="sheet">
    <tr><th>번호</th><th>점검 항목</th><th>담당자</th><th>점검자</th><th>이상</th><th>비고</th></tr>
    {% for r in d.sheet %}
    {% if r.is_group %}
    <tr class="group"><td class="code">{{ r.code }}</td><td colspan="5">{{ r.title }}</td></tr>
    {% else %}
    {% set res = r.result if (r.result and r.result.status == 'submitted') else None %}
    <tr><td class="code">{{ r.code }}</td><td style="padding-left: {{ 0.9 + (r.code|depth) * 1.2 }}rem">{{ r.title }}</td>
      <td>{{ r.owner_name or '-' }}</td><td>{{ res.checker_name if res else '' }}</td>
      <td>{% if res %}{{ '유' if res.issue else '무' }}{% else %}미제출{% endif %}</td>
      <td class="remark">{{ res.remark if res else '' }}</td></tr>
    {% endif %}
    {% endfor %}
  </table>
  <table class="sign">
    <tr><th>확인자</th><td>{{ d.approved.approver_name if d.approved and d.approved.approved_at else '' }}</td>
        <th>확인 시각</th><td>{{ d.approved.approved_at|dt if d.approved and d.approved.approved_at else '' }}</td></tr>
  </table>
</section>
{% else %}
<p>기록이 없습니다.</p>
{% endfor %}
{% endblock %}
```

- [ ] **Step 5: 통과 확인** — 전부 OK
- [ ] **Step 6: Commit** — `feat: v2 이력 조회, 인쇄/PDF, CSV`

---

### Task 5: 문서 갱신과 화면 확인

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README "처음 쓸 때" 교체**

```markdown
## 처음 쓸 때

1. 관리자로 로그인 → 비밀번호 변경.
2. 관리 화면에서 부서원 계정 추가(임시 비밀번호 전달).
3. 항목 관리 → 일괄 등록에 종이 점검표를 한 줄씩 붙여넣는다.
   ```
   1 | 네트워크 및 시스템 점검
   1-1 | 네트워크상태
   1-1-1 | 백본 및 각 층 네트워크 상태 | kim
   ```
   담당자ID를 비우면 제목줄(구분)이 된다.

> 이전 버전으로 만든 `checklist.db`가 있으면 실행 시 안내가 나온다. 기록이 없다면 파일을 지우고 다시 실행하면 된다.
```

- [ ] **Step 2: 화면 확인** — 샘플 데이터로 오늘 점검(내 항목/전체/오류), 현황판, 항목 관리, 인쇄 화면을 헤드리스 Edge로 스크린샷 찍어 확인
- [ ] **Step 3: 전체 테스트** — `venv/Scripts/python -m unittest` → 전부 OK
- [ ] **Step 4: Commit** — `docs: v2 사용법`
