# 일일 점검 v3 (번호 없는 트리) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 항목 계층을 사용자가 입력하는 번호(`1-1-1`) 대신 "상위 구분 + 순서"로 저장하고, 항목 관리를 [+하위]·↑↓·수정 버튼과 들여쓰기 붙여넣기로 바꾼다.

**Architecture:** `items.code`를 없애고 `parent_id`·`sort`를 둔다. `load_items`가 트리를 걸어 순서·깊이(`depth`)·상위 경로(`path`)를 계산하고, 나머지 화면(오늘 점검·현황판·이력·출력)은 이 결과를 그대로 쓴다. 제출 스냅샷의 `code`는 `path`(구분 경로 문자열)로 바뀐다.

**Tech Stack:** 기존과 같음 (Flask, SQLite, Jinja, 순수 CSS, unittest).

**Spec:** `docs/specs/2026-09-23-daily-check-design.md` (v3)

## Global Constraints

- 외부 패키지는 `flask`, `waitress`뿐. 모든 SQL 파라미터 바인딩. 삭제 없음(미사용만). `audit_log` 추가만. 모든 POST에 CSRF.
- 화면·출력에 번호를 표시하지 않는다. 계층은 들여쓰기와 구분 제목줄로 보인다.
- 테스트: `venv/Scripts/python -m unittest`.

## Review Focus

- 구분을 자기 하위 밑으로 옮기면 거부해야 한다(순환 방지)(`test_cannot_move_under_own_descendant`).
- 붙여넣기에서 점검 항목 아래 더 들여쓴 줄, 담을 구분이 없는 들여쓴 줄, 없는 담당자는 오류이고 아무것도 등록되지 않아야 한다(`test_parse_indented_errors`, `test_bulk_all_or_nothing`).
- 항목명 끝에 괄호가 있는 구분/점검(예: `녹취서버(IPPBX) (kim)`)은 마지막 괄호만 담당자로 읽어야 한다(`test_parse_indented_errors`).
- 구분을 미사용 처리하면 그 하위 전체가 오늘 표에서 빠지지만, 과거 날짜 표에는 남아야 한다(`test_inactive_group_hides_subtree`, v2의 `test_deactivated_item_still_required_before_retirement`).
- ↑↓는 같은 구분 안에서만 움직이고 끝에서는 아무 일도 없어야 한다(`test_move_up_down`).

---

### Task 1: 트리 모델로 전환 (데이터·항목 관리·점검 화면·출력)

**Files:**
- Modify: `db.py`, `app.py`, `static/style.css`
- Rewrite: `test_app.py`, `templates/items.html`, `templates/index.html`, `templates/dashboard.html`, `templates/print.html`

**Interfaces:**
- Produces:
  - `load_items(db, active_only=True) -> list[dict]` — 트리 순서, 각 dict에 `depth:int`, `path:list[str]`
  - `subtree_ids(db, item_id) -> set[int]`, `next_sort(db, parent_id) -> int`
  - `parse_indented(text, logins: dict[str,int]) -> (rows, errors)` — row: `{title, is_group, owner_id, parent: int|None(목록 안 순번)}`
  - 엔드포인트 `items_page`(GET /items?add=<id|top>&edit=<id>), `add_item`, `update_item`, `move_item`(POST /items/<id>/move, `direction`=up|down), `bulk_items`
  - `sheet_rows` 각 점검·구분 줄에 `path_text:str`; `results.path` 스냅샷

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

ITEMS = [  # id, parent_id, sort, title, is_group, owner_id
    (1, None, 0, "네트워크 및 시스템 점검", 1, None),
    (2, 1, 0, "네트워크상태", 1, None),
    (3, 2, 0, "백본 및 각 층 네트워크 상태", 0, 1),
    (4, 2, 1, "인터넷 방화벽,스위치 상태", 0, 1),
    (5, 1, 1, "시스템 및 업무 서비스", 1, None),
    (6, 5, 0, "시각동기화 시스템 정상 작동여부", 0, 2),
    (7, 1, 2, "전화 및 녹취", 0, 2),
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
            con.executemany(
                "INSERT INTO items(id, parent_id, sort, title, is_group, owner_id) VALUES(?,?,?,?,?,?)", ITEMS)
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

    def tok(self, item_id):
        """지금 DB에 저장된 줄 상태의 토큰(화면을 방금 연 것과 같음)."""
        return appmod.result_token(self.row("SELECT * FROM results WHERE date = ? AND item_id = ?", DAY, item_id))

    def items(self, active_only=True):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            return appmod.load_items(con, active_only)
        finally:
            con.close()


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
        sql = "INSERT INTO results(date, item_id, path, title, checker_id, status) VALUES(?, 3, 'p', 't', 1, 'draft')"
        self.exec(sql, DAY)
        with self.assertRaises(sqlite3.IntegrityError):
            self.exec(sql, DAY)

    def test_old_version_db_is_detected(self):
        fd, old = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        con = sqlite3.connect(old)
        con.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, code TEXT, title TEXT)")
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


