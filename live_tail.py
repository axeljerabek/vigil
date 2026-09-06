"""
live_tail.py — continuously copies newly-appended bytes from a growing
source into a named pipe (FIFO), so ffmpeg/PyAV can open it and read it
like a genuine live stream instead of hitting EOF the moment it catches
up to what's been written so far.

Two related use cases share this exact mechanism:
  1. Watchfolder mode 1 — a growing, streamable (moov-first or MPEG-TS)
     local file dropped by some external recording tool.
  2. The platform-stream bridge (platform_bridge.py) — yt-dlp/ffmpeg
     continuously downloading a 24/7 YouTube/Twitch/etc. stream. Rather
     than vaelen re-resolving and reconnecting to the platform URL itself
     on every hiccup, yt-dlp's own robust retry/live-download handling
     writes into a local file, and vaelen just tails that -- the same
     "keep reading, never hit EOF" problem either way.
"""
import os
import threading
import time


class GrowingFileTailer:
    """Runs in its own background thread. Reads new bytes appended to
    `source_path` and writes them into a FIFO at `fifo_path` (created if
    it doesn't already exist), so a reader (ffmpeg) opening the FIFO sees
    a continuous, live stream rather than a file that ends."""

    def __init__(self, source_path, fifo_path, poll_interval=0.2):
        self.source_path = source_path
        self.fifo_path = fifo_path
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread = None
        self.error = None

    def start(self):
        if not os.path.exists(self.fifo_path):
            os.mkfifo(self.fifo_path)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    @property
    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        # Öffnen eines FIFOs zum SCHREIBEN blockiert (POSIX-Verhalten,
        # kein Bug), bis ein Leser (ffmpeg) es zum Lesen öffnet -- dieser
        # Aufruf hier wartet also ggf., bis die Aufnahme-Pipeline tatsächlich
        # bereit ist zu lesen.
        try:
            with open(self.source_path, "rb") as src:
                fifo_fd = os.open(self.fifo_path, os.O_WRONLY)
                try:
                    while not self._stop_event.is_set():
                        chunk = src.read(65536)
                        if chunk:
                            os.write(fifo_fd, chunk)
                        else:
                            time.sleep(self.poll_interval)  # noch keine neuen Daten -- kurz warten statt busy-loopen
                finally:
                    os.close(fifo_fd)
        except BrokenPipeError:
            pass  # Leser (ffmpeg) hat die Verbindung beendet -- Ende, kein Fehler
        except Exception as e:
            self.error = str(e)

    def cleanup(self):
        """FIFO-Datei von der Platte entfernen -- nach dem Stoppen aufrufen,
        sonst bleiben verwaiste FIFOs im Dateisystem liegen."""
        try:
            if os.path.exists(self.fifo_path):
                os.remove(self.fifo_path)
        except OSError:
            pass
