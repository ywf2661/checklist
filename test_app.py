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

    def test_one_check_per_system_per_day(self):
        self.exec("INSERT INTO checks(date, system_id, user_id, status) VALUES(?, 1, 1, 'draft')", DAY)
        with self.assertRaises(sqlite3.IntegrityError):
            self.exec("INSERT INTO checks(date, system_id, user_id, status) VALUES(?, 1, 2, 'draft')", DAY)


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


if __name__ == "__main__":
    unittest.main()