class TreeTest(Base):
    def test_load_items_tree_order(self):
        rows = self.items()
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual([r["depth"] for r in rows], [0, 1, 2, 2, 1, 2, 1])
        self.assertEqual(rows[2]["path"], ["네트워크 및 시스템 점검", "네트워크상태"])
        self.assertEqual(appmod.fmt_dt("2026-09-23T16:01:55"), "2026-09-23 16:01")

    def test_inactive_group_hides_subtree(self):
        self.exec("UPDATE items SET active = 0 WHERE id = 2")
        self.assertEqual([r["id"] for r in self.items()], [1, 5, 6, 7])
        self.assertEqual(len(self.items(active_only=False)), 7)


class ParseTest(unittest.TestCase):
    def test_parse_indented(self):
        text = "네트워크 및 시스템 점검\n    네트워크상태\n\t\t백본 (m1)\n        방화벽 (m1)\n\n    전화 및 녹취 (m2)\n"
        rows, errors = appmod.parse_indented(text, {"m1": 1, "m2": 2})
        self.assertEqual(errors, [])
        self.assertEqual([(r["title"], r["is_group"], r["owner_id"], r["parent"]) for r in rows], [
            ("네트워크 및 시스템 점검", 1, None, None),
            ("네트워크상태", 1, None, 0),
            ("백본", 0, 1, 1),
            ("방화벽", 0, 1, 1),
            ("전화 및 녹취", 0, 2, 0),
        ])

    def test_whole_block_indented_is_fine(self):
        rows, errors = appmod.parse_indented("    보안\n        백신 (m1)", {"m1": 1})
        self.assertEqual((errors, rows[1]["parent"]), ([], 0))

    def test_parse_indented_errors(self):
        text = "\n".join([
            "구분A",
            "    점검 (m1)",
            "        점검 아래 (m1)",
            "    없는 사람 (zz)",
            "    녹취서버(IPPBX) (m1)",
            "  어긋난 줄 (m1)",
            "(m1)",
        ])
        rows, errors = appmod.parse_indented(text, {"m1": 1})
        self.assertEqual(len(errors), 3)
        self.assertIn("3번째 줄", errors[0])  # 점검 항목 아래
        self.assertIn("4번째 줄", errors[1])  # 없는 담당자
        self.assertIn("7번째 줄", errors[2])  # 항목명 없음
        self.assertEqual([(r["title"], r["parent"]) for r in rows],
                         [("구분A", None), ("점검", 0), ("녹취서버(IPPBX)", 0), ("어긋난 줄", 0)])


