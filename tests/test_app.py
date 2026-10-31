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

    async def fake_assess(wav, ref):
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
