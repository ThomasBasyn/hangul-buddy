import io
import json
import sys
import wave
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(ROOT))

from srs import Card, review, score_to_grade, similarity  # noqa: E402


# ------------------------------------------------------------------ SRS
def test_intervals_grow():
    c, d = Card(), date(2026, 11, 1)
    review(c, 4, d)
    assert c.interval == 1 and c.due == d + timedelta(days=1)
    review(c, 4, c.due)
    assert c.interval == 3
    review(c, 4, c.due)
    assert c.interval >= 7
    prev = c.interval
    review(c, 5, c.due)
    assert c.interval > prev


def test_fail_resets_and_returns_today():
    c, d = Card(), date(2026, 11, 1)
    for _ in range(3):
        review(c, 5, d)
    review(c, 1, d)
    assert c.reps == 0 and c.interval == 0 and c.due == d and c.lapses == 1
    assert c.ease >= 1.3


def test_score_to_grade():
    assert [score_to_grade(s) for s in (95, 80, 65, 45, 10)] == [5, 4, 3, 2, 1]


def test_similarity():
    assert similarity("감사합니다", "감사합니다") == 100
    assert similarity("감사합니다", "") == 0
    near = similarity("감사합니다", "감사함니다")  # ㅂ -> ㅁ, één klank verschil
    assert 85 < near < 100
    assert similarity("감사합니다", "안녕하세요") < 50


def test_lessons_valid():
    data = json.loads((ROOT / "content" / "lessons.json").read_text(encoding="utf-8"))
    ids = [it["id"] for les in data["lessons"] for it in les["items"]]
    assert len(ids) == len(set(ids))
    for les in data["lessons"]:
        assert les["title"] and les["items"]
        for it in les["items"]:
            assert it["ko"] and it["rom"] and it["nl"]


# ------------------------------------------------------------------ API
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NEW_PER_DAY", "3")
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.main = main
        yield c


