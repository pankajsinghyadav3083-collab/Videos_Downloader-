import ipaddress
import os
import shutil
import socket
import tempfile
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_file
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import yt_dlp

app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

HAS_FFMPEG = shutil.which("ffmpeg") is not None
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", 60))   # longest video allowed
MAX_ACTIVE = int(os.environ.get("MAX_ACTIVE", 2))      # downloads running at once
JOB_TTL = 15 * 60                                      # keep finished files 15 min

jobs = {}
lock = threading.Lock()


# ---------- helpers ----------
def check_url(url):
    """Allow only public http(s) URLs (stops the server reaching local networks)."""
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Enter a full link starting with http:// or https://")
    try:
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise ValueError("That address is not allowed.")
    except socket.gaierror:
        raise ValueError("Could not find that website.")
    return url


def pick_format(quality):
    if quality in ("audio", "mp3"):
        return "bestaudio/best"
    if quality.isdigit():
        h = int(quality)
        if HAS_FFMPEG:
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]"
        return f"best[height<={h}][ext=mp4]/best[height<={h}]"
    return "bestvideo+bestaudio/best" if HAS_FFMPEG else "best[ext=mp4]/best"


def fmt_speed(bps):
    return f"{bps / 1_048_576:.1f} MB/s" if bps else ""


def purge():
    """Delete old jobs and their files."""
    now = time.time()
    with lock:
        for key in list(jobs):
            job = jobs[key]
            age = now - job["created"]
            finished = job["status"] in ("done", "error")
            if (finished and age > JOB_TTL) or age > 3600:
                shutil.rmtree(job["tmp"], ignore_errors=True)
                del jobs[key]


def run_job(job_id, url, quality):
    job = jobs[job_id]
    seen = []

    def on_progress(d):
        if d["status"] != "downloading":
            return
        name = d.get("filename")
        if name not in seen:
            seen.append(name)
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        frac = (d.get("downloaded_bytes", 0) / total) if total else 0
        parts = len((d.get("info_dict") or {}).get("requested_formats") or [1])
        percent = ((seen.index(name) + frac) / parts) * 100
        job.update(status="downloading", percent=min(98, round(percent)),
                   speed=fmt_speed(d.get("speed")), eta=d.get("eta"))

    def on_postprocess(d):
        if d["status"] == "started":
            job.update(status="processing", percent=99, speed="", eta=None)

    opts = {
        "quiet": True,
        "noplaylist": True,
        "format": pick_format(quality),
        "outtmpl": os.path.join(job["tmp"], "%(title).80s.%(ext)s"),
        "restrictfilenames": True,
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_postprocess],
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <? {MAX_MINUTES * 60}"),
    }
    if HAS_FFMPEG and quality not in ("audio", "mp3"):
        opts["merge_output_format"] = "mp4"
    if quality == "mp3":
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [f for f in os.listdir(job["tmp"]) if not f.endswith((".part", ".ytdl"))]
        if not files:
            raise RuntimeError(f"No file was produced. Videos longer than {MAX_MINUTES} minutes are not allowed.")
        best = max(files, key=lambda f: os.path.getsize(os.path.join(job["tmp"], f)))
        job.update(status="done", percent=100, file=os.path.join(job["tmp"], best), speed="", eta=None)
    except yt_dlp.utils.DownloadError:
        job.update(status="error", error="Could not download this video.")
    except Exception as e:
        job.update(status="error", error=str(e) or "Download failed.")


# ---------- routes ----------
@app.get("/")
def index():
    return render_template("index.html", has_ffmpeg=HAS_FFMPEG, max_minutes=MAX_MINUTES)


@app.post("/api/info")
@limiter.limit("20 per minute")
def info():
    try:
        url = check_url((request.get_json(silent=True) or {}).get("url"))
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True, "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
        heights = sorted(
            {f["height"] for f in data.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
            reverse=True,
        )
        return jsonify(
            title=data.get("title", "Untitled"),
            thumbnail=data.get("thumbnail"),
            duration=data.get("duration"),
            uploader=data.get("uploader"),
            heights=heights,
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except yt_dlp.utils.DownloadError:
        return jsonify(error="No downloadable video found at that link."), 422
    except Exception:
        return jsonify(error="Something went wrong. Try again."), 500


@app.post("/api/start")
@limiter.limit("6 per minute")
def start():
    body = request.get_json(silent=True) or {}
    try:
        url = check_url(body.get("url"))
    except ValueError as e:
        return jsonify(error=str(e)), 400

    quality = str(body.get("quality", "best"))
    if quality not in ("best", "audio", "mp3") and not quality.isdigit():
        return jsonify(error="Invalid quality."), 400
    if quality == "mp3" and not HAS_FFMPEG:
        return jsonify(error="MP3 needs FFmpeg installed on the server. Install it, or pick 'Audio only (original)'."), 400

    purge()
    with lock:
        active = sum(1 for j in jobs.values() if j["status"] in ("queued", "downloading", "processing"))
        if active >= MAX_ACTIVE:
            return jsonify(error="The server is busy. Try again in a minute."), 503
        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "status": "queued", "percent": 0, "speed": "", "eta": None,
            "error": None, "file": None, "created": time.time(),
            "tmp": tempfile.mkdtemp(prefix="vd_"),
        }
    threading.Thread(target=run_job, args=(job_id, url, quality), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Download not found. Start it again."), 404
    return jsonify({k: job[k] for k in ("status", "percent", "speed", "eta", "error")})


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done" or not job["file"]:
        return jsonify(error="File not ready or expired."), 404
    return send_file(job["file"], as_attachment=True)


@app.errorhandler(429)
def too_many(_):
    return jsonify(error="Too many requests. Wait a minute and try again."), 429


if __name__ == "__main__":
    app.run(debug=True)