class ItemsTest(Base):
    def setUp(self):
        super().setUp()
        self.login("m1")

    def test_items_page_shows_tree_in_order(self):
        t = self.text(self.c.get("/items"))
        self.assertLess(t.index("시각동기화"), t.index("전화 및 녹취"))
        self.assertIn("+하위", t)

    def test_add_under_group_goes_last(self):
        self.post("/items", parent_id="2", title="지점 네트워크", is_group="0", owner_id="2")
        it = self.row("SELECT * FROM items WHERE title = '지점 네트워크'")
        self.assertEqual((it["parent_id"], it["sort"], it["owner_id"], it["created_on"]), (2, 2, 2, DAY))
        a = self.row("SELECT * FROM audit_log WHERE target = 'item'")
        self.assertEqual((a["action"], a["user_id"]), ("add", 1))

    def test_add_top_group(self):
        self.post("/items", parent_id="", title="보안 점검", is_group="1")
        it = self.row("SELECT * FROM items WHERE title = '보안 점검'")
        self.assertEqual((it["parent_id"], it["sort"], it["is_group"]), (None, 1, 1))

    def test_add_rejects_bad_input(self):
        for data, msg in [
            ({"parent_id": "2", "title": "", "is_group": "1"}, "항목명"),
            ({"parent_id": "2", "title": "x", "is_group": "0"}, "담당자"),
            ({"parent_id": "3", "title": "x", "is_group": "1"}, "구분 아래"),
        ]:
            self.post("/items", **data)
            self.assertIn(msg, self.text(self.c.get("/items")))
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM items")["n"], 7)

    def test_edit_form_and_update(self):
        self.assertIn('name="title" value="백본 및 각 층 네트워크 상태"', self.text(self.c.get("/items?edit=3")))
        self.post("/items/3", parent_id="5", title="백본 상태", is_group="0", owner_id="2", active="1")
        it = self.row("SELECT * FROM items WHERE id = 3")
        self.assertEqual((it["parent_id"], it["sort"], it["title"], it["owner_id"]), (5, 1, "백본 상태", 2))
        a = self.row("SELECT * FROM audit_log WHERE action = 'update'")
        self.assertEqual(json.loads(a["before"])["title"], "백본 및 각 층 네트워크 상태")

    def test_cannot_move_under_own_descendant(self):
        self.post("/items/1", parent_id="2", title="네트워크 및 시스템 점검", is_group="1", active="1")
        self.assertIsNone(self.row("SELECT parent_id FROM items WHERE id = 1")["parent_id"])
        self.assertIn("옮길 수 없습니다", self.text(self.c.get("/items")))

    def test_group_with_children_cannot_become_check(self):
        self.post("/items/2", parent_id="1", title="네트워크상태", is_group="0", owner_id="1", active="1")
        self.assertEqual(self.row("SELECT is_group FROM items WHERE id = 2")["is_group"], 1)

    def test_move_up_down(self):
        self.post("/items/4/move", direction="up")
        t = self.text(self.c.get("/items"))
        self.assertLess(t.index("인터넷 방화벽"), t.index("백본 및"))
        self.post("/items/4/move", direction="up")  # 이미 맨 위
        self.assertEqual(self.row("SELECT sort FROM items WHERE id = 4")["sort"], 0)
        self.post("/items/7/move", direction="down")  # 이미 맨 아래
        self.assertEqual([r["id"] for r in self.items()], [1, 2, 4, 3, 5, 6, 7])
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'move'")["n"], 1)

    def test_deactivate(self):
        self.post("/items/3", parent_id="2", title="백본 및 각 층 네트워크 상태", is_group="0", owner_id="1")
        it = self.row("SELECT * FROM items WHERE id = 3")
        self.assertEqual((it["active"], it["retired_on"]), (0, DAY))

    def test_bulk_indented(self):
        self.post("/items/bulk", text="보안 점검\n    백신 서버 (m2)")
        grp = self.row("SELECT * FROM items WHERE title = '보안 점검'")
        chk = self.row("SELECT * FROM items WHERE title = '백신 서버'")
        self.assertEqual((grp["parent_id"], grp["sort"], chk["parent_id"], chk["owner_id"]), (None, 1, grp["id"], 2))
        self.assertEqual(self.row("SELECT COUNT(*) AS n FROM audit_log WHERE action = 'add'")["n"], 2)

    def test_bulk_all_or_nothing(self):
        self.post("/items/bulk", text="보안 점검\n    백신 (nobody)")
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

    def test_all_view_tree_order(self):
        t = self.text(self.c.get("/?view=all"))
        self.assertLess(t.index("시스템 및 업무 서비스"), t.index("시각동기화"))
        self.assertLess(t.index("시각동기화"), t.index("전화 및 녹취"))

    def test_submit_only_selected_rows(self):
        r = self.post("/", action="submit", **form(r3=("0", "", ""), r4=(None, "", "")))
        self.assertEqual(r.status_code, 302)
        res = self.row("SELECT * FROM results WHERE item_id = 3")
        self.assertEqual((res["status"], res["issue"], res["checker_id"], res["title"], res["path"]),
                         ("submitted", 0, 1, "백본 및 각 층 네트워크 상태", "네트워크 및 시스템 점검 > 네트워크상태"))
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
        self.assertIn(f'name="base_3" value="{self.tok(3)}"', t)

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
        r = self.post("/", client=other, action="save", **form(r3=("1", "m2", "")))
        self.assertEqual(r.status_code, 302)
        t = self.text(other.get("/?view=all"))
        self.assertIn("먼저 입력", t)
        self.assertIn('value="m1"', t)
        self.assertEqual(self.row("SELECT remark FROM results")["remark"], "m1")
        self.post("/", client=other, action="submit", **form(r3=("0", "대무", self.tok(3))))
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
        t = self.text(self.post("/", action="edit", item_id="3", **form(r3=("1", "재부팅", ""))))
        self.assertIn("수정 사유", t)
        self.assertEqual(self.row("SELECT issue FROM results")["issue"], 0)
        self.post("/", action="edit", item_id="3", reason="확인 누락", **form(r3=("1", "재부팅", "")))
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
        self.assertIn("네트워크 및 시스템 점검 &gt; 시스템 및 업무 서비스", t)
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
        self.post("/", action="edit", item_id="3", reason="정정", **form(r3=("1", "재부팅", "")))
        self.assertIsNone(self.row("SELECT approved_by FROM days")["approved_by"])


