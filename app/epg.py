"""Fetch, filter, and cache XMLTV EPG data for playlist channels."""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, iterparse

import httpx

from playlist import load_config

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
EPG_CACHE_PATH = DATA_DIR / "epg.json"

XMLTV_TIME_RE = re.compile(
    r"^(\d{14})(?:\s*([+-]\d{4}))?$"
)


def parse_xmltv_time(value: str) -> float | None:
    """Parse XMLTV time (YYYYMMDDHHMMSS [±HHMM]) to Unix timestamp."""
    if not value:
        return None
    m = XMLTV_TIME_RE.match(value.strip())
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y%m%d%H%M%S")
    offset = m.group(2)
    if offset:
        sign = 1 if offset[0] == "+" else -1
        hours = int(offset[1:3])
        mins = int(offset[3:5])
        # Convert local wall time + offset into UTC
        total = hours * 3600 + mins * 60
        epoch = (dt.replace(tzinfo=timezone.utc).timestamp()) - sign * total
        return epoch
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _text(el: Element | None, default: str = "") -> str:
    if el is None or el.text is None:
        return default
    return el.text.strip()


def _programme_from_elem(elem: Element) -> dict[str, Any] | None:
    channel = elem.get("channel") or ""
    start = parse_xmltv_time(elem.get("start") or "")
    stop = parse_xmltv_time(elem.get("stop") or "")
    if not channel or start is None or stop is None or stop <= start:
        return None
    title = _text(elem.find("title"), "Untitled")
    desc = _text(elem.find("desc"))
    out: dict[str, Any] = {
        "title": title,
        "start": int(start),
        "stop": int(stop),
    }
    if desc:
        out["desc"] = desc
    return out


def normalize_channel_id(cid: str) -> str:
    """Strip feed/quality suffixes (@SD, @HD, @East, …) for EPG matching.

    Playlist tvg-ids often look like ``FoxNewsChannel.us@SD`` while XMLTV
    channel ids are the base form ``FoxNewsChannel.us``.
    """
    if not cid:
        return ""
    return cid.split("@", 1)[0].strip()


def _epg_lookup_key(cid: str) -> str:
    return normalize_channel_id(cid).lower()


def _id_stem(cid: str) -> str:
    """Channel name stem without country/platform suffix.

    ``CBSNewsChicago.us@SD`` / ``CBSNewsChicago.pluto`` → ``cbsnewschicago``.
    """
    base = normalize_channel_id(cid)
    if not base:
        return ""
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base.lower()


# Known playlist stem → XMLTV stem when names differ
STEM_ALIASES: dict[str, str] = {
    "blazelive": "blazetv",
    "lacrossetv": "lacrossechannel",
    "thefirsttv": "thefirst",
    "cbsnewsphilly": "cbsnewsphiladelphia",
    "cbsnews247": "cbsnews24/7",
    "news12plushudsonvalley": "news12hudsonvalley",
    "news12plusnewyork": "news12newyork",
    "news12plusconnecticut": "news12connecticut",
    "news12pluslongisland": "news12longisland",
    "news12plusnewjersey": "news12newjersey",
    "redbulltv": "redbulltv",
    # iptv-org uses BigTenNetwork.us; iptv-epg lists BigTen.us
    "bigtennetwork": "bigten",
}


def _register_alias(alias_map: dict[str, list[str]], key: str, cid: str) -> None:
    if not key:
        return
    bucket = alias_map.setdefault(key, [])
    if cid not in bucket:
        bucket.append(cid)


def _build_alias_map(channel_ids: set[str]) -> dict[str, list[str]]:
    """Map XMLTV channel keys (full id + stem) → playlist tvg_ids."""
    alias_map: dict[str, list[str]] = {}
    for cid in channel_ids:
        _register_alias(alias_map, _epg_lookup_key(cid), cid)
        stem = _id_stem(cid)
        _register_alias(alias_map, stem, cid)
        alt = STEM_ALIASES.get(stem)
        if alt:
            _register_alias(alias_map, alt, cid)
            # Also allow alt with common TLDs for full-id lookups
            for tld in (".us", ".pluto", ".peacocktv"):
                _register_alias(alias_map, alt + tld, cid)
    return alias_map


def _targets_for_epg_channel(
    channel: str,
    alias_map: dict[str, list[str]],
) -> list[str] | None:
    targets = alias_map.get(_epg_lookup_key(channel))
    if targets:
        return targets
    return alias_map.get(_id_stem(channel))


