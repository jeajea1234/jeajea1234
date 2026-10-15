import os
import re
import uuid
import shutil
import subprocess
import threading
import sys
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, request, jsonify, send_from_directory, abort
from werkzeug.utils import secure_filename

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB
app.config["SECRET_KEY"] = os.environ.get("MEDIACONVERT_SECRET", "change-this-local-dev-key")

ALLOWED_UPLOADS = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"
}
jobs = {}
jobs_lock = threading.Lock()


def ffmpeg_path():
    return shutil.which("ffmpeg")


def update_job(job_id, **values):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(values)


def download_authorized_direct_url(url, destination):
    """Fetch a user-authorized direct media URL, with basic SSRF protections."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("직접 미디어 URL은 HTTPS 주소만 허용합니다.")
    host = (parsed.hostname or "").lower()
    if not host or host in {"localhost"} or host.endswith(".local"):
        raise ValueError("허용되지 않는 주소입니다.")
    # Block common private/local IP literals. DNS rebinding protections require
    # a production-grade egress proxy; keep this app for local use.
    import ipaddress, socket
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError("내부 네트워크 주소는 허용되지 않습니다.")
    except ValueError as e:
        if "내부 네트워크" in str(e) or "허용되지" in str(e):
            raise
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                addr = ipaddress.ip_address(info[4][0])
                if not addr.is_global:
                    raise ValueError("내부 네트워크 주소는 허용되지 않습니다.")
        except socket.gaierror:
            raise ValueError("URL의 호스트를 확인할 수 없습니다.")

    with requests.get(url, stream=True, timeout=(10, 30), allow_redirects=False) as resp:
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        if not (content_type.startswith("video/") or content_type.startswith("audio/") or
                content_type in {"application/octet-stream", "binary/octet-stream"}):
            raise ValueError("주소가 직접 오디오/비디오 파일을 가리키는지 확인해 주세요.")
        length = int(resp.headers.get("Content-Length", "0") or 0)
        if length > 2 * 1024 * 1024 * 1024:
            raise ValueError("파일은 최대 2GB까지 처리할 수 있습니다.")
        total = 0
        with open(destination, "wb") as out:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 2 * 1024 * 1024 * 1024:
                    raise ValueError("파일은 최대 2GB까지 처리할 수 있습니다.")
                out.write(chunk)


def run_conversion(job_id, input_path, output_path, fmt, quality):
    try:
        ffmpeg = ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("FFmpeg를 찾을 수 없습니다. FFmpeg를 설치하고 PATH에 추가해 주세요.")

        update_job(job_id, status="processing", progress=5, message="미디어 정보를 확인하는 중…")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(input_path)],
            capture_output=True, text=True, timeout=30
        )
        duration = float(probe.stdout.strip()) if probe.returncode == 0 and probe.stdout.strip() else 0

        if fmt == "mp4":
            # Re-mux when possible; fall back to H.264/AAC for broad compatibility.
            cmd = [
                ffmpeg, "-y", "-i", str(input_path),
                "-map", "0:v:0?", "-map", "0:a:0?",
                "-c:v", "copy", "-c:a", "copy",
                "-movflags", "+faststart", str(output_path)
            ]
            update_job(job_id, progress=15, message="원본 스트림을 유지해 MP4로 처리하는 중…")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60)
            if result.returncode != 0:
                # Copy may fail for incompatible codecs/containers. Re-encode video to H.264.
                preset = "medium"
                crf = "18" if quality == "source" else ("23" if quality == "1080" else "25")
                scale = []
                if quality == "1080":
                    scale = ["-vf", "scale=-2:1080:force_original_aspect_ratio=decrease"]
                elif quality == "720":
                    scale = ["-vf", "scale=-2:720:force_original_aspect_ratio=decrease"]
                cmd = [
                    ffmpeg, "-y", "-i", str(input_path),
                    "-map", "0:v:0?", "-map", "0:a:0?",
                    *scale, "-c:v", "libx264", "-preset", preset, "-crf", crf,
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    str(output_path)
                ]
                update_job(job_id, progress=20, message="호환성을 위해 MP4로 인코딩하는 중…")
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60)
                if result.returncode != 0:
                    raise RuntimeError((result.stderr or "FFmpeg 변환에 실패했습니다.")[-1800:])
        elif fmt == "mp3":
            cmd = [ffmpeg, "-y", "-i", str(input_path), "-vn", "-codec:a", "libmp3lame",
                   "-q:a", "2", str(output_path)]
            update_job(job_id, progress=20, message="MP3 오디오를 만드는 중…")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60)
            if result.returncode != 0:
                raise RuntimeError((result.stderr or "FFmpeg 변환에 실패했습니다.")[-1800:])
        else:
            raise ValueError("지원하지 않는 출력 형식입니다.")

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("출력 파일이 생성되지 않았습니다.")
        update_job(job_id, status="done", progress=100, message="변환이 완료됐습니다.",
                   download=f"/download/{job_id}")
    except Exception as exc:
        update_job(job_id, status="error", progress=0, message=str(exc))
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/")
def index():
    return render_template("index.html")



def download_youtube(job_id, url, output_path, fmt, quality):
    # Run yt-dlp through the same Python interpreter that runs Flask.
    # This works even when the yt-dlp executable is not on Windows PATH.
    ytdlp = [sys.executable, "-m", "yt_dlp"]
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg가 설치되어 PATH에 등록되어 있어야 합니다.")

    template = str(UPLOAD_DIR / f"{job_id}_yt.%(ext)s")
    if fmt == "mp4":
        if quality == "1080":
            selector = "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"
        elif quality == "720":
            selector = "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        else:
            selector = "bestvideo+bestaudio/best"
        args = ytdlp + ["--no-playlist", "--ffmpeg-location", ffmpeg,
                        "-f", selector, "--merge-output-format", "mp4", "-o", template, url]
    else:
        args = ytdlp + ["--no-playlist", "--ffmpeg-location", ffmpeg,
                        "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                        "--audio-quality", "0", "-o", template, url]

    update_job(job_id, status="processing", progress=10, message="영상 정보를 확인하고 미디어를 가져오는 중…")
    result = subprocess.run(args, capture_output=True, text=True, timeout=60 * 60)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "미디어를 가져오지 못했습니다.")[-1600:])

    candidates = [p for p in UPLOAD_DIR.glob(f"{job_id}_yt.*") if p.is_file() and p.stat().st_size]
    if not candidates:
        raise RuntimeError("변환된 결과 파일을 찾을 수 없습니다.")
    wanted = [p for p in candidates if p.suffix.lower() == f".{fmt}"]
    produced = wanted[0] if wanted else candidates[0]
    shutil.move(str(produced), str(output_path))
    for p in UPLOAD_DIR.glob(f"{job_id}_yt.*"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/api/convert", methods=["POST"])
def convert():
    fmt = request.form.get("format", "mp4").lower()
    quality = request.form.get("quality", "source")
    if fmt not in {"mp4", "mp3"}:
        return jsonify(error="MP4 또는 MP3만 선택할 수 있습니다."), 400
    if quality not in {"source", "1080", "720"}:
        quality = "source"

    job_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}_input"
    output_path = OUTPUT_DIR / f"{job_id}.{fmt}"
    uploaded = request.files.get("file")
    direct_url = (request.form.get("url") or "").strip()
    allow_youtube = request.form.get("authorized_youtube") == "yes"

    parsed = urlparse(direct_url) if direct_url else None
    host = (parsed.hostname or "").lower() if parsed else ""
    is_youtube = host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"} or host.endswith(".youtube.com")

    try:
        if is_youtube:
            if parsed.scheme != "https":
                return jsonify(error="HTTPS 유튜브 링크를 입력해 주세요."), 400
            if not allow_youtube:
                return jsonify(error="유튜브 영상의 소유자이거나 다운로드 허가를 받았음을 확인해 주세요."), 400
        elif uploaded and uploaded.filename:
            ext = Path(secure_filename(uploaded.filename)).suffix.lower()
            if ext not in ALLOWED_UPLOADS:
                return jsonify(error="지원하지 않는 파일 형식입니다."), 400
            uploaded.save(input_path)
        elif direct_url:
            download_authorized_direct_url(direct_url, input_path)
        else:
            return jsonify(error="파일을 업로드하거나 미디어 URL을 입력해 주세요."), 400
    except Exception as exc:
        input_path.unlink(missing_ok=True)
        return jsonify(error=str(exc)), 400

    with jobs_lock:
        jobs[job_id] = {
            "status": "queued", "progress": 1, "message": "변환 대기 중…",
            "filename": output_path.name, "output_path": str(output_path)
        }

    if is_youtube:
        def worker():
            try:
                download_youtube(job_id, direct_url, output_path, fmt, quality)
                update_job(job_id, status="done", progress=100, message="변환이 완료됐습니다.",
                           download=f"/download/{job_id}")
            except Exception as exc:
                update_job(job_id, status="error", progress=0, message=str(exc))
        thread = threading.Thread(target=worker, daemon=True)
    else:
        thread = threading.Thread(
            target=run_conversion, args=(job_id, input_path, output_path, fmt, quality), daemon=True
        )
    thread.start()
    return jsonify(job_id=job_id), 202


@app.route("/api/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="작업을 찾을 수 없습니다."), 404
        return jsonify({k: v for k, v in job.items() if k not in {"output_path", "filename"}})


@app.route("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "done":
            abort(404)
        path = Path(job["output_path"])
    if not path.exists():
        abort(404)
    return send_from_directory(path.parent, path.name, as_attachment=True)


@app.errorhandler(413)
def too_large(_):
    return jsonify(error="파일은 최대 2GB까지 업로드할 수 있습니다."), 413


if __name__ == "__main__":
    # Local development only. Do not expose this debug server to the public internet.
    app.run(host="127.0.0.1", port=5000, debug=False)
