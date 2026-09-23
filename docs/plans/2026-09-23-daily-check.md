# 일일 시스템 점검 웹 체크리스트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 종이 점검표를 대체하는 사내용 웹 체크리스트(점검·제출·수정 이력·팀장 확인·출력)를 만든다.

**Architecture:** Flask 앱 하나(`app.py`)가 모든 화면을 서버에서 렌더링하고, 데이터는 SQLite 파일 하나(`checklist.db`)에 저장한다. `db.py`는 스키마·연결·감사로그 기록만 맡는다. 운영은 waitress로 띄운다.

**Tech Stack:** Python 3.10+, Flask 3.x, waitress 3.x, 표준 `sqlite3`/`csv`/`unittest`, Jinja2 템플릿, 순수 CSS.

**Spec:** `docs/specs/2026-09-23-daily-check-design.md`

## Global Constraints

- 외부 패키지는 `flask`, `waitress` 두 개뿐. 그 외 추가 금지(JS 라이브러리·CSS 프레임워크 포함).
- 모든 SQL은 `?` 또는 `:name` 파라미터 바인딩. 문자열 포맷으로 SQL 조립 금지.
- 삭제 기능 없음. 사용자·시스템·항목은 `active = 0` 처리만.
- `audit_log`는 추가만 한다(DB 트리거로 UPDATE/DELETE 차단).
- 모든 POST 폼에 `<input type="hidden" name="csrf" value="{{ csrf_token() }}">`.
- 화면 문구는 한국어.
- 시각은 서버 시간, `YYYY-MM-DDTHH:MM:SS` 문자열. 날짜는 `YYYY-MM-DD`.
- 테스트는 `python -m unittest -v` 로 전부 실행된다(파일 `test_app.py` 하나).

## Review Focus

- URL의 날짜를 바꿔 과거나 미래의 점검표를 새로 작성하려 하면 거부해야 한다(Task 3 `test_cannot_write_other_day`).
- 두 사람이 같은 시스템, 같은 날짜로 제출하면 먼저 제출한 기록이 유지되고 나중 사람은 안내를 받아야 한다(Task 3 `test_second_submit_rejected`).
- 비고에 `=`, `+`, `-`, `@`로 시작하는 문자열이 있으면 CSV를 엑셀에서 열 때 수식으로 실행되면 안 된다(Task 6 `test_csv_escapes_formula_and_has_bom`).
- 관리자가 미사용 처리한 사용자는 이미 로그인해 있던 세션도 즉시 차단되어야 한다(Task 7 `test_deactivated_user_session_is_blocked`).
- 항목이 하나도 없는 시스템은 제출하지 못하고 안내를 받아야 한다(Task 3 `test_system_without_items_cannot_submit`).

## 파일 구조

```
requirements.txt        # flask, waitress
.gitignore
db.py                   # 스키마, 연결, now(), audit()
app.py                  # 설정, 인증, 모든 라우트, CLI(init-admin, backup, 서버 실행)
templates/
  base.html             # 공통 레이아웃, 메뉴
  login.html, password.html
  index.html            # 오늘 점검 목록
  sheet.html            # 점검표(작성/조회/수정)
  dashboard.html        # 팀장 현황판
  history.html, print.html
  admin.html
static/style.css        # 화면 + 인쇄용 스타일
test_app.py             # 전체 테스트
README.md               # 설치·운영 방법
```

## 설계서 대비 세부 결정

- `check_items`에 `item_id` 칼럼을 추가해 작성 중인 점검표의 체크 값을 항목과 짝지을 수 있게 한다.
- `users`에 `must_change_pw` 칼럼을 추가한다. 최초 관리자 계정과 비밀번호를 초기화한 계정은 첫 로그인 때 비밀번호를 바꿔야 한다.
- 제출 전(미점검, 작성중) 점검표는 항상 현재 사용 중인 항목을 보여준다. 제출하는 순간 항목 문구와 체크 값을 `check_items`에 고정한다. 따라서 오늘 추가한 항목은 아직 제출하지 않은 오늘 점검표에 바로 나타난다.
- `has_issue`는 제출할 때만 계산해 저장하고, 작성중에는 0으로 둔다.

---

### Task 1: 프로젝트 뼈대와 DB 스키마

**Files:**
- Create: `requirements.txt`, `.gitignore`, `db.py`, `app.py`, `test_app.py`

**Interfaces:**
- Produces:
  - `db.init_db(path: str) -> None` — 스키마 생성(여러 번 호출해도 안전)
  - `db.get_db() -> sqlite3.Connection` — 요청마다 1개, `row_factory = sqlite3.Row`
  - `db.close_db(e=None) -> None`
  - `db.now() -> str` — `"2026-09-23T08:31:05"`
  - `db.audit(db, user_id, target, target_id, action, before=None, after=None, reason=None) -> None` — commit 하지 않음
  - `app.app` (Flask 객체), `app.today() -> str` — `app.config["TODAY"]`가 있으면 그 값(테스트용)
  - 테스트 베이스 클래스 `Base`(아래 코드)

- [ ] **Step 1: git 초기화와 기본 파일 작성**

```bash
git init
```

`requirements.txt`:
```
flask>=3.0,<4
waitress>=3.0,<4
```

`.gitignore`:
```
__pycache__/
venv/
wheels/
backup/
checklist.db
secret.key
```

```bash
python -m venv venv
venv/Scripts/pip install -r requirements.txt   # 리눅스: venv/bin/pip
```

- [ ] **Step 2: 실패하는 테스트 작성** — `test_app.py`

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
URL = f"/check/1/{DAY}"


class Base(unittest.TestCase):
    """임시 DB + 사용자 4명(m1, m2 부서원 / lead 팀장 / adm 관리자) + 시스템 2개."""

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
            con.execute("INSERT INTO systems(id, name, owner_id, sort) VALUES(1, '주문서버', 1, 1)")
            con.execute("INSERT INTO systems(id, name, owner_id, sort) VALUES(2, '빈시스템', 2, 2)")
            con.executemany(
                "INSERT INTO items(id, system_id, text, sort) VALUES(?, 1, ?, ?)",
                [(1, "프로세스 기동 확인", 1), (2, "배치 완료 확인", 2)],
            )
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
        with con:
            con.execute(sql, args)
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

    def test_one_check_per_system_per_day(self):
        self.exec("INSERT INTO checks(date, system_id, user_id, status) VALUES(?, 1, 1, 'draft')", DAY)
        with self.assertRaises(sqlite3.IntegrityError):
            self.exec("INSERT INTO checks(date, system_id, user_id, status) VALUES(?, 1, 2, 'draft')", DAY)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 실패 확인**

