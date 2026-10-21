#!/usr/bin/env bash
# Wrapper so bundled shared FFmpeg finds its libraries.
ROOT="$(cd "$(dirname "$0")" && pwd)"
export LD_LIBRARY_PATH="${ROOT}/ffmpeg-lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "${ROOT}/ffmpeg" "$@"
