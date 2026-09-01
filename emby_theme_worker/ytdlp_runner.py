from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path


class YtDlpError(RuntimeError):
    """A redacted yt-dlp failure."""


class YtDlpTimeout(YtDlpError):
    """yt-dlp exceeded its wall-clock deadline."""


def _run(command: list[str], timeout_seconds: int) -> str:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise YtDlpTimeout(f"yt-dlp exceeded {timeout_seconds}s deadline") from exc
    if process.returncode:
        raise YtDlpError(f"yt-dlp exited with status {process.returncode}")
    return stdout


def _base(socket_timeout_seconds: int) -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-config",
        "--no-cache-dir",
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout",
        str(min(socket_timeout_seconds, 15)),
        "--retries",
        "1",
        "--fragment-retries",
        "1",
        "--extractor-retries",
        "1",
        "--file-access-retries",
        "1",
    ]


def search(
    query: str,
    *,
    cookie_file: str | None,
    socket_timeout_seconds: int,
    hard_timeout_seconds: int,
) -> dict:
    command = _base(socket_timeout_seconds)
    command.extend(["--flat-playlist", "--playlist-end", "5", "--dump-single-json"])
    if cookie_file:
        command.extend(["--cookies", cookie_file])
    command.extend(["--", query])
    output = _run(command, hard_timeout_seconds)
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise YtDlpError("yt-dlp returned invalid JSON") from exc


def download(
    source: str,
    *,
    output_template: Path,
    cookie_file: str | None,
    socket_timeout_seconds: int,
    hard_timeout_seconds: int,
) -> None:
    command = _base(socket_timeout_seconds)
    command.extend([
        "--no-playlist",
        "--no-simulate",
        "--format",
        "bestaudio/best",
        "--output",
        str(output_template),
    ])
    if cookie_file:
        command.extend(["--cookies", cookie_file])
    command.extend(["--", source])
    _run(command, hard_timeout_seconds)
