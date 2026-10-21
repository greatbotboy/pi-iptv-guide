#!/usr/bin/env python3
"""List iptv-org feeds whose broadcast area matches the local market config."""

import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from playlist import load_config  # noqa: E402

cfg = load_config()
terms = [str(m).lower() for m in (cfg.get("local_markets") or [])]
terms += [str(x).lower() for x in (cfg.get("local_feed_ids") or [])]
if not terms:
    print("No local market config. Add app/config.local.yaml.")
    raise SystemExit(1)

feeds = httpx.get(
    "https://iptv-org.github.io/api/feeds.json",
    follow_redirects=True,
    timeout=60,
).json()
streams = httpx.get(
    "https://iptv-org.github.io/api/streams.json",
    follow_redirects=True,
    timeout=60,
).json()
stream_channels = {s.get("channel") for s in streams if s.get("channel")}

# Collect area tokens
interesting = []
for f in feeds:
    areas = f.get("broadcast_area") or []
    joined = " ".join(str(a) for a in areas)
    low = joined.lower()
    if any(t in low for t in terms):
        interesting.append((f.get("channel"), f.get("id"), areas, f.get("channel") in stream_channels))

print("interesting feeds", len(interesting))
for row in interesting[:50]:
    print(row)

# dump unique area strings that matched the configured markets
tokens = set()
for f in feeds:
    for a in f.get("broadcast_area") or []:
        s = str(a)
        if any(t in s.lower() for t in terms):
            tokens.add(s)
print("\nmatching area tokens:")
for t in sorted(tokens)[:80]:
    print(" ", t)
