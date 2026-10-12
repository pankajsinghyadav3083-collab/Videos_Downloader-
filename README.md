# Clipcatch – video downloader (Flask + yt-dlp)

Features: quality picker, MP3 conversion, live progress bar, light/dark mode,
recent-links history, rate limiting, max video length.

## Run it
```bash
python -m venv venv
venv\Scripts\activate        # Windows  (Mac/Linux: source venv/bin/activate)
pip install -r requirements.txt
python app.py
```
Open http://127.0.0.1:5000

## FFmpeg (needed for MP3 and 1080p+)
Windows: `winget install ffmpeg`, then open a new terminal.

## Settings (environment variables)
- `MAX_MINUTES` longest video allowed (default 60)
- `MAX_ACTIVE`  downloads running at once (default 2)

## Keep yt-dlp updated
`pip install -U yt-dlp`

## Deploy (Render)
Build: `pip install -r requirements.txt`   Start: `gunicorn app:app --workers 1 --threads 4`
(Use 1 worker: download progress is kept in memory.)

## How it works
1. `POST /api/info`  – title, thumbnail, qualities
2. `POST /api/start` – begins a background download, returns a job id
3. `GET /api/progress/<id>` – polled by the page for the progress bar
4. `GET /api/file/<id>` – the finished file; old files are deleted after 15 min
