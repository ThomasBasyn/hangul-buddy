"""Azure Speech: tekst-naar-spraak en uitspraakbeoordeling (REST, geen SDK nodig)."""

import asyncio
import base64
import json
import os
from xml.sax.saxutils import escape

import httpx

KEY = os.getenv("AZURE_SPEECH_KEY", "")
REGION = os.getenv("AZURE_SPEECH_REGION", "westeurope")
VOICE = os.getenv("AZURE_TTS_VOICE", "ko-KR-SunHiNeural")
LANG = "ko-KR"

FORMATS = {
    "mp3": ("audio-24khz-48kbitrate-mono-mp3", "audio/mpeg"),
    # voor de ESP32 (I2S-speaker): ruwe 16 kHz WAV
    "wav": ("riff-16khz-16bit-mono-pcm", "audio/wav"),
}


def enabled() -> bool:
    return bool(KEY)


async def tts(text: str, fmt: str = "mp3", rate: str = "-10%", key: str | None = None,
              region: str | None = None) -> bytes:
    key, region = key or KEY, region or REGION
    output, _ = FORMATS[fmt]
    ssml = (
        f'<speak version="1.0" xml:lang="{LANG}"><voice name="{VOICE}">'
        f'<prosody rate="{rate}">{escape(text)}</prosody></voice></speak>'
    )
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1",
            headers={
                "Ocp-Apim-Subscription-Key": key,
                "Content-Type": "application/ssml+xml",
                "X-Microsoft-OutputFormat": output,
                "User-Agent": "hangul-buddy",
            },
            content=ssml.encode("utf-8"),
        )
        r.raise_for_status()
        return r.content


STT_URL = "conversation/cognitiveservices/v1"


async def _score(client: httpx.AsyncClient, wav: bytes, reference: str, key: str, region: str) -> dict:
    """Uitspraakscore t.o.v. de referentietekst (Azure legt de opname langs die tekst)."""
    cfg = {
        "ReferenceText": reference,
        "GradingSystem": "HundredMark",
        "Granularity": "Phoneme",
        "Dimension": "Comprehensive",
        "EnableMiscue": True,
    }
    header = base64.b64encode(json.dumps(cfg).encode("utf-8")).decode()
    r = await client.post(
        f"https://{region}.stt.speech.microsoft.com/speech/recognition/{STT_URL}",
        params={"language": LANG, "format": "detailed"},
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
            "Pronunciation-Assessment": header,
            "Accept": "application/json",
        },
        content=wav,
    )
    r.raise_for_status()
    return parse_assessment(r.json())


async def _transcribe(client: httpx.AsyncClient, wav: bytes, key: str, region: str) -> str:
    """Gewone spraakherkenning zonder referentietekst: wat Azure echt hoorde."""
    r = await client.post(
        f"https://{region}.stt.speech.microsoft.com/speech/recognition/{STT_URL}",
        params={"language": LANG, "format": "simple"},
        headers={
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
            "Accept": "application/json",
        },
        content=wav,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("RecognitionStatus") != "Success":
        return ""
    return data.get("DisplayText", "")


async def verify(key: str, region: str) -> None:
    """Controleer of een sleutel en regio samen werken (vraagt een tijdelijk toegangstoken aan)."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"https://{region}.api.cognitive.microsoft.com/sts/v1.0/issueToken",
            headers={"Ocp-Apim-Subscription-Key": key, "Content-Length": "0"},
        )
        r.raise_for_status()


async def assess(wav: bytes, reference: str, key: str | None = None, region: str | None = None) -> dict:
    """Uitspraakbeoordeling. Verwacht 16 kHz, 16-bit, mono WAV."""
    key, region = key or KEY, region or REGION
    async with httpx.AsyncClient(timeout=30) as client:
        scored, heard = await asyncio.gather(
            _score(client, wav, reference, key, region),
            _transcribe(client, wav, key, region),
            return_exceptions=True,
        )
    if isinstance(scored, BaseException):
        raise scored
    # De "heard" uit de beoordeling is de referentietekst, dus niet bruikbaar als transcript.
    scored["heard"] = "" if isinstance(heard, BaseException) else heard
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
    return {
        "score": float(s.get("PronScore", s.get("AccuracyScore", 0))),
        "accuracy": s.get("AccuracyScore"),
        "fluency": s.get("FluencyScore"),
        "completeness": s.get("CompletenessScore"),
        "heard": best.get("Display") or best.get("Lexical", ""),
        "words": words,
        "status": "Success",
    }