Run: `python -m unittest -v`
Expected: `ModuleNotFoundError: No module named 'app'`

- [ ] **Step 4: `db.py` 작성**

```python
import json
import sqlite3
from datetime import datetime

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  login_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('member', 'leader', 'admin')),
  fail_count INTEGER NOT NULL DEFAULT 0,
  must_change_pw INTEGER NOT NULL DEFAULT 1,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS systems(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  owner_id INTEGER REFERENCES users(id),
  sort INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  system_id INTEGER NOT NULL REFERENCES systems(id),
  text TEXT NOT NULL,
  sort INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS checks(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  system_id INTEGER NOT NULL REFERENCES systems(id),
  user_id INTEGER NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK(status IN ('draft', 'submitted', 'approved')),
  remark TEXT NOT NULL DEFAULT '',
  has_issue INTEGER NOT NULL DEFAULT 0,
  submitted_at TEXT,
  approved_by INTEGER REFERENCES users(id),
  approved_at TEXT,
  UNIQUE(date, system_id));
CREATE TABLE IF NOT EXISTS check_items(
  check_id INTEGER NOT NULL REFERENCES checks(id),
  item_id INTEGER REFERENCES items(id),
  item_text TEXT NOT NULL,
  checked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  user_id INTEGER,
  target TEXT NOT NULL,
  target_id INTEGER,
  action TEXT NOT NULL,
  before TEXT,
  after TEXT,
  reason TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def init_db(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.close()


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now():
    return datetime.now().isoformat(timespec="seconds")


def _json(v):
    return None if v is None else json.dumps(v, ensure_ascii=False)


def audit(db, user_id, target, target_id, action, before=None, after=None, reason=None):
    db.execute(
        "INSERT INTO audit_log(at, user_id, target, target_id, action, before, after, reason)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (now(), user_id, target, target_id, action, _json(before), _json(after), reason),
    )
```

- [ ] **Step 5: `app.py` 뼈대 작성** (이후 작업에서 쓸 import를 미리 모두 넣는다)

```python
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
```

- [ ] **Step 6: 통과 확인**

Run: `python -m unittest -v`
Expected: 2 tests OK

- [ ] **Step 7: Commit**

```bash
git add .
git commit -m "feat: 프로젝트 뼈대와 DB 스키마"
```

---

### Task 2: 로그인, CSRF, 비밀번호 변경, 최초 관리자 생성

**Files:**
- Modify: `app.py` (끝에 추가)
- Create: `templates/base.html`, `templates/login.html`, `templates/password.html`, `templates/index.html`(임시), `static/style.css`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `get_db`, `audit`, `today`
- Produces:
  - `login_required(role="member")` 데코레이터 — 사용법 `@login_required()`, `@login_required("leader")`, `@login_required("admin")`
  - `g.user` — 로그인한 사용자 `sqlite3.Row` 또는 `None`(비활성 사용자는 `None`)
  - Jinja 전역 `csrf_token()`
  - 엔드포인트 `login`, `logout`, `change_password`, `index`
  - `ROLE_NAMES = {"member": "부서원", "leader": "팀장", "admin": "관리자"}`, `MAX_FAIL = 5`
  - `main(argv)` — `init-admin`, 그 외는 서버 실행

- [ ] **Step 1: 실패하는 테스트 추가** (`test_app.py`의 `if __name__` 위)

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: AuthTest 전부 FAIL/ERROR (404 등)

- [ ] **Step 3: `app.py`에 인증 코드 추가**

```python
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


@app.route("/")
@login_required()
def index():
    return render_template("index.html")  # Task 3에서 교체


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
    else:
        from waitress import serve
        host = os.environ.get("CHECKLIST_HOST", "0.0.0.0")
        port = int(os.environ.get("CHECKLIST_PORT", "8080"))
        print(f"http://{host}:{port} 에서 실행 중")
        serve(app, host=host, port=port)


if __name__ == "__main__":
    main(sys.argv[1:])
```

> 이후 작업에서 추가하는 코드는 모두 `def main(argv):` **바로 위**에 넣는다.

- [ ] **Step 4: 템플릿과 CSS 작성**

`templates/base.html` (메뉴 링크는 아직 없는 화면도 있으므로 `url_for` 대신 경로 문자열 사용):
```html
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}일일 시스템 점검{% endblock %}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
</head>
<body>
{% if g.user %}
<nav class="no-print">
  <a href="/">오늘 점검</a>
  {% if g.user.role in ('leader', 'admin') %}<a href="/dashboard">현황판</a>{% endif %}
  <a href="/history">이력 조회</a>
  {% if g.user.role == 'admin' %}<a href="/admin">관리</a>{% endif %}
  <span class="spacer"></span>
  <span>{{ g.user.name }}</span>
  <a href="/password">비밀번호 변경</a>
  <form method="post" action="/logout" class="inline">
    <input type="hidden" name="csrf" value="{{ csrf_token() }}"><button>로그아웃</button>
  </form>
</nav>
{% endif %}
<main>
{% for m in get_flashed_messages() %}<p class="flash">{{ m }}</p>{% endfor %}
{% block content %}{% endblock %}
</main>
</body>
</html>
```

`templates/login.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>일일 시스템 점검</h1>
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <p><label>아이디 <input name="login_id" required autofocus></label></p>
  <p><label>비밀번호 <input name="password" type="password" required></label></p>
  <button>로그인</button>
</form>
{% endblock %}
```

`templates/password.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>비밀번호 변경</h1>
{% if g.user.must_change_pw %}<p class="flash">처음 로그인했거나 비밀번호가 초기화되었습니다. 새 비밀번호를 정해주세요.</p>{% endif %}
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <p><label>현재 비밀번호 <input name="current_password" type="password" required></label></p>
  <p><label>새 비밀번호(8자 이상) <input name="new_password" type="password" minlength="8" required></label></p>
  <p><label>새 비밀번호 확인 <input name="new_password2" type="password" minlength="8" required></label></p>
  <button>변경</button>
</form>
{% endblock %}
```

`templates/index.html` (임시):
```html
{% extends "base.html" %}
{% block content %}<h1>오늘 점검</h1>{% endblock %}
```

