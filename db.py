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
CREATE TABLE IF NOT EXISTS days(
  date TEXT PRIMARY KEY,
  approved_by INTEGER REFERENCES users(id),
  approved_at TEXT);
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
    cols = [r[1] for r in con.execute("PRAGMA table_info(items)")]
    if cols and "parent_id" not in cols:
        con.close()
        raise RuntimeError("checklist.db가 이전 버전 형식입니다. 저장된 점검 기록이 없다면 파일을 지우고 다시 실행하세요.")
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