def wav_bytes(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return buf.getvalue()


def test_daily_flow(client):
    first = client.get("/api/next").json()
    assert first["kind"] == "new" and first["item"]["id"] == "1-1" and first["show_note"]

    # fout -> komt vandaag terug als 'relearn', na de andere kaarten
    client.post("/api/grade", json={"item_id": "1-1", "grade": 1})
    nxt = client.get("/api/next").json()
    assert nxt["kind"] == "relearn" and nxt["item"]["id"] == "1-1"
    client.post("/api/grade", json={"item_id": "1-1", "grade": 4})

    for _ in range(2):
        card = client.get("/api/next").json()
        assert card["kind"] == "new" and not card["show_note"]
        client.post("/api/grade", json={"item_id": card["item"]["id"], "grade": 5})

    done = client.get("/api/next").json()
    assert done["done"] and done["more_new_available"]
    extra = client.get("/api/next?extra=1").json()
    assert extra["kind"] == "new"

    stats = client.get("/api/stats").json()
    assert stats["learned"] == 3 and stats["streak"] == 1 and stats["reviews_today"] == 4


def test_lesson_unlocks(client):
    main = client.main
    for it in main.LESSONS[0]["items"]:
        client.post("/api/grade", json={"item_id": it["id"], "grade": 4})
    lessons = client.get("/api/lessons").json()
    assert lessons[0]["learned"] == lessons[0]["total"]
    assert lessons[1]["unlocked"] and lessons[1]["current"]
    assert not lessons[2]["unlocked"] and lessons[2]["items"] == []


def test_reviews_due_later(client):
    client.post("/api/grade", json={"item_id": "1-1", "grade": 4})
    tomorrow = client.main.today() + timedelta(days=1)
    client.main.today = lambda: tomorrow
    card = client.get("/api/next").json()
    assert card["kind"] == "review" and card["item"]["id"] == "1-1"


def test_assess_text_and_validation(client):
    r = client.post("/api/assess-text/1-2", json={"heard": "감사합니다"}).json()
    assert r["score"] == 100 and r["grade"] == 5
    assert client.post("/api/grade", json={"item_id": "x", "grade": 3}).status_code == 404
    assert client.post("/api/grade", json={"item_id": "1-1", "grade": 9}).status_code == 422
    assert client.post("/api/assess/1-1", content=wav_bytes()).status_code == 503
    assert client.get("/api/tts/1-1").status_code == 404


def test_assess_audio_with_azure(client, monkeypatch):
    main = client.main

    async def fake_assess(wav, ref, **kwargs):
        return main.azure_speech.parse_assessment({
            "RecognitionStatus": "Success",
            "NBest": [{"Display": "감사합니다.", "PronunciationAssessment": {"PronScore": 82, "AccuracyScore": 80},
                       "Words": [{"Word": "감사합니다", "PronunciationAssessment": {"AccuracyScore": 80, "ErrorType": "None"},
                                  "Syllables": [{"Grapheme": "감", "PronunciationAssessment": {"AccuracyScore": 95}}]}]}],
        })

    monkeypatch.setattr(main.azure_speech, "enabled", lambda: True)
    monkeypatch.setattr(main.azure_speech, "assess", fake_assess)
    r = client.post("/api/assess/1-2", content=wav_bytes()).json()
    assert r["score"] == 82 and r["grade"] == 4 and r["words"][0]["syllables"][0]["text"] == "감"
    assert client.post("/api/assess/1-2", content=wav_bytes(rate=44100)).status_code == 400
    assert client.post("/api/assess/1-2", content=wav_bytes(0.1)).status_code == 400
    assert client.post("/api/assess/1-2", content=b"junk").status_code == 400


def test_token(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_TOKEN", "geheim123")
    sys.modules.pop("main", None)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        assert c.get("/api/next").status_code == 401
        assert c.get("/api/next", headers={"X-App-Token": "geheim123"}).status_code == 200
        assert c.get("/").status_code == 200  # web-app zelf is publiek, de API niet


def test_azure_assess_uses_separate_transcript(monkeypatch):
    """'Gehoord' komt uit een gewone herkenning, niet uit de referentietekst."""
    import asyncio

    import httpx

    import azure_speech

    def handler(request):
        if "pronunciation-assessment" in request.headers:
            return httpx.Response(200, json={
                "RecognitionStatus": "Success",
                "NBest": [{"Display": "안녕히 계세요.", "PronScore": 8, "AccuracyScore": 8,
                           "Words": [{"Word": "안녕히", "AccuracyScore": 8, "ErrorType": "Mispronunciation"}]}],
            })
        return httpx.Response(200, json={"RecognitionStatus": "Success", "DisplayText": "고마워요."})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    result = asyncio.run(azure_speech.assess(b"wav", "안녕히 계세요"))
    assert result["heard"] == "고마워요."
    assert result["score"] == 8

    def failing(request):
        if "pronunciation-assessment" in request.headers:
            return handler(request)
        return httpx.Response(500)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(failing)))
    result = asyncio.run(azure_speech.assess(b"wav", "안녕히 계세요"))
    assert result["heard"] == ""  # liever niets dan de referentietekst als "gehoord"


def test_config_has_auto_grading_thresholds(client):
    cfg = client.get("/api/config").json()
    assert cfg["auto"] == {"easy": 90.0, "retry": 80.0}