`static/style.css`:
```css
body { font-family: "Malgun Gothic", system-ui, sans-serif; margin: 0; background: #f6f7f9; color: #222; }
nav { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; padding: .6rem 1rem; background: #1f3a5f; }
nav a, nav span { color: #fff; text-decoration: none; }
nav .spacer { flex: 1; }
nav button { background: none; border: 1px solid #fff; color: #fff; cursor: pointer; }
main { max-width: 960px; margin: 0 auto; padding: 1rem; }
table { border-collapse: collapse; width: 100%; background: #fff; margin-bottom: 1rem; }
th, td { border: 1px solid #ccc; padding: .4rem .6rem; text-align: left; vertical-align: top; }
th { background: #eef1f5; }
.flash { background: #fff3cd; border: 1px solid #e0c36b; padding: .5rem; }
.issue { color: #b00020; font-weight: bold; }
.mine { background: #eef6ff; }
.inline { display: inline; }
.remark { white-space: pre-wrap; }
textarea { width: 100%; min-height: 5rem; box-sizing: border-box; }
.sheet td.chk, .sheet th.chk { width: 3rem; text-align: center; }
.sheet input[type=checkbox] { width: 1.3rem; height: 1.3rem; }
.add-item { margin-top: 1.5rem; padding: .8rem; background: #fff; border: 1px dashed #aaa; }
.page { page-break-after: always; margin-bottom: 2rem; }
@media print {
  .no-print, nav { display: none !important; }
  body { background: #fff; }
  main { max-width: none; padding: 0; }
}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: 로그인, CSRF, 비밀번호 변경, 최초 관리자 생성"
```

---

### Task 3: 오늘 점검 목록과 점검표 작성·제출

**Files:**
- Modify: `app.py` (`index` 교체, 나머지는 `def main` 위에 추가)
- Create: `templates/sheet.html`
- Modify: `templates/index.html` (전체 교체)
- Test: `test_app.py`

**Interfaces:**
- Consumes: `login_required`, `today`, `get_db`, `audit`, `now`
- Produces:
  - `status_label(status: str|None, has_issue: int|None) -> str` (Jinja 전역) — `"미점검" | "작성중" | "제출" | "확인완료"` + 이상이면 `" · 이상"`
  - `parse_day(value: str) -> str` — 잘못된 날짜면 400
  - `load_sheet(db, system_id, day) -> (check_row|None, items: list[dict])` — items 원소 `{"item_id": int, "item_text": str, "checked": 0|1}`
  - `snapshot(remark, items) -> dict` — `{"remark": str, "items": [[text, checked], ...]}`
  - `write_items(db, check_id, items) -> None`
  - `save_sheet(db, system_id, day, check, items) -> str|None` (오류 메시지 또는 None)
  - 엔드포인트 `check_sheet` — `GET/POST /check/<int:system_id>/<day>`, 폼 필드 `action`(`save`|`submit`), `item`(여러 개, item_id), `remark`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
class SheetTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")

    def test_index_lists_systems_with_status(self):
        t = self.text(self.c.get("/"))
        self.assertIn("주문서버", t)
        self.assertIn("미점검", t)

    def test_save_draft_then_submit(self):
        self.post(URL, action="save", item=["1"])
        self.assertEqual(self.row("SELECT status FROM checks")["status"], "draft")
        self.assertIn('value="1" checked', self.text(self.c.get(URL)))
        self.post(URL, action="submit", item=["1", "2"])
        c = self.row("SELECT * FROM checks")
        self.assertEqual((c["status"], c["has_issue"], c["user_id"]), ("submitted", 0, 1))
        self.assertEqual(self.row("SELECT action FROM audit_log")["action"], "submit")

    def test_unchecked_item_requires_remark(self):
        t = self.text(self.post(URL, action="submit", item=["1"]))
        self.assertIn("비고를 입력", t)
        self.assertIsNone(self.row("SELECT * FROM checks"))
        self.post(URL, action="submit", item=["1"], remark="배치 재처리중")
        self.assertEqual(self.row("SELECT has_issue FROM checks")["has_issue"], 1)

    def test_error_keeps_posted_input(self):
        t = self.text(self.post(URL, action="submit", item=["2"], remark=""))
        self.assertIn('value="2" checked', t)

    def test_cannot_write_other_day(self):
        for day in ("2026-09-22", "2026-09-24"):
            t = self.text(self.post(f"/check/1/{day}", action="save", item=["1"]))
            self.assertIn("오늘 점검만", t)
        self.assertIsNone(self.row("SELECT * FROM checks"))

    def test_second_submit_rejected(self):
        self.post(URL, action="submit", item=["1", "2"])
        other = appmod.app.test_client()
        self.login("m2", client=other)
        t = self.text(self.post(URL, client=other, action="submit", item=["1"], remark="x"))
        self.assertIn("먼저 제출", t)
        c = self.row("SELECT * FROM checks")
        self.assertEqual((c["user_id"], c["has_issue"]), (1, 0))

    def test_system_without_items_cannot_submit(self):
        t = self.text(self.post(f"/check/2/{DAY}", action="submit"))
        self.assertIn("점검 항목이 없습니다", t)
        self.assertIsNone(self.row("SELECT * FROM checks"))

    def test_submitted_sheet_keeps_item_text(self):
        self.post(URL, action="submit", item=["1", "2"])
        self.exec("UPDATE items SET text = '바뀐 문구' WHERE id = 1")
        t = self.text(self.c.get(URL))
        self.assertIn("프로세스 기동 확인", t)
        self.assertNotIn("바뀐 문구", t)

    def test_bad_date_rejected(self):
        self.assertEqual(self.c.get("/check/1/abc").status_code, 400)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: SheetTest FAIL/ERROR (`/check/...` 404)

- [ ] **Step 3: `app.py`의 임시 `index` 라우트를 삭제하고 아래 코드를 `def main` 위에 추가**

```python
STATUS_NAMES = {None: "미점검", "draft": "작성중", "submitted": "제출", "approved": "확인완료"}


def status_label(status, has_issue=0):
    return STATUS_NAMES[status] + (" · 이상" if has_issue else "")


app.jinja_env.globals["status_label"] = status_label


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


def save_sheet(db, system_id, day, check, items):
    """폼 내용을 저장한다. 성공하면 None, 실패하면 오류 메시지를 돌려준다."""
    action = request.form.get("action")
    if action not in ("save", "submit"):
        abort(400)
    if day != today():
        return "오늘 점검만 작성할 수 있습니다."
    if check and check["status"] != "draft":
        return "다른 사용자가 먼저 제출했습니다."
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
    names = {r["id"]: r["name"] for r in db.execute("SELECT id, name FROM users")}
    return render_template("sheet.html", system=system, day=day, today=today(), check=check,
                           items=items, remark=remark, error=error, names=names, editing=False)
```

- [ ] **Step 4: 템플릿 작성**

