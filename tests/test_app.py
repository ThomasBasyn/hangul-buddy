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
        c.invite = main.create_user("Mama")
        c.owner = {"X-App-Token": "baas-code"}
        c.mama_token = c.post("/api/claim", json={"code": c.invite, "username": "Mama", "password": "mama-wachtwoord"}).json()["token"]
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


def test_login_required_and_secrets_are_not_stored_in_plain_text(multi):
    c = multi
    assert c.get("/api/next").status_code == 401
    assert c.get("/api/next", headers={"X-App-Token": "fout"}).status_code == 401
    with c.main.db() as con:
        dump = " ".join(str(v) for r in con.execute("SELECT * FROM users") for v in tuple(r))
        dump += " ".join(str(r[0]) for r in con.execute("SELECT token_hash FROM sessions"))
    for secret in (c.invite, c.mama_token, "baas-code", "mama-wachtwoord"):
        assert secret not in dump
    assert "scrypt$" in dump
    # een gebruikte uitnodigingscode werkt niet meer, ook niet als inlogtoken
    assert c.get("/api/next", headers={"X-App-Token": c.invite}).status_code == 401
    assert c.post("/api/claim", json={"code": c.invite, "username": "Ander", "password": "ander-wachtwoord"}).status_code == 401


def test_login_logout_and_password_change(multi):
    c = multi
    assert c.post("/api/login", json={"username": "mama", "password": "fout-wachtwoord"}).status_code == 401
    assert c.post("/api/login", json={"username": "Niemand", "password": "mama-wachtwoord"}).status_code == 401
    r = c.post("/api/login", json={"username": "mama", "password": "mama-wachtwoord"})  # hoofdletterongevoelig
    assert r.status_code == 200 and r.json()["name"] == "Mama"
    second = {"X-App-Token": r.json()["token"]}
    assert c.get("/api/next", headers=second).status_code == 200
    # wachtwoord wijzigen: oud wachtwoord nodig, andere apparaten worden uitgelogd
    assert c.put("/api/settings/password", json={"old": "nee", "new": "nieuw-wachtwoord"}, headers=second).status_code == 400
    assert c.put("/api/settings/password", json={"old": "mama-wachtwoord", "new": "kort"}, headers=second).status_code == 400
    assert c.put("/api/settings/password", json={"old": "mama-wachtwoord", "new": "nieuw-wachtwoord"}, headers=second).status_code == 200
    assert c.get("/api/next", headers=second).status_code == 200
    assert c.get("/api/next", headers=c.mama).status_code == 401
    assert c.post("/api/login", json={"username": "Mama", "password": "mama-wachtwoord"}).status_code == 401
    assert c.post("/api/login", json={"username": "Mama", "password": "nieuw-wachtwoord"}).status_code == 200
    c.post("/api/logout", headers=second)
    assert c.get("/api/next", headers=second).status_code == 401


def test_owner_claims_with_app_token_and_keeps_it_for_the_device(multi):
    c = multi
    r = c.post("/api/claim", json={"code": "baas-code", "username": "Thomas", "password": "baas-wachtwoord"})
    assert r.status_code == 200
    assert c.get("/api/config", headers={"X-App-Token": r.json()["token"]}).json()["user"] == "Thomas"
    assert c.get("/api/config", headers=c.owner).json()["user"] == "Thomas"  # toestel blijft werken
    assert c.post("/api/claim", json={"code": "baas-code", "username": "Weer", "password": "baas-wachtwoord"}).status_code == 401
    assert c.post("/api/login", json={"username": "thomas", "password": "baas-wachtwoord"}).status_code == 200


def test_username_rules_and_throttling(multi):
    c = multi
    code = c.main.create_user("Ella")
    good = {"code": code, "password": "ella-wachtwoord"}
    assert c.post("/api/claim", json=good | {"username": "mama"}).status_code == 409      # bezet (ook andere hoofdletters)
    assert c.post("/api/claim", json=good | {"username": "x"}).status_code == 400          # te kort
    assert c.post("/api/claim", json=good | {"username": "Ella", "password": "kort"}).status_code == 400
    assert c.post("/api/claim", json=good | {"username": "Ella"}).status_code == 200
    for _ in range(c.main.MAX_FAILS):
        assert c.post("/api/login", json={"username": "Ella", "password": "fout-fout-fout"}).status_code == 401
    assert c.post("/api/login", json={"username": "Ella", "password": "ella-wachtwoord"}).status_code == 429


