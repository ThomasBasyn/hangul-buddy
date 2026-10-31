"""Hangul Buddy - backend voor de Koreaanse woordjestrainer.

Dezelfde API wordt gebruikt door de web-app en later door het ESP32-toestel.
"""

import hashlib
import io
import json
import logging
import os
import secrets
import sqlite3
import wave
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
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
APP_TOKEN = os.getenv("APP_TOKEN", "")
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
CREATE TABLE IF NOT EXISTS progress (
    item_id     TEXT PRIMARY KEY,
    ease        REAL NOT NULL DEFAULT 2.5,
    interval    INTEGER NOT NULL DEFAULT 0,
    reps        INTEGER NOT NULL DEFAULT 0,
    lapses      INTEGER NOT NULL DEFAULT 0,
    due         TEXT NOT NULL,
    learned     INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_review TEXT,
    last_score  REAL
);
CREATE INDEX IF NOT EXISTS idx_progress_due ON progress(due);
CREATE TABLE IF NOT EXISTS review_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id  TEXT NOT NULL,
    ts       TEXT NOT NULL,
    day      TEXT NOT NULL,
    grade    INTEGER NOT NULL,
    score    REAL,
    source   TEXT NOT NULL DEFAULT 'self'
);
CREATE INDEX IF NOT EXISTS idx_log_day ON review_log(day);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(SCHEMA)


def progress_rows(con) -> dict[str, sqlite3.Row]:
    return {r["item_id"]: r for r in con.execute("SELECT * FROM progress")}


def current_lesson(rows: dict) -> dict | None:
    """Eerste les waarvan nog niet elk woord minstens één keer goed was."""
    for les in LESSONS:
        if any(not (rows.get(it["id"]) and rows[it["id"]]["learned"]) for it in les["items"]):
            return les
    return None


# ---------------------------------------------------------------- auth
def require_token(
    x_app_token: str = Header(default=""),
    token: str = Query(default=""),
) -> None:
    if not APP_TOKEN:
        return
    given = x_app_token or token
    if not secrets.compare_digest(given.encode(), APP_TOKEN.encode()):
        raise HTTPException(401, "ongeldige token")


# ---------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    log.info("%d lessen, %d items geladen. Azure: %s", len(LESSONS), len(ITEMS),
             "aan" if azure_speech.enabled() else "uit")
    yield


