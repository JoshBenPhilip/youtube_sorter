"""Fetch a channel's videos, write notes for each one, and group them into course topics."""

import json
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yt_dlp
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
CHANNELS_DIR = DATA_DIR / "channels"
VIDEO_CACHE_DIR = DATA_DIR / "videos"

# Per-video notes are high volume, so they use the low-cost model; the single
# topic-grouping call needs more judgment and uses a stronger one.
NOTES_MODEL = os.environ.get("NOTES_MODEL", "claude-haiku-5-5")
OUTLINE_MODEL = os.environ.get("OUTLINE_MODEL", "claude-sonnet-5-5")

# USD per million tokens (input, output), used to show what each channel cost.
PRICES = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
WORKERS = int(os.environ.get("WORKERS", "4"))

_client = None
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(max_retries=5)
    return _client


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------

class Chapter(BaseModel):
    start_seconds: int = Field(description="Where this section starts, in seconds from the start of the video")
    title: str = Field(description="Short section title")
    points: list[str] = Field(description="2-5 main points made in this section, one sentence each")


class VideoNotes(BaseModel):
    summary: str = Field(description="Blinkist-style summary: 2-4 short paragraphs capturing the core ideas")
    key_takeaways: list[str] = Field(description="3-7 key takeaways a learner should remember")
    chapters: list[Chapter] = Field(description="Table of contents in time order, covering the whole video")
    topics: list[str] = Field(description="1-3 short topic labels for this video, e.g. 'Pricing strategy'")


class Module(BaseModel):
    title: str = Field(description="Course module title")
    description: str = Field(description="One or two sentences on what this module teaches")
    video_ids: list[str] = Field(description="IDs of the videos in this module, in the recommended watching order")


class CourseOutline(BaseModel):
    modules: list[Module]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", text).strip("-").lower()[:80] or "channel"