def test_reset_password_gives_new_invite(multi):
    c, main = multi, multi.main
    new = main.reset_token("Mama")
    assert c.get("/api/next", headers=c.mama).status_code == 401
    assert c.post("/api/login", json={"username": "Mama", "password": "mama-wachtwoord"}).status_code == 401
    assert c.post("/api/claim", json={"code": new, "username": "Mama", "password": "helemaal-nieuw"}).status_code == 200
    assert main.reset_token("Baas") == ""  # eigenaar: opnieuw activeren met APP_TOKEN


def test_xp_levels_and_leaderboard(multi):
    c, main = multi, multi.main
    r = c.post("/api/grade", json={"item_id": "1-1", "grade": 5, "score": 95}, headers=c.mama).json()
    assert r["xp_gained"] == 20 and r["xp"]["total"] == 20 and r["xp"]["level"] == 1   # 10 + nieuw 5 + score 5
    # zelfde woord nog eens die dag: geen extra XP
    assert c.post("/api/grade", json={"item_id": "1-1", "grade": 5, "score": 99}, headers=c.mama).json()["xp_gained"] == 0
    # Moeilijk en later Makkelijk: alleen het verschil telt
    assert c.post("/api/grade", json={"item_id": "1-2", "grade": 3, "score": 60}, headers=c.mama).json()["xp_gained"] == 13
    assert c.post("/api/grade", json={"item_id": "1-2", "grade": 5, "score": 95}, headers=c.mama).json()["xp_gained"] == 7
    assert c.get("/api/stats", headers=c.mama).json()["xp"]["total"] == 40
    c.post("/api/grade", json={"item_id": "1-1", "grade": 4}, headers=c.owner)
    lb = c.get("/api/leaderboard", headers=c.owner).json()
    assert [(x["rank"], x["name"], x["xp"], x["you"]) for x in lb["rows"]] == [(1, "Mama", 40, False), (2, "Baas", 15, True)]
    assert c.get("/api/leaderboard?period=all", headers=c.owner).json()["rows"][0]["name"] == "Mama"
    assert c.get("/api/leaderboard?period=maand", headers=c.owner).status_code == 400
    assert c.get("/api/leaderboard").status_code == 401
    # levelgrenzen: 100, 300, 600 XP
    assert [main.level_for(x) for x in (0, 99, 100, 299, 300, 599, 600)] == [1, 1, 2, 2, 3, 3, 4]
    # een oud weekresultaat telt niet mee in de weekranglijst
    with main.db() as con:
        con.execute("INSERT INTO xp_log (user_id, ts, day, amount, reason) VALUES (2,'x','2020-01-01',500,'test')")
    wk = {x["name"]: x["xp"] for x in c.get("/api/leaderboard", headers=c.owner).json()["rows"]}
    al = {x["name"]: x["xp"] for x in c.get("/api/leaderboard?period=all", headers=c.owner).json()["rows"]}
    assert wk["Mama"] == 40 and al["Mama"] == 540


def test_user_management_rules(multi):
    main = multi.main
    with pytest.raises(ValueError):
        main.create_user("Mama")
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

    async def fake_assess(wav, ref, key=None, region=None, **kwargs):
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
        invite = main.create_user("Mama")
        session = c.post("/api/claim", json={"code": invite, "username": "Mama", "password": "mama-wachtwoord"}).json()["token"]
        assert c.post("/api/grade", json={"item_id": "1-1", "grade": 4}, headers={"X-App-Token": session}).status_code == 200
        # de oude geschiedenis (1-1 met grade 5) levert eenmalig XP op
        assert c.get("/api/stats", headers=h).json()["xp"]["total"] == 10
    assert (tmp_path / "hangul.db.bak-voor-gebruikers").exists()  # reservekopie
    # opnieuw opstarten migreert niet nog eens en verliest niets
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main as main2
    with TestClient(main2.app) as c2:
        assert c2.get("/api/stats", headers={"X-App-Token": "baas-code"}).json()["learned"] == 1