`templates/index.html` (전체 교체):
```html
{% extends "base.html" %}
{% block content %}
<h1>오늘 점검 <small>{{ today }}</small></h1>
<table>
  <tr><th>시스템</th><th>담당자</th><th>상태</th></tr>
  {% for r in rows %}
  <tr{% if r.owner_id == g.user.id %} class="mine"{% endif %}>
    <td><a href="{{ url_for('check_sheet', system_id=r.id, day=today) }}">{{ r.name }}</a></td>
    <td>{{ r.owner_name or '-' }}</td>
    <td{% if r.has_issue %} class="issue"{% endif %}>{{ status_label(r.status, r.has_issue) }}</td>
  </tr>
  {% else %}
  <tr><td colspan="3">등록된 시스템이 없습니다. 관리자에게 시스템 등록을 요청하세요.</td></tr>
  {% endfor %}
</table>
<p><small>파란색 줄이 내 담당 시스템입니다. 다른 시스템도 대신 점검할 수 있습니다.</small></p>
{% endblock %}
```

`templates/sheet.html`:
```html
{% extends "base.html" %}
{% block content %}
{% set writable = day == today and (not check or check.status == 'draft') %}
<h1>{{ system.name }} 점검표 <small>{{ day }}</small></h1>
<p>상태: <b{% if check and check.has_issue %} class="issue"{% endif %}>{{ status_label(check.status if check else None, check.has_issue if check else 0) }}</b>
{% if check %} · 점검자: {{ names[check.user_id] }}{% if check.submitted_at %} · 제출: {{ check.submitted_at }}{% endif %}{% endif %}</p>
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post" class="sheet">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <table>
    <tr><th class="chk">체크</th><th>점검 항목</th></tr>
    {% for it in items %}
    <tr>
      <td class="chk"><input type="checkbox" id="i{{ it.item_id }}" name="item" value="{{ it.item_id }}"{% if it.checked %} checked{% endif %}{% if not writable %} disabled{% endif %}></td>
      <td><label for="i{{ it.item_id }}">{{ it.item_text }}</label></td>
    </tr>
    {% else %}
    <tr><td colspan="2">점검 항목이 없습니다.</td></tr>
    {% endfor %}
  </table>
  <h2>비고</h2>
  <textarea name="remark"{% if not writable %} readonly{% endif %}>{{ remark }}</textarea>
  {% if writable %}
  <p><button name="action" value="save">임시저장</button> <button name="action" value="submit">제출</button></p>
  {% endif %}
</form>
{% endblock %}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: 오늘 점검 목록과 점검표 작성·제출"
```

---

### Task 4: 점검표에서 항목 추가·미사용 처리

**Files:**
- Modify: `app.py` (`def main` 위에 추가)
- Modify: `templates/sheet.html` (전체 교체)
- Test: `test_app.py`

**Interfaces:**
- Consumes: `login_required`, `today`, `audit`, `check_sheet`
- Produces: 엔드포인트 `add_item` — `POST /system/<int:system_id>/items` (필드 `text`), `deactivate_item` — `POST /item/<int:item_id>/deactivate`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
class ItemTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")

    def test_member_adds_item_and_it_is_logged(self):
        self.post("/system/1/items", text="디스크 여유 확인")
        self.assertIsNotNone(self.row("SELECT * FROM items WHERE text = '디스크 여유 확인' AND active = 1"))
        a = self.row("SELECT * FROM audit_log WHERE action = 'add'")
        self.assertEqual((a["target"], a["user_id"]), ("item", 1))
        self.assertIn("디스크 여유 확인", self.text(self.c.get(URL)))

    def test_blank_item_rejected(self):
        self.post("/system/1/items", text="   ")
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM items")["n"], 2)

    def test_new_item_not_added_to_submitted_record(self):
        self.post(URL, action="submit", item=["1", "2"])
        self.post("/system/1/items", text="디스크 여유 확인")
        self.assertNotIn("디스크 여유 확인", self.text(self.c.get(URL)))
        appmod.app.config["TODAY"] = "2026-09-24"
        self.assertIn("디스크 여유 확인", self.text(self.c.get("/check/1/2026-09-24")))

    def test_deactivate_hides_item_but_keeps_history(self):
        self.post(URL, action="submit", item=["1", "2"])
        self.post("/item/2/deactivate")
        self.assertEqual(self.row("SELECT active FROM items WHERE id = 2")["active"], 0)
        self.assertEqual(self.row("SELECT action FROM audit_log WHERE target = 'item'")["action"], "deactivate")
        self.assertIn("배치 완료 확인", self.text(self.c.get(URL)))
        appmod.app.config["TODAY"] = "2026-09-24"
        self.assertNotIn("배치 완료 확인", self.text(self.c.get("/check/1/2026-09-24")))
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: ItemTest FAIL (404)

- [ ] **Step 3: 라우트 추가**

```python
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
```

- [ ] **Step 4: `templates/sheet.html` 전체 교체**

```html
{% extends "base.html" %}
{% block content %}
{% set live = not check or check.status == 'draft' %}
{% set writable = day == today and live %}
<h1>{{ system.name }} 점검표 <small>{{ day }}</small></h1>
<p>상태: <b{% if check and check.has_issue %} class="issue"{% endif %}>{{ status_label(check.status if check else None, check.has_issue if check else 0) }}</b>
{% if check %} · 점검자: {{ names[check.user_id] }}{% if check.submitted_at %} · 제출: {{ check.submitted_at }}{% endif %}{% endif %}</p>
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post" class="sheet">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <table>
    <tr><th class="chk">체크</th><th>점검 항목</th>{% if live %}<th class="no-print"></th>{% endif %}</tr>
    {% for it in items %}
    <tr>
      <td class="chk"><input type="checkbox" id="i{{ it.item_id }}" name="item" value="{{ it.item_id }}"{% if it.checked %} checked{% endif %}{% if not writable %} disabled{% endif %}></td>
      <td><label for="i{{ it.item_id }}">{{ it.item_text }}</label></td>
      {% if live %}<td class="no-print"><button formaction="{{ url_for('deactivate_item', item_id=it.item_id) }}" formnovalidate>미사용</button></td>{% endif %}
    </tr>
    {% else %}
    <tr><td colspan="3">점검 항목이 없습니다. 아래에서 추가하세요.</td></tr>
    {% endfor %}
  </table>
  <h2>비고</h2>
  <textarea name="remark"{% if not writable %} readonly{% endif %}>{{ remark }}</textarea>
  {% if writable %}
  <p><button name="action" value="save">임시저장</button> <button name="action" value="submit">제출</button></p>
  {% endif %}
</form>
{% if system.active %}
<form method="post" action="{{ url_for('add_item', system_id=system.id) }}" class="no-print add-item">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <label>새 점검 항목 <input name="text" maxlength="200" size="40" required></label>
  <button>+ 항목 추가</button><br>
  <small>항목을 추가하면 화면이 새로 고쳐집니다. 체크한 내용은 먼저 임시저장하세요. 이미 제출한 점검표에는 다음 점검부터 나타납니다.</small>
</form>
{% endif %}
{% endblock %}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: 점검표에서 항목 추가·미사용 처리"
```

