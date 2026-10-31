"""Hangul Buddy - backend voor de Koreaanse woordjestrainer.

Dezelfde API wordt gebruikt door de web-app en later door het ESP32-toestel.
"""

import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sqlite3
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
def load_course() -> dict:
    course = json.loads(LESSONS_FILE.read_text(encoding="utf-8"))
    ids = set()
    for order, lesson in enumerate(course["lessons"]):
        lesson["order"] = order
        for i, item in enumerate(lesson["items"]):
            if item["id"] in ids:
                raise SystemExit(f"Dubbel item-id in lessen: {item['id']}")
            ids.add(item["id"])
            item["lesson"] = lesson["id"]
            item["pos"] = i
    return course


COURSE = load_course()
LESSONS = COURSE["lessons"]
ITEMS = {it["id"]: it for les in LESSONS for it in les["items"]}
LESSON_BY_ID = {les["id"]: les for les in LESSONS}


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
    created       TEXT NOT NULL
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
        con.executescript(SCHEMA)
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
    if con.execute("SELECT 1 FROM users WHERE id=1").fetchone():
        con.execute("UPDATE users SET name=?, token_hash=? WHERE id=1", (OWNER_NAME, token_hash))
    else:
        con.execute("INSERT INTO users (id, name, token_hash, created) VALUES (1,?,?,?)",
                    (OWNER_NAME, token_hash, now_iso()))


def progress_rows(con, user_id: int) -> dict[str, sqlite3.Row]:
    return {r["item_id"]: r for r in con.execute("SELECT * FROM progress WHERE user_id=?", (user_id,))}


# ---------------------------------------------------------------- gebruikersbeheer (ook voor manage.py)
def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not 1 <= len(name) <= 30:
        raise ValueError("naam moet 1 tot 30 tekens zijn")
    return name


def create_user(name: str) -> str:
    """Maak een gebruiker en geef zijn toegangscode terug (wordt maar één keer getoond)."""
    if not APP_TOKEN:
        raise ValueError("Zet eerst APP_TOKEN in .env: zonder login kent de app maar één gebruiker")
    name = clean_name(name)
    token = secrets.token_urlsafe(12)
    with db() as con:
        try:
            con.execute("INSERT INTO users (name, token_hash, created) VALUES (?,?,?)",
                        (name, hash_token(token), now_iso()))
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"gebruiker '{name}' bestaat al") from exc
    return token


def reset_token(name: str) -> str:
    token = secrets.token_urlsafe(12)
    with db() as con:
        if name.strip().lower() == OWNER_NAME.lower() or name.strip() == "1":
            raise ValueError("de toegangscode van de eigenaar is APP_TOKEN in .env")
        cur = con.execute("UPDATE users SET token_hash=? WHERE name=? AND id<>1", (hash_token(token), clean_name(name)))
        if not cur.rowcount:
            raise ValueError(f"gebruiker '{name}' niet gevonden")
    return token


def remove_user(name: str) -> None:
    with db() as con:
        row = con.execute("SELECT id FROM users WHERE name=? AND id<>1", (clean_name(name),)).fetchone()
        if not row:
            raise ValueError(f"gebruiker '{name}' niet gevonden (de eigenaar kan niet verwijderd worden)")
        for table in ("progress", "review_log"):
            con.execute(f"DELETE FROM {table} WHERE user_id=?", (row["id"],))
        con.execute("DELETE FROM users WHERE id=?", (row["id"],))


def list_users() -> list[dict]:
    with db() as con:
        return [dict(r) | {"words": con.execute("SELECT COUNT(*) FROM progress WHERE user_id=?", (r["id"],)).fetchone()[0]}
                for r in con.execute("SELECT id, name, azure_key_enc IS NOT NULL AS own_key, created FROM users ORDER BY id")]


def current_lesson(rows: dict) -> dict | None:
    """Eerste les waarvan nog niet elk woord minstens één keer goed was."""
    for les in LESSONS:
        if any(not (rows.get(it["id"]) and rows[it["id"]]["learned"]) for it in les["items"]):
            return les
    return None


# ---------------------------------------------------------------- auth
def current_user(
    x_app_token: str = Header(default=""),
    token: str = Query(default=""),
) -> dict:
    """De ingelogde gebruiker. Zonder APP_TOKEN is er geen login en is iedereen gebruiker 1."""
    with db() as con:
        if not APP_TOKEN:
            row = con.execute("SELECT * FROM users WHERE id=1").fetchone()
        else:
            given = x_app_token or token
            row = con.execute("SELECT * FROM users WHERE token_hash=?", (hash_token(given),)).fetchone() if given else None
    if not row:
        raise HTTPException(401, "ongeldige token")
    return dict(row)


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
    log.info("%d lessen, %d items geladen. Azure: %s", len(LESSONS), len(ITEMS),
             "aan" if azure_speech.enabled() else "uit")
    yield


