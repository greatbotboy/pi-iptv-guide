"""Fetch, parse, and cache IPTV channel lists."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_PATH = DATA_DIR / "channels.json"
LOCAL_M3U = DATA_DIR / "local.m3u"
CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

EXTINF_RE = re.compile(
    r"#EXTINF:-?\d+\s*(?P<attrs>[^,]*)\s*,\s*(?P<name>.*)",
    re.IGNORECASE,
)
ATTR_RE = re.compile(r'([\w-]+)="([^"]*)"')


def load_config() -> dict[str, Any]:
    """Load config.yaml, then overlay gitignored config.local.yaml when present."""
    with CONFIG_PATH.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"{CONFIG_PATH} must be a mapping")
    local_path = CONFIG_PATH.with_name("config.local.yaml")
    if local_path.is_file():
        with local_path.open(encoding="utf-8") as f:
            overlay = yaml.safe_load(f) or {}
        if not isinstance(overlay, dict):
            raise ValueError(f"{local_path} must be a mapping")
        cfg.update(overlay)
    return cfg


def _parse_attrs(attr_str: str) -> dict[str, str]:
    return {k.lower(): v for k, v in ATTR_RE.findall(attr_str or "")}


def parse_m3u(text: str, source: str = "remote") -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            m = EXTINF_RE.match(line)
            if m:
                attrs = _parse_attrs(m.group("attrs"))
                name = m.group("name").strip()
                url = ""
                j = i + 1
                while j < len(lines):
                    nxt = lines[j].strip()
                    if nxt and not nxt.startswith("#"):
                        url = nxt
                        break
                    j += 1
                if url:
                    channels.append(
                        {
                            "name": name,
                            "url": url,
                            "logo": attrs.get("tvg-logo", ""),
                            "group": attrs.get("group-title", ""),
                            "tvg_id": attrs.get("tvg-id", ""),
                            "source": source,
                        }
                    )
                i = j
        i += 1
    return channels


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _is_category(group: str, categories: list[str]) -> str | None:
    g = _normalize(group)
    parts = [p.strip() for p in re.split(r"[;|/]", g) if p.strip()]
    for cat in categories:
        cl = cat.lower()
        if g == cl or cl in parts:
            return cl
    return None


def _fetch_text(url: str, timeout: float = 90.0) -> str:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.text


def _fetch_json(url: str, timeout: float = 90.0) -> Any:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.json()


def _matches_market(blob: str, markets: list[str]) -> bool:
    b = _normalize(blob)
    for market in markets:
        if _normalize(market) in b:
            return True
    return False


def _logo_from_channel(meta: dict[str, Any] | None) -> str:
    if not meta:
        return ""
    # iptv-org channels.json has no logo; logos come from m3u when available
    return ""


def _build_local_from_api(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Build local channels from iptv-org streams + feeds + channels metadata."""
    streams_url = cfg.get("streams_api_url")
    feeds_url = cfg.get("feeds_api_url")
    channels_url = cfg.get("channels_api_url")
    markets = cfg.get("local_markets") or []
    areas = set(cfg.get("local_broadcast_areas") or [])
    feed_ids = {str(x).upper() for x in (cfg.get("local_feed_ids") or [])}
    channel_ids = set(cfg.get("local_channel_ids") or [])

    if not streams_url:
        return []

    try:
        streams = _fetch_json(streams_url)
    except Exception as e:
        log.warning("streams API failed: %s", e)
        return []

    feeds_by_channel: dict[str, list[dict[str, Any]]] = {}
    if feeds_url:
        try:
            for feed in _fetch_json(feeds_url):
                cid = feed.get("channel")
                if cid:
                    feeds_by_channel.setdefault(cid, []).append(feed)
        except Exception as e:
            log.warning("feeds API failed: %s", e)

    channel_meta: dict[str, dict[str, Any]] = {}
    if channels_url:
        try:
            for ch in _fetch_json(channels_url):
                if ch.get("id"):
                    channel_meta[ch["id"]] = ch
        except Exception as e:
            log.warning("channels API failed: %s", e)

    out: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for stream in streams:
        cid = stream.get("channel") or ""
        url = stream.get("url") or ""
        feed_key = (stream.get("feed") or "").upper()
        if not url or url in seen_urls:
            continue

        meta = channel_meta.get(cid) or {}
        if meta.get("country") and meta.get("country") != "US":
            continue

        name_bits = [
            cid,
            meta.get("name") or "",
            " ".join(meta.get("alt_names") or []),
            stream.get("title") or "",
            feed_key,
        ]
        blob = " ".join(name_bits)

        # Only the stream's own feed id counts (do not inherit sibling feeds)
        feed_hit = bool(feed_key) and feed_key in feed_ids

        area_hit = False
        if areas and feed_key:
            for feed in feeds_by_channel.get(cid, []):
                if (feed.get("id") or "").upper() != feed_key:
                    continue
                for area in feed.get("broadcast_area") or []:
                    if str(area) in areas:
                        area_hit = True
                        break

        market_hit = _matches_market(blob, markets)
        id_hit = cid in channel_ids
        if not feed_hit and not area_hit and not market_hit and not id_hit:
            continue

        display_name = meta.get("name") or stream.get("title") or cid
        if stream.get("title") and meta.get("name") and stream.get("title") != meta.get("name"):
            display_name = f"{meta.get('name')} ({stream.get('title')})"
        if feed_hit and feed_key and feed_key not in display_name.upper():
            display_name = f"{display_name} [{feed_key}]"

        out.append(
            {
                "name": display_name,
                "url": url,
                "logo": _logo_from_channel(meta),
                "group": "Local",
                "tvg_id": cid,
                "source": "api-local",
                "category": "local",
                "national_category": None,
                "tags": ["local"],
            }
        )
        seen_urls.add(url)

    log.info("API local channels: %d", len(out))
    return out


