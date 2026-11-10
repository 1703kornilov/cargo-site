#!/usr/bin/env python3
"""Бэкенд «Планировщика грузового пространства».

Без внешних библиотек: только стандартный Python 3.
Делает: регистрацию и вход по почте и паролю, сессии в cookie, историю расчётов,
отдаёт саму страницу (static/index.html).

Запуск:   python server.py            (порт 8000, база data/app.db)
Статистика: python server.py stats
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"
DB_PATH = Path(os.environ.get("DB_PATH", BASE / "data" / "app.db"))
PORT = int(os.environ.get("PORT", "8000"))
HOST = os.environ.get("HOST", "0.0.0.0")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"  # за Caddy: берём IP из X-Forwarded-For
SESSION_DAYS = 30
MAX_HISTORY_PER_USER = 100
MAX_BODY = 200 * 1024
MAX_SNAPSHOT = 120 * 1024

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
_local = threading.local()


# ---------- база данных ----------
def db():
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def q1(sql, params=()):
    """Одна строка (или None); запрос дочитывается до конца, чтобы не держать блокировку."""
    rows = db().execute(sql, params).fetchall()
    return rows[0] if rows else None


def close_db():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        finally:
            _local.conn = None


def init_db():
    c = db()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            pw_salt BLOB NOT NULL,
            pw_hash BLOB NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id DESC);
        """
    )
    c.commit()


# ---------- пароли и сессии ----------
def hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)


def create_user(email: str, password: str):
    salt = secrets.token_bytes(16)
    c = db()
    cur = c.execute(
        "INSERT INTO users(email, pw_salt, pw_hash, created_at) VALUES (?,?,?,?)",
        (email, salt, hash_password(password, salt), int(time.time())),
    )
    c.commit()
    return cur.lastrowid


def check_login(email: str, password: str):
    row = q1("SELECT id, pw_salt, pw_hash FROM users WHERE email=?", (email,))
    if row is None:
        # тратим то же время, чтобы нельзя было узнать, есть ли такая почта
        hash_password(password, b"\x00" * 16)
        return None
    if hmac.compare_digest(hash_password(password, row["pw_salt"]), row["pw_hash"]):
        return row["id"]
    return None


def new_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    c = db()
    c.execute("INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?,?,?)",
              (th, user_id, int(time.time()) + SESSION_DAYS * 86400))
    c.execute("DELETE FROM sessions WHERE expires_at < ?", (int(time.time()),))
    c.commit()
    return token


def user_by_token(token: str):
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    return q1(
        "SELECT u.id, u.email FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token_hash=? AND s.expires_at>?", (th, int(time.time()))
    )


def drop_session(token: str):
    if token:
        c = db()
        c.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))
        c.commit()


# ---------- ограничение частоты попыток ----------
_attempts = {}
_attempts_lock = threading.Lock()


