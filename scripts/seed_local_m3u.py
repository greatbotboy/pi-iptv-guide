#!/usr/bin/env python3
"""Seed data/local.m3u with local-market streams from iptv-org APIs."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from playlist import _build_local_from_api, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "local.m3u"


def main() -> None:
    cfg = load_config()
    channels = _build_local_from_api(cfg)
    lines = [
        "#EXTM3U",
        "# Auto-seeded local-market streams from iptv-org.",
        "# Add more stations below as needed.",
        "",
    ]
    for ch in channels:
        logo = ch.get("logo") or ""
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch.get("tvg_id","")}" tvg-logo="{logo}" '
            f'group-title="Local",{ch["name"]}'
        )
        lines.append(ch["url"])
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(channels)} channels to {OUT}")


if __name__ == "__main__":
    main()