class ReviewFixTest(Base):
    def lead(self):
        c = appmod.app.test_client()
        self.login("lead", client=c)
        return c

    def submit_all(self):
        self.login("m1")
        self.post("/", action="submit", **form(r3=("0", "", ""), r4=("0", "", "")))
        m2 = appmod.app.test_client()
        self.login("m2", client=m2)
        self.post("/", client=m2, action="submit", **form(r6=("0", "", ""), r7=("0", "", "")))

    def test_kind_change_refused_when_results_exist(self):
        self.submit_all()
        self.post("/items/3", parent_id="2", title="백본", is_group="1", active="1")
        self.assertEqual(self.row("SELECT is_group FROM items WHERE id = 3")["is_group"], 0)
        self.assertIn("기록이 있는 항목", self.text(self.c.get("/items")))

    def test_new_item_not_required_on_past_days(self):
        self.submit_all()
        appmod.app.config["TODAY"] = "2026-09-24"
        self.post("/items", parent_id="2", title="지점 네트워크", is_group="0", owner_id="1")
        self.c.get("/items")  # 추가 알림 소비
        self.assertNotIn("지점 네트워크", self.text(self.c.get(f"/?day={DAY}&view=all")))
        self.post(f"/approve/{DAY}", client=self.lead())
        self.assertEqual(self.row("SELECT approved_by FROM days")["approved_by"], 3)

    def test_deactivated_item_still_required_before_retirement(self):
        self.login("m1")
        self.post("/", action="submit", **form(r3=("0", "", "")))
        appmod.app.config["TODAY"] = "2026-09-24"
        self.post("/items/4", parent_id="2", title="인터넷 방화벽,스위치 상태", is_group="0", owner_id="1")
        self.assertIn("인터넷 방화벽", self.text(self.c.get(f"/?day={DAY}&view=all")))
        self.assertNotIn("인터넷 방화벽", self.text(self.c.get("/?view=all")))

    def test_deactivated_group_kept_on_past_days(self):
        appmod.app.config["TODAY"] = "2026-09-24"
        self.login("m1")
        self.post("/items/2", parent_id="1", title="네트워크상태", is_group="1")
        self.assertNotIn("백본 및", self.text(self.c.get("/?view=all")))
        self.assertIn("백본 및", self.text(self.c.get(f"/?day={DAY}&view=all")))

    def test_retired_items_draft_does_not_block_approval(self):
        self.login("m1")
        self.post("/", action="save", **form(r4=("1", "", "")))
        self.post("/", action="submit", **form(r3=("0", "", "")))
        m2 = appmod.app.test_client()
        self.login("m2", client=m2)
        self.post("/", client=m2, action="submit", **form(r6=("0", "", ""), r7=("0", "", "")))
        self.post("/items/4", parent_id="2", title="인터넷 방화벽,스위치 상태", is_group="0", owner_id="1")
        self.post(f"/approve/{DAY}", client=self.lead())
        self.assertEqual(self.row("SELECT approved_by FROM days")["approved_by"], 3)

    def test_submit_after_approval_unapproves(self):
        self.submit_all()
        self.post(f"/approve/{DAY}", client=self.lead())
        self.post("/items", parent_id="2", title="지점 네트워크", is_group="0", owner_id="1")
        new_id = self.row("SELECT id FROM items WHERE title = '지점 네트워크'")["id"]
        self.post("/", action="submit", **{"row": [str(new_id)], f"issue_{new_id}": "0",
                                           f"remark_{new_id}": "", f"base_{new_id}": ""})
        self.assertIsNone(self.row("SELECT approved_by FROM days")["approved_by"])
        self.assertEqual(self.row("SELECT action FROM audit_log WHERE target = 'day' ORDER BY id DESC")["action"], "unapprove")

    def test_editor_cannot_approve(self):
        self.submit_all()
        lead = self.lead()
        self.post("/", client=lead, action="edit", item_id="3", reason="정정", **form(r3=("1", "재부팅", "")))
        self.post(f"/approve/{DAY}", client=lead)
        self.assertIsNone(self.row("SELECT * FROM days"))

    def test_newer_draft_not_overwritten_by_stale_page(self):
        self.login("m1")
        self.post("/", action="save", **form(r3=("0", "처음", "")))
        stale = self.tok(3)
        self.post("/", action="save", **form(r3=("1", "나중", stale)))
        other = appmod.app.test_client()
        self.login("m2", client=other)
        self.post("/", client=other, action="submit", **form(r3=("0", "대무", stale)))
        res = self.row("SELECT * FROM results")
        self.assertEqual((res["remark"], res["status"]), ("나중", "draft"))


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
        self.assertTrue(t.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(t.lstrip("﻿"))))
        self.assertEqual(rows[0][1], "구분")
        self.assertEqual(len(rows), 5)  # 머리글 + 점검 항목 4개
        self.assertEqual(rows[1][1], "네트워크 및 시스템 점검 > 네트워크상태")
        self.assertEqual(rows[1][6], "'=HYPERLINK(\"http://x\")")
        self.assertEqual(rows[3][7], "미입력")

    def test_print_view_like_paper(self):
        t = self.text(self.c.get(f"/history?start={DAY}&format=print"))
        self.assertIn("네트워크상태", t)
        self.assertIn("백본 및 각 층 네트워크 상태", t)
        self.assertIn("확인자", t)

    def test_reversed_range_still_works(self):
        self.assertIn("2/4", self.text(self.c.get(f"/history?start=2026-09-24&end={DAY}")))


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

