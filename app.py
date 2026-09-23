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
