#!/usr/bin/env python3
"""Search iptv-org for channels matching local_markets from config."""

import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from playlist import load_config  # noqa: E402

cfg = load_config()
markets = [str(m).lower() for m in (cfg.get("local_markets") or [])]
if not markets:
    print("No local_markets configured. Add them in app/config.local.yaml.")
    raise SystemExit(1)

text = httpx.get(
    "https://iptv-org.github.io/iptv/countries/us.m3u",
    follow_redirects=True,
    timeout=60,
).text
ids = set()
names = []
for line in text.splitlines():
    if line.startswith("#EXTINF"):
        m = re.search(r'tvg-id="([^"]*)"', line)
        n = line.split(",", 1)[-1].strip()
        tid = m.group(1) if m else ""
        if tid:
            ids.add(tid)
        names.append((tid, n, line))

print("M3U name hits:")
for tid, n, line in names:
    blob = (tid + " " + n + " " + line).lower()
    if any(m in blob for m in markets):
        print(" ", tid, "|", n)

chs = httpx.get(
    "https://iptv-org.github.io/api/channels.json",
    follow_redirects=True,
    timeout=60,
).json()
print("\nAPI US city hits:")
n = 0
stream_hits = 0
for ch in chs:
    if ch.get("country") != "US":
        continue
    city = (ch.get("city") or "").lower()
    if not any(m in city for m in markets):
        continue
    in_m3u = ch.get("id") in ids
    print(" ", ch.get("id"), ch.get("name"), "|", ch.get("city"), "in_m3u", in_m3u)
    if in_m3u:
        stream_hits += 1
    n += 1
    if n >= 60:
        break
print("shown", n, "stream_hits_in_sample", stream_hits)