- [ ] **Step 2: 실패 확인** — `venv/Scripts/python -m unittest` → 다수 FAIL/ERROR

- [ ] **Step 3: `db.py`** — `items`·`results` 정의와 `init_db` 교체

```sql
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY,
  parent_id INTEGER REFERENCES items(id),  -- NULL = 맨 위
  sort INTEGER NOT NULL DEFAULT 0,         -- 같은 구분 안의 순서
  title TEXT NOT NULL,
  is_group INTEGER NOT NULL DEFAULT 0,
  owner_id INTEGER REFERENCES users(id),
  active INTEGER NOT NULL DEFAULT 1,
  created_on TEXT,   -- 이 날부터 점검 대상 (NULL = 처음부터)
  retired_on TEXT);  -- 미사용 처리한 날. 이 날부터 대상 아님
CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  path TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL,
  owner_name TEXT,
  checker_id INTEGER NOT NULL REFERENCES users(id),
  issue INTEGER CHECK(issue IN (0, 1)),
  remark TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK(status IN ('draft', 'submitted')),
  submitted_at TEXT,
  UNIQUE(date, item_id));
```

```python
def init_db(path):
    con = sqlite3.connect(path)
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    if cols and "parent_id" not in cols:
        con.close()
        raise RuntimeError("checklist.db가 이전 버전 형식입니다. 저장된 점검 기록이 없다면 파일을 지우고 다시 실행하세요.")
    con.executescript(SCHEMA)
    con.close()
```

- [ ] **Step 4: `app.py` — 번호 관련 코드를 트리 코드로 교체**

`def fmt_dt`부터 `def bulk_items` 끝까지(= `# ---- 오늘 점검` 앞까지)를 아래로 바꾼다.