---

### Task 5: 제출 후 수정(사유·이력), 팀장 확인, 현황판

**Files:**
- Modify: `app.py` (`save_sheet`, `check_sheet` 교체 + 라우트 추가)
- Modify: `templates/sheet.html` (전체 교체)
- Create: `templates/dashboard.html`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `load_sheet`, `apply_form`, `snapshot`, `write_items`, `parse_day`, `status_label`
- Produces:
  - `save_sheet`의 `action == "edit"` 지원(필드 `reason` 필수)
  - `GET /check/<id>/<day>?edit=1` — 수정 모드
  - 엔드포인트 `approve` — `POST /approve/<int:check_id>` (leader 이상)
  - 엔드포인트 `dashboard` — `GET /dashboard?day=YYYY-MM-DD` (leader 이상)

- [ ] **Step 1: 실패하는 테스트 추가**

```python
class ReviewTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")
        self.post(URL, action="submit", item=["1", "2"])

    def test_edit_requires_reason(self):
        t = self.text(self.post(URL, action="edit", item=["1"], remark="재기동"))
        self.assertIn("수정 사유를 입력하세요", t)
        self.assertEqual(self.row("SELECT remark FROM checks")["remark"], "")

    def test_edit_logs_before_and_after(self):
        self.post(URL, action="edit", item=["1"], remark="재기동함", reason="체크 누락 정정")
        c = self.row("SELECT * FROM checks")
        self.assertEqual((c["has_issue"], c["remark"], c["status"]), (1, "재기동함", "submitted"))
        a = self.row("SELECT * FROM audit_log WHERE action = 'edit'")
        self.assertEqual(a["reason"], "체크 누락 정정")
        self.assertEqual(json.loads(a["before"])["items"][1], ["배치 완료 확인", 1])
        self.assertEqual(json.loads(a["after"])["items"][1], ["배치 완료 확인", 0])

    def test_edit_mode_shows_reason_field(self):
        self.assertIn('name="reason"', self.text(self.c.get(URL + "?edit=1")))

    def test_member_cannot_approve(self):
        self.assertEqual(self.post("/approve/1").status_code, 403)

    def test_approve_then_edit_requires_reapproval(self):
        lead = appmod.app.test_client()
        self.login("lead", client=lead)
        self.post("/approve/1", client=lead)
        c = self.row("SELECT * FROM checks")
        self.assertEqual((c["status"], c["approved_by"]), ("approved", 3))
        self.post(URL, action="edit", item=["1", "2"], remark="메모", reason="비고 추가")
        c = self.row("SELECT * FROM checks")
        self.assertEqual((c["status"], c["approved_by"], c["approved_at"]), ("submitted", None, None))

    def test_dashboard(self):
        self.assertEqual(self.c.get("/dashboard").status_code, 403)
        lead = appmod.app.test_client()
        self.login("lead", client=lead)
        t = self.text(lead.get("/dashboard"))
        self.assertIn("주문서버", t)
        self.assertIn("빈시스템", t)
        self.assertIn("미점검 <b>1</b>", t)
        self.assertIn("확인 대기 <b>1</b>", t)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: ReviewTest FAIL (edit는 400, `/approve`, `/dashboard` 404)

- [ ] **Step 3: `save_sheet` 맨 앞부분 교체** — docstring 바로 아래의 다음 세 줄을

```python
    action = request.form.get("action")
    if action not in ("save", "submit"):
        abort(400)
```

아래로 바꾼다

```python
    action = request.form.get("action")
    if action == "edit":
        return edit_sheet(db, check, items)
    if action not in ("save", "submit"):
        abort(400)
```

그리고 `save_sheet` 위에 추가:

```python
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
```

- [ ] **Step 4: `check_sheet`의 `render_template` 호출 위에 `editing` 계산 추가**

`editing=False` 를 `editing=editing` 으로 바꾸고, `names = ...` 줄 바로 위에 추가:

```python
    editing = bool(check and check["status"] != "draft"
                   and (request.args.get("edit") == "1" or request.form.get("action") == "edit"))
```

- [ ] **Step 5: 확인·현황판 라우트 추가** (`def main` 위)

```python
@app.post("/approve/<int:check_id>")
@login_required("leader")
def approve(check_id):
    db = get_db()
    check = db.execute("SELECT * FROM checks WHERE id = ?", (check_id,)).fetchone()
    if check is None:
        abort(404)
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
                  c.has_issue, u.name AS checker_name, c.submitted_at
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
```

- [ ] **Step 6: `templates/sheet.html` 전체 교체**

```html
{% extends "base.html" %}
{% block content %}
{% set live = not check or check.status == 'draft' %}
{% set writable = (day == today and live) or editing %}
<h1>{{ system.name }} 점검표 <small>{{ day }}</small></h1>
<p>상태: <b{% if check and check.has_issue %} class="issue"{% endif %}>{{ status_label(check.status if check else None, check.has_issue if check else 0) }}</b>
{% if check %} · 점검자: {{ names[check.user_id] }}{% if check.submitted_at %} · 제출: {{ check.submitted_at }}{% endif %}{% if check.approved_at %} · 확인: {{ names[check.approved_by] }} {{ check.approved_at }}{% endif %}{% endif %}</p>
{% if error %}<p class="flash">{{ error }}</p>{% endif %}
<form method="post" class="sheet">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <table>
    <tr><th class="chk">체크</th><th>점검 항목</th>{% if live %}<th class="no-print"></th>{% endif %}</tr>
    {% for it in items %}
    <tr>
      <td class="chk"><input type="checkbox" id="i{{ it.item_id }}" name="item" value="{{ it.item_id }}"{% if it.checked %} checked{% endif %}{% if not writable %} disabled{% endif %}></td>
      <td><label for="i{{ it.item_id }}">{{ it.item_text }}</label></td>
      {% if live %}<td class="no-print"><button formaction="{{ url_for('deactivate_item', item_id=it.item_id) }}" formnovalidate>미사용</button></td>{% endif %}
    </tr>
    {% else %}
    <tr><td colspan="3">점검 항목이 없습니다. 아래에서 추가하세요.</td></tr>
    {% endfor %}
  </table>
  <h2>비고</h2>
  <textarea name="remark"{% if not writable %} readonly{% endif %}>{{ remark }}</textarea>
  {% if editing %}
  <p><label>수정 사유 <input name="reason" size="50" required></label></p>
  <p><button name="action" value="edit">수정 저장</button>
     <a href="{{ url_for('check_sheet', system_id=system.id, day=day) }}">취소</a></p>
  {% elif writable %}
  <p><button name="action" value="save">임시저장</button> <button name="action" value="submit">제출</button></p>
  {% elif not live %}
  <p class="no-print"><a href="{{ url_for('check_sheet', system_id=system.id, day=day, edit=1) }}">수정하기</a>
  {% if check.status == 'submitted' and g.user.role in ('leader', 'admin') %}
    <button formaction="{{ url_for('approve', check_id=check.id) }}" formnovalidate>확인</button>
  {% endif %}</p>
  {% endif %}
