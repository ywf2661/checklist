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
