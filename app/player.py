"""Launch and stop media playback on the Pi display.

Chooses backend from the runtime environment:
- Desktop (X11/Wayland available) → VLC fullscreen with ALSA
- Console / no desktop → ffmpeg → /dev/fb0 + aplay (HDMI audio)

For Starlink / high-jitter links, console playback uses a local HLS rebuffer
in /dev/shm so decode runs from RAM at a steady pace instead of the WAN.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
BUNDLED_FFMPEG = ROOT / "bin" / "ffmpeg"
BUNDLED_FFMPEG_SH = ROOT / "bin" / "ffmpeg.sh"
FFMPEG_LIB = ROOT / "bin" / "ffmpeg-lib"
DEFAULT_BUFFER_DIR = Path("/dev/shm/iptv-guide")

_VLC_NOISE = (
    "vlcpulse",
    "PulseAudio",
    "globalhotkeys",
    "no suitable interface module",
    "dummy interface",
    "interface module",
)


@dataclass
class PlayerState:
    running: bool = False
    name: str | None = None
    url: str | None = None
    pid: int | None = None
    started_at: float | None = None
    error: str | None = None
    backend: str | None = None


@dataclass(frozen=True)
class OutputMode:
    """Detected display environment."""

    kind: str  # "desktop" | "framebuffer" | "none"
    display: str | None = None
    detail: str = ""


def _x11_socket(display: str) -> Path | None:
    # ":0" → /tmp/.X11-unix/X0
    name = display.lstrip(":")
    if not name or not name[0].isdigit():
        return None
    num = name.split(".")[0]
    return Path(f"/tmp/.X11-unix/X{num}")


def _wayland_socket() -> Path | None:
    uid = os.getuid()
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{uid}"))
    name = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    sock = runtime / name
    return sock if sock.exists() else None


def detect_output(display: str = ":0") -> OutputMode:
    """Pick desktop vs framebuffer without assuming a graphical login."""
    xsock = _x11_socket(display)
    if xsock is not None and xsock.exists():
        return OutputMode(kind="desktop", display=display, detail=f"X11 {xsock}")

    wl = _wayland_socket()
    if wl is not None:
        return OutputMode(kind="desktop", display=display, detail=f"Wayland {wl}")

    # Optional live probe — only if xdpyinfo exists and socket was missing
    # (socket check is enough on Raspberry Pi OS)

    fb = Path("/dev/fb0")
    if fb.exists() and os.access(fb, os.W_OK):
        return OutputMode(kind="framebuffer", display=None, detail=str(fb))

    if fb.exists():
        return OutputMode(
            kind="framebuffer",
            display=None,
            detail=f"{fb} exists but not writable by uid {os.getuid()}",
        )

    return OutputMode(kind="none", display=None, detail="no X11/Wayland socket and no /dev/fb0")


def detect_alsa_device(override: str | None = None) -> str:
    """Pick HDMI ALSA device from the connected DRM connector; else headphones."""
    if override:
        return override
    drm = Path("/sys/class/drm")
    if drm.is_dir():
        for status_path in sorted(drm.glob("card*-HDMI-A-*/status")):
            try:
                if status_path.read_text().strip() != "connected":
                    continue
            except OSError:
                continue
            m = re.search(r"HDMI-A-(\d+)$", status_path.parent.name)
            if not m:
                continue
            # HDMI-A-1 → vc4hdmi0, HDMI-A-2 → vc4hdmi1
            idx = int(m.group(1)) - 1
            return f"plughw:CARD=vc4hdmi{idx},DEV=0"
    return "plughw:CARD=Headphones,DEV=0"


def _friendly_vlc_error(stderr: str) -> str:
    text = (stderr or "").strip()
    if not text:
        return "VLC exited immediately — check HDMI display and stream URL"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    useful = [
        ln for ln in lines if not any(n.lower() in ln.lower() for n in _VLC_NOISE)
    ]
    if useful:
        return useful[-1][:200]
    if any("PulseAudio" in ln or "vlcpulse" in ln for ln in lines):
        return "VLC audio failed (no PulseAudio session)"
    return "VLC exited immediately — desktop may not be ready"


def resolve_hls_media_url(url: str, timeout: float = 12.0) -> str:
    """If url is an HLS master playlist, return the lowest-bandwidth media URL."""
    if "m3u8" not in url.lower():
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "iptv-guide/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read(256_000).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("HLS resolve failed for %s: %s", url, e)
        return url

    if "#EXT-X-STREAM-INF" not in text:
        return url

    best_bw: int | None = None
    best_uri: str | None = None
    pending_bw: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF"):
            m = re.search(r"BANDWIDTH=(\d+)", line, re.I)
            pending_bw = int(m.group(1)) if m else 10**12
            continue
        if line.startswith("#"):
            continue
        if pending_bw is None:
            continue
        if best_bw is None or pending_bw < best_bw:
            best_bw = pending_bw
            best_uri = line
        pending_bw = None

    if not best_uri:
        return url
    resolved = urljoin(url, best_uri)
    log.info("HLS master → media (%s bps): %s", best_bw, resolved)
    return resolved


@dataclass
class VlcPlayer:
    display: str = ":0"
    alsa_device: str | None = None
    # Starlink / high-jitter links need multi-second demuxer buffers.
    network_cache_ms: int = 8000
    hls_live_start_index: int = -6
    # Local RAM rebuffer before decode (console/ffmpeg path).
    stream_buffer: bool = True
    stream_buffer_segments: int = 5
    stream_buffer_dir: str = str(DEFAULT_BUFFER_DIR)
    max_display_fps: int = 30
    # Positive = hold audio so lips match a late picture (seconds).
    audio_delay_sec: float = 0.0
    # DRM mode for HDMI playback, e.g. "1920x1080@60". Empty keeps the TV's preferred mode.
    hdmi_mode: str = ""
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _feeder: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _aplay: subprocess.Popen | None = field(default=None, init=False, repr=False)
    state: PlayerState = field(default_factory=PlayerState)
    _last_mode: OutputMode | None = field(default=None, init=False, repr=False)
    _last_alsa: str | None = field(default=None, init=False, repr=False)
    _play_url: str | None = field(default=None, init=False, repr=False)

    def detect(self) -> OutputMode:
        mode = detect_output(self.display)
        self._last_mode = mode
        return mode

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        env.setdefault("WAYLAND_DISPLAY", "wayland-0")
        uid = os.getuid()
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")

        if "XAUTHORITY" not in env:
            for candidate in (
                Path.home() / ".Xauthority",
                Path(f"/run/user/{uid}/.Xauthority"),
                Path(f"/run/user/{uid}/gdm/Xauthority"),
                Path(f"/run/user/{uid}/lightdm/Xauthority"),
            ):
                if candidate.is_file():
                    env["XAUTHORITY"] = str(candidate)
                    break

        env.setdefault("ALSA_CARD", "0")

        if FFMPEG_LIB.is_dir():
            lib = str(FFMPEG_LIB)
            prev = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = f"{lib}:{prev}" if prev else lib
        return env

    def _find_cvlc(self) -> str | None:
        return shutil.which("cvlc") or shutil.which("vlc")

    def _find_mpv(self) -> str | None:
        return shutil.which("mpv")

    def _drm_connector(self) -> str | None:
        """Pick a connected HDMI DRM connector (e.g. HDMI-A-1)."""
        drm = Path("/sys/class/drm")
        if not drm.is_dir():
            return None
        connected: list[str] = []
        for status_path in sorted(drm.glob("card*-*/status")):
            try:
                if status_path.read_text().strip() != "connected":
                    continue
            except OSError:
                continue
            name = status_path.parent.name  # card1-HDMI-A-2
            if "-" not in name:
                continue
            conn = name.split("-", 1)[1]  # HDMI-A-2
            if conn.upper().startswith("HDMI"):
                connected.append(conn)
        return connected[0] if connected else None

    def _find_ffmpeg(self) -> str | None:
        # Prefer wrapper script when it is a real executable file (not a mistaken dir).
        if BUNDLED_FFMPEG_SH.is_file() and os.access(BUNDLED_FFMPEG_SH, os.X_OK):
            return str(BUNDLED_FFMPEG_SH)
        if BUNDLED_FFMPEG.is_file() and os.access(BUNDLED_FFMPEG, os.X_OK):
            return str(BUNDLED_FFMPEG)
        return shutil.which("ffmpeg")

    def _console_helper(self, action: str) -> bool:
        """Run /usr/local/sbin/iptv-console quiet|restore via passwordless sudo."""
        helper = "/usr/local/sbin/iptv-console"
        if not os.path.isfile(helper):
            return False
        try:
            r = subprocess.run(
                ["sudo", "-n", helper, action],
                capture_output=True,
                timeout=3,
            )
            if r.returncode != 0:
                log.warning(
                    "iptv-console %s failed: %s",
                    action,
                    (r.stderr or b"").decode("utf-8", errors="replace")[:200],
                )
                return False
            return True
        except Exception as e:
            log.warning("iptv-console %s error: %s", action, e)
            return False

    def _set_cpu_governor(self, mode: str) -> None:
        """mode: performance | ondemand (via iptv-console helper when installed)."""
        action = "cpu-performance" if mode == "performance" else "cpu-ondemand"
        if self._console_helper(action):
            return
        # Direct sysfs if somehow writable
        try:
            for gov in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor"):
                gov.write_text(("performance" if mode == "performance" else "ondemand") + "\n")
        except OSError:
            pass

    def _ensure_fb_ready(self) -> None:
        """Hide login cursor / switch VT to graphics mode when helper is installed."""
        self._set_cpu_governor("performance")
        if self._console_helper("quiet"):
            return
        # Fallbacks that often lack permission without the helper
        try:
            subprocess.run(
                ["sudo", "-n", "sh", "-c", "echo 0 > /sys/class/graphics/fbcon/cursor_blink"],
                capture_output=True,
                timeout=2,
            )
        except Exception:
            pass
        try:
            subprocess.run(
                ["setterm", "-cursor", "off", "-blank", "0"],
                capture_output=True,
                timeout=2,
                env={**os.environ, "TERM": "linux"},
            )
        except Exception:
            pass

    def _restore_console(self) -> None:
        self._console_helper("restore")
        self._set_cpu_governor("ondemand")

    def _stop_feeder(self) -> None:
        if self._feeder is not None and self._feeder.poll() is None:
            try:
                os.killpg(self._feeder.pid, signal.SIGTERM)
                try:
                    self._feeder.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(self._feeder.pid, signal.SIGKILL)
            except Exception as e:
                log.warning("Error stopping feeder: %s", e)
        self._feeder = None
        subprocess.run(
            ["pkill", "-f", r"ffmpeg.*/dev/shm/iptv-guide"],
            capture_output=True,
        )

    def stop(self) -> PlayerState:
        if self._proc is not None and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
                try:
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self._proc.pid, signal.SIGKILL)
            except Exception as e:
                log.warning("Error stopping player: %s", e)
        self._proc = None
        self._stop_feeder()

        if self._aplay is not None and self._aplay.poll() is None:
            try:
                self._aplay.terminate()
                self._aplay.wait(timeout=2)
            except Exception:
                try:
                    self._aplay.kill()
                except Exception:
                    pass
        self._aplay = None

        subprocess.run(["pkill", "-f", r"(^|/)(c?vlc)( |$)"], capture_output=True)
        subprocess.run(["pkill", "-f", r"(^|/)mpv( |$)"], capture_output=True)
        subprocess.run(["pkill", "-f", r"ffmpeg.*fbdev.*/dev/fb0"], capture_output=True)
        subprocess.run(["pkill", "-f", r"ffmpeg.*/dev/shm/iptv-guide"], capture_output=True)
        subprocess.run(["pkill", "-f", r"aplay.*(vc4hdmi|Headphones)"], capture_output=True)
        time.sleep(0.3)
        self._restore_console()

        self.state = PlayerState(running=False)
        self._play_url = None
        return self.state

    def _start(self, cmd: list[str], backend: str, name: str | None, url: str) -> PlayerState:
        log.info("Starting %s: %s", backend, name or url)
        try:
            self._proc = subprocess.Popen(
                cmd,
                env=self._env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            self.state = PlayerState(
                running=False,
                name=name,
                url=url,
                error=f"{backend} not found",
                backend=backend,
            )
            return self.state
        except Exception as e:
            self.state = PlayerState(
                running=False,
                name=name,
                url=url,
                error=str(e),
                backend=backend,
            )
            return self.state

        time.sleep(1.2)
        if self._proc.poll() is not None:
            err = ""
            try:
                err = (self._proc.stderr.read() or b"").decode("utf-8", errors="replace")
            except Exception:
                pass
            log.warning("%s exited: %s", backend, err[:500])
            friendly = (
                _friendly_vlc_error(err)
                if backend == "vlc"
                else (err.strip()[:200] or f"{backend} exited immediately")
            )
            self.state = PlayerState(
                running=False,
                name=name,
                url=url,
                error=friendly,
                backend=backend,
            )
            self._proc = None
            return self.state

        self.state = PlayerState(
            running=True,
            name=name,
            url=url,
            pid=self._proc.pid,
            started_at=time.time(),
            backend=backend,
        )
        return self.state

    def _cache_ms(self) -> int:
        return max(1000, int(self.network_cache_ms or 8000))

    def _vlc_cmd(self, binary: str, url: str) -> list[str]:
        cache = str(self._cache_ms())
        return [
            binary,
            "-I",
            "dummy",
            "--fullscreen",
            "--no-video-title-show",
            "--no-osd",
            "--no-qt-privacy-ask",
            "--aout=alsa",
            f"--alsa-audio-device={detect_alsa_device(self.alsa_device)}",
            f"--network-caching={cache}",
            f"--live-caching={cache}",
            f"--file-caching={cache}",
            f"--http-caching={cache}",
            # Keep A/V locked to the audio clock (default synchro=1).
            "--clock-synchro=1",
            "--play-and-exit",
            url,
        ]

    def _fb_size(self) -> tuple[int, int]:
        path = Path("/sys/class/graphics/fb0/virtual_size")
        try:
            raw = path.read_text().strip()
            w, h = raw.split(",")
            return int(w), int(h)
        except Exception:
            return 1920, 1080

    def _ffmpeg_reconnect_opts(self, url: str) -> list[str]:
        """Network/HLS resilience flags shared by feeder and decode paths."""
        cache_ms = self._cache_ms()
        # rw_timeout is microseconds for several ffmpeg protocols
        rw_timeout = max(15_000_000, cache_ms * 2000)
        opts = [
            "-reconnect",
            "1",
            "-reconnect_at_eof",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_on_network_error",
            "1",
            "-reconnect_on_http_error",
            "4xx,5xx",
            "-reconnect_delay_max",
            "5",
            "-rw_timeout",
            str(rw_timeout),
            "-probesize",
            "4M",
            "-analyzeduration",
            "5M",
            "-err_detect",
            "ignore_err",
        ]
        # HLS: start several segments behind live so the demuxer has a cushion.
        if "m3u8" in url.lower():
            opts += [
                "-live_start_index",
                str(int(self.hls_live_start_index or -6)),
                "-http_persistent",
                "1",
            ]
        return opts

    def _ffmpeg_input_opts(self, url: str) -> list[str]:
        """Input flags for decode paths (timestamp repair OK here)."""
        return [
            *self._ffmpeg_reconnect_opts(url),
            "-fflags",
            "+genpts+discardcorrupt",
        ]

    def _buffer_playlist_path(self) -> Path:
        return Path(self.stream_buffer_dir) / "live.m3u8"

    def _prepare_buffer_dir(self) -> Path:
        d = Path(self.stream_buffer_dir)
        if d.exists():
            for child in d.iterdir():
                try:
                    child.unlink()
                except OSError:
                    pass
        else:
            d.mkdir(parents=True, exist_ok=True)
        return d

    def _start_hls_feeder(self, ffmpeg: str, remote_url: str) -> Path | None:
        """Copy remote HLS into /dev/shm so the display process reads local RAM."""
        self._stop_feeder()
        out_dir = self._prepare_buffer_dir()
        playlist = out_dir / "live.m3u8"
        media = resolve_hls_media_url(remote_url)
        # Stream-copy remux: keep original A/V timestamps. Do NOT use +genpts here —
        # regenerating PTS on a remux is a common cause of lipsync drift.
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            *self._ffmpeg_reconnect_opts(media),
            "-fflags",
            "+discardcorrupt",
            "-i",
            media,
            "-map",
            "0",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            # Keep the sliding window short so mpv stays near the live edge.
            "-hls_list_size",
            "6",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist",
            str(playlist),
        ]
        log.info("Starting HLS feeder → %s", playlist)
        try:
            self._feeder = subprocess.Popen(
                cmd,
                env=self._env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            log.warning("Feeder start failed: %s", e)
            self._feeder = None
            return None

        need = max(2, int(self.stream_buffer_segments or 5))
        deadline = time.time() + 25.0
        while time.time() < deadline:
            if self._feeder.poll() is not None:
                err = ""
                try:
                    err = (self._feeder.stderr.read() or b"").decode("utf-8", errors="replace")
                except Exception:
                    pass
                log.warning("Feeder exited early: %s", err[:300])
                self._feeder = None
                return None
            if playlist.is_file():
                try:
                    segs = sum(
                        1
                        for ln in playlist.read_text(errors="replace").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")
                    )
                except OSError:
                    segs = 0
                if segs >= need:
                    log.info("Local buffer ready (%d segments)", segs)
                    return playlist
            time.sleep(0.4)

        log.warning("Timed out waiting for local HLS buffer")
        self._stop_feeder()
        return None

    def _ffmpeg_fb_cmd(
        self,
        ffmpeg: str,
        url: str,
        with_audio_pipe: bool = False,
        *,
        local: bool = False,
    ) -> list[str]:
        w, h = self._fb_size()
        fps = int(self.max_display_fps or 0)
        # 0 = keep the stream frame rate. A low cap (24) makes 60fps sports look choppy.
        scale = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
        )
        vf = f"fps={fps},{scale}" if 0 < fps < 55 else scale
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
        ]
        if local:
            # Pace from the RAM buffer; feeder keeps filling ahead on the WAN.
            cmd += ["-re"]
        else:
            cmd += self._ffmpeg_input_opts(url)
        cmd += [
            "-i",
            url,
            "-map",
            "0:v:0",
            "-vf",
            vf,
            "-pix_fmt",
            "rgb565le",
            "-f",
            "fbdev",
            "/dev/fb0",
        ]
        if with_audio_pipe:
            # Bundled ffmpeg has no ALSA muxer — WAV → aplay on the HDMI device.
            # Stronger async keeps lipsync when soft-scale starvation stretches video.
            cmd += [
                "-map",
                "0:a:0?",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-af",
                "aresample=async=2000:min_hard_comp=0.05:first_pts=0",
                "-f",
                "wav",
                "pipe:1",
            ]
        return cmd

    def _mpv_cmd(
        self,
        binary: str,
        url: str,
        connector: str | None,
        *,
        local: bool = False,
    ) -> list[str]:
        cache_ms = self._cache_ms()
        fps = int(self.max_display_fps or 0)
        # Local RAM HLS is already smoothed. Keep a few seconds queued so a
        # Starlink spike does not underrun, without letting audio run away.
        if local:
            readahead = 4.0
            max_bytes = "24MiB"
            back_bytes = "8MiB"
            cache_pause = "no"
        else:
            readahead = max(6.0, cache_ms / 1000.0)
            max_bytes = "80MiB"
            back_bytes = "10MiB"
            cache_pause = "yes"
        alsa = detect_alsa_device(self.alsa_device)
        self._last_alsa = alsa
        audio = alsa if alsa.startswith("alsa/") else f"alsa/{alsa}"
        delay = float(self.audio_delay_sec or 0.0)
        cmd = [
            binary,
            "--no-config",
            "--vo=drm",
            "--hwdec=no",
            "--gpu-context=drm",
            "--ao=alsa",
            f"--audio-device={audio}",
            "--fullscreen",
            "--really-quiet",
            "--no-osc",
            "--no-input-default-bindings",
            "--keep-open=no",
            "--force-window=immediate",
            "--cache=yes",
            f"--demuxer-readahead-secs={readahead:.0f}",
            f"--demuxer-max-bytes={max_bytes}",
            f"--demuxer-max-back-bytes={back_bytes}",
            f"--cache-pause={cache_pause}",
            "--audio-buffer=0.2",
            # Match the 60Hz panel. Dropping to 24fps and skipping B-frames
            # is what made motion stutter and the picture break up.
            "--video-sync=display-resample",
            "--framedrop=vo",
            "--vd-lavc-threads=4",
            f"--audio-delay={delay:.3f}",
            "--input-ipc-server=/tmp/iptv-mpv.sock",
            "--hls-bitrate=min",
            "--network-timeout=30",
            "--stream-lavf-o=reconnect=1,reconnect_streamed=1,reconnect_delay_max=5",
        ]
        if 0 < fps < 55:
            cmd.append(f"--vf=fps={fps}")
        if local and "m3u8" in url.lower():
            # Prefer the newest local segments (feeder write head), not the oldest.
            cmd.append("--demuxer-lavf-o=live_start_index=-1")
        if not local and cache_pause == "yes":
            cmd += ["--cache-pause-wait=2"]
        if connector:
            cmd.append(f"--drm-connector={connector}")
        hdmi_mode = (self.hdmi_mode or "").strip()
        if hdmi_mode:
            cmd.append(f"--drm-mode={hdmi_mode}")
        cmd.append(url)
        return cmd

    def _play_mpv(self, url: str, name: str | None) -> PlayerState | None:
        mpv = self._find_mpv()
        if not mpv:
            return None
        connector = self._drm_connector()
        self._play_url = url
        self._ensure_fb_ready()
        # Prefer local RAM buffer when enabled — mpv then reads steady local HLS.
        play_url = url
        local = False
        if self.stream_buffer and "m3u8" in url.lower():
            ffmpeg = self._find_ffmpeg()
            if ffmpeg:
                playlist = self._start_hls_feeder(ffmpeg, url)
                if playlist is not None:
                    play_url = str(playlist)
                    local = True
                else:
                    play_url = resolve_hls_media_url(url)
            else:
                play_url = resolve_hls_media_url(url)
        elif "m3u8" in url.lower():
            play_url = resolve_hls_media_url(url)

        log.info(
            "Starting mpv-drm (connector=%s, mode=%s, local=%s): %s",
            connector or "auto",
            (self.hdmi_mode or "").strip() or "preferred",
            local,
            name or url,
        )
        try:
            self._proc = subprocess.Popen(
                self._mpv_cmd(mpv, play_url, connector, local=local),
                env=self._env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            self._stop_feeder()
            return PlayerState(
                running=False, name=name, url=url, error=str(e), backend="mpv-drm"
            )

        time.sleep(2.5)
        if self._proc.poll() is not None:
            err = ""
            try:
                err = (self._proc.stderr.read() or b"").decode("utf-8", errors="replace")
            except Exception:
                pass
            log.warning("mpv exited: %s", err[:500])
            self._stop_feeder()
            self._proc = None
            return PlayerState(
                running=False,
                name=name,
                url=url,
                error=(err.strip()[:200] or "mpv exited immediately"),
                backend="mpv-drm",
            )

        self.state = PlayerState(
            running=True,
            name=name,
            url=url,
            pid=self._proc.pid,
            started_at=time.time(),
            backend="mpv-drm",
        )
        return self.state

    def _play_vlc(self, url: str, name: str | None) -> PlayerState | None:
        cvlc = self._find_cvlc()
        if not cvlc:
            return None
        state = self._start(self._vlc_cmd(cvlc, url), "vlc", name, url)
        if state.running:
            return state
        # Retry with default audio output (Pulse) once
        log.info("VLC ALSA attempt failed; retrying with default audio")
        self.stop()
        cache = str(self._cache_ms())
        cmd = [
            cvlc,
            "-I",
            "dummy",
            "--fullscreen",
            "--no-video-title-show",
            "--no-osd",
            "--no-qt-privacy-ask",
            f"--network-caching={cache}",
            f"--live-caching={cache}",
            f"--http-caching={cache}",
            "--play-and-exit",
            url,
        ]
        return self._start(cmd, "vlc", name, url)

    def _play_ffmpeg(self, url: str, name: str | None) -> PlayerState | None:
        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            return None
        self._ensure_fb_ready()
        aplay = shutil.which("aplay")
        device = detect_alsa_device(self.alsa_device)
        self._last_alsa = device
        self._play_url = url

        play_url = url
        local = False
        backend = "ffmpeg-fbdev"
        if self.stream_buffer and "m3u8" in url.lower():
            playlist = self._start_hls_feeder(ffmpeg, url)
            if playlist is not None:
                play_url = str(playlist)
                local = True
                backend = "ffmpeg-buffered"
            else:
                log.warning("Falling back to direct stream (no local buffer)")

        if aplay:
            return self._start_ffmpeg_with_aplay(
                ffmpeg, aplay, device, play_url, name, local=local, backend=backend
            )

        log.warning("aplay not found — video only")
        return self._start(
            self._ffmpeg_fb_cmd(ffmpeg, play_url, local=local),
            backend,
            name,
            url,
        )

    def _start_ffmpeg_with_aplay(
        self,
        ffmpeg: str,
        aplay: str,
        device: str,
        url: str,
        name: str | None,
        *,
        local: bool = False,
        backend: str = "ffmpeg-fbdev",
    ) -> PlayerState:
        """Video → /dev/fb0, audio → aplay (HDMI). Same process group for stop()."""
        ff_cmd = self._ffmpeg_fb_cmd(ffmpeg, url, with_audio_pipe=True, local=local)
        # shell pipeline so killpg stops both ffmpeg and aplay
        # Larger ALSA periods absorb Starlink jitter / decode stalls without underruns.
        script = (
            f"{shlex.join(ff_cmd)} | "
            f"{shlex.quote(aplay)} -q -D {shlex.quote(device)} "
            f"-B 1000000 -F 250000"
        )
        log.info("Starting %s+alsa (%s): %s", backend, device, name or url)
        try:
            self._proc = subprocess.Popen(
                ["bash", "-c", script],
                env=self._env(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            self._stop_feeder()
            self.state = PlayerState(
                running=False,
                name=name,
                url=self._play_url or url,
                error=str(e),
                backend=backend,
            )
            return self.state

        # Local buffer is already warm; direct streams need a few seconds to latch.
        time.sleep(1.5 if local else 3.0)
        if self._proc.poll() is not None:
            err = ""
            try:
                err = (self._proc.stderr.read() or b"").decode("utf-8", errors="replace")
            except Exception:
                pass
            log.warning("ffmpeg+aplay exited: %s", err[:500])
            self._stop_feeder()
            self.state = PlayerState(
                running=False,
                name=name,
                url=self._play_url or url,
                error=(err.strip()[:200] or "ffmpeg/aplay exited immediately"),
                backend=backend,
            )
            self._proc = None
            return self.state

        self.state = PlayerState(
            running=True,
            name=name,
            url=self._play_url or url,
            pid=self._proc.pid,
            started_at=time.time(),
            backend=backend,
        )
        return self.state

    def play(self, url: str, name: str | None = None) -> PlayerState:
        self.stop()
        mode = self.detect()
        log.info("Output mode: %s (%s)", mode.kind, mode.detail)

        if mode.kind == "desktop":
            state = self._play_vlc(url, name)
            if state is not None and state.running:
                return state
            if state is not None:
                log.warning("VLC failed on desktop (%s); trying framebuffer", state.error)
            fb = self._play_ffmpeg(url, name)
            if fb is not None:
                return fb
            self.state = state or PlayerState(
                running=False,
                name=name,
                url=url,
                error="Desktop detected but VLC/ffmpeg unavailable",
            )
            return self.state

        if mode.kind == "framebuffer":
            # Console: prefer mpv DRM (better cache/framedrop), else ffmpeg→fbdev.
            log.info("No desktop session — trying mpv DRM then ffmpeg (buffered=%s)", self.stream_buffer)
            mpv_state = self._play_mpv(url, name)
            if mpv_state is not None and mpv_state.running:
                return mpv_state
            if mpv_state is not None:
                log.warning("mpv DRM failed (%s); falling back to ffmpeg", mpv_state.error)
            fb = self._play_ffmpeg(url, name)
            if fb is not None and fb.running:
                return fb
            if fb is not None:
                return fb
            state = self._play_vlc(url, name)
            if state is not None:
                return state
            self.state = PlayerState(
                running=False,
                name=name,
                url=url,
                error="Framebuffer mode but no mpv/ffmpeg/vlc found",
            )
            return self.state

        self.state = PlayerState(
            running=False,
            name=name,
            url=url,
            error=(
                "No display detected (no desktop session and no /dev/fb0). "
                "Log into the Pi desktop, or enable console framebuffer."
            ),
        )
        return self.state

    def status(self) -> PlayerState:
        if self.state.running:
            if self._proc is not None and self._proc.poll() is not None:
                self.state.running = False
                self.state.pid = None
        return self.state

    def output_info(self) -> dict[str, str | None]:
        mode = self._last_mode or self.detect()
        alsa = self._last_alsa or detect_alsa_device(self.alsa_device)
        return {
            "output_kind": mode.kind,
            "output_detail": mode.detail,
            "display": mode.display or self.display,
            "alsa_device": alsa,
        }