</form>
{% if system.active %}
<form method="post" action="{{ url_for('add_item', system_id=system.id) }}" class="no-print add-item">
  <input type="hidden" name="csrf" value="{{ csrf_token() }}">
  <label>새 점검 항목 <input name="text" maxlength="200" size="40" required></label>
  <button>+ 항목 추가</button><br>
  <small>항목을 추가하면 화면이 새로 고쳐집니다. 체크한 내용은 먼저 임시저장하세요. 이미 제출한 점검표에는 다음 점검부터 나타납니다.</small>
</form>
{% endif %}
{% endblock %}
```

`templates/dashboard.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>현황판</h1>
<form method="get" class="no-print">
  <label>날짜 <input type="date" name="day" value="{{ day }}"></label> <button>조회</button>
</form>
<p>{{ day }} · 전체 {{ rows|length }} · 미점검 <b>{{ missing }}</b> · 이상 <b class="issue">{{ issues }}</b> · 확인 대기 <b>{{ waiting }}</b></p>
<table>
  <tr><th>시스템</th><th>담당자</th><th>점검자</th><th>제출 시각</th><th>상태</th><th class="no-print">확인</th></tr>
  {% for r in rows %}
  <tr>
    <td><a href="{{ url_for('check_sheet', system_id=r.system_id, day=day) }}">{{ r.name }}</a></td>
    <td>{{ r.owner_name or '-' }}</td>
    <td>{{ r.checker_name or '-' }}</td>
    <td>{{ r.submitted_at or '-' }}</td>
    <td{% if r.has_issue %} class="issue"{% endif %}>{{ status_label(r.status, r.has_issue) }}</td>
    <td class="no-print">{% if r.status == 'submitted' %}
      <form method="post" action="{{ url_for('approve', check_id=r.check_id) }}" class="inline">
        <input type="hidden" name="csrf" value="{{ csrf_token() }}"><button>확인</button>
      </form>{% endif %}</td>
  </tr>
  {% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 7: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 8: Commit**

```bash
git add .
git commit -m "feat: 제출 후 수정 이력, 팀장 확인, 현황판"
```

---

### Task 6: 이력 조회, 인쇄/PDF, CSV

**Files:**
- Modify: `app.py` (`def main` 위에 추가)
- Create: `templates/history.html`, `templates/print.html`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `parse_day`, `status_label`, `today`
- Produces:
  - `query_history(start, end, system_id=None) -> list[dict]` — 각 dict에 `checks` 칼럼 + `system_name`, `checker_name`, `approver_name`, `lines`(`[{"item_text", "checked"}]`)
  - `csv_cell(v) -> str`
  - 엔드포인트 `history` — `GET /history?start=&end=&system_id=&format=(print|csv)`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
class HistoryTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")
        self.post(URL, action="submit", item=["1"], remark='=HYPERLINK("http://x")')

    def test_history_lists_check(self):
        t = self.text(self.c.get(f"/history?start={DAY}"))
        self.assertIn("주문서버", t)
        self.assertIn("HYPERLINK", t)

    def test_csv_escapes_formula_and_has_bom(self):
        r = self.c.get(f"/history?start={DAY}&format=csv")
        self.assertIn("attachment", r.headers["Content-Disposition"])
        t = self.text(r)
        self.assertTrue(t.startswith("\ufeff"))
        rows = list(csv.reader(io.StringIO(t.lstrip("\ufeff"))))
        self.assertEqual(len(rows), 3)  # 머리글 + 항목 2개
        self.assertEqual(rows[1][-1], "'=HYPERLINK(\"http://x\")")
        self.assertEqual(rows[2][-2], "X")

    def test_print_view(self):
        t = self.text(self.c.get(f"/history?start={DAY}&format=print"))
        self.assertIn("프로세스 기동 확인", t)
        self.assertIn("✘", t)

    def test_reversed_range_still_works(self):
        t = self.text(self.c.get(f"/history?start=2026-09-24&end={DAY}"))
        self.assertIn("주문서버", t)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: HistoryTest FAIL (404)

- [ ] **Step 3: 코드 추가**

```python
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
```

- [ ] **Step 4: 템플릿 작성**

`templates/history.html`:
```html
{% extends "base.html" %}
{% block content %}
<h1>이력 조회</h1>
<form method="get" class="no-print">
  <label>시작 <input type="date" name="start" value="{{ start }}"></label>
  <label>종료 <input type="date" name="end" value="{{ end }}"></label>
  <label>시스템
    <select name="system_id"><option value="">전체</option>
      {% for s in systems %}<option value="{{ s.id }}"{% if s.id == system_id %} selected{% endif %}>{{ s.name }}</option>{% endfor %}
    </select></label>
  <button>조회</button>
  <button name="format" value="print">인쇄/PDF</button>
  <button name="format" value="csv">엑셀(CSV)</button>
</form>
<table>
  <tr><th>날짜</th><th>시스템</th><th>점검자</th><th>상태</th><th>확인자</th><th>비고</th></tr>
  {% for r in rows %}
  <tr>
    <td>{{ r.date }}</td>
    <td><a href="{{ url_for('check_sheet', system_id=r.system_id, day=r.date) }}">{{ r.system_name }}</a></td>
    <td>{{ r.checker_name }}</td>
    <td{% if r.has_issue %} class="issue"{% endif %}>{{ status_label(r.status, r.has_issue) }}</td>
    <td>{{ r.approver_name or '-' }}</td>
    <td class="remark">{{ r.remark }}</td>
  </tr>
  {% else %}
  <tr><td colspan="6">기록이 없습니다.</td></tr>
  {% endfor %}
</table>
{% endblock %}
```

`templates/print.html`:
```html
{% extends "base.html" %}
{% block title %}점검 기록 {{ start }} ~ {{ end }}{% endblock %}
{% block content %}
<p class="no-print"><button onclick="window.print()">인쇄 / PDF로 저장</button>
  <small>인쇄 창에서 프린터를 "PDF로 저장"으로 고르면 PDF 파일이 됩니다.</small></p>
{% for r in rows %}
<section class="page sheet">
  <h2>{{ r.system_name }} 일일 점검표 — {{ r.date }}</h2>
  <p>점검자: {{ r.checker_name }} · 제출: {{ r.submitted_at or '-' }} · 상태: {{ status_label(r.status, r.has_issue) }}
     · 확인: {{ r.approver_name or '-' }} {{ r.approved_at or '' }}</p>
  <table>
    <tr><th class="chk">체크</th><th>점검 항목</th></tr>
    {% for it in r.lines %}
    <tr><td class="chk">{{ '✔' if it.checked else '✘' }}</td><td>{{ it.item_text }}</td></tr>
    {% endfor %}
  </table>
  <p><b>비고:</b> <span class="remark">{{ r.remark or '-' }}</span></p>
</section>
{% else %}
<p>기록이 없습니다.</p>
{% endfor %}
{% endblock %}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: 이력 조회, 인쇄/PDF, CSV 내려받기"
```

---

### Task 7: 관리 화면 (사용자·시스템·항목)

**Files:**
- Modify: `app.py` (`def main` 위에 추가)
- Create: `templates/admin.html`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `login_required("admin")`, `audit`, `ROLE_NAMES`, `MAX_FAIL`
- Produces: 엔드포인트
  - `admin` — `GET /admin`
  - `admin_add_user` — `POST /admin/users` (`login_id`, `name`, `role`, `password`)
  - `admin_update_user` — `POST /admin/users/<id>` (`action` = `role`|`reset_pw`|`unlock`|`deactivate`|`activate`, 필요 시 `role`, `password`)
  - `admin_add_system` — `POST /admin/systems` (`name`, `owner_id`, `sort`, `active`)
  - `admin_update_system` — `POST /admin/systems/<id>` (같은 필드)
  - `admin_update_item` — `POST /admin/items/<id>` (`sort`, `active`)

- [ ] **Step 1: 실패하는 테스트 추가**

```python
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
        u = self.row("SELECT * FROM users WHERE id = 1")
        self.assertEqual(u["must_change_pw"], 1)
        a = self.row("SELECT * FROM audit_log WHERE action = 'reset_pw'")
        self.assertNotIn("pw_hash", a["before"] + a["after"])

    def test_system_add_update_and_item_sort(self):
        self.post("/admin/systems", name="원장서버", owner_id="2", sort="3", active="1")
        s = self.row("SELECT * FROM systems WHERE name = '원장서버'")
        self.assertEqual((s["owner_id"], s["sort"], s["active"]), (2, 3, 1))
        self.post(f"/admin/systems/{s['id']}", name="원장서버A", owner_id="", sort="3")
        s = self.row("SELECT * FROM systems WHERE id = ?", s["id"])
        self.assertEqual((s["name"], s["owner_id"], s["active"]), ("원장서버A", None, 0))
        self.post("/admin/items/2", sort="0", active="1")
        self.assertEqual(self.row("SELECT sort FROM items WHERE id = 2")["sort"], 0)
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM audit_log WHERE target = 'system'")["n"], 2)
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: AdminTest FAIL (404)

- [ ] **Step 3: 코드 추가**

```python
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
```

- [ ] **Step 4: `templates/admin.html` 작성** (표 안의 입력칸은 `form` 속성으로 각 줄의 폼에 연결)

```html
{% extends "base.html" %}
{% block content %}
{% macro csrf() %}<input type="hidden" name="csrf" value="{{ csrf_token() }}">{% endmacro %}
<h1>관리</h1>

<h2>사용자</h2>
<table>
  <tr><th>ID</th><th>이름</th><th>역할</th><th>상태</th><th>작업</th></tr>
  {% for u in users %}
  <tr>
    <td>{{ u.login_id }}</td>
    <td>{{ u.name }}</td>
    <td><form method="post" action="{{ url_for('admin_update_user', user_id=u.id) }}" class="inline">{{ csrf() }}
      <select name="role">{% for k, v in roles.items() %}<option value="{{ k }}"{% if k == u.role %} selected{% endif %}>{{ v }}</option>{% endfor %}</select>
      <button name="action" value="role">변경</button></form></td>
    <td>{% if not u.active %}미사용{% elif u.fail_count >= max_fail %}<span class="issue">잠김</span>{% else %}정상{% endif %}</td>
    <td><form method="post" action="{{ url_for('admin_update_user', user_id=u.id) }}" class="inline">{{ csrf() }}
      <input name="password" type="password" placeholder="새 임시 비밀번호" size="14">
      <button name="action" value="reset_pw">비밀번호 초기화</button>
      <button name="action" value="unlock">잠금 해제</button>
      {% if u.active %}<button name="action" value="deactivate">미사용</button>{% else %}<button name="action" value="activate">사용</button>{% endif %}
    </form></td>
  </tr>
  {% endfor %}
</table>
<form method="post" action="{{ url_for('admin_add_user') }}">{{ csrf() }}
  <input name="login_id" placeholder="ID" required>
  <input name="name" placeholder="이름" required>
  <select name="role">{% for k, v in roles.items() %}<option value="{{ k }}">{{ v }}</option>{% endfor %}</select>
  <input name="password" type="password" placeholder="임시 비밀번호(8자 이상)" minlength="8" required>
  <button>사용자 추가</button>
</form>

<h2>시스템</h2>
<table>
  <tr><th>이름</th><th>담당자</th><th>순서</th><th>사용</th><th></th></tr>
  {% for s in systems %}
  <tr>
    <td><input form="sys{{ s.id }}" name="name" value="{{ s.name }}" required></td>
    <td><select form="sys{{ s.id }}" name="owner_id"><option value="">-</option>
      {% for u in users if u.active %}<option value="{{ u.id }}"{% if u.id == s.owner_id %} selected{% endif %}>{{ u.name }}</option>{% endfor %}</select></td>
    <td><input form="sys{{ s.id }}" name="sort" type="number" value="{{ s.sort }}" style="width:4rem"></td>
    <td><input form="sys{{ s.id }}" name="active" type="checkbox" value="1"{% if s.active %} checked{% endif %}></td>
    <td><form id="sys{{ s.id }}" method="post" action="{{ url_for('admin_update_system', system_id=s.id) }}">{{ csrf() }}<button>저장</button></form></td>
  </tr>
  {% endfor %}
</table>
<form method="post" action="{{ url_for('admin_add_system') }}">{{ csrf() }}
  <input name="name" placeholder="시스템 이름" required>
  <select name="owner_id"><option value="">담당자 없음</option>
    {% for u in users if u.active %}<option value="{{ u.id }}">{{ u.name }}</option>{% endfor %}</select>
  <input name="sort" type="number" value="0" style="width:4rem">
  <input type="hidden" name="active" value="1">
  <button>시스템 추가</button>
</form>

<h2>점검 항목</h2>
<p><small>항목 추가는 각 점검표 화면에서 합니다. 여기서는 순서와 사용 여부만 바꿉니다.</small></p>
<table>
  <tr><th>시스템</th><th>항목</th><th>순서</th><th>사용</th><th></th></tr>
  {% for i in items %}
  <tr>
    <td>{{ i.system_name }}</td>
    <td>{{ i.text }}</td>
    <td><input form="item{{ i.id }}" name="sort" type="number" value="{{ i.sort }}" style="width:4rem"></td>
    <td><input form="item{{ i.id }}" name="active" type="checkbox" value="1"{% if i.active %} checked{% endif %}></td>
    <td><form id="item{{ i.id }}" method="post" action="{{ url_for('admin_update_item', item_id=i.id) }}">{{ csrf() }}<button>저장</button></form></td>
  </tr>
  {% endfor %}
</table>
{% endblock %}
```

- [ ] **Step 5: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 6: Commit**

```bash
git add .
git commit -m "feat: 관리 화면(사용자·시스템·항목)"
```

---

### Task 8: 백업 명령, 설치·운영 문서, 실제 실행 확인

**Files:**
- Modify: `app.py` (`backup()` 추가, `main`에 `backup` 분기 추가)
- Create: `README.md`
- Test: `test_app.py`

**Interfaces:**
- Produces: `backup(out_dir: str) -> str` — 만든 백업 파일 경로. CLI `python app.py backup [폴더]`

- [ ] **Step 1: 실패하는 테스트 추가**

```python
class BackupTest(Base):
    def test_backup_creates_consistent_copy(self):
        out = tempfile.mkdtemp()
        path = appmod.backup(out)
        con = sqlite3.connect(path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4)
        con.close()
```

- [ ] **Step 2: 실패 확인**

Run: `python -m unittest -v`
Expected: `AttributeError: module 'app' has no attribute 'backup'`

- [ ] **Step 3: `backup()` 추가** (`def main` 위) — 사용 중에도 안전한 SQLite 온라인 백업 API 사용

```python
def backup(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, f"checklist-{datetime.now():%Y%m%d-%H%M%S}.db")
    src, dst = sqlite3.connect(app.config["DATABASE"]), sqlite3.connect(dest)
    src.backup(dst)
    src.close()
    dst.close()
    return dest
```

`main`의 `if argv[:1] == ["init-admin"]:` 블록과 `else:` 사이에 추가:

```python
    elif argv[:1] == ["backup"]:
        print(backup(argv[1] if len(argv) > 1 else os.path.join(BASE, "backup")))
```

- [ ] **Step 4: 통과 확인**

Run: `python -m unittest -v`
Expected: 전부 OK

- [ ] **Step 5: `README.md` 작성**

````markdown
# 일일 시스템 점검

종이 점검표를 대신하는 사내 웹 체크리스트.

## 설치 (인터넷 없는 서버)

1. 인터넷 되는 PC에서 **서버와 같은 OS·같은 파이썬 버전**으로 패키지를 받는다.
   ```
   pip download -r requirements.txt -d wheels
   ```
2. 이 폴더 전체와 `wheels/`를 서버로 옮긴다.
3. 서버에서:
   ```
   python -m venv venv
   venv/bin/pip install --no-index --find-links wheels -r requirements.txt   # 윈도우: venv\Scripts\pip
   venv/bin/python app.py init-admin      # 관리자 계정 생성
   venv/bin/python app.py                 # http://서버주소:8080
   ```
   포트·주소 변경: 환경변수 `CHECKLIST_PORT`, `CHECKLIST_HOST`. DB 위치 변경: `CHECKLIST_DB`.

## 처음 쓸 때

1. 관리자로 로그인 → 비밀번호 변경.
2. 관리 화면에서 부서원 계정 추가(임시 비밀번호 전달) → 시스템 추가·담당자 지정.
3. 각 시스템 점검표 화면에서 "+ 항목 추가"로 종이 점검표 항목을 옮겨 적는다.

## 운영

- **자동 실행**: 리눅스는 systemd, 윈도우는 작업 스케줄러("컴퓨터 시작 시")에 `venv/bin/python app.py` 등록.
- **백업**: 매일 한 번 `venv/bin/python app.py backup /백업/경로` 를 스케줄러(cron 등)에 등록.
  사용 중에도 안전하게 복사된다. 복구는 서버를 멈추고 백업 파일을 `checklist.db`로 바꿔 넣으면 된다.
- **지우면 안 되는 파일**: `checklist.db`(모든 기록), `secret.key`(없어지면 모두 다시 로그인해야 함).
- **HTTPS**: 필요하면 사내 리버스 프록시(웹서버)에서 처리한다.
- **테스트**: `venv/bin/python -m unittest -v`
````

- [ ] **Step 6: 실제 실행 확인**

```bash
CHECKLIST_DB=./demo.db python app.py init-admin    # admin / 임의 비밀번호
CHECKLIST_DB=./demo.db python app.py
```
브라우저에서 `http://localhost:8080` 을 열고 아래를 순서대로 확인한다.
1. 로그인 → 비밀번호 변경 화면으로 이동
2. 관리: 사용자·시스템 추가
3. 점검표: 항목 2개 추가 → 하나만 체크하고 제출하면 비고를 요구함 → 비고 입력 후 제출
4. 현황판: "제출 · 이상"으로 표시 → 확인
5. 이력 조회: 인쇄/PDF 화면에서 체크 표시가 보이고, CSV를 엑셀로 열었을 때 한글이 깨지지 않음

확인이 끝나면 `demo.db`를 지운다.

- [ ] **Step 7: Commit**

```bash
git add .
git commit -m "feat: 백업 명령과 설치·운영 문서"
```