def _lock(slug: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(slug, threading.Lock())


def load_channel(slug: str) -> dict | None:
    path = CHANNELS_DIR / f"{slug}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_channel(data: dict) -> None:
    CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHANNELS_DIR / f"{data['slug']}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def update_channel(slug: str, fn) -> dict:
    with _lock(slug):
        data = load_channel(slug)
        fn(data)
        save_channel(data)
        return data


def list_channels() -> list[dict]:
    if not CHANNELS_DIR.exists():
        return []
    out = []
    for path in CHANNELS_DIR.glob("*.json"):
        d = json.loads(path.read_text())
        out.append({k: d.get(k) for k in ("slug", "title", "url", "status", "created_at", "total", "done")})
    return sorted(out, key=lambda d: d.get("created_at") or "", reverse=True)


# ---------------------------------------------------------------------------
# YouTube
# ---------------------------------------------------------------------------

def normalize_channel_url(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("@"):
        return f"https://www.youtube.com/{raw}/videos"
    if not raw.startswith("http"):
        if "youtube.com" in raw or "youtu.be" in raw:
            raw = "https://" + raw
        else:
            return f"https://www.youtube.com/@{raw}/videos"
    if "list=" in raw or "/playlist" in raw:
        return raw
    # A bare channel URL lists its tabs instead of its uploads; point at the Videos tab.
    m = re.match(r"(https?://(?:www\.|m\.)?youtube\.com/(?:@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+))/?$", raw)
    if m:
        return m.group(1) + "/videos"
    return raw


def fetch_channel_videos(url: str, limit: int | None) -> tuple[str, list[dict]]:
    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True}
    if limit:
        opts["playlistend"] = limit
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    title = info.get("channel") or info.get("uploader") or info.get("title") or url
    videos = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        videos.append({
            "id": e["id"],
            "title": e.get("title") or e["id"],
            "duration": e.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
        })
    return title, videos


def fetch_transcript(video_id: str) -> list[dict] | None:
    api = YouTubeTranscriptApi()
    try:
        transcripts = api.list(video_id)
        try:
            transcript = transcripts.find_transcript(["en", "en-US", "en-GB"])
        except Exception:
            transcript = next(iter(transcripts))
        fetched = transcript.fetch()
    except Exception:
        return None
    return [{"start": s.start, "text": s.text} for s in fetched.snippets]


def fetch_video_details(video_id: str) -> dict:
    """Fallback when there is no transcript: description and creator chapters."""
    opts = {"quiet": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except Exception:
        return {}
    return {
        "description": info.get("description") or "",
        "chapters": info.get("chapters") or [],
        "duration": info.get("duration"),
    }


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def transcript_to_text(snippets: list[dict], block_seconds: int = 20) -> str:
    """Merge caption snippets into ~20s timestamped lines to keep the prompt compact."""
    lines, current, block_start = [], [], None
    for s in snippets:
        if block_start is None:
            block_start = s["start"]
        current.append(s["text"].replace("\n", " "))
        if s["start"] - block_start >= block_seconds:
            lines.append(f"[{fmt_ts(block_start)}] {' '.join(current)}")
            current, block_start = [], None
    if current:
        lines.append(f"[{fmt_ts(block_start)}] {' '.join(current)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

NOTES_SYSTEM = (
    "You turn YouTube videos into study notes for an online course built from the creator's videos. "
    "Write in clear, plain language. Base everything on what the video actually says; do not invent content. "
    "Chapter start times must come from the timestamps in the transcript, and the first chapter starts at 0."
)

OUTLINE_SYSTEM = (
    "You organize a creator's YouTube videos into a course. Group videos by topic into modules a learner "
    "would take in order, from foundational to advanced. Every video ID must appear in exactly one module. "
    "Use only the video IDs provided. Aim for modules of roughly 3-15 videos; use a small 'Miscellaneous' "
    "module only for videos that fit nowhere else."
)


def _cost(model: str, usage) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    if model == "claude-haiku-5-5" and usage.input_tokens > 100_000:
        price_in, price_out = 0.50, 2.50  # Haiku's long-prompt rate card
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000


def _parse(model: str, effort: str, system: str, prompt: str, output_format):
    """Returns (parsed output, cost in USD). Haiku refusals are retried on OUTLINE_MODEL."""
    kwargs = {}
    if model != "claude-haiku-5-5":  # Haiku has no server-side refusal fallback
        kwargs = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    response = client().beta.messages.parse(
        model=model,
        max_tokens=16000,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": effort},
        output_format=output_format,
        **kwargs,
    )
    cost = _cost(response.model or model, response.usage)
    if response.stop_reason == "refusal":
        if model != OUTLINE_MODEL:
            parsed, retry_cost = _parse(OUTLINE_MODEL, effort, system, prompt, output_format)
            return parsed, cost + retry_cost
        raise RuntimeError("Claude declined to process this content")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("Response was cut off (too long)")
    return response.parsed_output, cost


def make_video_notes(video: dict) -> tuple[dict, float]:
    snippets = fetch_transcript(video["id"])
    if snippets:
        source = "transcript"
        body = f"Transcript (timestamps are [m:ss]):\n{transcript_to_text(snippets)}"
    else:
        details = fetch_video_details(video["id"])
        if not details.get("description") and not details.get("chapters"):
            raise RuntimeError("No transcript or description available")
        source = "description"
        chapters = "\n".join(f"[{fmt_ts(c['start_time'])}] {c['title']}" for c in details["chapters"])
        body = (
            "No transcript is available. Work from the description and creator chapters only, "
            "and keep the notes brief rather than guessing.\n\n"
            f"Description:\n{details['description']}\n\nCreator chapters:\n{chapters or '(none)'}"
        )
    prompt = f"Video title: {video['title']}\n\n{body}"
    notes, cost = _parse(NOTES_MODEL, "medium", NOTES_SYSTEM, prompt, VideoNotes)
    result = notes.model_dump()
    result["chapters"].sort(key=lambda c: c["start_seconds"])
    result["source"] = source
    return result, cost


def make_outline(videos: list[dict]) -> tuple[list[dict], float]:
    lines = []
    for v in videos:
        n = v["notes"]
        first_para = n["summary"].split("\n")[0][:400]
        lines.append(f"- id={v['id']} | {v['title']} | topics: {', '.join(n['topics'])} | {first_para}")
    prompt = "Videos:\n" + "\n".join(lines)
    outline, cost = _parse(OUTLINE_MODEL, "medium", OUTLINE_SYSTEM, prompt, CourseOutline)

    # Guarantee every video lands in exactly one module, whatever the model returned.
    known = {v["id"] for v in videos}
    seen: set[str] = set()
    modules = []
    for m in outline.modules:
        ids = [i for i in m.video_ids if i in known and i not in seen]
        seen.update(ids)
        if ids:
            modules.append({"title": m.title, "description": m.description, "video_ids": ids})
    missing = [v["id"] for v in videos if v["id"] not in seen]
    if missing:
        modules.append({"title": "Other videos", "description": "Videos that were not placed in a module.",
                        "video_ids": missing})
    return modules, cost


# ---------------------------------------------------------------------------
# Job orchestration
# ---------------------------------------------------------------------------

def _cached_notes(video_id: str) -> dict | None:
    path = VIDEO_CACHE_DIR / f"{video_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _cache_notes(video_id: str, notes: dict) -> None:
    VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (VIDEO_CACHE_DIR / f"{video_id}.json").write_text(json.dumps(notes, indent=2))


def start_job(raw_url: str, limit: int | None) -> str:
    url = normalize_channel_url(raw_url)
    slug = slugify(re.sub(r"^https?://(www\.)?youtube\.com/", "", url).removesuffix("/videos"))
    existing = load_channel(slug)
    if existing and existing["status"] in ("listing", "summarizing", "organizing"):
        return slug
    save_channel({
        "cost_usd": existing.get("cost_usd", 0.0) if existing else 0.0,
        "slug": slug, "url": url, "title": raw_url.strip(), "status": "listing",
        "created_at": datetime.now(timezone.utc).isoformat(), "limit": limit,
        "total": 0, "done": 0, "failed": 0, "error": None, "videos": [], "modules": [],
    })
    threading.Thread(target=_run_job, args=(slug, url, limit), daemon=True).start()
    return slug


def _run_job(slug: str, url: str, limit: int | None) -> None:
    try:
        title, videos = fetch_channel_videos(url, limit)
        if not videos:
            raise RuntimeError("No videos found at that URL")

        def set_listing(d):
            d.update(title=title, total=len(videos), status="summarizing",
                     videos=[{**v, "notes": None, "error": None} for v in videos])
        update_channel(slug, set_listing)

        def process(index: int, video: dict):
            notes, error, cost = _cached_notes(video["id"]), None, 0.0
            if notes is None:
                try:
                    notes, cost = make_video_notes(video)
                    _cache_notes(video["id"], notes)
                except Exception as e:  # keep going; one bad video shouldn't sink the channel
                    error = str(e)

            def record(d):
                d["videos"][index].update(notes=notes, error=error)
                d["cost_usd"] += cost
                d["done"] += 1
                d["failed"] += error is not None
            update_channel(slug, record)

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda iv: process(*iv), enumerate(videos)))

        update_channel(slug, lambda d: d.update(status="organizing"))
        data = load_channel(slug)
        ready = [v for v in data["videos"] if v["notes"]]
        if not ready:
            raise RuntimeError("Could not produce notes for any video (see per-video errors)")
        modules, cost = make_outline(ready)
        update_channel(slug, lambda d: d.update(modules=modules, status="done", cost_usd=d["cost_usd"] + cost))
    except Exception as e:
        update_channel(slug, lambda d: d.update(status="error", error=str(e)))


def to_markdown(data: dict) -> str:
    by_id = {v["id"]: v for v in data["videos"]}
    out = [f"# {data['title']} — Course outline", ""]
    for i, m in enumerate(data["modules"], 1):
        out += [f"## Module {i}: {m['title']}", "", m["description"], ""]
        for vid in m["video_ids"]:
            v = by_id[vid]
            n = v["notes"]
            out += [f"### {v['title']}", f"https://www.youtube.com/watch?v={vid}", "", "**Summary**", "", n["summary"], ""]
            out += ["**Key takeaways**", ""] + [f"- {t}" for t in n["key_takeaways"]] + [""]
            out += ["**Contents**", ""]
            for c in n["chapters"]:
                link = f"https://www.youtube.com/watch?v={vid}&t={c['start_seconds']}s"
                out.append(f"- [{fmt_ts(c['start_seconds'])}]({link}) **{c['title']}**")
                out += [f"  - {p}" for p in c["points"]]
            out.append("")
    return "\n".join(out)
