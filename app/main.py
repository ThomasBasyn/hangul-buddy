"""Hangul Buddy - backend voor de Koreaanse woordjestrainer.

Dezelfde API wordt gebruikt door de web-app en later door het ESP32-toestel.
"""

import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
import wave
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import azure_speech
from srs import Card, review, score_to_grade, similarity

# ---------------------------------------------------------------- config
BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "hangul.db"
TTS_CACHE = DATA_DIR / "tts"
LESSONS_FILE = Path(os.getenv("LESSONS_FILE", BASE / "content" / "lessons.json"))
NEW_PER_DAY = int(os.getenv("NEW_PER_DAY", "5"))
AUTO_EASY = float(os.getenv("AUTO_EASY", "90"))    # automatisch beoordelen: vanaf hier Makkelijk
AUTO_RETRY = float(os.getenv("AUTO_RETRY", "80"))  # onder deze score: Moeilijk en opnieuw
APP_TOKEN = os.getenv("APP_TOKEN", "")  # toegangscode van de eigenaar (gebruiker 1)
KEY_SECRET = os.getenv("KEY_SECRET", "") or APP_TOKEN  # versleutelt de Azure-sleutels van gebruikers
OWNER_NAME = os.getenv("OWNER_NAME", "Eigenaar")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Brussels"))
MAX_WAV_BYTES = 2_000_000  # ±60 s audio op 16 kHz

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hangul")


# ---------------------------------------------------------------- lesinhoud
# Elke taal heeft een eigen cursus. Koreaanse item-ids blijven zoals ze waren ("1-1"); bij de andere
# talen krijgt elk id een voorvoegsel ("de:1-1"), zodat voortgang per woord uniek blijft.
LANGS = {
    "ko": {"name": "Koreaans", "flag": "🇰🇷", "locale": "ko-KR", "voice": azure_speech.VOICE, "file": LESSONS_FILE},
    "de": {"name": "Duits", "flag": "🇩🇪", "locale": "de-DE", "voice": "de-DE-KatjaNeural", "file": BASE / "content" / "de.json"},
    "it": {"name": "Italiaans", "flag": "🇮🇹", "locale": "it-IT", "voice": "it-IT-ElsaNeural", "file": BASE / "content" / "it.json"},
    "ja": {"name": "Japans", "flag": "🇯🇵", "locale": "ja-JP", "voice": "ja-JP-NanamiNeural", "file": BASE / "content" / "ja.json"},
    "zh": {"name": "Chinees", "flag": "🇨🇳", "locale": "zh-CN", "voice": "zh-CN-XiaoxiaoNeural", "file": BASE / "content" / "zh.json"},
}
DEFAULT_LANG = "ko"


def load_course(lang: str) -> dict:
    meta = LANGS[lang]
    course = json.loads(Path(meta["file"]).read_text(encoding="utf-8"))
    seen = set()
    for order, lesson in enumerate(course["lessons"]):
        lesson["order"] = order
        for i, item in enumerate(lesson["items"]):
            if item["id"] in seen:
                raise SystemExit(f"Dubbel item-id in lessen ({lang}): {item['id']}")
            seen.add(item["id"])
            if lang != DEFAULT_LANG:
                item["id"] = f"{lang}:{item['id']}"
            item["text"] = item.get("text") or item["ko"]
            item["ko"] = item["text"]  # oude naam, het toestel gebruikt die nog
            item["lang"] = lang
            item["lesson"] = lesson["id"]
            item["pos"] = i
    course.update(lang=lang, name=meta["name"], flag=meta["flag"], locale=meta["locale"], voice=meta["voice"])
    course["items"] = {it["id"]: it for les in course["lessons"] for it in les["items"]}
    course["lesson_by_id"] = {les["id"]: les for les in course["lessons"]}
    return course


COURSES = {lang: load_course(lang) for lang in LANGS}
COURSE = COURSES[DEFAULT_LANG]
LESSONS = COURSE["lessons"]
ITEMS = {iid: it for c in COURSES.values() for iid, it in c["items"].items()}


def course_for(user: dict) -> dict:
    return COURSES.get(user.get("lang") or DEFAULT_LANG, COURSE)