def fetch_and_filter_epg(
    url: str,
    channel_ids: set[str],
    timeout: float = 180.0,
) -> dict[str, list[dict[str, Any]]]:
    """Download XMLTV (optionally .gz) and keep programmes for channel_ids only."""
    if not channel_ids:
        return {}

    alias_map = _build_alias_map(channel_ids)

    log.info(
        "Fetching EPG %s (%d channel ids, %d alias keys)",
        url,
        len(channel_ids),
        len(alias_map),
    )
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            raw = b"".join(resp.iter_bytes())

    if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
        stream: Any = gzip.GzipFile(fileobj=io.BytesIO(raw))
    else:
        stream = io.BytesIO(raw)

    by_channel: dict[str, list[dict[str, Any]]] = {cid: [] for cid in channel_ids}
    kept = 0

    for event, elem in iterparse(stream, events=("end",)):
        if elem.tag != "programme":
            continue
        channel = elem.get("channel") or ""
        targets = _targets_for_epg_channel(channel, alias_map)
        if targets:
            prog = _programme_from_elem(elem)
            if prog:
                for tid in targets:
                    by_channel[tid].append(prog)
                kept += 1
        elem.clear()

    # Drop empty keys and sort programmes
    result: dict[str, list[dict[str, Any]]] = {}
    for cid, progs in by_channel.items():
        if not progs:
            continue
        progs.sort(key=lambda p: p["start"])
        result[cid] = progs

    log.info("EPG filtered: %d programmes across %d channels", kept, len(result))
    return result


def save_epg_cache(programs: dict[str, list[dict[str, Any]]]) -> float:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    updated_at = time.time()
    payload = {"updated_at": updated_at, "programs": programs}
    EPG_CACHE_PATH.write_text(json.dumps(payload), encoding="utf-8")
    return updated_at


def load_epg_cache() -> tuple[dict[str, list[dict[str, Any]]], float | None]:
    if not EPG_CACHE_PATH.exists():
        return {}, None
    try:
        data = json.loads(EPG_CACHE_PATH.read_text(encoding="utf-8"))
        programs = data.get("programs") or {}
        if not isinstance(programs, dict):
            return {}, None
        return programs, data.get("updated_at")
    except Exception as e:
        log.warning("Bad EPG cache: %s", e)
        return {}, None


def clip_programs(
    programs: list[dict[str, Any]],
    window_start: float,
    window_end: float,
) -> list[dict[str, Any]]:
    """Return programmes overlapping [window_start, window_end), clipped to the window."""
    out: list[dict[str, Any]] = []
    for p in programs:
        start = float(p["start"])
        stop = float(p["stop"])
        if stop <= window_start or start >= window_end:
            continue
        clipped = {
            "title": p.get("title") or "Untitled",
            "start": int(max(start, window_start)),
            "stop": int(min(stop, window_end)),
        }
        if p.get("desc"):
            clipped["desc"] = p["desc"]
        # Keep original times for info panel when useful
        clipped["orig_start"] = int(start)
        clipped["orig_stop"] = int(stop)
        out.append(clipped)
    return out


def _gap_cell(
    start: float,
    stop: float,
    title: str = "No listings",
    desc: str = "",
) -> dict[str, Any]:
    cell: dict[str, Any] = {
        "title": title,
        "start": int(start),
        "stop": int(stop),
        "orig_start": int(start),
        "orig_stop": int(stop),
        "live": True,
    }
    if desc:
        cell["desc"] = desc
    return cell


def fill_program_gaps(
    clipped: list[dict[str, Any]],
    window_start: float,
    window_end: float,
) -> list[dict[str, Any]]:
    """Ensure the guide row is fully covered — grey cells for holes in the schedule."""
    if not clipped:
        return [_gap_cell(window_start, window_end)]
    filled: list[dict[str, Any]] = []
    cursor = float(window_start)
    for p in sorted(clipped, key=lambda x: x["start"]):
        start = float(p["start"])
        stop = float(p["stop"])
        if start > cursor + 1:
            filled.append(_gap_cell(cursor, start))
        filled.append(p)
        cursor = max(cursor, stop)
    if cursor < window_end - 1:
        filled.append(_gap_cell(cursor, window_end))
    return filled


def live_placeholder(window_start: float, window_end: float) -> list[dict[str, Any]]:
    return [_gap_cell(window_start, window_end)]


