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
