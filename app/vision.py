"""Vision: SAM 3.1 object location (Responses API) and frame sampling for video.

SAM returns its matches as special-token text: one `<|box;x1=..;y1=..;x2=..;y2=..;w=..;h=..|>` per
object (source pixels), followed by a mask we don't need for tickets.
"""
from __future__ import annotations

import base64
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from app.config import settings

SAM_MODEL = "sam-3.1"
_BOX = re.compile(r"<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)\|>")


def parse_boxes(text: str) -> list[dict]:
    return [{"x1": int(a), "y1": int(b), "x2": int(c), "y2": int(d), "frame_w": int(w), "frame_h": int(h)}
            for a, b, c, d, w, h in _BOX.findall(text)]


def meta_segment(image: bytes, prompt: str, mime: str = "image/jpeg") -> list[dict]:
    from openai import OpenAI
    client = OpenAI(api_key=__import__("app.vault", fromlist=["get"]).get("META_API_KEY"), base_url=settings.meta_base_url)
    data_url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    res = client.responses.create(model=SAM_MODEL, input=[{"role": "user", "content": [
        {"type": "input_text", "text": prompt},
        {"type": "input_image", "image_url": data_url}]}])
    return parse_boxes(res.output_text or "")


_segmenter: Callable[[bytes, str, str], list[dict]] = meta_segment


def segment(image: bytes, prompt: str, mime: str = "image/jpeg") -> list[dict]:
    """Locate every instance of `prompt` (a short noun phrase) in the image."""
    return _segmenter(image, prompt, mime)


def set_segmenter(fn: Callable[[bytes, str, str], list[dict]]) -> None:
    global _segmenter
    _segmenter = fn


def sample_frames(video: bytes, every_sec: float = 2.0, max_frames: int = 6) -> list[bytes]:
    """Pull JPEG frames out of a clip with ffmpeg (every N seconds, capped)."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "clip"
        src.write_bytes(video)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(src), "-vf", f"fps=1/{every_sec}",
                        "-frames:v", str(max_frames), "-q:v", "4", str(Path(d) / "f%02d.jpg")], check=True)
        return [p.read_bytes() for p in sorted(Path(d).glob("f*.jpg"))]
