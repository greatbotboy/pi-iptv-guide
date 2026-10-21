#!/usr/bin/env python3
"""Search iptv-org channel metadata using the local market config."""

import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from playlist import load_config  # noqa: E402

cfg = load_config()
needles = [str(m).lower() for m in (cfg.get("local_markets") or [])]
needles += [str(x).lower() for x in (cfg.get("local_feed_ids") or [])]
needles += [str(x).lower() for x in (cfg.get("local_channel_ids") or [])]
if not needles:
    print("No local market config. Add app/config.local.yaml.")
    raise SystemExit(1)

chs = httpx.get(
    "https://iptv-org.github.io/api/channels.json",
    follow_redirects=True,
    timeout=60,
).json()
print("count", len(chs))
print("sample keys", chs[0].keys() if chs else None)
print("sample", json.dumps(chs[0], indent=2)[:500])

print("\nSearch name/network/owners:")
found = 0
for ch in chs:
    blob = json.dumps(ch).lower()
    if ch.get("country") not in ("US", "USA", "United States") and ch.get("country") != "US":
        # country might be ISO
        pass
    if any(n in blob for n in needles):
        print(ch.get("id"), ch.get("name"), ch.get("country"), ch.get("city"), ch.get("broadcast_area"))
        found += 1
        if found >= 40:
            break
print("found", found)

# country value samples
countries = {}
for ch in chs:
    c = ch.get("country")
    countries[c] = countries.get(c, 0) + 1
print("top countries", sorted(countries.items(), key=lambda x: -x[1])[:10])

# US channels with city set
us_with_city = [c for c in chs if c.get("country") == "US" and c.get("city")]
print("US with city", len(us_with_city))
print("city samples", [c.get("city") for c in us_with_city[:20]])

# streams json?
streams = httpx.get(
    "https://iptv-org.github.io/api/streams.json",
    follow_redirects=True,
    timeout=60,
).json()
print("streams", len(streams), "keys", streams[0].keys() if streams else None)
