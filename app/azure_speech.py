"""Azure Speech: tekst-naar-spraak en uitspraakbeoordeling (REST, geen SDK nodig)."""

import asyncio
import base64
import json
import os
import weakref
from xml.sax.saxutils import escape

import httpx

KEY = os.getenv("AZURE_SPEECH_KEY", "")
REGION = os.getenv("AZURE_SPEECH_REGION", "westeurope")
VOICE = os.getenv("AZURE_TTS_VOICE", "ko-KR-SunHiNeural")
LANG = "ko-KR"
# Welke score telt: "accuracy" (uitspraak x volledigheid, vloeiendheid telt niet mee) of "pron" (Azure-totaal)
SCORE_MODE = os.getenv("AZURE_SCORE_MODE", "accuracy").lower()

# De gratis laag (F0) staat maar één spraakaanroep tegelijk toe. We doen ze daarom één voor één per sleutel
# (zet AZURE_STT_CONCURRENCY hoger bij een betaalde laag) en proberen het bij 429 of 503 nog twee keer.
STT_CONCURRENCY = int(os.getenv("AZURE_STT_CONCURRENCY", "1"))
RETRIES = 2
RETRY_BASE = 0.8  # seconden, wordt bij elke nieuwe poging groter
_slots: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]]" = weakref.WeakKeyDictionary()


async def _post(client: httpx.AsyncClient, url: str, key: str, limit: bool = True, **kwargs) -> httpx.Response:
    """POST met wachtrij per sleutel en nieuwe pogingen bij 'te druk'. Geeft een HTTPStatusError bij falen."""
    per_loop = _slots.setdefault(asyncio.get_running_loop(), {})
    sem = per_loop.setdefault(key, asyncio.Semaphore(STT_CONCURRENCY))
    for attempt in range(RETRIES + 1):
        if limit:
            async with sem:
                r = await client.post(url, **kwargs)
        else:
            r = await client.post(url, **kwargs)
        if r.status_code not in (429, 503) or attempt == RETRIES:
            r.raise_for_status()
            return r
        try:
            wait = float(r.headers.get("Retry-After", ""))
        except ValueError:
            wait = RETRY_BASE * (attempt + 1)
        await asyncio.sleep(min(wait, 3.0))
    raise AssertionError("onbereikbaar")


FORMATS = {
    "mp3": ("audio-24khz-48kbitrate-mono-mp3", "audio/mpeg"),
    # voor de ESP32 (I2S-speaker): ruwe 16 kHz WAV
    "wav": ("riff-16khz-16bit-mono-pcm", "audio/wav"),
}


def enabled() -> bool:
    return bool(KEY)


async def tts(text: str, fmt: str = "mp3", rate: str = "-10%", key: str | None = None,
              region: str | None = None, voice: str | None = None, locale: str | None = None) -> bytes:
    key, region = key or KEY, region or REGION
    voice, locale = voice or VOICE, locale or LANG
    output, _ = FORMATS[fmt]
    ssml = (
        f'<speak version="1.0" xml:lang="{locale}"><voice name="{voice}">'
        f'<prosody rate="{rate}">{escape(text)}</prosody></voice></speak>'
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await _post(
            client, f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1", key, limit=False,
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": output,
                "User-Agent": "hangul-buddy",
            },
            content=ssml.encode("utf-8"),
        )
        return r.content


STT_URL = "conversation/cognitiveservices/v1"


async def _score(client: httpx.AsyncClient, wav: bytes, reference: str, key: str, region: str,
                 locale: str = LANG) -> dict:
    """Uitspraakscore t.o.v. de referentietekst (Azure legt de opname langs die tekst)."""
    cfg = {
        "ReferenceText": reference,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": True,
    }
    header = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode()
    r = await _post(
        client, f"https://{region}.stt.speech.microsoft.com/speech/recognition/{STT_URL}", key,
        params={"language": locale, "format": "detailed"},
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
            "Pronunciation-Assessment": header,
            "Accept": "application/json",
        },
        content=wav,
    )
    return parse_assessment(r.json())


async def verify(key: str, region: str) -> None:
    """Controleer of een sleutel en regio samen werken (vraagt een tijdelijk toegangstoken aan)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
            headers={"Ocp-Apim-Subscription-Key": key, "Content-Length": "0"},
        )
        r.raise_for_status()


async def assess(wav: bytes, reference: str, key: str | None = None, region: str | None = None,
                 locale: str | None = None) -> dict:
    """Uitspraakbeoordeling. Verwacht 16 kHz, 16-bit, mono WAV."""
    key, region, locale = key or KEY, region or REGION, locale or LANG
    # Eén aanroep per opname: een tweede (voor "Gehoord") zou de Azure-minuten verdubbelen.
    async with httpx.AsyncClient(timeout=30) as client:
        scored = await _score(client, wav, reference, key, region, locale)
    scored["heard"] = ""  # de tekst in de beoordeling is de referentietekst, geen transcript
    return scored


def _scores(obj: dict) -> dict:
    """Azure zet scores soms direct op het object, soms onder PronunciationAssessment."""
    return obj.get("PronunciationAssessment") or obj


def parse_assessment(data: dict) -> dict:
    if data.get("RecognitionStatus") != "Success" or not data.get("NBest"):
        return {"score": 0.0, "heard": "", "words": [], "status": data.get("RecognitionStatus", "Error")}
    best = data["NBest"][0]
    s = _scores(best)
    words = []
    for w in best.get("Words", []):
        ws = _scores(w)
        words.append({
            "word": w.get("Word", ""),
            "score": ws.get("AccuracyScore", 0),
            "error": ws.get("ErrorType", "None"),
            "syllables": [
                {"text": sy.get("Grapheme") or sy.get("Syllable", ""),
                 "score": _scores(sy).get("AccuracyScore", 0)}
                for sy in w.get("Syllables", [])
            ],
        })
    pron = float(s.get("PronScore", s.get("AccuracyScore", 0)))
    acc, comp = s.get("AccuracyScore"), s.get("CompletenessScore")
    # Bij één of twee woorden drukt een korte pauze de vloeiendheid, ook bij een goede uitspraak.
    # Daarom telt standaard alleen de nauwkeurigheid (met aftrek voor ontbrekende woorden).
    score = float(acc) * min(float(comp), 100.0) / 100.0 if SCORE_MODE == "accuracy" and acc is not None and comp is not None else pron
    return {
        "score": round(score, 1),
        "pron_score": pron,
        "accuracy": s.get("AccuracyScore"),
        "fluency": s.get("FluencyScore"),
        "completeness": s.get("CompletenessScore"),
        "heard": best.get("Display") or best.get("Lexical", ""),
        "words": words,
        "status": "Success",
    }