```python
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


def item_form(db):
    """항목 폼 검증. (값 dict, None) 또는 (None, 오류 메시지)."""
    f = request.form
    title = f.get("title", "").strip()[:200]
    is_group = int(f.get("is_group") == "1")
    owner_id = None if is_group else f.get("owner_id", type=int)
    parent_id = f.get("parent_id", type=int)
    if not title:
        return None, "항목명을 입력하세요."
    if parent_id is not None:
        parent = db.execute("SELECT * FROM items WHERE id = ?", (parent_id,)).fetchone()
        if parent is None or not parent["is_group"] or not parent["active"]:
            return None, "하위 항목은 사용 중인 구분 아래에만 둘 수 있습니다."
    if not is_group:
        if owner_id is None:
            return None, "점검 항목은 담당자를 지정해야 합니다."
        if db.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (owner_id,)).fetchone() is None:
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
    return render_template(
        "items.html", items=items, add=request.args.get("add"), edit=edit,
        groups=[i for i in items if i["is_group"] and i["active"] and i["id"] not in blocked],
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


@app.post("/items/<int:item_id>")
@login_required()
def update_item(item_id):
    db = get_db()
    row = db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    values, error = item_form(db)
    if not error and values["parent_id"] in subtree_ids(db, item_id):
        error = "자기 자신이나 하위 항목 밑으로는 옮길 수 없습니다."
    if not error and values["is_group"] != row["is_group"]:
        if db.execute("SELECT 1 FROM results WHERE item_id = ?", (item_id,)).fetchone():
            error = "점검 기록이 있는 항목은 종류(구분/점검)를 바꿀 수 없습니다. 미사용 처리 후 새로 추가하세요."
        elif db.execute("SELECT 1 FROM items WHERE parent_id = ?", (item_id,)).fetchone():
            error = "하위 항목이 있는 구분은 점검 항목으로 바꿀 수 없습니다."
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
```

- [ ] **Step 5: `app.py` — 오늘 점검·출력 부분을 트리에 맞게 수정**

(a) `sheet_rows` 본문 교체:
```python
def sheet_rows(db, day):
    """그날 표의 줄(트리 순서). 점검 줄에는 'result'를 붙이고, 제출된 줄은 제출 당시 항목명·담당자·구분 경로를 보여준다.
    그날 대상이 아닌 항목(추가 전·미사용 후, 또는 상위 구분이 그런 경우)은 제출 기록이 있을 때만 나온다."""
    results = {r["item_id"]: dict(r) for r in db.execute(
        "SELECT r.*, u.name AS checker_name FROM results r JOIN users u ON u.id = r.checker_id WHERE r.date = ?",
        (day,))}
    rows, alive = [], {}
    for it in load_items(db, active_only=False):
        alive[it["id"]] = existed(it, day) and (it["parent_id"] is None or alive.get(it["parent_id"], False))
        it["path_text"] = " > ".join(it["path"])
        res = results.get(it["id"])
        if it["is_group"]:
            if alive[it["id"]]:
                rows.append(it)
            continue
        if res and res["status"] == "submitted":
            it.update(title=res["title"], owner_name=res["owner_name"], path_text=res["path"])
        elif not alive[it["id"]]:
            continue
        it["result"] = res
        rows.append(it)
    return rows
```

(b) `keep_with_groups` 교체:
```python
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
```

(c) `save_rows`:
- 오류 문구 `f"{it['code']} 항목: 이상 '유'는 비고를 입력해야 합니다."` → `f"'{it['title']}' 항목: 이상 '유'는 비고를 입력해야 합니다."`
- `vals`의 `"code": it["code"]` → `"path": " > ".join(it["path"])`
- INSERT의 열 `code` → `path`, 값 `:code` → `:path`; UPDATE의 `code = :code` → `path = :path`
- 제출 감사로그 `after={"date": day, "code": it["code"], ...}` → `after={"date": day, "title": it["title"], ...}`

(d) `edit_row`의 감사로그 `"code": res["code"]` 두 곳 → `"title": res["title"]`

(e) CSV: `CSV_HEADER`의 `"번호"` → `"구분"`, 행의 `r["code"]` → `r["path_text"]`

- [ ] **Step 6: 템플릿**

