# Hangul Buddy 한

A self-hosted Korean word trainer with spaced repetition and pronunciation feedback.
It starts as a web app and is designed to become a small **ESP32 desk device** with a screen and three buttons:

| Button | What it does |
|---|---|
| 🔊 Listen | Plays the native pronunciation |
| 🎙️ Speak | Records you and scores your pronunciation per word |
| 👁️ Translate | Shows romanization, translation and pronunciation tips |

The web app and the future device talk to the **same API**, so the hardware only needs to be a thin client.

## Features

- **Course structure**: 10 progressive beginner lessons (greetings → introductions → numbers → family → food → places → verbs → time → useful phrases), each with a short grammar note. A lesson unlocks when every word of the previous one has been answered correctly once.
- **Spaced repetition (SM-2)**: correct answers come back after 1 day, 3 days, then increasingly longer intervals; mistakes return the same day.
- **Pronunciation scoring** with [Azure Speech pronunciation assessment](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-pronunciation-assessment) (overall and per-word scores; Azure only offers per-syllable scores for en-US), with a fallback to browser speech recognition plus a jamo-level (ㄱ ㅏ ㅁ …) similarity score.
- **Text-to-speech** with Azure neural voices (cached on disk, MP3 for the web, 16 kHz WAV for the ESP32), falling back to the browser's voice.
- **Auto-grading switch**: with a score of 90% or more the word counts as *Easy* and you move on; between 80% and 90% nothing is saved and you try again; below 80% it is marked *Hard* straight away and you stay on the word until you reach 90%. Thresholds are in `/api/config`.
- **Lesson word lists and practice**: every unlocked lesson lists its words with a play button, and a *Practice* mode lets you drill the words you have already seen (it does not affect your review schedule, and with auto-grading on it moves to the next word at 90%).
- **Multiple users**: every user has their own login code and progress. Users can store their own Azure Speech key in the *Instellingen* tab (checked, then stored encrypted); without one they fall back to the owner's key from `.env`.
- Streaks, daily limits, lesson overview, keyboard shortcuts, dark mode.

## Architecture

```
┌──────────────┐     ┌──────────────────────────┐     ┌──────────────┐
│ Web app      │────▶│ FastAPI + SQLite         │────▶│ Azure Speech │
│ (now)        │     │ lessons, SRS, scoring    │     │ TTS + scoring│
├──────────────┤     │ runs in Docker on my     │     └──────────────┘
│ ESP32 device │────▶│ home server              │
│ (planned)    │     └──────────────────────────┘
└──────────────┘
```

## Run it

```bash
cp .env.example .env        # optionally add an Azure Speech key
docker compose up -d --build
# open http://localhost:3310
```

The microphone only works over **HTTPS or localhost**. On a home server, put it behind
`tailscale serve`, a Cloudflare Tunnel or a reverse proxy with TLS.

Development without Docker:

```bash
pip install -r app/requirements.txt pytest
cd app && DATA_DIR=../data uvicorn main:app --reload
pytest ../tests
```

## Users

The first user is the owner and logs in with `APP_TOKEN` (name: `OWNER_NAME`). Add others with the CLI; the code is shown once:

```bash
docker compose exec hangul-buddy python manage.py add-user Mama
docker compose exec hangul-buddy python manage.py list
docker compose exec hangul-buddy python manage.py reset-token Mama
docker compose exec hangul-buddy python manage.py remove-user Mama   # also deletes their progress
```

Codes are stored hashed. Personal Azure keys are encrypted with `KEY_SECRET` (default `APP_TOKEN`) and are never sent back to the browser. Without `APP_TOKEN` the app runs in open single-user mode. An existing single-user database is migrated automatically (a backup `hangul.db.bak-voor-gebruikers` is made first).

## API (also used by the device)

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/next` | Next card (reviews first, then new words). `?extra=1` ignores the daily limit |
| POST | `/api/grade` | `{"item_id": "1-2", "grade": 0-5, "score": 82}` updates the schedule |
| GET | `/api/tts/{item_id}?fmt=mp3\|wav` | Pronunciation audio |
| POST | `/api/assess/{item_id}` | Body: raw WAV (16 kHz, mono, 16-bit) → overall score, per-word scores, suggested grade |
| POST | `/api/assess-text/{item_id}` | `{"heard": "..."}` → similarity score (fallback) |
| GET | `/api/lessons`, `/api/stats`, `/api/config` | Overview and settings |
| GET | `/api/settings` | Own user, key source (`own`/`shared`) |
| PUT/DELETE | `/api/settings/azure` | `{"key": "...", "region": "..."}` check and store / remove own Azure key |

If `APP_TOKEN` is set, send your personal code as the `X-App-Token` header. Interactive docs: `/api/docs`.

## Adding lessons

Lessons live in [`app/content/lessons.json`](app/content/lessons.json). Each item has an `id`, `ko`, `rom`, `nl` and an optional `tip`. Item ids must be unique and should never be changed once used, because progress is stored per id.

## Roadmap

- [x] Web app with the same three-button flow as the device
- [ ] ESP32-S3 firmware: OLED/e-ink screen, INMP441 microphone, MAX98357A speaker, three buttons
- [ ] 3D-printed case
- [ ] Pronunciation tips in plain language from an LLM, based on the per-word scores
- [ ] More lessons (TOPIK I vocabulary)

## Tech

Python · FastAPI · SQLite · vanilla JS (Web Audio, Web Speech) · Docker · Azure AI Speech · pytest
