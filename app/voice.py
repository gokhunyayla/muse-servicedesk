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


def transcribe(audio: bytes, mime: str = "audio/wav") -> str:
    wav = audio if mime == "audio/wav" else to_wav(audio)
    return _transcriber(wav)


def set_transcriber(fn: Callable[[bytes], str]) -> None:
    global _transcriber
    _transcriber = fn