def schedule_for_window(
    programs: list[dict[str, Any]],
    window_start: float,
    window_end: float,
) -> list[dict[str, Any]]:
    """Clip to the guide window, fill holes, or show next/prev when nothing overlaps."""
    if not programs:
        return live_placeholder(window_start, window_end)

    clipped = clip_programs(programs, window_start, window_end)
    if clipped:
        return fill_program_gaps(clipped, window_start, window_end)

    # Schedule exists but nothing falls in this window (common for sparse Pluto guides)
    upcoming = [p for p in programs if float(p["start"]) >= window_start]
    previous = [p for p in programs if float(p["stop"]) <= window_start]
    if upcoming:
        nxt = min(upcoming, key=lambda p: float(p["start"]))
        when = int(nxt["start"])
        title = f"Next: {nxt.get('title') or 'Untitled'}"
        desc = nxt.get("desc") or ""
        return [
            _gap_cell(
                window_start,
                window_end,
                title=title,
                desc=desc,
            )
            | {"orig_start": when, "orig_stop": int(nxt["stop"])}
        ]
    if previous:
        prev = max(previous, key=lambda p: float(p["stop"]))
        return [
            _gap_cell(
                window_start,
                window_end,
                title=f"Was: {prev.get('title') or 'Untitled'}",
                desc=prev.get("desc") or "",
            )
            | {
                "orig_start": int(prev["start"]),
                "orig_stop": int(prev["stop"]),
            }
        ]
    return live_placeholder(window_start, window_end)


CATEGORY_BASE = {
    "local": 100,
    "news": 200,
    "sports": 300,
    "entertainment": 400,
    "general": 600,
    "movies": 1000,
}


def assign_dial_numbers(channels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign stable virtual dial numbers by category ranges."""
    counters = {cat: 0 for cat in (*CATEGORY_BASE, "other")}
    out: list[dict[str, Any]] = []
    for ch in channels:
        cat = ch.get("category") or "other"
        if cat not in CATEGORY_BASE:
            cat = "other"
        base = CATEGORY_BASE.get(cat, 1200)
        counters[cat] = counters.get(cat, 0) + 1
        numbered = {**ch, "number": base + counters[cat]}
        out.append(numbered)
    return out


class EpgStore:
    def __init__(self) -> None:
        self.config = load_config()
        self.programs: dict[str, list[dict[str, Any]]] = {}
        self.updated_at: float | None = None

    def ensure_loaded(self, channel_ids: set[str] | None = None) -> None:
        cached, ts = load_epg_cache()
        refresh_h = float(self.config.get("epg_refresh_hours", self.config.get("refresh_hours", 6)))
        stale = ts is None or (time.time() - ts) > refresh_h * 3600
        if cached and not stale:
            self.programs = cached
            self.updated_at = ts
            return
        if cached:
            self.programs = cached
            self.updated_at = ts
        if channel_ids is None:
            return
        try:
            self.refresh(channel_ids)
        except Exception as e:
            log.error("EPG refresh failed, using cache: %s", e)

    def refresh(self, channel_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
        self.config = load_config()
        url = self.config.get(
            "epg_url",
            "https://iptv-epg.org/files/epg-us.xml.gz",
        )
        programs = fetch_and_filter_epg(url, channel_ids)
        self.updated_at = save_epg_cache(programs)
        self.programs = programs
        return programs

    def _raw_programs(self, tvg_id: str) -> list[dict[str, Any]]:
        if not tvg_id:
            return []
        raw = self.programs.get(tvg_id) or []
        if raw:
            return raw
        # Fallback: match by base id (handles older caches / alias drift)
        key = _epg_lookup_key(tvg_id)
        if key:
            for cid, progs in self.programs.items():
                if _epg_lookup_key(cid) == key and progs:
                    return progs
        return []

    def has_guide(self, tvg_id: str) -> bool:
        """True when at least one programme is cached for this channel id."""
        return bool(self._raw_programs(tvg_id))

    @property
    def ready(self) -> bool:
        """True once any programme schedules are loaded (required before hiding)."""
        return bool(self.programs)

    def programs_for(
        self,
        tvg_id: str,
        window_start: float,
        window_end: float,
    ) -> list[dict[str, Any]]:
        if not tvg_id:
            return live_placeholder(window_start, window_end)
        return schedule_for_window(
            self._raw_programs(tvg_id), window_start, window_end
        )