`templates/items.html` 전체:
```html
{% extends "base.html" %}
{% block content %}
{% macro csrf() %}<input type="hidden" name="csrf" value="{{ csrf_token() }}">{% endmacro %}
{% macro owner_select(current) %}
<select name="owner_id"><option value="">담당자</option>
  {% for u in users %}<option value="{{ u.id }}"{% if u.id == current %} selected{% endif %}>{{ u.name }}{% if not u.active %} (미사용){% endif %}</option>{% endfor %}
</select>
{% endmacro %}
{% macro add_form(parent_id, depth, default_group) %}
<tr class="add-row" id="add"><td colspan="4" style="padding-left: {{ 0.9 + depth * 1.4 }}rem">
  <form method="post" action="{{ url_for('add_item') }}" class="inline-form">{{ csrf() }}
    <input type="hidden" name="parent_id" value="{{ parent_id if parent_id is not none else '' }}">
    <input name="title" placeholder="항목명" required autofocus>
    <label><input type="radio" name="is_group" value="0"{% if not default_group %} checked{% endif %}> 점검</label>
    <label><input type="radio" name="is_group" value="1"{% if default_group %} checked{% endif %}> 구분</label>
    {{ owner_select(None) }}
    <button class="primary">추가</button> <a href="{{ url_for('items_page') }}">취소</a>
  </form></td></tr>
{% endmacro %}
<h1>항목 관리</h1>
<p><small><b>■ 구분</b>은 제목줄, <b>· 점검</b>은 담당자가 이상 유/무를 입력하는 줄입니다.
  구분 옆 <b>+하위</b>로 그 아래에 항목을 추가하고, <b>↑↓</b>로 순서를 바꿉니다.</small></p>
<table class="items">
  <tr><th>항목</th><th>담당자</th><th>상태</th><th></th></tr>
  {% for i in items %}
  <tr class="{{ 'group' if i.is_group }}{{ ' inactive' if not i.active }}">
    <td style="padding-left: {{ 0.9 + i.depth * 1.4 }}rem">{{ '■' if i.is_group else '·' }} {{ i.title }}</td>
    <td class="nowrap">{{ i.owner_name or '' }}</td>
    <td>{% if not i.active %}<span class="badge st-none">미사용</span>{% endif %}</td>
    <td class="nowrap tree-actions">
      {% if i.is_group and i.active %}<a href="{{ url_for('items_page', add=i.id) }}#add">+하위</a>{% endif %}
      <form method="post" action="{{ url_for('move_item', item_id=i.id) }}">{{ csrf() }}<button name="direction" value="up" class="small" title="위로">↑</button><button name="direction" value="down" class="small" title="아래로">↓</button></form>
      <a href="{{ url_for('items_page', edit=i.id) }}#edit">수정</a>
    </td>
  </tr>
  {% if edit == i.id %}
  <tr class="add-row" id="edit"><td colspan="4" style="padding-left: {{ 0.9 + i.depth * 1.4 }}rem">
    <form method="post" action="{{ url_for('update_item', item_id=i.id) }}" class="inline-form">{{ csrf() }}
      <input name="title" value="{{ i.title }}" required>
      <select name="is_group"><option value="0"{% if not i.is_group %} selected{% endif %}>점검</option><option value="1"{% if i.is_group %} selected{% endif %}>구분</option></select>
      {{ owner_select(i.owner_id) }}
      <select name="parent_id" title="상위 구분"><option value="">(맨 위)</option>
        {% for p in groups %}<option value="{{ p.id }}"{% if p.id == i.parent_id %} selected{% endif %}>{{ '　' * p.depth }}{{ p.title }}</option>{% endfor %}</select>
      <label><input type="checkbox" name="active" value="1"{% if i.active %} checked{% endif %}> 사용</label>
      <button class="primary">저장</button> <a href="{{ url_for('items_page') }}">취소</a>
    </form></td></tr>
  {% endif %}
  {% if add == i.id|string %}{{ add_form(i.id, i.depth + 1, False) }}{% endif %}
  {% else %}
  <tr><td colspan="4">등록된 항목이 없습니다. 아래 들여쓰기 붙여넣기로 한 번에 등록하거나 맨 위 구분을 추가하세요.</td></tr>
  {% endfor %}
  {% if add == 'top' %}{{ add_form(None, 0, True) }}{% endif %}
</table>
<p><a href="{{ url_for('items_page', add='top') }}#add">+ 맨 위 구분 추가</a></p>

<h2>들여쓰기 붙여넣기</h2>
<form method="post" action="{{ url_for('bulk_items') }}" class="card">{{ csrf() }}
  <p><small>들여쓰기(스페이스·탭)로 계층을 나타내고, 점검 항목은 줄 끝에 <code>(담당자ID)</code>를 붙이세요. 괄호가 없으면 구분(제목줄)이 됩니다.
    기존 목록 맨 뒤에 추가되며, 한 줄이라도 틀리면 아무것도 등록되지 않습니다.</small></p>
  <textarea name="text" rows="9" placeholder="네트워크 및 시스템 점검&#10;    네트워크상태&#10;        백본 및 각 층 네트워크 상태 (kim)&#10;        인터넷 방화벽,스위치 상태 (kim)"></textarea>
  <p class="actions"><button class="primary">붙여넣은 목록 등록</button></p>
</form>
{% endblock %}
```