app = FastAPI(title="Hangul Buddy", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


def item_out(item: dict) -> dict:
    return {k: item.get(k) for k in ("id", "ko", "rom", "nl", "tip", "lesson")}


def lesson_out(les: dict) -> dict:
    return {"id": les["id"], "title": les["title"], "note": les.get("note", "")}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config(user: dict = Depends(current_user)):
    creds = azure_creds(user)
    return {
        "azure": creds is not None,
        "azure_source": creds[2] if creds else None,
        "user": user["name"],
        "new_per_day": NEW_PER_DAY,
        "auto": {"easy": AUTO_EASY, "retry": AUTO_RETRY},
        "course": COURSE.get("course", ""),
        "lessons": len(LESSONS),
        "items": len(ITEMS),
    }


@app.get("/api/next")
def next_card(extra: bool = False, user: dict = Depends(current_user)):
    """Volgend woord: eerst herhalingen die vandaag verschijnen, dan nieuwe woorden."""
    t = today().isoformat()
    with db() as con:
        rows = progress_rows(con, user["id"])
        due = con.execute(
            """SELECT item_id, reps, learned FROM progress WHERE user_id = ? AND due <= ?
               ORDER BY COALESCE(last_review, ''), due""",
            (user["id"], t),
        ).fetchall()
        new_today = con.execute(
            "SELECT COUNT(*) FROM progress WHERE user_id = ? AND first_seen = ?", (user["id"], t)
        ).fetchone()[0]

    due = [r for r in due if r["item_id"] in ITEMS]
    les = current_lesson(rows)
    candidates = [it for it in les["items"] if it["id"] not in rows] if les else []
    capacity = len(candidates) if extra else max(0, NEW_PER_DAY - new_today)
    new_left = min(capacity, len(candidates))

    counts = {"due": len(due), "new_left": new_left, "new_today": new_today}
    if due:
        r = due[0]
        item = ITEMS[r["item_id"]]
        kind = "relearn" if r["reps"] == 0 else "review"
        return {"done": False, "kind": kind, "item": item_out(item),
                "lesson": lesson_out(LESSON_BY_ID[item["lesson"]]),
                "show_note": False, "counts": counts}
    if new_left:
        item = candidates[0]
        lesson_started = any(it["id"] in rows for it in les["items"])
        return {"done": False, "kind": "new", "item": item_out(item),
                "lesson": lesson_out(les), "show_note": not lesson_started,
                "counts": counts}

    with db() as con:
        nxt = con.execute("SELECT MIN(due) FROM progress WHERE user_id = ? AND due > ?",
                          (user["id"], t)).fetchone()[0]
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
    return {"item_id": body.item_id, "interval": card.interval, "due": card.due.isoformat(),
            "ease": round(card.ease, 2), "learned": bool(learned)}


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
        result = await azure_speech.assess(data, item["ko"], key=creds[0], region=creds[1])
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
    score = similarity(item["ko"], body.heard)
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
    key = hashlib.sha256(f"{azure_speech.VOICE}|{item['ko']}|{fmt}".encode()).hexdigest()[:24]
    path = TTS_CACHE / f"{key}.{fmt}"
    if not path.exists():
        try:
            audio = await azure_speech.tts(item["ko"], fmt, key=creds[0], region=creds[1])
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
    with db() as con:
        rows = progress_rows(con, user["id"])
    cur = current_lesson(rows)
    cur_order = cur["order"] if cur else len(LESSONS)
    out = []
    for les in LESSONS:
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
    with db() as con:
        rows = progress_rows(con, uid)
        days = {r[0] for r in con.execute("SELECT DISTINCT day FROM review_log WHERE user_id=?", (uid,))}
        reviews_today = con.execute(
            "SELECT COUNT(*) FROM review_log WHERE user_id=? AND day=?", (uid, t.isoformat())
        ).fetchone()[0]
        avg_score = con.execute(
            "SELECT AVG(score) FROM review_log WHERE user_id=? AND score IS NOT NULL AND day >= ?",
            (uid, (t - timedelta(days=7)).isoformat()),
        ).fetchone()[0]
    streak = 0
    d = t if t.isoformat() in days else t - timedelta(days=1)
    while d.isoformat() in days:
        streak += 1
        d -= timedelta(days=1)
    return {
        "learned": sum(1 for r in rows.values() if r["learned"]),
        "total": len(ITEMS),
        "due_today": sum(1 for r in rows.values() if r["due"] <= t.isoformat()),
        "reviews_today": reviews_today,
        "streak": streak,
        "avg_score_7d": round(avg_score, 1) if avg_score is not None else None,
    }


# ---------------------------------------------------------------- instellingen
class AzureKeyIn(BaseModel):
    key: str = Field(min_length=20, max_length=200)
    region: str = Field(min_length=3, max_length=40)


def settings_out(user: dict) -> dict:
    creds = azure_creds(user)
    return {
        "name": user["name"],
        "is_owner": user["id"] == 1,
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


# Web-app (moet als laatste: vangt alle overige paden op)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
