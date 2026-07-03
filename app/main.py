import ipaddress
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


API_KEY = os.getenv("CUSTOMSONGS_CONVERTER_KEY", "")
HARD_MAX_DURATION_SECONDS = int(os.getenv("MAX_DURATION_SECONDS", "600"))
HARD_MAX_DOWNLOAD_BYTES = int(os.getenv("MAX_DOWNLOAD_BYTES", str(120 * 1024 * 1024)))
HARD_MAX_OGG_BYTES = int(os.getenv("MAX_OGG_BYTES", str(32 * 1024 * 1024)))
PROCESS_TIMEOUT_SECONDS = int(os.getenv("PROCESS_TIMEOUT_SECONDS", "600"))
YTDLP_USER_AGENT = os.getenv(
    "YTDLP_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

app = FastAPI(title="Custom Songs Converter", version="0.1.0")

DIRECT_AUDIO_EXTENSIONS = (".mp3", ".ogg", ".wav", ".m4a", ".aac", ".flac", ".opus", ".webm")


class ConvertRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)
    title: str = Field(default="", max_length=120)
    max_duration_seconds: int = Field(default=480, ge=1, le=HARD_MAX_DURATION_SECONDS)
    max_download_bytes: int = Field(default=80 * 1024 * 1024, ge=1024, le=HARD_MAX_DOWNLOAD_BYTES)
    max_ogg_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=HARD_MAX_OGG_BYTES)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/convert")
def convert(request: ConvertRequest, x_api_key: str | None = Header(default=None)):
    require_api_key(x_api_key)
    validate_url(request.url)

    workdir = Path(tempfile.mkdtemp(prefix="customsongs-"))
    try:
        metadata = {}
        input_file = try_download_direct_audio(request.url, workdir, request.max_download_bytes)
        if input_file is None:
            metadata = read_metadata(request.url)
        duration = float(metadata.get("duration") or 0)
        if duration and duration > request.max_duration_seconds:
            raise HTTPException(status_code=413, detail=f"Трек слишком длинный: {round(duration)} сек.")

        if input_file is None:
            input_file = download_audio(request.url, workdir, request.max_download_bytes)
        if not duration:
            duration = probe_duration(input_file)
        if duration > request.max_duration_seconds:
            raise HTTPException(status_code=413, detail=f"Трек слишком длинный: {round(duration)} сек.")

        output_file = workdir / "output.ogg"
        convert_to_ogg(input_file, output_file)
        output_size = output_file.stat().st_size
        if output_size > request.max_ogg_bytes:
            raise HTTPException(status_code=413, detail="Файл после конвертации слишком большой.")

        return FileResponse(
            output_file,
            media_type="audio/ogg",
            filename="customsongs.ogg",
            headers={
                "X-Audio-Duration": str(duration),
                "X-Audio-Bytes": str(output_size),
            },
            background=BackgroundTask(lambda: shutil.rmtree(workdir, ignore_errors=True)),
        )
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=504, detail="Конвертация заняла слишком много времени.")
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=clean_error(str(exc)))


def require_api_key(value: str | None) -> None:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="CUSTOMSONGS_CONVERTER_KEY не задан на converter-сервисе.")
    if value != API_KEY:
        raise HTTPException(status_code=401, detail="Неверный ключ converter API.")


def validate_url(raw_url: str) -> None:
    parsed = urlparse(raw_url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Разрешены только https-ссылки.")
    if not parsed.hostname:
        raise HTTPException(status_code=400, detail="В ссылке нет хоста.")
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(status_code=400, detail="Локальные адреса запрещены.")
    try:
        for result in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(result[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                raise HTTPException(status_code=400, detail="Приватные IP-адреса запрещены.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Не удалось проверить хост ссылки.")


def read_metadata(url: str) -> dict:
    result = run_command([
        "yt-dlp",
        "--user-agent",
        YTDLP_USER_AGENT,
        "--referer",
        referer_for(url),
        "--dump-single-json",
        "--no-playlist",
        "--skip-download",
        url,
    ])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def download_audio(url: str, workdir: Path, max_bytes: int) -> Path:
    run_command([
        "yt-dlp",
        "--user-agent",
        YTDLP_USER_AGENT,
        "--referer",
        referer_for(url),
        "--no-playlist",
        "--max-filesize",
        str(max_bytes),
        "-f",
        "bestaudio/best",
        "-o",
        str(workdir / "input.%(ext)s"),
        url,
    ])
    files = sorted(workdir.glob("input.*"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not files:
        raise HTTPException(status_code=400, detail="Не удалось скачать аудио.")
    input_file = files[0]
    if input_file.stat().st_size > max_bytes:
        raise HTTPException(status_code=413, detail="Скачанный файл слишком большой.")
    return input_file


def try_download_direct_audio(url: str, workdir: Path, max_bytes: int) -> Path | None:
    parsed = urlparse(url)
    looks_like_audio = parsed.path.lower().endswith(DIRECT_AUDIO_EXTENSIONS)
    request = Request(
        url,
        headers={
            "User-Agent": YTDLP_USER_AGENT,
            "Referer": referer_for(url),
            "Accept": "audio/*,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            content_type = response.headers.get("content-type", "").lower()
            if not looks_like_audio and not content_type.startswith("audio/"):
                return None
            suffix = Path(parsed.path).suffix or ".bin"
            output = workdir / f"input{suffix}"
            total = 0
            with output.open("wb") as file:
                while True:
                    chunk = response.read(8192)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise HTTPException(status_code=413, detail="Скачанный файл слишком большой.")
                    file.write(chunk)
            return output
    except HTTPException:
        raise
    except Exception as exc:
        if looks_like_audio:
            raise HTTPException(status_code=400, detail=f"Прямая аудиоссылка недоступна для converter-сервера: {clean_error(str(exc))}")
        return None


def probe_duration(input_file: Path) -> float:
    result = run_command([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_file),
    ])
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Не удалось определить длину трека.")


def convert_to_ogg(input_file: Path, output_file: Path) -> None:
    run_command([
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-vn",
        "-map_metadata",
        "-1",
        "-acodec",
        "libvorbis",
        "-q:a",
        "4",
        str(output_file),
    ])


def run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=PROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        raise HTTPException(status_code=400, detail=clean_error(result.stderr or result.stdout))
    return result


def referer_for(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def clean_error(value: str) -> str:
    value = " ".join((value or "неизвестная ошибка").split())
    return value[:300]