`templates/index.html` — 표 부분(`<table class="sheet">`부터 `</table>`까지)을 교체:
```html
  <table class="sheet">
    <tr><th>점검 항목</th><th>담당자</th><th>이상</th><th>비고</th><th>상태</th></tr>
    {% for r in rows %}
    {% if r.is_group %}
    <tr class="group"><td colspan="5" style="padding-left: {{ 0.9 + r.depth * 1.4 }}rem">{{ r.title }}</td></tr>
    {% else %}
    {% set res = r.result %}
    {% set open = (writable and not (res and res.status == 'submitted')) or editing == r.id %}
    <tr>
      <td style="padding-left: {{ 0.9 + r.depth * 1.4 }}rem">{{ r.title }}</td>
      <td class="nowrap">{{ r.owner_name or '-' }}{% if res and res.checker_id != r.owner_id %}<br><small>점검: {{ res.checker_name }}</small>{% endif %}</td>
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
    <tr><td colspan="5">{% if view == 'mine' %}내 담당 항목이 없습니다. '전체'를 눌러 보세요.{% else %}등록된 점검 항목이 없습니다. '항목 관리'에서 추가하세요.{% endif %}</td></tr>
    {% endfor %}
  </table>
```

`templates/dashboard.html` — 이상 항목·미제출 항목 표의 `<th>번호</th>` → `<th>구분</th>`, `<td class="code">{{ r.code }}</td>` → `<td><small>{{ r.path_text }}</small></td>` (두 곳 모두).

`templates/print.html` — 표 부분 교체:
```html
  <table class="sheet">
    <tr><th>점검 항목</th><th>담당자</th><th>점검자</th><th>이상</th><th>비고</th></tr>
    {% for r in d.sheet %}
    {% if r.is_group %}
    <tr class="group"><td colspan="5" style="padding-left: {{ 0.9 + r.depth * 1.4 }}rem">{{ r.title }}</td></tr>
    {% else %}
    {% set res = r.result if (r.result and r.result.status == 'submitted') else None %}
    <tr><td style="padding-left: {{ 0.9 + r.depth * 1.4 }}rem">{{ r.title }}</td>
      <td class="nowrap">{{ r.owner_name or '-' }}</td><td class="nowrap">{{ res.checker_name if res else '' }}</td>
      <td class="nowrap">{% if res %}{{ '유' if res.issue else '무' }}{% else %}미제출{% endif %}</td>
      <td class="remark">{{ res.remark if res else '' }}</td></tr>
    {% endif %}
    {% endfor %}
  </table>
```

- [ ] **Step 7: `static/style.css`** — `/* 계층 점검표 */` 블록 교체

```css
/* 계층 점검표·항목 관리 */
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
.tree-actions form { display: inline; }
.tree-actions a { margin: 0 .35rem; }
.add-row td { background: #fffbea !important; }
.inline-form { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
.inline-form input[name=title] { min-width: 18rem; }
```

- [ ] **Step 8: 통과 확인** — `venv/Scripts/python -m unittest` → 전부 OK
- [ ] **Step 9: Commit** — `feat: 번호 없는 트리 항목(+하위, ↑↓, 들여쓰기 붙여넣기)`

---

### Task 2: 문서와 화면 확인

- [ ] **Step 1: README "처음 쓸 때" 3번 교체**
````markdown
3. 항목 관리 아래 **들여쓰기 붙여넣기**에 종이 점검표를 옮겨 적는다. 들여쓰기로 계층, 줄 끝 `(담당자ID)`가 있으면 점검 항목.
   ```
   네트워크 및 시스템 점검
       네트워크상태
           백본 및 각 층 네트워크 상태 (kim)
           인터넷 방화벽,스위치 상태 (kim)
   ```
   이후 추가·순서 변경은 항목 관리 화면의 **+하위**, **↑↓**, **수정** 버튼으로 한다.
````
- [ ] **Step 2: 화면 확인** — 샘플 트리로 항목 관리(+하위 열림, 수정 열림), 오늘 점검, 인쇄를 헤드리스 Edge 스크린샷으로 확인
- [ ] **Step 3: 전체 테스트 → Commit** — `docs: v3 사용법`
