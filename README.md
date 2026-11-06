# Hangul Buddy 한

A self-hosted language trainer (Korean, German, Italian, Japanese and Chinese) with spaced repetition and pronunciation feedback.
It starts as a web app and is designed to become a small **ESP32 desk device** with a screen and three buttons:

| Button | What it does |
|---|---|
| 🔊 Listen | Plays the native pronunciation |
| 🎙️ Speak | Records you and scores your pronunciation per word |
| 👁️ Translate | Shows romanization, translation and pronunciation tips |

The web app and the future device talk to the **same API**, so the hardware only needs to be a thin client.

## Features

- **Five languages**: Korean, German, Italian, Japanese and Mandarin Chinese, each with its own beginner course (Korean 18 lessons, the others 10), explained in Dutch. Every user can learn several languages, with separate progress and XP per language (switch with the *Ik leer* dropdown). Pronunciation scoring and text-to-speech use the language's Azure locale and voice.
- **Course structure**: progressive beginner lessons (greetings → introductions → numbers → family → food → places → verbs → time → useful phrases; Korean continues with sentence structure, formal speech, past tense, negation and questions), each with a short grammar note. A lesson unlocks when every word of the previous one has been answered correctly once.
- **Spaced repetition (SM-2)**: correct answers come back after 1 day, 3 days, then increasingly longer intervals; mistakes return the same day.
- **Pronunciation scoring** with [Azure Speech pronunciation assessment](https://learn.microsoft.com/azure/ai-services/speech-service/how-to-pronunciation-assessment) (overall and per-word scores; Azure only offers per-syllable scores for en-US), with a fallback to browser speech recognition plus a jamo-level (ㄱ ㅏ ㅁ …) similarity score.
- **Smart recording**: the recording stops by itself shortly after you finish speaking and trims the silence before and after, which also saves Azure audio time. It can be switched off in Settings (per device), and you can always tap Stop yourself.
- **Sentences word by word**: for multi-word items (Korean, German, Italian) you speak each word separately, the sentence gets the average score. Long sentences are scored much more strictly than single words, so this keeps scores fair. Can be switched off in Settings; whole sentences then get a longer pause tolerance.
- **Typing practice**: a *Spreken / Typen* switch on the practice screen. Reviews show the Dutch meaning and you type the word (new words are typed over). Case, spaces and punctuation are ignored; a small typo or a missing accent counts as *Hard*. For Japanese and Chinese romaji/pinyin is accepted as well. Needs a keyboard for the language you learn.
- **Text-to-speech** with Azure neural voices (cached on disk, MP3 for the web, 16 kHz WAV for the ESP32), falling back to the browser's voice.
- **Auto-grading switch**: with a score of 90% or more the word counts as *Easy* and you move on; between 80% and 90% nothing is saved and you try again; below 80% it is marked *Hard* straight away and you stay on the word until you reach 90%. Thresholds are in `/api/config`.
- **Lesson word lists and practice**: every unlocked lesson lists its words with a play button, and a *Practice* mode lets you drill the words you have already seen (it does not affect your review schedule, and with auto-grading on it moves to the next word at 90%).
- **XP, levels and leaderboard**: every review earns XP (Easy 10, Hard 8, new word +5, pronunciation of 90% or more +5; per word per day only your best attempt counts). Levels need 100, 300, 600 … XP. A weekly (Monday to Sunday) and an all-time leaderboard show everyone's XP, level and streak, as a total over all languages or per language.
- **Multiple users**: every user has a username and password and their own progress. Users can store their own Azure Speech key in the *Instellingen* tab (checked, then stored encrypted); without one they fall back to the owner's key from `.env`.
- Monthly speaking time per user in Settings (admin sees everyone), streaks, daily limits, lesson overview, keyboard shortcuts, dark mode.

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

The first user is the owner. Everyone else gets an invitation code from the owner and picks their own username and password in the app:

```bash
docker compose exec hangul-buddy python manage.py add-user Mama        # prints a one-time invitation code
docker compose exec hangul-buddy python manage.py list
docker compose exec hangul-buddy python manage.py reset-password Mama  # forgot password: wipes it, prints a new code
docker compose exec hangul-buddy python manage.py remove-user Mama     # also deletes their progress
```

The owner activates the account the same way, using `APP_TOKEN` as the code. `APP_TOKEN` keeps working as a header for the ESP32 device.

Security: passwords are hashed with scrypt, session tokens and invitation codes are stored as SHA-256 hashes, sessions last 90 days, and repeated failed logins are throttled. Everything lives in the SQLite database in the Docker volume (`/data`, ignored by git together with `.env`). Personal Azure keys are encrypted with `KEY_SECRET` (default `APP_TOKEN`) and are never sent back to the browser. Without `APP_TOKEN` the app runs in open single-user mode. An existing database is migrated automatically (a backup `hangul.db.bak-voor-gebruikers` is made first) and existing history is turned into XP once.

## API (also used by the device)

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/next` | Next card (reviews first, then new words). `?extra=1` ignores the daily limit |
| POST | `/api/grade` | `{"item_id": "1-2", "grade": 0-5, "score": 82}` updates the schedule |
| GET | `/api/tts/{item_id}?fmt=mp3\|wav` | Pronunciation audio |
| POST | `/api/assess/{item_id}` | Body: raw WAV (16 kHz, mono, 16-bit) → overall score, per-word scores, suggested grade |
| POST | `/api/assess-text/{item_id}` | `{"heard": "..."}` → similarity score (fallback) |
| GET | `/api/lessons`, `/api/stats`, `/api/config` | Overview and settings |
| POST | `/api/login`, `/api/claim`, `/api/logout` | `{username, password}` → session token; `{code, username, password}` activates an account; end session |
| GET | `/api/leaderboard?period=week\|all&lang=all\|ko\|de\|it\|ja\|zh` | Ranking with XP, level and streak (total or per language) |
| GET | `/api/languages` | All languages with your progress and XP |
| PUT | `/api/lang` | `{"lang": "de"}` choose the language you are learning |
| GET | `/api/settings` | Own user, key source (`own`/`shared`) |
| PUT | `/api/settings/password` | `{old, new}` change password (logs out other devices) |
| PUT/DELETE | `/api/settings/azure` | `{"key": "...", "region": "..."}` check and store / remove own Azure key |

If `APP_TOKEN` is set, send the session token from `/api/login` (or the owner's `APP_TOKEN`) as the `X-App-Token` header. Interactive docs: `/api/docs`.

## Adding lessons and languages

Each language has a course file in `app/content/`: `lessons.json` (Korean), `de.json`, `it.json`, `ja.json` and `zh.json`. An item has an `id`, the text in `text` (Korean uses the older name `ko`), an optional `rom` (romanization / pinyin), `nl` (Dutch translation) and an optional `tip`. Ids only need to be unique within a file: the loader prefixes them with the language (`de:1-1`), Korean ids stay as they are. Never change an id once used, because progress is stored per id.

A new language needs an entry in `LANGS` in `app/main.py` (name, flag, Azure locale and neural voice) and a course file. Azure's pronunciation assessment supports a limited set of locales, see the Azure documentation; for other languages the app still plays audio but scoring is less precise.

## Roadmap

- [x] Web app with the same three-button flow as the device
- [ ] ESP32-S3 firmware: OLED/e-ink screen, INMP441 microphone, MAX98357A speaker, three buttons
- [ ] 3D-printed case
- [ ] Pronunciation tips in plain language from an LLM, based on the per-word scores
- [ ] More lessons (TOPIK I vocabulary)

## Tech

Python · FastAPI · SQLite · vanilla JS (Web Audio, Web Speech) · Docker · Azure AI Speech · pytest