# ------------------------------------------------------------------ meerdere gebruikers
@pytest.fixture()
def multi(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_TOKEN", "baas-code")
    monkeypatch.setenv("OWNER_NAME", "Baas")
    monkeypatch.setenv("NEW_PER_DAY", "3")
    monkeypatch.delenv("AZURE_SPEECH_KEY", raising=False)
    monkeypatch.delenv("KEY_SECRET", raising=False)
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        c.main = main
        c.mama_token = main.create_user("Mama")
        c.owner = {"X-App-Token": "baas-code"}
        c.mama = {"X-App-Token": c.mama_token}
        yield c


def test_users_have_separate_progress(multi):
    c = multi
    assert c.get("/api/config", headers=c.owner).json()["user"] == "Baas"
    assert c.get("/api/config", headers=c.mama).json()["user"] == "Mama"
    c.post("/api/grade", json={"item_id": "1-1", "grade": 5}, headers=c.owner)
    c.post("/api/grade", json={"item_id": "1-2", "grade": 5}, headers=c.owner)
    assert c.get("/api/stats", headers=c.owner).json()["learned"] == 2
    assert c.get("/api/stats", headers=c.mama).json()["learned"] == 0
    # mama begint bij het eerste woord, de eigenaar bij het volgende
    assert c.get("/api/next", headers=c.mama).json()["item"]["id"] == "1-1"
    assert c.get("/api/next", headers=c.owner).json()["item"]["id"] == "1-3"
    c.post("/api/grade", json={"item_id": "1-1", "grade": 1}, headers=c.mama)
    assert c.get("/api/stats", headers=c.owner).json()["learned"] == 2
    assert c.get("/api/stats", headers=c.mama).json()["streak"] == 1


def test_login_required_and_codes_are_not_stored_in_plain_text(multi):
    c = multi
    assert c.get("/api/next").status_code == 401
    assert c.get("/api/next", headers={"X-App-Token": "fout"}).status_code == 401
    with c.main.db() as con:
        stored = " ".join(str(r["token_hash"]) for r in con.execute("SELECT token_hash FROM users"))
    assert c.mama_token not in stored and "baas-code" not in stored
    # nieuwe code maakt de oude ongeldig
    new = c.main.reset_token("Mama")
    assert c.get("/api/next", headers=c.mama).status_code == 401
    assert c.get("/api/next", headers={"X-App-Token": new}).status_code == 200


def test_user_management_rules(multi):
    main = multi.main
    with pytest.raises(ValueError):
        main.create_user("Mama")
    with pytest.raises(ValueError):
        main.reset_token("Baas")
    with pytest.raises(ValueError):
        main.remove_user("Baas")
    multi.post("/api/grade", json={"item_id": "1-1", "grade": 5}, headers=multi.mama)
    main.remove_user("Mama")
    assert multi.get("/api/next", headers=multi.mama).status_code == 401
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 0


def test_own_azure_key_with_fallback(multi, monkeypatch):
    c, main = multi, multi.main
    calls = []

    async def fake_verify(key, region):
        calls.append(("verify", key, region))

    async def fake_assess(wav, ref, key=None, region=None):
        calls.append(("assess", key, region))
        return {"score": 91.0, "heard": "x", "words": [], "status": "Success"}

    monkeypatch.setattr(main.azure_speech, "verify", fake_verify)
    monkeypatch.setattr(main.azure_speech, "assess", fake_assess)

    # geen sleutel en geen gedeelde sleutel: Azure uit
    assert c.get("/api/config", headers=c.mama).json()["azure"] is False
    assert c.post("/api/assess/1-1", content=wav_bytes(), headers=c.mama).status_code == 503

    # gedeelde sleutel van de server: valt erop terug
    monkeypatch.setattr(main.azure_speech, "enabled", lambda: True)
    monkeypatch.setattr(main.azure_speech, "KEY", "gedeelde-sleutel-123456789")
    monkeypatch.setattr(main.azure_speech, "REGION", "westeurope")
    cfg = c.get("/api/config", headers=c.mama).json()
    assert cfg["azure"] and cfg["azure_source"] == "shared"
    c.post("/api/assess/1-1", content=wav_bytes(), headers=c.mama)
    assert calls[-1] == ("assess", "gedeelde-sleutel-123456789", "westeurope")

    # eigen sleutel: wordt gecontroleerd, versleuteld bewaard en daarna gebruikt
    own = "eigen-sleutel-van-mama-0123456789"
    r = c.put("/api/settings/azure", json={"key": own, "region": " SwedenCentral "}, headers=c.mama)
    assert r.status_code == 200 and r.json()["azure_source"] == "own" and r.json()["azure_region"] == "swedencentral"
    assert own not in r.text and ("verify", own, "swedencentral") in calls
    with main.db() as con:
        row = con.execute("SELECT azure_key_enc FROM users WHERE name='Mama'").fetchone()
    assert row[0] and own not in row[0]
    assert own not in c.get("/api/settings", headers=c.mama).text
    c.post("/api/assess/1-1", content=wav_bytes(), headers=c.mama)
    assert calls[-1] == ("assess", own, "swedencentral")

    # de eigenaar blijft op de gedeelde sleutel
    assert c.get("/api/settings", headers=c.owner).json()["azure_source"] == "shared"
    c.post("/api/assess/1-1", content=wav_bytes(), headers=c.owner)
    assert calls[-1] == ("assess", "gedeelde-sleutel-123456789", "westeurope")

    # eigen sleutel verwijderen: terug naar de gedeelde
    r = c.delete("/api/settings/azure", headers=c.mama)
    assert r.json()["azure_source"] == "shared"


def test_azure_key_is_checked_before_saving(multi, monkeypatch):
    import httpx

    main = multi.main

    async def reject(key, region):
        raise httpx.HTTPStatusError("nee", request=httpx.Request("POST", "http://x"), response=httpx.Response(401))

    monkeypatch.setattr(main.azure_speech, "verify", reject)
    r = multi.put("/api/settings/azure", json={"key": "x" * 32, "region": "westeurope"}, headers=multi.mama)
    assert r.status_code == 400 and "weigert" in r.json()["detail"]
    assert multi.put("/api/settings/azure", json={"key": "x" * 32, "region": "wes/teurope"}, headers=multi.mama).status_code == 400
    assert multi.get("/api/settings", headers=multi.mama).json()["azure_source"] is None


def test_keys_need_a_secret(tmp_path, monkeypatch):
    """Zonder APP_TOKEN of KEY_SECRET weigert de server om sleutels op te slaan."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("APP_TOKEN", raising=False)
    monkeypatch.delenv("KEY_SECRET", raising=False)
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        assert c.get("/api/settings").json()["can_store_key"] is False
        assert c.put("/api/settings/azure", json={"key": "x" * 32, "region": "westeurope"}).status_code == 503
        with pytest.raises(ValueError):
            main.create_user("Mama")  # zonder login kent de app maar één gebruiker


def test_old_single_user_database_is_migrated(tmp_path, monkeypatch):
    import sqlite3

    old = sqlite3.connect(tmp_path / "hangul.db")
    old.executescript("""
    CREATE TABLE progress (item_id TEXT PRIMARY KEY, ease REAL NOT NULL DEFAULT 2.5, interval INTEGER NOT NULL DEFAULT 0,
        reps INTEGER NOT NULL DEFAULT 0, lapses INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL, learned INTEGER NOT NULL DEFAULT 0,
        first_seen TEXT NOT NULL, last_review TEXT, last_score REAL);
    CREATE INDEX idx_progress_due ON progress(due);
    CREATE TABLE review_log (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL, ts TEXT NOT NULL, day TEXT NOT NULL,
        grade INTEGER NOT NULL, score REAL, source TEXT NOT NULL DEFAULT 'self');
    CREATE INDEX idx_log_day ON review_log(day);
    INSERT INTO progress VALUES ('1-1', 2.6, 1, 1, 0, '2999-01-01', 1, '2026-10-30', '2026-10-30T10:00:00', 91.0);
    INSERT INTO progress VALUES ('1-2', 2.5, 0, 0, 1, '2026-10-30', 0, '2026-10-30', '2026-10-30T10:01:00', 40.0);
    INSERT INTO review_log (item_id, ts, day, grade, score, source) VALUES ('1-1', '2026-10-30T10:00:00', '2026-10-30', 5, 91.0, 'azure');
    """)
    old.commit()
    old.close()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_TOKEN", "baas-code")
    monkeypatch.setenv("OWNER_NAME", "Baas")
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        h = {"X-App-Token": "baas-code"}
        stats = c.get("/api/stats", headers=h).json()
        assert stats["learned"] == 1 and stats["streak"] >= 0
        rows = {r["item_id"]: r for r in main.db().execute("SELECT * FROM progress WHERE user_id=1")}
        assert rows["1-1"]["ease"] == 2.6 and rows["1-2"]["lapses"] == 1 and rows["1-1"]["last_score"] == 91.0
        with main.db() as con:
            assert con.execute("SELECT COUNT(*) FROM review_log WHERE user_id=1").fetchone()[0] == 1
            assert not con.execute("SELECT name FROM sqlite_master WHERE name='progress_old'").fetchone()
        # nog een gebruiker kan nu dezelfde woorden leren zonder conflict
        token = main.create_user("Mama")
        assert c.post("/api/grade", json={"item_id": "1-1", "grade": 4}, headers={"X-App-Token": token}).status_code == 200
    assert (tmp_path / "hangul.db.bak-voor-gebruikers").exists()  # reservekopie
    # opnieuw opstarten migreert niet nog eens en verliest niets
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main as main2
    with TestClient(main2.app) as c2:
        assert c2.get("/api/stats", headers={"X-App-Token": "baas-code"}).json()["learned"] == 1