app = FastAPI(title="Hangul Buddy", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
api = Depends(require_token)


def item_out(item: dict) -> dict:
    return {k: item.get(k) for k in ("id", "ko", "rom", "nl", "tip", "lesson")}


def lesson_out(les: dict) -> dict:
    return {"id": les["id"], "title": les["title"], "note": les.get("note", "")}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config", dependencies=[api])
def config():
    return {
        "azure": azure_speech.enabled(),
        "new_per_day": NEW_PER_DAY,
        "course": COURSE.get("course", ""),
        "lessons": len(LESSONS),
        "items": len(ITEMS),
    }


@app.get("/api/next", dependencies=[api])
def next_card(extra: bool = False):
    """Volgend woord: eerst herhalingen die vandaag verschijnen, dan nieuwe woorden."""
    t = today().isoformat()
    with db() as con:
        rows = progress_rows(con)
        due = con.execute(
            """SELECT item_id, reps, learned FROM progress WHERE due <= ?
               ORDER BY COALESCE(last_review, ''), due""",
            (t,),
        ).fetchall()
        new_today = con.execute(
            "SELECT COUNT(*) FROM progress WHERE first_seen = ?", (t,)
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
        nxt = con.execute("SELECT MIN(due) FROM progress WHERE due > ?", (t,)).fetchone()[0]
    return {"done": True, "counts": counts, "next_due": nxt,
            "more_new_available": bool(candidates), "course_finished": les is None}


class GradeIn(BaseModel):
    item_id: str
    grade: int = Field(ge=0, le=5)
    score: float | None = Field(default=None, ge=0, le=100)
    source: str = Field(default="self", max_length=20)


@app.post("/api/grade", dependencies=[api])
def grade(body: GradeIn):
    if body.item_id not in ITEMS:
        raise HTTPException(404, "onbekend item")
    t = today()
    with db() as con:
        row = con.execute("SELECT * FROM progress WHERE item_id=?", (body.item_id,)).fetchone()
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
            """INSERT INTO progress (item_id, ease, interval, reps, lapses, due, learned,
                   first_seen, last_review, last_score)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(item_id) DO UPDATE SET ease=excluded.ease,
                   interval=excluded.interval, reps=excluded.reps, lapses=excluded.lapses,
                   due=excluded.due, learned=excluded.learned, last_review=excluded.last_review,
                   last_score=COALESCE(excluded.last_score, progress.last_score)""",
            (body.item_id, card.ease, card.interval, card.reps, card.lapses,
             card.due.isoformat(), learned, first_seen, ts, body.score),
        )
        con.execute(
            "INSERT INTO review_log (item_id, ts, day, grade, score, source) VALUES (?,?,?,?,?,?)",
            (body.item_id, ts, t.isoformat(), body.grade, body.score, body.source),
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


@app.post("/api/assess/{item_id}", dependencies=[api])
async def assess_audio(item_id: str, request: Request):
    """Uitspraak beoordelen met Azure. Body: ruwe WAV (16 kHz, mono, 16-bit)."""
    item = ITEMS.get(item_id)
    if not item:
        raise HTTPException(404, "onbekend item")
    if not azure_speech.enabled():
        raise HTTPException(503, "Azure Speech is niet ingesteld")
    data = await request.body()
    check_wav(data)
    try:
        result = await azure_speech.assess(data, item["ko"])
    except httpx.HTTPError as exc:
        log.error("Azure-beoordeling mislukt: %s", exc)
        raise HTTPException(502, "Azure Speech gaf een fout") from exc
    result["grade"] = score_to_grade(result["score"])
    result["provider"] = "azure"
    return result


class HeardIn(BaseModel):
    heard: str = Field(max_length=300)


@app.post("/api/assess-text/{item_id}", dependencies=[api])
def assess_text(item_id: str, body: HeardIn):
    """Fallback: vergelijk wat spraakherkenning hoorde met het woord (klank per klank)."""
    item = ITEMS.get(item_id)
    if not item:
        raise HTTPException(404, "onbekend item")
    score = similarity(item["ko"], body.heard)
    return {"score": score, "heard": body.heard, "words": [], "grade": score_to_grade(score),
            "provider": "speech-recognition"}


@app.get("/api/tts/{item_id}", dependencies=[api])
async def tts(item_id: str, fmt: str = "mp3"):
    """Uitspraak als audio. fmt=wav voor het ESP32-toestel. Wordt op schijf gecachet."""
    item = ITEMS.get(item_id)
    if not item:
        raise HTTPException(404, "onbekend item")
    if fmt not in azure_speech.FORMATS:
        raise HTTPException(400, "fmt moet mp3 of wav zijn")
    if not azure_speech.enabled():
        raise HTTPException(404, "geen server-TTS, gebruik de browserstem")
    key = hashlib.sha256(f"{azure_speech.VOICE}|{item['ko']}|{fmt}".encode()).hexdigest()[:24]
    path = TTS_CACHE / f"{key}.{fmt}"
    if not path.exists():
        try:
            audio = await azure_speech.tts(item["ko"], fmt)
        except httpx.HTTPError as exc:
            log.error("Azure-TTS mislukt: %s", exc)
            raise HTTPException(502, "Azure TTS gaf een fout") from exc
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(audio)
        tmp.replace(path)
    return FileResponse(path, media_type=azure_speech.FORMATS[fmt][1],
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


# ---------------------------------------------------------------- overzicht
@app.get("/api/lessons", dependencies=[api])
def lessons():
    with db() as con:
        rows = progress_rows(con)
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


@app.get("/api/stats", dependencies=[api])
def stats():
    t = today()
    with db() as con:
        rows = progress_rows(con)
        days = {r[0] for r in con.execute("SELECT DISTINCT day FROM review_log")}
        reviews_today = con.execute(
            "SELECT COUNT(*) FROM review_log WHERE day=?", (t.isoformat(),)
        ).fetchone()[0]
        avg_score = con.execute(
            "SELECT AVG(score) FROM review_log WHERE score IS NOT NULL AND day >= ?",
            ((t - timedelta(days=7)).isoformat(),),
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


# Web-app (moet als laatste: vangt alle overige paden op)
app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