# ------------------------------------------------------------------ meerdere talen
def test_all_courses_are_valid(multi):
    import re
    main = multi.main
    assert set(main.COURSES) == {"ko", "de", "it", "ja", "zh"}
    all_ids = set()
    for code, course in main.COURSES.items():
        assert len(course["lessons"]) == 10, code
        for les in course["lessons"]:
            assert les["title"] and les["note"] and len(les["items"]) >= 7, (code, les["id"])
            for it in les["items"]:
                assert it["text"].strip() and it["nl"].strip(), it
                assert it["id"].count("-") == 1
                if code != "ko":
                    assert re.fullmatch(code + r":\d+-\d+", it["id"]), it["id"]
                    assert it["ko"] == it["text"]
                if code in ("ja", "zh"):
                    assert it.get("rom"), it  # romaji / pinyin
        assert all_ids.isdisjoint(course["items"])
        all_ids |= set(course["items"])
    assert set(main.ITEMS) == all_ids
    assert main.COURSES["zh"]["locale"] == "zh-CN" and main.COURSES["ja"]["voice"].startswith("ja-JP")


def test_language_progress_is_separate(multi):
    c = multi
    assert c.get("/api/config", headers=c.mama).json()["lang"] == "ko"
    assert c.get("/api/next", headers=c.mama).json()["item"]["id"] == "1-1"
    c.post("/api/grade", json={"item_id": "1-1", "grade": 5}, headers=c.mama)

    assert c.put("/api/lang", json={"lang": "xx"}, headers=c.mama).status_code == 400
    assert c.put("/api/lang", json={"lang": "de"}, headers=c.mama).status_code == 200
    cfg = c.get("/api/config", headers=c.mama).json()
    assert cfg["lang"] == "de" and cfg["locale"] == "de-DE" and [x["code"] for x in cfg["languages"]] == ["ko", "de", "it", "ja", "zh"]
    card = c.get("/api/next", headers=c.mama).json()
    assert card["item"]["id"] == "de:1-1" and card["item"]["text"] == "Hallo" and card["item"]["lang"] == "de"
    st = c.get("/api/stats", headers=c.mama).json()
    assert st["learned"] == 0 and st["total"] == len(c.main.COURSES["de"]["items"]) and st["lang"] == "de"
    les = c.get("/api/lessons", headers=c.mama).json()
    assert les[0]["title"] == "Begroetingen" and les[0]["items"][0]["id"] == "de:1-1" and not les[1]["unlocked"]
    c.post("/api/grade", json={"item_id": "de:1-1", "grade": 5}, headers=c.mama)
    assert c.get("/api/stats", headers=c.mama).json()["learned"] == 1

    # terug naar Koreaans: de oude voortgang is er nog en de Duitse telt daar niet mee
    c.put("/api/lang", json={"lang": "ko"}, headers=c.mama)
    st = c.get("/api/stats", headers=c.mama).json()
    assert st["learned"] == 1 and st["total"] == len(c.main.COURSES["ko"]["items"])
    assert c.get("/api/next", headers=c.mama).json()["item"]["id"] == "1-2"
    langs = {x["code"]: x for x in c.get("/api/languages", headers=c.mama).json()}
    assert langs["ko"]["learned"] == 1 and langs["de"]["learned"] == 1 and langs["ja"]["learned"] == 0
    assert langs["ko"]["current"] and not langs["de"]["current"]
    # een andere gebruiker blijft op zijn eigen taal
    assert c.get("/api/config", headers=c.owner).json()["lang"] == "ko"


def test_xp_leaderboards_per_language_and_total(multi):
    c = multi
    c.post("/api/grade", json={"item_id": "1-1", "grade": 5, "score": 95}, headers=c.mama)        # ko 20
    c.post("/api/grade", json={"item_id": "de:1-1", "grade": 5, "score": 95}, headers=c.mama)     # de 20
    c.post("/api/grade", json={"item_id": "ja:1-1", "grade": 4}, headers=c.mama)                  # ja 15
    c.post("/api/grade", json={"item_id": "de:1-2", "grade": 4}, headers=c.owner)                 # de 15

    def board(**q):
        rows = c.get("/api/leaderboard", params=q, headers=c.owner).json()["rows"]
        return [(r["name"], r["xp"]) for r in rows]

    assert board() == [("Mama", 55), ("Baas", 15)]                      # totaal over alle talen
    assert board(lang="de") == [("Mama", 20), ("Baas", 15)]
    assert board(lang="ja") == [("Mama", 15)]                           # alleen wie in die taal XP heeft
    assert board(lang="ko") == [("Mama", 20)]
    assert board(lang="zh") == []
    assert board(lang="de", period="all") == [("Mama", 20), ("Baas", 15)]
    assert c.get("/api/leaderboard", params={"lang": "xx"}, headers=c.owner).status_code == 400
    st = c.get("/api/stats", headers=c.mama).json()
    assert st["xp"]["total"] == 55 and st["lang_xp"]["total"] == 20      # standaard Koreaans
    c.put("/api/lang", json={"lang": "ja"}, headers=c.mama)
    assert c.get("/api/stats", headers=c.mama).json()["lang_xp"]["total"] == 15
    assert [x["xp"] for x in c.get("/api/languages", headers=c.mama).json()] == [20, 20, 0, 15, 0]