# ---------------------------------------------------------------- database
def today() -> date:
    return datetime.now(TZ).date()


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    token_hash    TEXT UNIQUE,
    azure_key_enc TEXT,
    azure_region  TEXT,
    created       TEXT NOT NULL,
    pw_hash       TEXT,
    lang          TEXT NOT NULL DEFAULT 'ko'
);
CREATE TABLE IF NOT EXISTS progress (
    user_id     INTEGER NOT NULL DEFAULT 1,
    item_id     TEXT NOT NULL,
    ease        REAL NOT NULL DEFAULT 2.5,
    interval    INTEGER NOT NULL DEFAULT 0,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    due         TEXT NOT NULL,
    learned     INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_review TEXT,
    last_score  REAL,
    PRIMARY KEY (user_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_progress_user_due ON progress(user_id, due);
CREATE TABLE IF NOT EXISTS review_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL DEFAULT 1,
    item_id  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    day      TEXT NOT NULL,
    grade    INTEGER NOT NULL,
    score    REAL,
    source   TEXT NOT NULL DEFAULT 'self'
);
CREATE INDEX IF NOT EXISTS idx_log_user_day ON review_log(user_id, day);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    created    TEXT NOT NULL,
    expires    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS xp_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    day     TEXT NOT NULL,
    amount  INTEGER NOT NULL,
    reason  TEXT NOT NULL,
    item_id TEXT,
    lang    TEXT NOT NULL DEFAULT 'ko'
);
CREATE INDEX IF NOT EXISTS idx_xp_user_day ON xp_log(user_id, day);
"""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    with db() as con:
        old_cols = {r["name"] for r in con.execute("PRAGMA table_info(progress)")}
        migrating = bool(old_cols) and "user_id" not in old_cols
        if migrating:
            # Database van vóór meerdere gebruikers: eerst een reservekopie, dan alles naar gebruiker 1
            log.info("Database migreren naar meerdere gebruikers (reservekopie naast hangul.db)")
            backup = sqlite3.connect(DB_PATH.with_name("hangul.db.bak-voor-gebruikers"))
            con.backup(backup)
            backup.close()
            con.execute("ALTER TABLE progress RENAME TO progress_old")
            con.execute("DROP INDEX IF EXISTS idx_progress_due")
            con.execute("DROP INDEX IF EXISTS idx_log_day")
            log_cols = {r["name"] for r in con.execute("PRAGMA table_info(review_log)")}
            if "user_id" not in log_cols:
                con.execute("ALTER TABLE review_log ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        had_xp = bool(con.execute("SELECT 1 FROM sqlite_master WHERE name='xp_log'").fetchone())
        con.executescript(SCHEMA)
        user_cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "pw_hash" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN pw_hash TEXT")
        if "lang" not in user_cols:
            con.execute("ALTER TABLE users ADD COLUMN lang TEXT NOT NULL DEFAULT 'ko'")
        if "lang" not in {r["name"] for r in con.execute("PRAGMA table_info(xp_log)")}:
            con.execute("ALTER TABLE xp_log ADD COLUMN lang TEXT NOT NULL DEFAULT 'ko'")
        if not had_xp:
            # Bestaande oefengeschiedenis telt mee: per woord per dag de beste beoordeling
            con.execute(
                """INSERT INTO xp_log (user_id, ts, day, amount, reason, item_id)
                   SELECT user_id, MAX(ts), day,
                          CASE WHEN MAX(grade) >= 4 THEN 10 WHEN MAX(grade) = 3 THEN 8 ELSE 2 END,
                          'backfill', item_id
                   FROM review_log GROUP BY user_id, item_id, day"""
            )
        if migrating:
            con.execute(
                """INSERT INTO progress (user_id, item_id, ease, interval, reps, lapses, due, learned,
                       first_seen, last_review, last_score)
                   SELECT 1, item_id, ease, interval, reps, lapses, due, learned,
                       first_seen, last_review, last_score FROM progress_old"""
            )
            con.execute("DROP TABLE progress_old")
        ensure_owner(con)


def ensure_owner(con) -> None:
    """Gebruiker 1 is de eigenaar: zijn toegangscode is APP_TOKEN (zonder APP_TOKEN is er geen login)."""
    token_hash = hash_token(APP_TOKEN) if APP_TOKEN else None
    row = con.execute("SELECT pw_hash FROM users WHERE id=1").fetchone()
    if row:
        con.execute("UPDATE users SET token_hash=? WHERE id=1", (token_hash,))
        if row["pw_hash"] is None:  # na het kiezen van een eigen gebruikersnaam blijft die staan
            try:
                con.execute("UPDATE users SET name=? WHERE id=1", (OWNER_NAME,))
            except sqlite3.IntegrityError:
                pass
    else:
        con.execute("INSERT INTO users (id, name, token_hash, created) VALUES (1,?,?,?)",
                    (OWNER_NAME, token_hash, now_iso()))


def progress_rows(con, user_id: int, course: dict | None = None) -> dict[str, sqlite3.Row]:
    """Voortgang van een gebruiker, desgewenst alleen voor de woorden van één cursus."""
    rows = {r["item_id"]: r for r in con.execute("SELECT * FROM progress WHERE user_id=?", (user_id,))}
    if course is not None:
        rows = {k: v for k, v in rows.items() if k in course["items"]}
    return rows


# ---------------------------------------------------------------- gebruikersbeheer (ook voor manage.py)
USERNAME_RE = re.compile(r"^\w[\w .\-]{1,19}$")


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not 1 <= len(name) <= 30:
        raise ValueError("naam moet 1 tot 30 tekens zijn")
    return name


def clean_username(name: str) -> str:
    name = " ".join(name.split())
    if not USERNAME_RE.match(name):
        raise ValueError("Gebruikersnaam: 2 tot 20 tekens (letters, cijfers, spatie, punt, streepje)")
    return name


def check_password(pw: str) -> None:
    if not 8 <= len(pw) <= 200:
        raise ValueError("Wachtwoord moet minstens 8 tekens zijn")


def name_taken(con, name: str, except_id: int = 0) -> bool:
    return bool(con.execute("SELECT 1 FROM users WHERE lower(name)=lower(?) AND id<>?", (name, except_id)).fetchone())


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(pw: str, stored: str | None) -> bool:
    try:
        _, salt, digest = (stored or "scrypt$00$00").split("$")
        dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1, dklen=32)
        return secrets.compare_digest(dk.hex(), digest) and stored is not None
    except ValueError:
        return False


def create_user(name: str) -> str:
    """Maak een gebruiker en geef zijn uitnodigingscode terug (wordt maar één keer getoond).

    Met die code kiest de gebruiker zelf een gebruikersnaam en wachtwoord."""
    if not APP_TOKEN:
        raise ValueError("Zet eerst APP_TOKEN in .env: zonder login kent de app maar één gebruiker")
    name = clean_name(name)
    token = secrets.token_urlsafe(12)
    with db() as con:
        if name_taken(con, name):
            raise ValueError(f"gebruiker '{name}' bestaat al")
        con.execute("INSERT INTO users (name, token_hash, created) VALUES (?,?,?)",
                    (name, hash_token(token), now_iso()))
    return token


def reset_token(name: str) -> str:
    """Wachtwoord vergeten: wist het wachtwoord en alle logins, en geeft een nieuwe uitnodigingscode."""
    token = secrets.token_urlsafe(12)
    with db() as con:
        row = con.execute("SELECT id FROM users WHERE lower(name)=lower(?)", (clean_name(name),)).fetchone()
        if not row:
            raise ValueError(f"gebruiker '{name}' niet gevonden")
        if row["id"] == 1:  # de eigenaar activeert opnieuw met APP_TOKEN
            con.execute("UPDATE users SET pw_hash=NULL WHERE id=1")
            con.execute("DELETE FROM sessions WHERE user_id=1")
            return ""
        con.execute("UPDATE users SET token_hash=?, pw_hash=NULL WHERE id=?", (hash_token(token), row["id"]))
        con.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
    return token


def remove_user(name: str) -> None:
    with db() as con:
        row = con.execute("SELECT id FROM users WHERE lower(name)=lower(?) AND id<>1", (clean_name(name),)).fetchone()
        if not row:
            raise ValueError(f"gebruiker '{name}' niet gevonden (de eigenaar kan niet verwijderd worden)")
        for table in ("progress", "review_log", "xp_log", "sessions"):
            con.execute(f"DELETE FROM {table} WHERE user_id=?", (row["id"],))
        con.execute("DELETE FROM users WHERE id=?", (row["id"],))


def list_users() -> list[dict]:
    with db() as con:
        return [dict(r) | {"words": con.execute("SELECT COUNT(*) FROM progress WHERE user_id=?", (r["id"],)).fetchone()[0]}
                for r in con.execute(
                    "SELECT id, name, azure_key_enc IS NOT NULL AS own_key, pw_hash IS NOT NULL AS active, created "
                    "FROM users ORDER BY id")]


def current_lesson(rows: dict, course: dict | None = None) -> dict | None:
    """Eerste les waarvan nog niet elk woord minstens één keer goed was."""
    for les in (course or COURSE)["lessons"]:
        if any(not (rows.get(it["id"]) and rows[it["id"]]["learned"]) for it in les["items"]):
            return les
    return None


# ---------------------------------------------------------------- auth
def current_user(
    x_app_token: str = Header(default=""),
    token: str = Query(default=""),
) -> dict:
    """De ingelogde gebruiker. Zonder APP_TOKEN is er geen login en is iedereen gebruiker 1.

    Het token is een sessie van een gebruiker (na inloggen) of de APP_TOKEN van de eigenaar (voor het toestel)."""
    with db() as con:
        if not APP_TOKEN:
            row = con.execute("SELECT * FROM users WHERE id=1").fetchone()
        else:
            given = x_app_token or token
            row = None
            if given:
                h = hash_token(given)
                row = con.execute(
                    "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?",
                    (h, now_iso())).fetchone()
                if not row:
                    row = con.execute("SELECT * FROM users WHERE id=1 AND token_hash=?", (h,)).fetchone()
    if not row:
        raise HTTPException(401, "ongeldige token")
    return dict(row)


# ---------------------------------------------------------------- inloggen
SESSION_DAYS = 90
MAX_FAILS, FAIL_WINDOW = 8, 600
_fails: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


def throttle_check(*keys: str) -> None:
    now = time.monotonic()
    for k in keys:
        _fails[k] = [x for x in _fails.get(k, []) if now - x < FAIL_WINDOW]
        if len(_fails[k]) >= MAX_FAILS:
            raise HTTPException(429, "Te veel pogingen. Wacht een paar minuten en probeer opnieuw.")


def throttle_fail(*keys: str) -> None:
    for k in keys:
        _fails.setdefault(k, []).append(time.monotonic())


def new_session(con, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(TZ)
    con.execute("DELETE FROM sessions WHERE expires<?", (now_iso(),))
    con.execute("INSERT INTO sessions (token_hash, user_id, created, expires) VALUES (?,?,?,?)",
                (hash_token(token), user_id, now_iso(), (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    return token


def fernet() -> Fernet | None:
    if not KEY_SECRET:
        return None
    digest = hashlib.sha256(("hangul-buddy-keys|" + KEY_SECRET).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def azure_creds(user: dict) -> tuple[str, str, str] | None:
    """(sleutel, regio, bron): eigen sleutel van de gebruiker, anders de gedeelde van de server."""
    if user.get("azure_key_enc"):
        f = fernet()
        if f:
            try:
                key = f.decrypt(user["azure_key_enc"].encode()).decode()
                return key, user.get("azure_region") or azure_speech.REGION, "own"
            except InvalidToken:
                log.error("Azure-sleutel van %s is onleesbaar (KEY_SECRET of APP_TOKEN veranderd?)", user["name"])
    if azure_speech.enabled():
        return azure_speech.KEY, azure_speech.REGION, "shared"
    return None


def azure_error(exc: httpx.HTTPError) -> HTTPException:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return HTTPException(502, "Azure weigert de sleutel. Controleer je sleutel en regio bij Instellingen.")
        if code == 429:
            return HTTPException(502, "Azure-limiet bereikt (te veel tegelijk, of je gratis tijd van deze maand is op).")
    return HTTPException(502, "Azure Speech gaf een fout")


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    log.info("%d talen, %d items geladen. Azure: %s", len(COURSES), len(ITEMS),
             "aan" if azure_speech.enabled() else "uit")
    yield


app = FastAPI(title="Hangul Buddy", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


def item_out(item: dict) -> dict:
    return {k: item.get(k) for k in ("id", "text", "ko", "rom", "nl", "tip", "lesson", "lang")}


def lesson_out(les: dict) -> dict:
    return {"id": les["id"], "title": les["title"], "note": les.get("note", "")}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config(user: dict = Depends(current_user)):
    creds = azure_creds(user)
    course = course_for(user)
    return {
        "azure": creds is not None,
        "azure_source": creds[2] if creds else None,
        "user": user["name"],
        "new_per_day": NEW_PER_DAY,
        "auto": {"easy": AUTO_EASY, "retry": AUTO_RETRY},
        "lang": course["lang"],
        "locale": course["locale"],
        "languages": [{"code": c, "name": m["name"], "flag": m["flag"]} for c, m in LANGS.items()],
        "course": course.get("course", ""),
        "lessons": len(course["lessons"]),
        "items": len(course["items"]),
    }


@app.get("/api/next")
def next_card(extra: bool = False, user: dict = Depends(current_user)):
    """Volgend woord: eerst herhalingen die vandaag verschijnen, dan nieuwe woorden."""
    t = today().isoformat()
    course = course_for(user)
    with db() as con:
        rows = progress_rows(con, user["id"], course)
        due = con.execute(
            """SELECT item_id, reps, learned FROM progress WHERE user_id = ? AND due <= ?
               ORDER BY COALESCE(last_review, ''), due""",
            (user["id"], t),
        ).fetchall()
        new_today = sum(1 for r in con.execute(
            "SELECT item_id FROM progress WHERE user_id = ? AND first_seen = ?", (user["id"], t)
        ) if r["item_id"] in course["items"])

    due = [r for r in due if r["item_id"] in course["items"]]
    les = current_lesson(rows, course)
    candidates = [it for it in les["items"] if it["id"] not in rows] if les else []
    capacity = len(candidates) if extra else max(0, NEW_PER_DAY - new_today)
    new_left = min(capacity, len(candidates))

    counts = {"due": len(due), "new_left": new_left, "new_today": new_today}
    if due:
        r = due[0]
        item = course["items"][r["item_id"]]
        kind = "relearn" if r["reps"] == 0 else "review"
        return {"done": False, "kind": kind, "item": item_out(item),
                "lesson": lesson_out(course["lesson_by_id"][item["lesson"]]),
                "show_note": False, "counts": counts}
    if new_left:
        item = candidates[0]
        lesson_started = any(it["id"] in rows for it in les["items"])
        return {"done": False, "kind": "new", "item": item_out(item),
                "lesson": lesson_out(les), "show_note": not lesson_started,
                "counts": counts}

    with db() as con:
        upcoming = [r["due"] for r in con.execute("SELECT item_id, due FROM progress WHERE user_id = ? AND due > ?",
                                                  (user["id"], t)) if r["item_id"] in course["items"]]
        nxt = min(upcoming) if upcoming else None
    return {"done": True, "counts": counts, "next_due": nxt,
            "more_new_available": bool(candidates), "course_finished": les is None}


class GradeIn(BaseModel):
    item_id: str
    grade: int = Field(ge=0, le=5)
    score: float | None = Field(default=None, ge=0, le=100)
    source: str = Field(default="self", max_length=20)


@app.post("/api/grade")
def grade(body: GradeIn, user: dict = Depends(current_user)):
    if body.item_id not in ITEMS:
        raise HTTPException(404, "onbekend item")
    t = today()
    with db() as con:
        row = con.execute("SELECT * FROM progress WHERE user_id=? AND item_id=?",
                          (user["id"], body.item_id)).fetchone()
        card = Card()
        first_seen = t.isoformat()
        learned = 0
        if row:
            card = Card(ease=row["ease"], interval=row["interval"], reps=row["reps"],
                        lapses=row["lapses"])
            first_seen, learned = row["first_seen"], row["learned"]
        review(card, body.grade, t)
        is_new = first_seen == t.isoformat()
        learned = 1 if (learned or body.grade >= 3) else 0
        ts = now_iso()
        con.execute(
            """INSERT INTO progress (user_id, item_id, ease, interval, reps, lapses, due, learned,
                   first_seen, last_review, last_score)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(user_id, item_id) DO UPDATE SET ease=excluded.ease,
                   interval=excluded.interval, reps=excluded.reps, lapses=excluded.lapses,
                   due=excluded.due, learned=excluded.learned, last_review=excluded.last_review,
                   last_score=COALESCE(excluded.last_score, progress.last_score)""",
            (user["id"], body.item_id, card.ease, card.interval, card.reps, card.lapses,
             card.due.isoformat(), learned, first_seen, ts, body.score),
        )
        con.execute(
            "INSERT INTO review_log (user_id, item_id, ts, day, grade, score, source) VALUES (?,?,?,?,?,?,?)",
            (user["id"], body.item_id, ts, t.isoformat(), body.grade, body.score, body.source),
        )
        gained = award_xp(con, user["id"], body.item_id, body.grade, body.score, is_new, t, ts)
        xp = xp_summary(con, user["id"], t)
        lang_xp = xp_summary(con, user["id"], t, ITEMS[body.item_id]["lang"])
    return {"item_id": body.item_id, "interval": card.interval, "due": card.due.isoformat(),
            "ease": round(card.ease, 2), "learned": bool(learned), "xp_gained": gained, "xp": xp,
            "lang": ITEMS[body.item_id]["lang"], "lang_xp": lang_xp}


# ---------------------------------------------------------------- uitspraak
def check_wav(data: bytes) -> None:
    if len(data) > MAX_WAV_BYTES:
        raise HTTPException(413, "opname te lang")
    try:
        with wave.open(io.BytesIO(data)) as w:
            ok = w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
            frames = w.getnframes()
    except (wave.Error, EOFError) as exc:
        raise HTTPException(400, "geen geldige WAV") from exc
    if not ok:
        raise HTTPException(400, "WAV moet 16 kHz, mono, 16-bit zijn")
    if frames < 16000 * 0.3:
        raise HTTPException(400, "opname te kort")


@app.post("/api/assess/{item_id}")
async def assess_audio(item_id: str, request: Request, user: dict = Depends(current_user)):
    """Uitspraak beoordelen met Azure. Body: ruwe WAV (16 kHz, mono, 16-bit)."""
    item = ITEMS.get(item_id)
    if not item:
        raise HTTPException(404, "onbekend item")
    creds = azure_creds(user)
    if not creds:
        raise HTTPException(503, "Azure Speech is niet ingesteld")
    data = await request.body()
    check_wav(data)
    try:
        result = await azure_speech.assess(data, item["text"], key=creds[0], region=creds[1],
                                           locale=COURSES[item["lang"]]["locale"])
    except httpx.HTTPError as exc:
        log.error("Azure-beoordeling mislukt voor %s (%s): %s", user["name"], creds[2], exc)
        raise azure_error(exc) from exc
    result["grade"] = score_to_grade(result["score"])
    result["provider"] = "azure"
    return result


class HeardIn(BaseModel):
    heard: str = Field(max_length=300)


@app.post("/api/assess-text/{item_id}")
def assess_text(item_id: str, body: HeardIn, user: dict = Depends(current_user)):
    """Fallback: vergelijk wat spraakherkenning hoorde met het woord (klank per klank)."""
    item = ITEMS.get(item_id)
    if not item:
        raise HTTPException(404, "onbekend item")
    score = similarity(item["text"], body.heard, item["lang"])
    return {"score": score, "heard": body.heard, "words": [], "grade": score_to_grade(score),
            "provider": "speech-recognition"}


@app.get("/api/tts/{item_id}")
async def tts(item_id: str, fmt: str = "mp3", user: dict = Depends(current_user)):
    """Uitspraak als audio. fmt=wav voor het ESP32-toestel. Wordt op schijf gecachet."""
    item = ITEMS.get(item_id)
    if not item:
        raise HTTPException(404, "onbekend item")
    if fmt not in azure_speech.FORMATS:
        raise HTTPException(400, "fmt moet mp3 of wav zijn")
    creds = azure_creds(user)
    if not creds:
        raise HTTPException(404, "geen server-TTS, gebruik de browserstem")
    course = COURSES[item["lang"]]
    key = hashlib.sha256(f"{course['voice']}|{item['text']}|{fmt}".encode()).hexdigest()[:24]
    path = TTS_CACHE / f"{key}.{fmt}"
    if not path.exists():
        try:
            audio = await azure_speech.tts(item["text"], fmt, key=creds[0], region=creds[1],
                                           voice=course["voice"], locale=course["locale"])
        except httpx.HTTPError as exc:
            log.error("Azure-TTS mislukt voor %s (%s): %s", user["name"], creds[2], exc)
            raise azure_error(exc) from exc
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(audio)
        tmp.replace(path)
    return FileResponse(path, media_type=azure_speech.FORMATS[fmt][1],
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------- overzicht
@app.get("/api/lessons")
def lessons(user: dict = Depends(current_user)):
    course = course_for(user)
    with db() as con:
        rows = progress_rows(con, user["id"], course)
    cur = current_lesson(rows, course)
    cur_order = cur["order"] if cur else len(course["lessons"])
    out = []
    for les in course["lessons"]:
        learned = sum(1 for it in les["items"] if rows.get(it["id"]) and rows[it["id"]]["learned"])
        out.append({
            **lesson_out(les),
            "total": len(les["items"]),
            "learned": learned,
            "unlocked": les["order"] <= cur_order,
            "current": cur is not None and les["id"] == cur["id"],
            "items": [
                {**item_out(it),
                 "seen": it["id"] in rows,
                 "due": rows[it["id"]]["due"] if it["id"] in rows else None}
                for it in les["items"]
            ] if les["order"] <= cur_order else [],
        })
    return out


@app.get("/api/stats")
def stats(user: dict = Depends(current_user)):
    t = today()
    uid = user["id"]
    course = course_for(user)
    with db() as con:
        rows = progress_rows(con, uid, course)
        streak = streak_for(con, uid, t)
        xp = xp_summary(con, uid, t)
        lang_xp = xp_summary(con, uid, t, course["lang"])
        reviews_today = con.execute(
            "SELECT COUNT(*) FROM review_log WHERE user_id=? AND day=?", (uid, t.isoformat())
        ).fetchone()[0]
        avg_score = con.execute(
            "SELECT AVG(score) FROM review_log WHERE user_id=? AND score IS NOT NULL AND day >= ?",
            (uid, (t - timedelta(days=7)).isoformat()),
        ).fetchone()[0]
    return {
        "xp": xp,
        "lang": course["lang"],
        "lang_xp": lang_xp,
        "learned": sum(1 for r in rows.values() if r["learned"]),
        "total": len(course["items"]),
        "due_today": sum(1 for r in rows.values() if r["due"] <= t.isoformat()),
        "reviews_today": reviews_today,
        "streak": streak,
        "avg_score_7d": round(avg_score, 1) if avg_score is not None else None,
    }


# ---------------------------------------------------------------- xp en ranglijst
def xp_for_review(grade: int, score: float | None, is_new: bool) -> int:
    base = 10 if grade >= 4 else 8 if grade == 3 else 2
    return base + (5 if is_new else 0) + (5 if score is not None and score >= AUTO_EASY else 0)


def award_xp(con, uid: int, item_id: str, grade: int, score: float | None, is_new: bool, t: date, ts: str) -> int:
    """XP voor een beoordeling. Per woord per dag kun je maximaal één keer het hoogste bedrag verdienen
    (een betere poging later die dag levert alleen het verschil op), zodat herhalen geen XP oplevert."""
    target = xp_for_review(grade, score, is_new)
    already = con.execute("SELECT COALESCE(SUM(amount),0) FROM xp_log WHERE user_id=? AND item_id=? AND day=?",
                          (uid, item_id, t.isoformat())).fetchone()[0]
    gain = max(0, target - already)
    if gain:
        con.execute("INSERT INTO xp_log (user_id, ts, day, amount, reason, item_id, lang) VALUES (?,?,?,?,?,?,?)",
                    (uid, ts, t.isoformat(), gain, "review", item_id, ITEMS[item_id]["lang"]))
    return gain


def level_for(total: int) -> int:
    """Level n begint bij 50*n*(n-1) XP: 100, 300, 600, 1000 ..."""
    return max(1, int((1 + math.sqrt(1 + 4 * total / 50)) / 2))


def week_start(t: date) -> date:
    return t - timedelta(days=t.weekday())


def xp_summary(con, uid: int, t: date, lang: str | None = None) -> dict:
    """XP van een gebruiker: totaal over alle talen, of alleen voor één taal."""
    where, extra = ("AND lang=?", (lang,)) if lang else ("", ())
    total = con.execute(f"SELECT COALESCE(SUM(amount),0) FROM xp_log WHERE user_id=? {where}", (uid, *extra)).fetchone()[0]
    week = con.execute(f"SELECT COALESCE(SUM(amount),0) FROM xp_log WHERE user_id=? AND day>=? {where}",
                       (uid, week_start(t).isoformat(), *extra)).fetchone()[0]
    today_xp = con.execute(f"SELECT COALESCE(SUM(amount),0) FROM xp_log WHERE user_id=? AND day=? {where}",
                           (uid, t.isoformat(), *extra)).fetchone()[0]
    lvl = level_for(total)
    floor_ = 50 * lvl * (lvl - 1)
    return {"total": total, "week": week, "today": today_xp, "level": lvl,
            "into_level": total - floor_, "level_size": 100 * lvl}


def streak_for(con, uid: int, t: date) -> int:
    days = {r[0] for r in con.execute("SELECT DISTINCT day FROM review_log WHERE user_id=?", (uid,))}
    streak = 0
    d = t if t.isoformat() in days else t - timedelta(days=1)
    while d.isoformat() in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


@app.get("/api/leaderboard")
def leaderboard(period: str = "week", lang: str = "all", user: dict = Depends(current_user)):
    """Ranglijst: lang=all telt alle talen samen, anders alleen de XP van die taal."""
    if period not in ("week", "all"):
        raise HTTPException(400, "period is 'week' of 'all'")
    if lang != "all" and lang not in COURSES:
        raise HTTPException(400, "onbekende taal")
    t = today()
    since = week_start(t).isoformat() if period == "week" else ""
    clause, extra = ("AND lang=?", (lang,)) if lang != "all" else ("", ())
    with db() as con:
        totals = dict(con.execute(f"SELECT user_id, SUM(amount) FROM xp_log WHERE 1=1 {clause} GROUP BY user_id", extra).fetchall())
        shown = dict(con.execute(f"SELECT user_id, SUM(amount) FROM xp_log WHERE day>=? {clause} GROUP BY user_id",
                                 (since, *extra)).fetchall())
        users = con.execute("SELECT id, name FROM users").fetchall()
        # een taalranglijst toont iedereen die in die taal ooit XP heeft verdiend
        users = [u for u in users if lang == "all" or u["id"] in totals]
        people = [{"id": u["id"], "name": u["name"], "xp": shown.get(u["id"], 0),
                   "level": level_for(totals.get(u["id"], 0)), "streak": streak_for(con, u["id"], t),
                   "you": u["id"] == user["id"]}
                  for u in users]
    people.sort(key=lambda p: (-p["xp"], p["name"].lower()))
    rank = 0
    for i, p in enumerate(people):
        if i == 0 or p["xp"] != people[i - 1]["xp"]:
            rank = i + 1
        p["rank"] = rank
        del p["id"]
    return {"period": period, "lang": lang, "since": since or None,
            "days_left": 7 - t.weekday() if period == "week" else None, "rows": people[:50]}


# ---------------------------------------------------------------- talen
class LangIn(BaseModel):
    lang: str = Field(max_length=5)


@app.get("/api/languages")
def languages(user: dict = Depends(current_user)):
    """Alle talen met jouw voortgang en XP per taal."""
    with db() as con:
        rows = progress_rows(con, user["id"])
        xp = dict(con.execute("SELECT lang, SUM(amount) FROM xp_log WHERE user_id=? GROUP BY lang", (user["id"],)).fetchall())
    return [{"code": c, "name": course["name"], "flag": course["flag"], "current": c == (user.get("lang") or DEFAULT_LANG),
             "learned": sum(1 for k, r in rows.items() if k in course["items"] and r["learned"]),
             "total": len(course["items"]), "xp": xp.get(c, 0)}
            for c, course in COURSES.items()]


@app.put("/api/lang")
def set_language(body: LangIn, user: dict = Depends(current_user)):
    if body.lang not in COURSES:
        raise HTTPException(400, "onbekende taal")
    with db() as con:
        con.execute("UPDATE users SET lang=? WHERE id=?", (body.lang, user["id"]))
    return {"lang": body.lang}


# ---------------------------------------------------------------- instellingen
class AzureKeyIn(BaseModel):
    key: str = Field(min_length=20, max_length=200)
    region: str = Field(min_length=3, max_length=40)


def settings_out(user: dict) -> dict:
    creds = azure_creds(user)
    return {
        "name": user["name"],
        "is_owner": user["id"] == 1,
        "has_password": user.get("pw_hash") is not None,
        "azure_source": creds[2] if creds else None,
        "azure_region": creds[1] if creds and creds[2] == "own" else None,
        "can_store_key": fernet() is not None,
        "shared_available": azure_speech.enabled(),
    }


@app.get("/api/settings")
def get_settings(user: dict = Depends(current_user)):
    return settings_out(user)


@app.put("/api/settings/azure")
async def set_azure_key(body: AzureKeyIn, user: dict = Depends(current_user)):
    """Eigen Azure-sleutel opslaan (versleuteld). De sleutel wordt eerst bij Azure gecontroleerd."""
    f = fernet()
    if not f:
        raise HTTPException(503, "De server kan sleutels niet veilig bewaren: zet APP_TOKEN of KEY_SECRET in .env")
    key, region = body.key.strip(), body.region.strip().lower()
    if not re.fullmatch(r"[a-z0-9]{3,40}", region):
        raise HTTPException(400, "Regio ziet er niet goed uit, bv. westeurope of swedencentral")
    try:
        await azure_speech.verify(key, region)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (401, 403):
            raise HTTPException(400, "Azure weigert deze sleutel. Controleer of je KEY 1 gekopieerd hebt en of de regio klopt.") from exc
        raise HTTPException(502, "Azure gaf een fout bij het controleren van de sleutel") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Kon Azure niet bereiken. Klopt de regio?") from exc
    with db() as con:
        con.execute("UPDATE users SET azure_key_enc=?, azure_region=? WHERE id=?",
                    (f.encrypt(key.encode()).decode(), region, user["id"]))
        row = con.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return settings_out(dict(row))


@app.delete("/api/settings/azure")
def remove_azure_key(user: dict = Depends(current_user)):
    with db() as con:
        con.execute("UPDATE users SET azure_key_enc=NULL, azure_region=NULL WHERE id=?", (user["id"],))
        row = con.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
    return settings_out(dict(row))


# ---------------------------------------------------------------- inloggen en wachtwoord
class LoginIn(BaseModel):
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


class ClaimIn(BaseModel):
    code: str = Field(max_length=100)
    username: str = Field(max_length=60)
    password: str = Field(max_length=200)


class PasswordIn(BaseModel):
    old: str = Field(max_length=200)
    new: str = Field(max_length=200)


@app.post("/api/login")
def login(body: LoginIn, request: Request):
    if not APP_TOKEN:
        raise HTTPException(400, "Deze server draait zonder login")
    name = " ".join(body.username.split())
    keys = (f"u:{name.lower()}", f"ip:{client_ip(request)}")
    throttle_check(*keys)
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE lower(name)=lower(?)", (name,)).fetchone()
        ok = verify_password(body.password, row["pw_hash"] if row else None)
        if not (row and ok):
            throttle_fail(*keys)
            raise HTTPException(401, "Gebruikersnaam of wachtwoord klopt niet")
        token = new_session(con, row["id"])
    return {"token": token, "name": row["name"]}


@app.post("/api/claim")
def claim(body: ClaimIn, request: Request):
    """Account activeren met een uitnodigingscode (of APP_TOKEN voor de eigenaar): kies gebruikersnaam en wachtwoord."""
    if not APP_TOKEN:
        raise HTTPException(400, "Deze server draait zonder login")
    keys = (f"ip:{client_ip(request)}", "claim")
    throttle_check(*keys)
    try:
        name = clean_username(body.username)
        check_password(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db() as con:
        row = con.execute("SELECT * FROM users WHERE token_hash=? AND pw_hash IS NULL", (hash_token(body.code.strip()),)).fetchone()
        if not row:
            throttle_fail(*keys)
            raise HTTPException(401, "Deze code klopt niet of is al gebruikt")
        if name_taken(con, name, row["id"]):
            raise HTTPException(409, "Deze gebruikersnaam is al bezet")
        # de eigenaar houdt APP_TOKEN voor het toestel; bij anderen is de uitnodigingscode nu verbruikt
        con.execute("UPDATE users SET name=?, pw_hash=?, token_hash=? WHERE id=?",
                    (name, hash_password(body.password), row["token_hash"] if row["id"] == 1 else None, row["id"]))
        token = new_session(con, row["id"])
    return {"token": token, "name": name}


@app.post("/api/logout")
def logout(x_app_token: str = Header(default="")):
    if x_app_token:
        with db() as con:
            con.execute("DELETE FROM sessions WHERE token_hash=?", (hash_token(x_app_token),))
    return {"ok": True}


@app.put("/api/settings/password")
def change_password(body: PasswordIn, x_app_token: str = Header(default=""),
                    user: dict = Depends(current_user)):
    keys = (f"pw:{user['id']}",)
    throttle_check(*keys)
    if not verify_password(body.old, user.get("pw_hash")):
        throttle_fail(*keys)
        raise HTTPException(400, "Je huidige wachtwoord klopt niet")
    try:
        check_password(body.new)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db() as con:
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(body.new), user["id"]))
        # andere apparaten worden uitgelogd, dit apparaat blijft ingelogd
        con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user["id"], hash_token(x_app_token)))
    return {"ok": True}


# Web-app (moet als laatste: vangt alle overige paden op)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
