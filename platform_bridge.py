"""
platform_bridge.py — runs a persistent background process that downloads
a platform live stream (YouTube/Twitch/Vimeo/etc.) via yt-dlp and pipes it
into a named pipe (FIFO) in an always-streamable format (MPEG-TS), which
vaelen's camera-ingestion can then open exactly like a live RTSP feed --
same mechanism as watchfolder mode 1 (see live_tail.py), just fed by a
controlled writer instead of an external tool.

This deliberately decouples vaelen's own camera-connection lifecycle from
platform-specific reconnect/URL-expiry quirks entirely: yt-dlp resolves
and reconnects to the platform on its own, robustly, restarting the whole
download pipeline on failure; vaelen just reads a local FIFO that's always
either flowing or briefly blocked, never "expired" the way a raw resolved
URL can be after some minutes.
"""
import os
import subprocess
import threading
import time


class PlatformStreamBridge:
    """One instance per platform camera. Owns a FIFO and keeps a
    yt-dlp | ffmpeg pipeline feeding it alive, restarting the pipeline on
    failure (channel went offline, network hiccup, either process crashed)
    rather than propagating that failure up to vaelen's own camera-connect
    retry loop."""

    def __init__(self, url, fifo_path, restart_delay=10):
        self.url = url
        self.fifo_path = fifo_path
        self.restart_delay = restart_delay
        self._stop_event = threading.Event()
        self._thread = None
        self._ytdlp_proc = None
        self._ffmpeg_proc = None
        self.last_error = None
        self.restart_count = 0

    def start(self):
        if not os.path.exists(self.fifo_path):
            os.mkfifo(self.fifo_path)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop_event.set()
        for proc in (self._ytdlp_proc, self._ffmpeg_proc):
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        first_attempt = True
        while not self._stop_event.is_set():
            if not first_attempt:
                self.restart_count += 1
            first_attempt = False
            try:
                self._ytdlp_proc = subprocess.Popen(
                    ["yt-dlp", "--no-warnings", "-o", "-", self.url],
                    stdout=subprocess.PIPE
                )
                self._ffmpeg_proc = subprocess.Popen(
                    ["ffmpeg", "-y", "-i", "pipe:0", "-c", "copy", "-f", "mpegts", self.fifo_path],
                    stdin=self._ytdlp_proc.stdout, stderr=subprocess.PIPE
                )
                self._ytdlp_proc.stdout.close()
                _, ffmpeg_stderr = self._ffmpeg_proc.communicate()
                if self._ffmpeg_proc.returncode != 0 and not self._stop_event.is_set():
                    self.last_error = (ffmpeg_stderr or b"").decode(errors="replace")[-500:]
            except Exception as e:
                self.last_error = str(e)
            if not self._stop_event.is_set():
                time.sleep(self.restart_delay)

    def cleanup(self):
        """FIFO von der Platte entfernen -- nach dem Stoppen aufrufen."""
        try:
            if os.path.exists(self.fifo_path):
                os.remove(self.fifo_path)
        except OSError:
            pass