def test_speech_uses_locale_and_voice_of_the_language(multi, monkeypatch):
    c, main = multi, multi.main
    seen = {}

    async def fake_assess(wav, ref, key=None, region=None, locale=None):
        seen["assess"] = (ref, locale)
        return {"score": 88.0, "heard": ref, "words": [], "status": "Success"}

    async def fake_tts(text, fmt="mp3", rate="-10%", key=None, region=None, voice=None, locale=None):
        seen["tts"] = (text, voice, locale)
        return b"ID3audio"

    monkeypatch.setattr(main.azure_speech, "enabled", lambda: True)
    monkeypatch.setattr(main.azure_speech, "KEY", "gedeelde-sleutel-123456789")
    monkeypatch.setattr(main.azure_speech, "assess", fake_assess)
    monkeypatch.setattr(main.azure_speech, "tts", fake_tts)
    for item, locale, voice in (("1-1", "ko-KR", main.azure_speech.VOICE), ("de:1-1", "de-DE", "de-DE-KatjaNeural"),
                                ("it:1-1", "it-IT", "it-IT-ElsaNeural"), ("ja:1-1", "ja-JP", "ja-JP-NanamiNeural"),
                                ("zh:1-1", "zh-CN", "zh-CN-XiaoxiaoNeural")):
        ref = main.ITEMS[item]["text"]
        assert c.post(f"/api/assess/{item}", content=wav_bytes(), headers=c.mama).status_code == 200
        assert seen["assess"] == (ref, locale)
        assert c.get(f"/api/tts/{item}", headers=c.mama).status_code == 200
        assert seen["tts"] == (ref, voice, locale)
    # zelfde tekst in een andere stem wordt apart gecachet
    assert len(list((main.TTS_CACHE).glob("*.mp3"))) == 5


def test_text_similarity_for_other_languages():
    assert similarity("Guten Tag", "guten tag!", "de") == 100
    assert similarity("Café", "cafe", "it") == 100
    assert similarity("Guten Tag", "Guten Morgen", "de") < 70
    assert similarity("こんにちは", "こんにちは", "ja") == 100
    assert similarity("你好", "你好吗", "zh") < 100 and similarity("你好", "", "zh") == 0
    assert similarity("감사합니다", "감사합니다", "ko") == 100


def test_database_from_before_languages_is_upgraded(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_TOKEN", "baas-code")
    for mod in ("main", "azure_speech"):
        sys.modules.pop(mod, None)
    import main
    main.init_db()
    with main.db() as con:
        if sqlite3.sqlite_version_info < (3, 35):
            pytest.skip("DROP COLUMN vraagt SQLite 3.35")
        con.execute("ALTER TABLE users DROP COLUMN lang")
        con.execute("ALTER TABLE xp_log DROP COLUMN lang")
        con.execute("INSERT INTO xp_log (user_id, ts, day, amount, reason, item_id) VALUES (1,'x','2026-10-30',15,'review','1-1')")
    main.init_db()
    with main.db() as con:
        assert con.execute("SELECT lang FROM users WHERE id=1").fetchone()[0] == "ko"
        assert con.execute("SELECT lang, amount FROM xp_log").fetchone()[:] == ("ko", 15)


def test_score_ignores_fluency_by_default(monkeypatch):
    import azure_speech
    data = {"RecognitionStatus": "Success", "NBest": [{"Display": "Guten Morgen.", "PronunciationAssessment": {
        "PronScore": 84, "AccuracyScore": 96, "FluencyScore": 62, "CompletenessScore": 100}, "Words": []}]}
    r = azure_speech.parse_assessment(data)
    assert r["score"] == 96 and r["pron_score"] == 84 and r["fluency"] == 62
    # ontbrekend woord: aftrek via volledigheid
    data["NBest"][0]["PronunciationAssessment"]["CompletenessScore"] = 50
    assert azure_speech.parse_assessment(data)["score"] == 48
    # terug naar het Azure-totaal
    monkeypatch.setattr(azure_speech, "SCORE_MODE", "pron")
    assert azure_speech.parse_assessment(data)["score"] == 84
