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


class ReviewFixTest(Base):
    def test_inactive_system_refuses_new_check(self):
        self.login("m1")
        self.exec("UPDATE systems SET active = 0 WHERE id = 1")
        t = self.text(self.post(URL, action="submit", item=["1", "2"]))
        self.assertIn("사용하지 않는 시스템", t)
        self.assertIsNone(self.row("SELECT * FROM checks"))

    def test_leader_cannot_approve_own_check(self):
        self.login("lead")
        self.post(URL, action="submit", item=["1", "2"])
        self.post("/approve/1")
        self.assertEqual(self.row("SELECT status FROM checks")["status"], "submitted")
        self.assertIn("본인이 제출한", self.text(self.c.get("/dashboard")))

    def test_stale_page_does_not_overwrite_other_users_draft(self):
        self.login("m1")
        self.post(URL, action="save", item=["1"], remark="m1 메모")
        other = appmod.app.test_client()
        self.login("m2", client=other)
        t = self.text(self.post(URL, client=other, action="save", item=["2"], remark="m2"))
        self.assertIn("M1님이 작성 중", t)
        c = self.row("SELECT * FROM checks")
        self.assertEqual((c["user_id"], c["remark"]), (1, "m1 메모"))
        # 새로고침해서 m1의 작성 내용을 본 뒤에는 이어받아 저장할 수 있다(대무)
        self.assertIn('name="draft_user" value="1"', self.text(other.get(URL)))
        self.post(URL, client=other, action="save", item=["1", "2"], remark="m1 메모", draft_user="1")
        self.assertEqual(self.row("SELECT user_id FROM checks")["user_id"], 2)


class StyleTest(Base):
    def test_status_badges_have_color_classes(self):
        self.assertEqual(appmod.status_class(None, 0), "st-none")
        self.assertEqual(appmod.status_class("draft", 0), "st-draft")
        self.assertEqual(appmod.status_class("submitted", 0), "st-submitted")
        self.assertEqual(appmod.status_class("submitted", 1), "st-issue")
        self.assertEqual(appmod.status_class("approved", 1), "st-approved")
        self.login("m1")
        self.assertIn('class="badge st-none"', self.text(self.c.get("/")))

    def test_timestamps_display_without_t(self):
        self.assertEqual(appmod.fmt_dt("2026-09-23T16:01:55"), "2026-09-23 16:01")
        self.assertEqual(appmod.fmt_dt(None), "-")


class BackupTest(Base):
    def test_backup_creates_consistent_copy(self):
        out = tempfile.mkdtemp()
        path = appmod.backup(out)
        con = sqlite3.connect(path)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 4)
        con.close()


if __name__ == "__main__":
    unittest.main()