def too_many(key: str, limit: int, window: int) -> bool:
    now = time.time()
    with _attempts_lock:
        lst = [t for t in _attempts.get(key, []) if now - t < window]
        if len(lst) >= limit:
            _attempts[key] = lst
            return True
        lst.append(now)
        _attempts[key] = lst
        if len(_attempts) > 5000:  # чтобы память не росла бесконечно
            for k in [k for k, v in _attempts.items() if not v or now - v[-1] > window]:
                _attempts.pop(k, None)
    return False


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "CargoPlanner"
    sys_version = ""

    def finish(self):
        try:
            super().finish()
        finally:
            close_db()  # один запрос - одно соединение с базой

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_ip(), fmt % args))

    # --- вспомогательное ---
    def client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[-1].strip()
        return self.client_address[0]

    def is_https(self):
        return TRUST_PROXY and self.headers.get("X-Forwarded-Proto", "") == "https"

    def token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid":
                return v
        return ""

    def current_user(self):
        return user_by_token(self.token())

    def send_json(self, code, obj, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def cookie(self, value, max_age):
        c = "sid=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (value, max_age)
        if self.is_https():
            c += "; Secure"
        return c

    def read_json(self):
        # JSON-тип нужен, чтобы чужой сайт не мог отправить запрос от имени пользователя
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_json(415, {"error": "Нужен JSON"})
            return None
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self.send_json(403, {"error": "Запрос с чужого сайта"})
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self.send_json(413, {"error": "Слишком большой запрос"})
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self.send_json(400, {"error": "Некорректный JSON"})
            return None
        if not isinstance(data, dict):
            self.send_json(400, {"error": "Некорректный запрос"})
            return None
        return data

    # --- маршруты ---
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/me":
            u = self.current_user()
            return self.send_json(200, {"user": {"email": u["email"]} if u else None})
        if path == "/api/history":
            u = self.current_user()
            if not u:
                return self.send_json(401, {"error": "Нужно войти"})
            rows = db().execute(
                "SELECT id, title, summary, created_at FROM history WHERE user_id=? ORDER BY id DESC", (u["id"],)
            ).fetchall()
            return self.send_json(200, {"items": [dict(r) for r in rows]})
        m = re.fullmatch(r"/api/history/(\d+)", path)
        if m:
            u = self.current_user()
            if not u:
                return self.send_json(401, {"error": "Нужно войти"})
            r = q1("SELECT id, title, data FROM history WHERE id=? AND user_id=?",
                   (int(m.group(1)), u["id"]))
            if not r:
                return self.send_json(404, {"error": "Не найдено"})
            return self.send_json(200, {"id": r["id"], "title": r["title"], "data": json.loads(r["data"])})
        if path == "/healthz":
            return self.send_json(200, {"ok": True})
        if path.startswith("/api/"):
            return self.send_json(404, {"error": "Не найдено"})
        return self.serve_static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/register":
            return self.register()
        if path == "/api/login":
            return self.login()
        if path == "/api/logout":
            if self.read_json() is None:
                return
            drop_session(self.token())
            return self.send_json(200, {"ok": True}, cookie=self.cookie("", 0))
        if path == "/api/history":
            return self.save_history()
        self.send_json(404, {"error": "Не найдено"})

    def do_DELETE(self):
        m = re.fullmatch(r"/api/history/(\d+)", urlparse(self.path).path)
        if not m:
            return self.send_json(404, {"error": "Не найдено"})
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_json(403, {"error": "Запрос с чужого сайта"})
        u = self.current_user()
        if not u:
            return self.send_json(401, {"error": "Нужно войти"})
        c = db()
        c.execute("DELETE FROM history WHERE id=? AND user_id=?", (int(m.group(1)), u["id"]))
        c.commit()
        self.send_json(200, {"ok": True})

    # --- действия ---
    @staticmethod
    def parse_credentials(data):
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        return email, password

    def register(self):
        data = self.read_json()
        if data is None:
            return
        if too_many("reg:" + self.client_ip(), 10, 3600):
            return self.send_json(429, {"error": "Слишком много попыток. Попробуйте позже."})
        email, password = self.parse_credentials(data)
        if not EMAIL_RE.match(email) or len(email) > 254:
            return self.send_json(400, {"error": "Проверьте адрес почты"})
        if len(password) < 8:
            return self.send_json(400, {"error": "Пароль должен быть не короче 8 символов"})
        if len(password) > 200:
            return self.send_json(400, {"error": "Пароль слишком длинный"})
        try:
            uid = create_user(email, password)
        except sqlite3.IntegrityError:
            return self.send_json(409, {"error": "Эта почта уже зарегистрирована. Войдите."})
        tok = new_session(uid)
        self.send_json(200, {"user": {"email": email}}, cookie=self.cookie(tok, SESSION_DAYS * 86400))

    def login(self):
        data = self.read_json()
        if data is None:
            return
        ip = self.client_ip()
        email, password = self.parse_credentials(data)
        if too_many("login:" + ip, 20, 600) or too_many("loginmail:" + email, 8, 600):
            return self.send_json(429, {"error": "Слишком много попыток. Подождите 10 минут."})
        uid = check_login(email, password[:200])
        if uid is None:
            return self.send_json(401, {"error": "Неверная почта или пароль"})
        tok = new_session(uid)
        self.send_json(200, {"user": {"email": email}}, cookie=self.cookie(tok, SESSION_DAYS * 86400))

    def save_history(self):
        data = self.read_json()
        if data is None:
            return
        u = self.current_user()
        if not u:
            return self.send_json(401, {"error": "Нужно войти"})
        title = str(data.get("title", "")).strip()[:120] or "Расчёт"
        summary = str(data.get("summary", "")).strip()[:300]
        snap = data.get("data")
        if not isinstance(snap, dict):
            return self.send_json(400, {"error": "Нет данных расчёта"})
        raw = json.dumps(snap, ensure_ascii=False)
        if len(raw.encode("utf-8")) > MAX_SNAPSHOT:
            return self.send_json(413, {"error": "Расчёт слишком большой"})
        c = db()
        n = q1("SELECT COUNT(*) FROM history WHERE user_id=?", (u["id"],))[0]
        if n >= MAX_HISTORY_PER_USER:
            return self.send_json(409, {"error": "В истории уже %d расчётов. Удалите ненужные." % MAX_HISTORY_PER_USER})
        cur = c.execute("INSERT INTO history(user_id, title, summary, data, created_at) VALUES (?,?,?,?,?)",
                        (u["id"], title, summary, raw, int(time.time())))
        c.commit()
        self.send_json(200, {"id": cur.lastrowid})

    # --- статика ---
    def serve_static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC / path.lstrip("/")).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            return self.send_json(404, {"error": "Не найдено"})
        ctype = {".html": "text/html; charset=utf-8", ".png": "image/png", ".ico": "image/x-icon",
                 ".svg": "image/svg+xml", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(body)


def stats():
    init_db()
    c = db()
    users = q1("SELECT COUNT(*) FROM users")[0]
    hist = q1("SELECT COUNT(*) FROM history")[0]
    print("Пользователей: %d, сохранённых расчётов: %d" % (users, hist))
    for r in c.execute("SELECT email, datetime(created_at,'unixepoch') d FROM users ORDER BY id DESC LIMIT 20"):
        print("  %s  %s" % (r["d"], r["email"]))


def main():
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Слушаю %s:%d, база %s" % (HOST, PORT, DB_PATH), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        stats()
    else:
        main()
