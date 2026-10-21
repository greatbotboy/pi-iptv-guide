"""IPTV channel guide API and static UI."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from epg import EpgStore, assign_dial_numbers
from player import VlcPlayer
from playlist import PlaylistStore, load_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("iptv")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

store = PlaylistStore()
epg = EpgStore()
config = load_config()
_alsa = config.get("alsa_device") or None
player = VlcPlayer(
    display=str(config.get("display", ":0")),
    alsa_device=str(_alsa) if _alsa else None,
    network_cache_ms=int(config.get("network_cache_ms") or 8000),
    hls_live_start_index=int(config.get("hls_live_start_index") or -6),
    stream_buffer=bool(config.get("stream_buffer", True)),
    stream_buffer_segments=int(config.get("stream_buffer_segments") or 5),
    stream_buffer_dir=str(config.get("stream_buffer_dir") or "/dev/shm/iptv-guide"),
    max_display_fps=int(config.get("max_display_fps", 30)),
    audio_delay_sec=float(config.get("audio_delay_sec") or 0),
    hdmi_mode=str(config.get("hdmi_mode") or ""),
)


def _channel_ids() -> set[str]:
    return {c["tvg_id"] for c in store.channels if c.get("tvg_id")}


def _hide_unguided() -> bool:
    return bool(store.config.get("hide_unguided", True))


def _guided_channels(
    q: str | None = None,
    category: str | None = None,
) -> list[dict[str, Any]]:
    """List channels, optionally dropping those with no EPG schedule."""
    channels = store.list(q=q, category=category)
    if _hide_unguided() and epg.ready:
        channels = [c for c in channels if epg.has_guide(c.get("tvg_id") or "")]
    return assign_dial_numbers(channels)


def _channel_payload(c: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": c["name"],
        "url": c["url"],
        "logo": c.get("logo") or "",
        "category": c.get("category"),
        "tags": c.get("tags") or [],
        "group": c.get("group") or "",
        "tvg_id": c.get("tvg_id") or "",
        "number": c.get("number"),
    }


def _half_hour_floor(ts: float) -> float:
    return ts - (ts % 1800)


def _load_epg_background() -> None:
    try:
        log.info("Loading EPG in background…")
        epg.ensure_loaded(_channel_ids())
        log.info("EPG ready (%d channels with schedules)", len(epg.programs))
    except Exception as e:
        log.error("Background EPG load failed: %s", e)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("Loading channel playlist…")
    store.ensure_loaded()
    log.info("%d channels ready", len(store.channels))
    # EPG download/parse can take minutes — do not block the API/play endpoints
    threading.Thread(target=_load_epg_background, name="epg-load", daemon=True).start()
    yield


app = FastAPI(title="Pi IPTV Guide", lifespan=lifespan)


class PlayRequest(BaseModel):
    url: str = Field(..., min_length=1)
    name: str | None = None


@app.get("/api/channels")
def api_channels(
    q: str | None = Query(None),
    category: str | None = Query(None),
) -> dict[str, Any]:
    channels = _guided_channels(q=q, category=category)
    return {
        "updated_at": store.updated_at,
        "count": len(channels),
        "channels": [_channel_payload(c) for c in channels],
    }


@app.get("/api/guide")
def api_guide(
    q: str | None = Query(None),
    category: str | None = Query(None),
    from_ts: float | None = Query(None, alias="from"),
    hours: float = Query(4, ge=1, le=24),
) -> dict[str, Any]:
    now = time.time()
    window_start = _half_hour_floor(from_ts if from_ts is not None else now)
    window_end = window_start + hours * 3600

    channels = _guided_channels(q=q, category=category)
    rows: list[dict[str, Any]] = []
    for c in channels:
        tvg_id = c.get("tvg_id") or ""
        programs = epg.programs_for(tvg_id, window_start, window_end)
        rows.append({**_channel_payload(c), "programs": programs})

    return {
        "now": now,
        "window_start": window_start,
        "window_end": window_end,
        "hours": hours,
        "updated_at": store.updated_at,
        "epg_updated_at": epg.updated_at,
        "count": len(rows),
        "channels": rows,
    }


def _catalog_urls() -> set[str]:
    return {c["url"] for c in store.channels if c.get("url")}


@app.post("/api/play")
def api_play(body: PlayRequest) -> dict[str, Any]:
    # Playback is an unauthenticated LAN control. Only start URLs already
    # in the loaded catalog so this process cannot be pointed at arbitrary hosts.
    url = body.url.strip()
    if url not in _catalog_urls():
        return {"ok": False, "error": "Stream is not in the catalog"}
    state = player.play(url, name=body.name)
    return {
        "ok": state.running and not state.error,
        "running": state.running,
        "name": state.name,
        "url": state.url,
        "pid": state.pid,
        "error": state.error,
        "backend": state.backend,
    }


@app.post("/api/stop")
def api_stop() -> dict[str, Any]:
    state = player.stop()
    return {"ok": True, "running": state.running}


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    state = player.status()
    out = player.output_info()
    return {
        "running": state.running,
        "name": state.name,
        "url": state.url,
        "pid": state.pid,
        "started_at": state.started_at,
        "error": state.error,
        "backend": state.backend,
        "output_kind": out["output_kind"],
        "output_detail": out["output_detail"],
        "display": out["display"],
        "alsa_device": out.get("alsa_device"),
        "tagline": str(store.config.get("tagline") or ""),
        "channel_count": len(store.channels),
        "playlist_updated_at": store.updated_at,
        "epg_updated_at": epg.updated_at,
        "epg_channel_count": len(epg.programs),
    }


@app.post("/api/refresh")
def api_refresh() -> dict[str, Any]:
    channels = store.refresh()
    try:
        epg.refresh(_channel_ids())
        epg_ok = True
        epg_error = None
    except Exception as e:
        log.error("EPG refresh failed: %s", e)
        epg_ok = False
        epg_error = str(e)
    return {
        "ok": True,
        "count": len(channels),
        "updated_at": store.updated_at,
        "epg_ok": epg_ok,
        "epg_updated_at": epg.updated_at,
        "epg_channel_count": len(epg.programs),
        "epg_error": epg_error,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "main:app",
        host=cfg.get("host", "0.0.0.0"),
        port=int(cfg.get("port", 8080)),
        reload=False,
    )