def _classify_remote(
    raw: list[dict[str, Any]],
    categories: list[str],
    markets: list[str],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for ch in raw:
        url = ch["url"]
        if url in seen_urls:
            continue

        cat = _is_category(ch.get("group", ""), categories)
        blob = f"{ch.get('name','')} {ch.get('tvg_id','')} {ch.get('group','')}"
        is_local = _matches_market(blob, markets)

        if not cat and not is_local:
            continue

        category = "local" if is_local else (cat or "other")
        tags: list[str] = []
        if is_local:
            tags.append("local")
        if cat:
            tags.append(cat)

        out.append({**ch, "category": category, "national_category": cat, "tags": tags})
        seen_urls.add(url)

    return out


def _parse_local_file() -> list[dict[str, Any]]:
    if not LOCAL_M3U.exists():
        return []
    text = LOCAL_M3U.read_text(encoding="utf-8", errors="replace")
    channels = parse_m3u(text, source="local")
    for ch in channels:
        ch["category"] = "local"
        ch["national_category"] = None
        ch["tags"] = ["local"]
        g = _normalize(ch.get("group", ""))
        if g in ("news", "sports", "entertainment"):
            ch["tags"].append(g)
    return channels


def build_channels(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cfg = config or load_config()
    categories = [c.lower() for c in cfg.get("categories", ["news", "sports"])]
    markets = cfg.get("local_markets", [])
    playlist_url = cfg["playlist_url"]

    log.info("Fetching playlist %s", playlist_url)
    m3u_text = _fetch_text(playlist_url)
    raw = parse_m3u(m3u_text, source="remote")
    remote = _classify_remote(raw, categories, markets)

    api_local = _build_local_from_api(cfg)
    file_local = _parse_local_file()

    # Merge: local.m3u first, then API locals, then remote news/sports/entertainment
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_ids: set[str] = set()
    seen_local_names: set[str] = set()

    def _base_name(name: str) -> str:
        # Strip trailing quality / parenthetical variants for local dedupe
        n = _normalize(name)
        n = re.sub(r"\s*\(\d+p\)\s*$", "", n)
        n = re.sub(r"\s*\[[^\]]+\]\s*$", "", n)
        return n.strip()

    for group in (file_local, api_local, remote):
        for ch in group:
            url = ch["url"]
            tid = ch.get("tvg_id") or ""
            if url in seen_urls:
                continue
            if tid and tid in seen_ids:
                continue
            if ch.get("category") == "local":
                bn = _base_name(ch.get("name", ""))
                if bn and bn in seen_local_names:
                    continue
                if bn:
                    seen_local_names.add(bn)
            seen_urls.add(url)
            if tid:
                seen_ids.add(tid)
            merged.append(ch)

    def sort_key(c: dict[str, Any]) -> tuple:
        cat_order = {
            "local": 0,
            "news": 1,
            "sports": 2,
            "entertainment": 3,
        }.get(c.get("category", ""), 9)
        return (cat_order, _normalize(c.get("name", "")))

    merged.sort(key=sort_key)
    return merged


def save_cache(channels: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": time.time(), "channels": channels}
    CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_cache() -> tuple[list[dict[str, Any]], float | None]:
    if not CACHE_PATH.exists():
        return [], None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data.get("channels", []), data.get("updated_at")
    except Exception as e:
        log.warning("Bad cache: %s", e)
        return [], None


class PlaylistStore:
    def __init__(self) -> None:
        self.config = load_config()
        self.channels: list[dict[str, Any]] = []
        self.updated_at: float | None = None

    def ensure_loaded(self) -> None:
        cached, ts = load_cache()
        refresh_h = float(self.config.get("refresh_hours", 6))
        stale = ts is None or (time.time() - ts) > refresh_h * 3600
        if cached and not stale:
            self.channels = cached
            self.updated_at = ts
            return
        if cached:
            self.channels = cached
            self.updated_at = ts
        try:
            self.refresh()
        except Exception as e:
            log.error("Refresh failed, using cache: %s", e)
            if not self.channels and cached:
                self.channels = cached
                self.updated_at = ts

    def refresh(self) -> list[dict[str, Any]]:
        self.config = load_config()
        channels = build_channels(self.config)
        save_cache(channels)
        self.channels = channels
        self.updated_at = time.time()
        log.info("Cached %d channels", len(channels))
        return channels

    def list(
        self,
        q: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self.channels
        if category and category.lower() not in ("", "all"):
            cat = category.lower()
            items = [
                c
                for c in items
                if c.get("category") == cat or cat in (c.get("tags") or [])
            ]
        if q:
            needle = _normalize(q)
            items = [
                c
                for c in items
                if needle in _normalize(c.get("name", ""))
                or needle in _normalize(c.get("group", ""))
            ]
        return items
