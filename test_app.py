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
