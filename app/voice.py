"""Speech to text with Muse Voice Transcribe (POST /v1/asr/transcribe).

The endpoint takes WAV only, so other formats (WhatsApp sends OGG/Opus) are converted
with ffmpeg first.
"""
from __future__ import annotations

import json
import subprocess
from typing import Callable

import httpx

from app.config import settings

TRANSCRIBE_MODEL = "muse-voice-transcribe-1.0"


def to_wav(audio: bytes) -> bytes:
    """Convert any ffmpeg-readable audio to 16 kHz mono PCM WAV."""
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"],
        input=audio, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:200]}")
    return proc.stdout


def meta_transcribe(wav: bytes) -> str:
    request = {"model": TRANSCRIBE_MODEL, "audioEncoding": "WAV", "mode": "PUSH_TO_TALK"}
    r = httpx.post(
        f"{settings.meta_base_url}/asr/transcribe",
        headers={"Authorization": f"Bearer {__import__("app.vault", fromlist=["get"]).get("META_API_KEY")}", "Accept": "application/json"},
        files={"request": (None, json.dumps(request), "application/json"),
               "audio": ("audio.wav", wav, "audio/wav")},
        timeout=120)
    r.raise_for_status()
    return r.json().get("transcript", "").strip()


_transcriber: Callable[[bytes], str] = meta_transcribe


def plausible(text: str) -> bool:
    """A desk transcript is Turkish or English: a result with no Latin letters (the model guessing another
    script on a short or noisy note) counts as a failed transcription, so the employee is asked again."""
    import re
    return bool(re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", text or ""))


def enhance(wav: bytes) -> bytes:
    """Second chance for a short or quiet note: normalise loudness and pad half a second of silence at both
    ends, which keeps the first word from being cut and gives the model a clean onset."""
    proc = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11,adelay=500|500,apad=pad_dur=0.5",
                           "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1"], input=wav, capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 and proc.stdout else wav


def transcribe(audio: bytes, mime: str = "audio/wav") -> str:
    """Speech → text. A transcript that is not Turkish/English (the model guessing another script on a short,
    quiet note) is retried once on an enhanced copy; if that fails too the caller gets "" and asks the
    employee to repeat or type."""
    wav = audio if mime == "audio/wav" else to_wav(audio)
    text = _transcriber(wav)
    if plausible(text):
        return text
    text2 = _transcriber(enhance(wav))
    return text2 if plausible(text2) else ""
def set_transcriber(fn: Callable[[bytes], str]) -> None:
    global _transcriber
    _transcriber = fn
