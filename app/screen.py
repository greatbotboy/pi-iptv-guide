"""Show the channel guide on HDMI while nothing is playing.

Cog's direct DRM path paints a frame then snows/crashes on this Pi. Cage owns
the display as a tiny Wayland compositor, and Chromium (or cog wl) renders the
page inside it. Stop the session before mpv takes the same HDMI output.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from urllib.parse import urlencode

from player import detect_output

log = logging.getLogger("iptv.screen")


def _find_browser() -> tuple[str, list[str]] | None:
    """Return (label, argv without URL) for a kiosk browser."""
    for name in ("chromium", "chromium-browser", "google-chrome"):
        path = shutil.which(name)
        if path:
            return (
                name,
                [
                    path,
                    "--ozone-platform=wayland",
                    "--ozone-platform-hint=wayland",
                    "--enable-features=UseOzonePlatform",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--kiosk",
                    "--noerrdialogs",
                    "--disable-infobars",
                    "--disable-session-crashed-bubble",
                    "--no-first-run",
                    "--disable-features=TranslateUI",
                    "--check-for-update-interval=31536000",
                    "--autoplay-policy=no-user-gesture-required",
                    "--force-device-scale-factor=1",
                    "--window-size=1920,1080",
                    "--user-data-dir=/tmp/iptv-guide-chromium",
                ],
            )
    cog = shutil.which("cog")
    if cog:
        return ("cog-wl", [cog, "--platform=wl", "--bg-color=#0a0e14"])
    return None


class GuideScreen:
    def __init__(self, *, port: int, hdmi_mode: str, enabled: bool, display: str = ":0") -> None:
        self.port = port
        self.hdmi_mode = hdmi_mode
        self.enabled = enabled
        self.display = display
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._suspended = False
        self._started_at = 0.0
        self._fail_until = 0.0
        self._noted_missing = False
        self._cage = shutil.which("cage")
        self._browser = _find_browser()
        self._kiosk: bool | None = None

    def is_running(self) -> bool:
        with self._lock:
            self._reap_locked()
            return self._alive_locked()

    def suspend(self) -> None:
        """Hide the guide and ignore idle restarts until resume()."""
        with self._lock:
            self._suspended = True
            self._stop_locked()

    def resume(self, *, show: bool) -> None:
        with self._lock:
            self._suspended = False
            if show:
                self._start_locked()
            else:
                self._stop_locked()

    def sync(self, *, playing: bool) -> None:
        """Match the TV to playback: video owns HDMI, otherwise scroll the guide."""
        with self._lock:
            self._reap_locked()
            if self._suspended or playing or not self._allowed():
                if self._alive_locked():
                    self._stop_locked()
                return
            if not self._alive_locked():
                self._start_locked()

    def _allowed(self) -> bool:
        if not self.enabled:
            return False
        if not self._cage or not self._browser:
            if not self._noted_missing:
                log.info(
                    "Idle guide skipped — install cage and chromium "
                    "(./deploy/install-screen.sh)"
                )
                self._noted_missing = True
            return False
        # Decide once before cage starts. Cage creates a Wayland socket that
        # must not be mistaken for a logged-in desktop during playback.
        if self._kiosk is None:
            try:
                kind = detect_output(self.display).kind
            except Exception as e:
                log.warning("Display check failed: %s", e)
                return False
            self._kiosk = kind == "framebuffer"
            log.info("Idle guide %s (output %s)", "on" if self._kiosk else "off", kind)
        return self._kiosk

    def _alive_locked(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _reap_locked(self) -> None:
        if self._proc is None or self._proc.poll() is None:
            return
        lived = time.time() - self._started_at
        code = self._proc.returncode
        log.warning("Guide screen exited (%s) after %.1fs", code, lived)
        self._proc = None
        self._cleanup_helpers()
        if lived < 4:
            self._fail_until = time.time() + 30

    def _start_locked(self) -> None:
        if self._alive_locked() or not self._allowed():
            return
        if time.time() < self._fail_until:
            return
        assert self._cage is not None and self._browser is not None
        label, browser_cmd = self._browser
        query = urlencode({"screen": "1"})
        url = f"http://127.0.0.1:{self.port}/?{query}"
        env = os.environ.copy()
        env.pop("DISPLAY", None)
        env.pop("WAYLAND_DISPLAY", None)
        uid = os.getuid()
        runtime = f"/run/user/{uid}"
        env["XDG_RUNTIME_DIR"] = runtime
        env["XDG_SESSION_TYPE"] = "wayland"
        env["WLR_BACKENDS"] = "drm"
        env["WLR_LIBINPUT_NO_DEVICES"] = "1"
        # Prefer the same 1080p mode we use for playback when the TV allows it.
        mode = (self.hdmi_mode or "").strip()
        if mode:
            env.setdefault("WLR_DRM_MODE", mode.split("@", 1)[0])
        cmd = [self._cage, "-s", "--", *browser_cmd, url]
        log.info("Starting idle guide via cage+%s (%s): %s", label, mode or "preferred", url)
        try:
            self._proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            log.warning("Could not start guide screen: %s", e)
            self._fail_until = time.time() + 30
            self._proc = None
            return
        self._started_at = time.time()
        proc = self._proc
        threading.Thread(target=_drain_stderr, args=(proc,), name="guide-screen-log", daemon=True).start()

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError as e:
                log.warning("Error stopping guide screen: %s", e)
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._cleanup_helpers()
        time.sleep(0.4)

    @staticmethod
    def _cleanup_helpers() -> None:
        # Narrow patterns only — never pkill bare "chromium" against the whole system
        # in a way that races the guide service. Kill the kiosk session group first.
        for pattern in (
            r"/usr/bin/cage .*iptv|/usr/bin/cage -s --",
            r"/tmp/iptv-guide-chromium",
            r"cog --platform=wl",
            r"cog --platform=drm",
            r"/wpe-webkit-.*/WPEWebProcess",
            r"/wpe-webkit-.*/WPENetworkProcess",
        ):
            subprocess.run(["pkill", "-f", pattern], capture_output=True)


def _drain_stderr(proc: subprocess.Popen) -> None:
    stream = proc.stderr
    if stream is None:
        return
    try:
        for raw in stream:
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                log.info("guide screen: %s", line[:400])
    except Exception:
        pass
