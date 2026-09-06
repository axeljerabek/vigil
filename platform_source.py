"""
platform_source.py — resolves platform URLs (YouTube, Twitch, Vimeo, and
hundreds of others yt-dlp supports) into a direct, ffmpeg/PyAV-consumable
stream URL, so vaelen's existing camera-ingestion pipeline can treat them
exactly like an RTSP/RTMP camera without any platform-specific code.

Resolved URLs are typically time-limited/signed -- they expire after some
minutes and need re-resolving, not just a reconnect to the same URL. The
pipeline calls resolve_stream_url() again on every reconnect attempt for a
platform camera rather than caching the resolved URL long-term.
"""
import json
import subprocess

_PLATFORM_HOSTS = (
    "youtube.com", "youtu.be", "twitch.tv", "vimeo.com",
    "facebook.com", "dailymotion.com", "kick.com",
)


def needs_resolution(url):
    """Whether this URL is a platform link that needs yt-dlp resolution,
    as opposed to a plain rtsp://, rtmp://, http(s) direct-stream, or
    /dev/videoX URL vaelen already handles natively."""
    return any(host in url for host in _PLATFORM_HOSTS)


def resolve_stream_url(url, timeout=20):
    """Returns (resolved_url, error). resolved_url is a direct HLS/DASH/HTTP
    stream URL that ffmpeg/PyAV can open directly, or None on failure --
    error is then a short, human-readable reason."""
    try:
        result = subprocess.run(
            ["yt-dlp", "-g", "--no-warnings", "-f", "best[protocol^=m3u8]/best", url],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            reason = result.stderr.strip()[-500:] or "yt-dlp returned no stream URL."
            return None, reason
        lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
        if not lines:
            return None, "yt-dlp resolved no playable stream (offline, private, or not currently live?)."
        return lines[0], None
    except subprocess.TimeoutExpired:
        return None, "Resolving the stream URL timed out."
    except FileNotFoundError:
        return None, "yt-dlp is not installed. Install it with: pip install yt-dlp"
    except Exception as e:
        return None, str(e)


def is_live(url, timeout=15):
    """Best-effort check whether a URL currently points to an active live
    stream, as opposed to an offline channel or a plain VOD -- avoids
    repeatedly trying (and failing) to connect to something that simply
    isn't live right now. Fails safe (returns False) on any error, since
    callers should treat 'unknown' the same as 'not currently live'."""
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-warnings", "-j", "--no-playlist", url],
            capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            return False
        info = json.loads(result.stdout)
        return bool(info.get("is_live") or info.get("live_status") == "is_live")
    except Exception:
        return False
