# Sharing tier: methodology-shareable (transcript fetcher; CONTENTS are INNER ONLY since they may include paid course material)
"""YouTube transcript extractor for Pella's strategy-discovery channel.

Given a YouTube URL or video_id, fetches the transcript (via
youtube-transcript-api primary, yt-dlp fallback) and saves it as a markdown
file under ``<paths.research_root>/video_transcripts/``. Optionally extracts
trading-strategy keywords and proposes a stub queue entry to the idea queue.

CLI:
    python tools/youtube_extractor.py URL_OR_ID [--language en] [--propose-spec] [--no-save]

Implementation notes:
    - Captures both ASR (auto-generated) and uploader-provided captions.
    - On rate-limit (HTTP 429) sleeps 30s and retries once.
    - Idempotent: re-runs overwrite the .md file.
    - Never crashes the calling shell unless the transcript is truly unavailable.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure sibling tools/ + pella package are importable when run as a script
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from pella import config, state  # noqa: E402
from pella import logger as pella_logger  # noqa: E402
from pella.clients import AlertClient  # noqa: E402

_log = pella_logger.get_logger("youtube_extractor")

# ---------------------------------------------------------------------------
# Bootstrap: youtube-transcript-api (auto-install once if missing)
# ---------------------------------------------------------------------------
def _ensure_youtube_transcript_api() -> Any:
    """Import youtube_transcript_api, attempting one-time pip install on failure.

    Returns the module on success, or None if install/import both fail.
    """
    try:
        import youtube_transcript_api  # type: ignore
        return youtube_transcript_api
    except ImportError:
        _log.warning("youtube-transcript-api not installed; attempting pip install")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "youtube-transcript-api"],
                check=True, timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
            _log.event("WARN", "ytapi_install_failed", error=str(e))  # type: ignore[attr-defined]
            return None
        try:
            import youtube_transcript_api  # type: ignore
            return youtube_transcript_api
        except ImportError as e:
            _log.event("WARN", "ytapi_import_failed_after_install", error=str(e))  # type: ignore[attr-defined]
            return None


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------
_VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:[\w=&-]*&)?v=|embed/|shorts/|v/)|youtu\.be/)"
    r"(?P<id>[A-Za-z0-9_-]{11})"
    r"|^(?P<bare>[A-Za-z0-9_-]{11})$"
)


def parse_video_id(url_or_id: str) -> str | None:
    """Extract the 11-char video_id from a URL OR accept a bare id.

    Supports: youtube.com/watch?v=ID, youtu.be/ID, youtube.com/embed/ID,
    youtube.com/shorts/ID, youtube.com/v/ID, and bare 11-char IDs.
    """
    if not url_or_id:
        return None
    s = url_or_id.strip()
    m = _VIDEO_ID_RE.search(s)
    if not m:
        return None
    return m.group("id") or m.group("bare")


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------
def sanitize_title(title: str, max_len: int = 80) -> str:
    """Strip non-alphanumeric except dash/underscore. Default empty -> 'untitled'."""
    if not title:
        return "untitled"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", title).strip("_-")
    cleaned = cleaned[:max_len].strip("_-")
    return cleaned or "untitled"


# ---------------------------------------------------------------------------
# Transcript fetch — primary path (youtube-transcript-api)
# ---------------------------------------------------------------------------
def _fetch_via_ytapi(video_id: str, languages: list[str]) -> dict | None:
    """Fetch via youtube-transcript-api. Returns dict or None on failure.

    Result shape:
        {"entries": [{"text", "start", "duration"}, ...],
         "quality": "auto" | "manual",
         "language": <lang_code>}
    """
    yta = _ensure_youtube_transcript_api()
    if yta is None:
        return None

    YouTubeTranscriptApi = yta.YouTubeTranscriptApi  # type: ignore[attr-defined]

    quality = "auto"  # default; refined below if list_transcripts works
    lang_code = languages[0] if languages else "en"

    # Quality detection (best-effort; may fail on some videos)
    try:
        listing = YouTubeTranscriptApi.list_transcripts(video_id)
        # Prefer manual transcript in requested language
        for t in listing:
            if t.language_code in languages and not getattr(t, "is_generated", True):
                quality = "manual"
                lang_code = t.language_code
                break
        else:
            for t in listing:
                if t.language_code in languages:
                    quality = "auto" if getattr(t, "is_generated", True) else "manual"
                    lang_code = t.language_code
                    break
    except Exception as e:
        _log.event("INFO", "list_transcripts_failed", error=str(e))  # type: ignore[attr-defined]

    # Fetch with one retry on HTTP 429
    for attempt in (1, 2):
        try:
            entries = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
            return {"entries": entries, "quality": quality, "language": lang_code}
        except Exception as e:
            msg = str(e)
            if "429" in msg or "Too Many Requests" in msg:
                if attempt == 1:
                    _log.event("WARN", "ytapi_rate_limited_retry", video_id=video_id)  # type: ignore[attr-defined]
                    time.sleep(30)
                    continue
                _log.event("WARN", "ytapi_rate_limited_giving_up", video_id=video_id)  # type: ignore[attr-defined]
                return None
            _log.event("INFO", "ytapi_fetch_failed", video_id=video_id, error=msg)  # type: ignore[attr-defined]
            return None
    return None


# ---------------------------------------------------------------------------
# Transcript fetch — fallback path (yt-dlp + VTT parser)
# ---------------------------------------------------------------------------
_VTT_TIMESTAMP_RE = re.compile(
    r"^(\d{2}:\d{2}:\d{2}[\.,]\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2}[\.,]\d{3})"
)


def _vtt_ts_to_seconds(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.replace(",", ".").split(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _parse_vtt(vtt_path: Path) -> list[dict]:
    """Minimal VTT parser. Returns list of {text, start, duration}."""
    text = vtt_path.read_text(encoding="utf-8", errors="replace")
    out: list[dict] = []
    block_lines: list[str] = []
    block_start: float | None = None
    block_end: float | None = None

    def _flush() -> None:
        nonlocal block_lines, block_start, block_end
        if block_start is not None and block_end is not None and block_lines:
            txt = " ".join(line.strip() for line in block_lines if line.strip())
            # Strip embedded timing tags <00:00:01.000> and <c>..</c>
            txt = re.sub(r"<[^>]+>", "", txt).strip()
            if txt:
                out.append({
                    "text": txt,
                    "start": block_start,
                    "duration": max(0.0, block_end - block_start),
                })
        block_lines = []
        block_start = None
        block_end = None

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            _flush()
            continue
        m = _VTT_TIMESTAMP_RE.match(line)
        if m:
            _flush()
            block_start = _vtt_ts_to_seconds(m.group(1))
            block_end = _vtt_ts_to_seconds(m.group(2))
            continue
        # skip header lines / cue identifiers
        if line.upper().startswith("WEBVTT") or line.startswith("NOTE") \
                or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if block_start is None:
            # cue identifier line — ignore
            continue
        block_lines.append(line)
    _flush()
    return out


def _fetch_via_ytdlp(video_id: str, language: str) -> dict | None:
    """Fallback: shell out to yt-dlp, parse the .vtt subtitle file."""
    if shutil.which("yt-dlp") is None:
        _log.event("WARN", "ytdlp_missing",  # type: ignore[attr-defined]
                   hint="pip install yt-dlp  OR  uv tool install yt-dlp")
        return None

    tmp_dir = Path(tempfile.mkdtemp(prefix="yt_extract_"))
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        cmd = [
            "yt-dlp", "--skip-download",
            "--write-auto-sub", "--write-sub",
            "--sub-lang", language,
            "--sub-format", "vtt",
            "--no-warnings", "--quiet",
            "-o", str(tmp_dir / "%(id)s.%(ext)s"),
            url,
        ]
        subprocess.run(cmd, check=False, timeout=120,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Find the produced .vtt
        candidates = list(tmp_dir.glob(f"{video_id}*.vtt"))
        if not candidates:
            _log.event("INFO", "ytdlp_no_vtt", video_id=video_id)  # type: ignore[attr-defined]
            return None
        # Prefer the one matching language; else first
        vtt = next((p for p in candidates if f".{language}." in p.name), candidates[0])
        entries = _parse_vtt(vtt)
        if not entries:
            return None
        # yt-dlp sub naming: <id>.<lang>.vtt for manual; auto-sub same shape
        # We can't reliably distinguish auto vs manual from the file alone, so:
        quality = "auto"
        return {"entries": entries, "quality": quality, "language": language}
    except (subprocess.TimeoutExpired, OSError) as e:
        _log.event("WARN", "ytdlp_failed", video_id=video_id, error=str(e))  # type: ignore[attr-defined]
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Metadata fetch (title, channel, duration) — best effort
# ---------------------------------------------------------------------------
def _fetch_metadata(video_id: str) -> dict:
    """Try yt-dlp --dump-json for metadata. Returns {} on failure."""
    if shutil.which("yt-dlp") is None:
        return {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--skip-download", "--dump-json", "--no-warnings", "--quiet", url],
            check=False, timeout=30,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if result.returncode != 0 or not result.stdout:
            return {}
        meta = json.loads(result.stdout.decode("utf-8", errors="replace").splitlines()[0])
        return {
            "title": meta.get("title") or "",
            "channel": meta.get("channel") or meta.get("uploader") or "",
            "duration": meta.get("duration_string") or (
                f"{meta['duration']}s" if meta.get("duration") else ""
            ),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        _log.event("INFO", "metadata_fetch_failed", video_id=video_id, error=str(e))  # type: ignore[attr-defined]
        return {}


# ---------------------------------------------------------------------------
# Strategy keyword extraction
# ---------------------------------------------------------------------------
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("entry_rules", re.compile(r"\b(buy|enter|long|short|sell)\s+when\s+", re.IGNORECASE)),
    ("conditions", re.compile(r"\bif\s+\w+\s+(above|below|crosses|breaks)\s+\w+", re.IGNORECASE)),
    ("rsi_thresholds", re.compile(r"\bRSI\s*[<>]?\s*\d+", re.IGNORECASE)),
    ("indicators", re.compile(r"\b(SMA|EMA|MA|MACD|ADX|ATR)\s*\(?\d+\)?", re.IGNORECASE)),
    ("calendar", re.compile(r"\bevery\s+(monday|tuesday|wednesday|thursday|friday)", re.IGNORECASE)),
    ("exit_rules", re.compile(r"\b(stop|stop loss|sl|take profit|tp)\s*(at|of|=|:)", re.IGNORECASE)),
    ("lookback", re.compile(
        r"\b(\d+)\s*(period|bar|day|hour|minute)s?\s+(?:donchian|breakout|channel|range)",
        re.IGNORECASE,
    )),
]


def _split_sentences(text: str) -> list[str]:
    # Cheap sentence splitter; transcripts rarely have proper punctuation but
    # we still grab whatever boundaries exist.
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def extract_strategy_hypothesis(full_text: str) -> str:
    """Regex-extract trading-strategy keyword contexts from `full_text`.

    Returns a markdown section body. If 0 matches, returns the
    `[NO_STRATEGY_KEYWORDS_DETECTED]` placeholder.
    """
    if not full_text.strip():
        return "[NO_STRATEGY_KEYWORDS_DETECTED]"

    sentences = _split_sentences(full_text)
    matches_by_cat: dict[str, list[str]] = {cat: [] for cat, _ in _PATTERNS}

    for i, sent in enumerate(sentences):
        for cat, pat in _PATTERNS:
            if pat.search(sent):
                before = sentences[i - 1] if i > 0 else ""
                after = sentences[i + 1] if i + 1 < len(sentences) else ""
                ctx = " ".join(s for s in (before, sent, after) if s)
                # cap context length
                ctx = ctx[:500]
                matches_by_cat[cat].append(ctx)
                break  # avoid double-categorising one sentence

    total_hits = sum(len(v) for v in matches_by_cat.values())
    if total_hits == 0:
        return "[NO_STRATEGY_KEYWORDS_DETECTED]"

    out_lines: list[str] = []
    for cat, _ in _PATTERNS:
        hits = matches_by_cat[cat]
        if not hits:
            continue
        out_lines.append(f"### {cat} ({len(hits)} match{'es' if len(hits) != 1 else ''})")
        for h in hits[:5]:  # cap per-category
            out_lines.append(f"- {h}")
        if len(hits) > 5:
            out_lines.append(f"- _(+{len(hits) - 5} more)_")
        out_lines.append("")
    return "\n".join(out_lines).rstrip() or "[NO_STRATEGY_KEYWORDS_DETECTED]"


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------
def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def render_markdown(
    *,
    video_id: str,
    title: str,
    channel: str,
    duration: str,
    quality: str,
    entries: list[dict],
    hypothesis_section: str,
) -> str:
    fetched = datetime.now(timezone.utc).isoformat()
    lines: list[str] = []
    lines.append(f"# {title or video_id}")
    lines.append(f"**URL**: https://youtube.com/watch?v={video_id}")
    lines.append(f"**Channel**: {channel or 'unknown'}")
    lines.append(f"**Length**: {duration or 'unknown'}")
    lines.append(f"**Fetched**: {fetched}")
    lines.append(f"**Transcript quality**: {quality} "
                 f"(`auto` = ASR autogenerated; `manual` = uploader provided)")
    lines.append("")
    lines.append("## Sharing tier")
    lines.append("INNER ONLY (may include paid course material — never push to public mirror)")
    lines.append("")
    lines.append("## Strategy hypothesis (regex-extracted, low confidence)")
    lines.append(hypothesis_section)
    lines.append("")
    lines.append("## Full transcript")
    lines.append("")
    if not entries:
        lines.append("[TRANSCRIPT_UNAVAILABLE]")
    else:
        for e in entries:
            ts = _format_timestamp(float(e.get("start", 0.0)))
            text = (e.get("text") or "").replace("\n", " ").strip()
            lines.append(f"{ts} {text}")
    lines.append("")
    return "\n".join(lines)


def _stub_unavailable_markdown(video_id: str, reason: str) -> str:
    fetched = datetime.now(timezone.utc).isoformat()
    return (
        f"# {video_id}\n"
        f"**URL**: https://youtube.com/watch?v={video_id}\n"
        f"**Channel**: unknown\n"
        f"**Length**: unknown\n"
        f"**Fetched**: {fetched}\n"
        f"**Transcript quality**: unavailable\n\n"
        f"## Sharing tier\n"
        f"INNER ONLY (may include paid course material — never push to public mirror)\n\n"
        f"## Strategy hypothesis (regex-extracted, low confidence)\n"
        f"[NO_STRATEGY_KEYWORDS_DETECTED]\n\n"
        f"## Full transcript\n\n"
        f"[TRANSCRIPT_UNAVAILABLE] reason: {reason}\n"
    )


# ---------------------------------------------------------------------------
# Idea-queue stub proposal
# ---------------------------------------------------------------------------
def _propose_spec_via_queue_manager(*, source_ref: str, hypothesis: str) -> str | None:
    """Try to add a stub entry to the idea queue.

    Returns the new queue id on success, or None on failure (logged).
    """
    paths = config.get_paths()
    summary = "Video extraction (auto-summary): " + hypothesis.strip().replace("\n", " ")[:200]
    entry = {
        "source": "video",
        "source_ref": source_ref,
        "hypothesis": summary,
        "test_spec": {
            "_TODO_FILL_FROM_TRANSCRIPT": True,
            "expert": None,
            "symbol": None,
        },
        "priority": 3,
    }

    # Preferred: sibling tools/idea_queue_manager.add_entry()
    try:
        import idea_queue_manager  # type: ignore  # sibling tool
        if hasattr(idea_queue_manager, "add_entry"):
            new_id = idea_queue_manager.add_entry(**entry)
            return str(new_id)
    except ImportError:
        _log.event("INFO", "idea_queue_manager_not_found_using_fallback")  # type: ignore[attr-defined]
    except Exception as e:
        _log.event("WARN", "idea_queue_manager_add_entry_failed", error=str(e))  # type: ignore[attr-defined]

    # Fallback: direct write to discovery_queue.json
    try:
        queue_path = Path(paths["discovery_queue"])
        existing = state.read_json(queue_path, default={"entries": []})
        if not isinstance(existing, dict) or "entries" not in existing:
            existing = {"entries": []}
        new_id = f"video_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        record = {
            "id": new_id,
            "added_ts": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            **entry,
        }
        existing["entries"].append(record)
        state.write_json_atomic(queue_path, existing)
        return new_id
    except Exception as e:
        _log.event("WARN", "discovery_queue_fallback_write_failed", error=str(e))  # type: ignore[attr-defined]
        return None


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------
def fetch_transcript(video_id: str, languages: list[str]) -> dict | None:
    """Try ytapi -> yt-dlp. Return result dict or None."""
    result = _fetch_via_ytapi(video_id, languages)
    if result and result.get("entries"):
        _log.event("INFO", "transcript_fetched_via_ytapi",  # type: ignore[attr-defined]
                   video_id=video_id, entries=len(result["entries"]),
                   quality=result.get("quality"))
        return result
    # Fallback
    for lang in languages:
        result = _fetch_via_ytdlp(video_id, lang)
        if result and result.get("entries"):
            _log.event("INFO", "transcript_fetched_via_ytdlp",  # type: ignore[attr-defined]
                       video_id=video_id, entries=len(result["entries"]), language=lang)
            return result
    return None


def run(
    *,
    url_or_id: str,
    languages: list[str],
    propose_spec: bool,
    no_save: bool,
) -> int:
    """Main orchestration. Returns process exit code (0 ok, 1 unavailable)."""
    video_id = parse_video_id(url_or_id)
    if not video_id:
        _log.event("WARN", "video_id_unparseable", input=url_or_id)  # type: ignore[attr-defined]
        print(f"ERROR: could not parse video_id from {url_or_id!r}", file=sys.stderr)
        return 1

    _log.event("INFO", "extraction_start",  # type: ignore[attr-defined]
               video_id=video_id, languages=languages,
               propose_spec=propose_spec, no_save=no_save)

    fetched = fetch_transcript(video_id, languages)
    meta = _fetch_metadata(video_id)
    title = meta.get("title") or video_id
    channel = meta.get("channel") or ""
    duration = meta.get("duration") or ""

    if not fetched or not fetched.get("entries"):
        # Auth / availability problem — surface as KILL alert per spec
        try:
            AlertClient("youtube_extractor").alert(
                "WARN",
                "Transcript unavailable",
                f"Could not fetch transcript for {video_id} via ytapi or yt-dlp.",
            )
        except Exception:
            pass

        if no_save:
            print(f"[TRANSCRIPT_UNAVAILABLE] {video_id}")
            return 1

        out_dir = Path(config.get_paths()["research_root"]) / "video_transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{sanitize_title(title)}__{video_id}.md"
        out_path.write_text(
            _stub_unavailable_markdown(video_id, "ytapi+ytdlp_both_failed"),
            encoding="utf-8",
        )
        print(f"[TRANSCRIPT_UNAVAILABLE] stub written -> {out_path}")
        return 1

    entries = fetched["entries"]
    quality = fetched.get("quality", "auto")
    full_text = " ".join((e.get("text") or "").strip() for e in entries)
    hypothesis_section = extract_strategy_hypothesis(full_text)

    md = render_markdown(
        video_id=video_id,
        title=title,
        channel=channel,
        duration=duration,
        quality=quality,
        entries=entries,
        hypothesis_section=hypothesis_section,
    )

    if no_save:
        sys.stdout.write(md + "\n")
    else:
        out_dir = Path(config.get_paths()["research_root"]) / "video_transcripts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{sanitize_title(title)}__{video_id}.md"
        out_path.write_text(md, encoding="utf-8")
        _log.event("INFO", "transcript_saved",  # type: ignore[attr-defined]
                   video_id=video_id, path=str(out_path),
                   entries=len(entries), quality=quality)
        print(f"Saved -> {out_path}")

    if propose_spec:
        url_full = f"https://www.youtube.com/watch?v={video_id}"
        new_id = _propose_spec_via_queue_manager(
            source_ref=url_full,
            hypothesis=hypothesis_section,
        )
        if new_id:
            print(f"Queue stub added: id={new_id}")
            _log.event("INFO", "queue_stub_added", queue_id=new_id, video_id=video_id)  # type: ignore[attr-defined]
        else:
            print("WARN: could not add queue stub (see logs).")

    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="youtube_extractor",
        description="Fetch a YouTube transcript and save to research/video_transcripts/.",
    )
    p.add_argument("url_or_id", help="Full YouTube URL or bare 11-char video_id")
    p.add_argument("--language", default="en",
                   help="Comma-separated preferred languages (default: en)")
    p.add_argument("--propose-spec", action="store_true",
                   help="Add a stub entry to the discovery idea queue.")
    p.add_argument("--no-save", action="store_true",
                   help="Print transcript markdown to stdout, skip file write (debug).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    languages = [s.strip() for s in args.language.split(",") if s.strip()]
    if not languages:
        languages = ["en"]
    try:
        return run(
            url_or_id=args.url_or_id,
            languages=languages,
            propose_spec=args.propose_spec,
            no_save=args.no_save,
        )
    except Exception as e:
        _log.event("WARN", "extractor_unhandled_exception", error=str(e),  # type: ignore[attr-defined]
                   tb=traceback.format_exc())
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
