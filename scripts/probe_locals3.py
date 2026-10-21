#!/usr/bin/env python3
"""Match iptv-org channels and streams against the local market config."""

import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from playlist import load_config  # noqa: E402

cfg = load_config()
terms = [str(m) for m in (cfg.get("local_markets") or [])]
terms += [str(x) for x in (cfg.get("local_feed_ids") or [])]
terms += [str(x) for x in (cfg.get("local_channel_ids") or [])]
if not terms:
    print("No local market config. Add app/config.local.yaml.")
    raise SystemExit(1)
patterns = [re.escape(t.lower()) for t in terms]

chs = httpx.get(
    "https://iptv-org.github.io/api/channels.json",
    follow_redirects=True,
    timeout=60,
).json()
streams = httpx.get(
    "https://iptv-org.github.io/api/streams.json",
    follow_redirects=True,
    timeout=60,
).json()
stream_by_channel = {}
for s in streams:
    cid = s.get("channel")
    if cid:
        stream_by_channel.setdefault(cid, []).append(s)

print("Matching US channels:")
matched = []
for ch in chs:
    if ch.get("country") != "US":
        continue
    blob = " ".join(
        [
            ch.get("id") or "",
            ch.get("name") or "",
            " ".join(ch.get("alt_names") or []),
            ch.get("network") or "",
        ]
    ).lower()
    if any(re.search(p, blob) for p in patterns):
        has = ch["id"] in stream_by_channel
        matched.append((ch, has))
        print(
            f"{'STREAM' if has else 'no-stream':8} {ch['id']:40} {ch['name']}"
        )

print("total matched", len(matched), "with streams", sum(1 for _, h in matched if h))

# Also check feeds.json for broadcast_area
try:
    feeds = httpx.get(
        "https://iptv-org.github.io/api/feeds.json",
        follow_redirects=True,
        timeout=60,
    ).json()
    print("\nfeeds sample keys", feeds[0].keys() if feeds else None)
    areas = set()
    for f in feeds:
        ba = f.get("broadcast_area") or []
        for a in ba:
            blob = str(a).lower()
            if "us" in blob and any(t.lower() in blob for t in terms):
                areas.add((f.get("channel"), str(ba)))
    print("interesting areas sample", list(areas)[:30])
except Exception as e:
    print("feeds err", e)

# Print stream URLs for matched with streams
print("\nStream URLs:")
for ch, has in matched:
    if not has:
        continue
    for s in stream_by_channel[ch["id"]][:2]:
        print(ch["name"], "->", s.get("url")[:120])
