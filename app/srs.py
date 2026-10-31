"""Spaced repetition (SM-2 variant) en uitspraakscore -> beoordeling."""

import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class Card:
    ease: float = 2.5
    interval: int = 0
    reps: int = 0
    lapses: int = 0
    due: date | None = None


def review(card: Card, grade: int, today: date) -> Card:
    """Verwerk een beoordeling (0-5) volgens SM-2.

    - grade < 3: fout -> woord komt vandaag nog eens terug, teller opnieuw.
    - eerste goede herhaling: morgen, tweede: over 3 dagen, daarna interval x ease.
    """
    grade = max(0, min(5, int(grade)))
    if grade < 3:
        card.reps = 0
        card.lapses += 1
        card.interval = 0
    else:
        if card.reps == 0:
            card.interval = 1
        elif card.reps == 1:
            card.interval = 3
        else:
            card.interval = max(card.interval + 1, round(card.interval * card.ease))
        card.reps += 1
    card.ease = max(1.3, card.ease + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    card.due = today + timedelta(days=card.interval)
    return card


def score_to_grade(score: float) -> int:
    """Uitspraakscore (0-100) omzetten naar een SM-2-beoordeling."""
    if score >= 90:
        return 5
    if score >= 75:
        return 4
    if score >= 60:
        return 3
    if score >= 40:
        return 2
    return 1


# ------------------------------------------------------------- Hangul-vergelijking
_L = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_V = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_T = ["", *"ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"]


def jamo(text: str) -> str:
    """Splits Hangul-lettergrepen in losse klanken (jamo), zonder spaties/leestekens."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_L[code // 588])
            out.append(_V[(code % 588) // 28])
            if code % 28:
                out.append(_T[code % 28])
        elif "ㄱ" <= ch <= "ㆎ":
            out.append(ch)
    return "".join(out)


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def letters(text: str) -> str:
    """Kleine letters zonder accenten, spaties en leestekens (voor talen met ons alfabet, kana en hanzi)."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch for ch in text if ch.isalnum())


def similarity(expected: str, heard: str, lang: str = "ko") -> float:
    """Score 0-100: klank-voor-klank overeenkomst (Koreaans: op jamo, andere talen: op letters of tekens)."""
    a, b = (jamo(expected), jamo(heard)) if lang == "ko" else (letters(expected), letters(heard))
    if not a or not b:
        return 0.0
    return round(max(0.0, 1 - _levenshtein(a, b) / max(len(a), len(b))) * 100, 1)
