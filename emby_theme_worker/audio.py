from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
from array import array
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from .models import Candidate
from .security import safe_http_url_from_strm
from .ytdlp_runner import download as ytdlp_download


LOG = logging.getLogger(__name__)


class AudioProcessor:
    def __init__(
        self,
        target_seconds: int,
        bitrate: str,
        timeout: int,
        cookies: dict[str, str],
        ytdlp_download_timeout: int = 180,
    ):
        self.target_seconds = target_seconds
        self.bitrate = bitrate
        self.timeout = timeout
        self.cookies = cookies
        self.ytdlp_download_timeout = ytdlp_download_timeout

    def fetch_candidate(self, candidate: Candidate, workdir: Path) -> Path:
        source = candidate.source_url
        if source.startswith(("ytsearch", "bilisearch")) or candidate.provider in {"youtube", "bilibili", "themerrdb", "tunefind", "netease", "qqmusic"}:
            return self._ytdlp(source, candidate.provider, workdir)
        return self._direct(source, workdir)

    def _direct(self, url: str, workdir: Path) -> Path:
        suffix = Path(urlsplit(url).path).suffix or ".media"
        target = workdir / f"download{suffix[:10]}"
        with httpx.stream("GET", url, timeout=self.timeout, follow_redirects=True, headers={"User-Agent": "emby-theme-worker/0.1"}) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 256):
                    handle.write(chunk)
        return target

    def _ytdlp(self, url: str, provider: str, workdir: Path) -> Path:
        template = str(workdir / "source.%(ext)s")
        cookie = self.cookies.get(provider) or self.cookies.get("youtube")
        ytdlp_download(
            url,
            output_template=Path(template),
            cookie_file=cookie,
            socket_timeout_seconds=self.timeout,
            hard_timeout_seconds=self.ytdlp_download_timeout,
        )
        matches = list(workdir.glob("source.*"))
        if not matches:
            raise FileNotFoundError("yt-dlp produced no media")
        return matches[0]

    def normalize_online(self, source: Path, target: Path) -> None:
        start = self._highest_energy_start(source)
        self._transcode(source, target, start=start, strip_initial_silence=False)

    def extract_opening(self, media_path: str, target: Path) -> None:
        source = media_path
        if media_path.casefold().endswith(".strm"):
            source = safe_http_url_from_strm(Path(media_path).read_text(encoding="utf-8-sig"))
        self._transcode(source, target, start=0, strip_initial_silence=True)

    def _highest_energy_start(self, source: Path) -> float:
        command = [
            "ffmpeg", "-v", "error", "-i", str(source), "-t", "1200", "-vn",
            "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1",
        ]
        raw = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300).stdout
        samples = array("h")
        samples.frombytes(raw)
        per_second = 8000
        rms: list[float] = []
        for offset in range(0, len(samples), per_second):
            window = samples[offset:offset + per_second]
            if not window:
                continue
            rms.append(sum(value * value for value in window) / len(window))
        width = min(self.target_seconds, len(rms))
        if width <= 0:
            raise ValueError("decoded audio is empty")
        rolling = sum(rms[:width])
        best = rolling
        best_index = 0
        for index in range(width, len(rms)):
            rolling += rms[index] - rms[index - width]
            if rolling > best:
                best = rolling
                best_index = index - width + 1
        return float(best_index)

    def _transcode(self, source: str | Path, target: Path, *, start: float, strip_initial_silence: bool) -> None:
        filters: list[str] = []
        if strip_initial_silence:
            filters.append("silenceremove=start_periods=1:start_silence=0:start_threshold=-45dB")
        filters.extend([
            "loudnorm=I=-16:LRA=11:TP=-1.5",
            "afade=t=in:st=0:d=0.5",
            f"afade=t=out:st={max(0, self.target_seconds - 1)}:d=1",
        ])
        command = ["ffmpeg", "-v", "error", "-y"]
        if start:
            command.extend(["-ss", f"{start:.3f}"])
        command.extend([
            "-i", str(source), "-vn", "-t", str(self.target_seconds), "-af", ",".join(filters),
            "-codec:a", "libmp3lame", "-b:a", self.bitrate, str(target),
        ])
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)

    def validate(self, path: Path) -> dict:
        command = [
            "ffprobe", "-v", "error", "-show_entries", "format=duration,size",
            "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path),
        ]
        import json
        payload = json.loads(subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30).stdout)
        duration = float(payload.get("format", {}).get("duration") or 0)
        size = int(payload.get("format", {}).get("size") or 0)
        if not any(stream.get("codec_type") == "audio" for stream in payload.get("streams", [])):
            raise ValueError("output has no audio stream")
        if duration < self.target_seconds - 2 or duration > self.target_seconds + 2:
            raise ValueError(f"output duration out of range: {duration:.2f}")
        if size < 32_000:
            raise ValueError("output audio is unexpectedly small")
        return {"duration": duration, "size": size, "sha256": sha256(path)}

    def atomic_commit(self, prepared: Path, target_dir: Path) -> tuple[bool, Path, str]:
        target_dir.mkdir(parents=False, exist_ok=True)
        target = target_dir / "theme.mp3"
        lock_path = target_dir / ".theme.mp3.lock"
        try:
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                if target.exists():
                    return False, target, sha256(target)
                temp = target_dir / f".theme.{os.getpid()}.tmp.mp3"
                try:
                    shutil.copyfile(prepared, temp)
                    with temp.open("rb") as handle:
                        os.fsync(handle.fileno())
                    os.replace(temp, target)
                    dir_fd = os.open(target_dir, os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                finally:
                    temp.unlink(missing_ok=True)
        finally:
            lock_path.unlink(missing_ok=True)
        return True, target, sha256(target)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def temporary_directory() -> tempfile.TemporaryDirectory[str]:
    return tempfile.TemporaryDirectory(prefix="emby-theme-")
