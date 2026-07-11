import collections
import glob
import hmac
import ipaddress
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import urllib.parse
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
DOWNLOAD_DIR = os.environ.get("DOWNLOADS_PATH", os.path.join(os.path.dirname(__file__), "downloads"))
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
DOWNLOAD_DIR_RESOLVED = Path(DOWNLOAD_DIR).resolve()

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 3))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", 900))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1000"))
MAX_FILESIZE = os.environ.get("MAX_FILESIZE", "2G")
MIN_FREE_DISK_MB = int(os.environ.get("MIN_FREE_DISK_MB", "1024"))
download_semaphore = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# C2 — shared-secret token (fail-closed)
_RECLIP_API_TOKEN = os.environ.get("RECLIP_API_TOKEN", "")
if not _RECLIP_API_TOKEN:
    raise RuntimeError(
        "RECLIP_API_TOKEN environment variable is required. "
        "Generate one with: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
    )

# H2 — bounded LRU job registry
jobs: "collections.OrderedDict[str, dict]" = collections.OrderedDict()

PROGRESS_TEMPLATE = (
    'download:{"downloaded_bytes":%(progress.downloaded_bytes)s,'
    '"total_bytes":%(progress.total_bytes)s,'
    '"speed":%(progress.speed)s,'
    '"eta":%(progress.eta)s}'
)

# ---------------------------------------------------------------------------
# URL validation (C1 — SSRF guard)
# ---------------------------------------------------------------------------

ALLOWED_URL_SCHEMES = {"http", "https"}
_BLOCKED_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _validate_url(url: str) -> None:
    """Raise ValueError if `url` is not a safe outbound target."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() not in ALLOWED_URL_SCHEMES:
        raise ValueError(f"scheme {parsed.scheme!r} not allowed")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL must have a hostname")
    # Reject bare IPv4/IPv6 literals early so we don't DNS-resolve nonsense.
    try:
        literal = ipaddress.ip_address(hostname)
        infos = [(None, None, None, None, (str(literal),))]
    except ValueError:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            raise ValueError(f"DNS resolution failed: {e}") from e
    for entry in infos:
        sockaddr = entry[4]
        ip = ipaddress.ip_address(sockaddr[0])
        # Normalize IPv4-mapped IPv6 (::ffff:127.0.0.1) so it hits the IPv4
        # deny-list entries instead of slipping past as an IPv6 address.
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        for net in _BLOCKED_NETS:
            if ip in net:
                raise ValueError(f"blocked destination: {ip}")


# ---------------------------------------------------------------------------
# Auth (C2)
# ---------------------------------------------------------------------------


def _require_token(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        expected = _RECLIP_API_TOKEN
        provided = request.headers.get("X-Reclip-Token", "")
        if not hmac.compare_digest(expected, provided):
            abort(401)
        return func(*args, **kwargs)

    return wrapper


# ---------------------------------------------------------------------------
# M4 / M5 — input whitelists
# ---------------------------------------------------------------------------

FORMAT_ID_RE = re.compile(r"^[A-Za-z0-9_+\-]{1,40}$")
TITLE_RE = re.compile(r"[A-Za-z0-9._\- ]+")
JOB_ID_RE = re.compile(r"^[a-f0-9]{1,32}$")


def _sanitize_title(title: str) -> str:
    cleaned = "".join(TITLE_RE.findall(title or ""))[:200]
    return cleaned.strip() or "download"


def _sanitize_thumbnail(url: str) -> str:
    """Whitelist https/http scheme; reject javascript:, data: (other than images), etc."""
    if not url:
        return ""
    if not isinstance(url, str):
        return ""
    if url.lower().startswith(("https://", "http://")):
        return url
    return ""


def _sanitize_format_id(fid: object) -> str | None:
    if fid is None:
        return None
    if not isinstance(fid, str):
        return None
    return fid if FORMAT_ID_RE.match(fid) else None


# ---------------------------------------------------------------------------
# H1 — yt-dlp stderr sanitization
# ---------------------------------------------------------------------------

_GENERIC_ERROR_BY_CODE = {
    1: "download failed",
    2: "invalid URL or unsupported site",
    -9: "download timed out",
}
_BENIGN_STDERR_RE = re.compile(r"^\[?(?:generic|info|warning)\]?[^\n]{1,200}$")


def _friendly_error(returncode: int, stderr_tail: str) -> str:
    if returncode in _GENERIC_ERROR_BY_CODE:
        return _GENERIC_ERROR_BY_CODE[returncode]
    tail = (stderr_tail or "").strip()
    if tail and _BENIGN_STDERR_RE.match(tail):
        return tail[:200]
    return "internal error"


# ---------------------------------------------------------------------------
# H2 — LRU eviction
# ---------------------------------------------------------------------------


def _register_job(job_id: str, entry: dict) -> None:
    while len(jobs) >= MAX_JOBS:
        old_id, old = jobs.popitem(last=False)
        # best-effort cleanup of evicted job
        f = old.get("file")
        if f:
            try:
                os.remove(f)
            except OSError:
                pass
    jobs[job_id] = entry
    jobs.move_to_end(job_id)


def _active_download_count() -> int:
    return sum(1 for j in jobs.values() if j.get("status") == "downloading")


# ---------------------------------------------------------------------------
# H4 — process group kill
# ---------------------------------------------------------------------------


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except Exception:
            pass
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# L2 — security headers
# ---------------------------------------------------------------------------


@app.after_request
def _security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "interest-cohort=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "img-src https: data:; "
        "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
        "font-src https://fonts.gstatic.com; "
        "script-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'"
    )
    return resp


# ---------------------------------------------------------------------------
# download runner
# ---------------------------------------------------------------------------


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]

    if not download_semaphore.acquire(timeout=30):
        job["status"] = "error"
        job["error"] = "Too many concurrent downloads, please try again later"
        return

    try:
        _do_download(job_id, url, format_choice, format_id)
    finally:
        download_semaphore.release()


def _do_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = [
        "yt-dlp", "--no-playlist", "-o", out_template,
        "--progress-template", PROGRESS_TEMPLATE,
        "--force-ipv4",
        "--downloader", "hls:ffmpeg",
        "--concurrent-fragments", "4",
        "--socket-timeout", "20",
        "--retries", "5",
        "--fragment-retries", "10",
        "--throttled-rate", "50K",
        "--max-filesize", MAX_FILESIZE,
    ]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bv*[vcodec~='^(avc|h264)']+ba/b[vcodec~='^(avc|h264)']/bv*+ba/b", "--merge-output-format", "mp4"]

    cmd.append(url)

    process = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )

    timed_out = threading.Event()

    def _kill_on_timeout():
        timed_out.set()
        _kill_process_group(process)

    deadline_timer = threading.Timer(DOWNLOAD_TIMEOUT, _kill_on_timeout)
    deadline_timer.daemon = True
    deadline_timer.start()

    stderr_lines = []

    def _read_stderr():
        try:
            for line in process.stderr:
                line = line.rstrip("\n")
                stderr_lines.append(line)
                try:
                    json_str = line.removeprefix("download:")
                    progress_data = json.loads(json_str)
                    total = progress_data.get("total_bytes")
                    downloaded = progress_data.get("downloaded_bytes")
                    percent = None
                    if total and downloaded and total > 0:
                        percent = round(downloaded / total * 100, 1)
                    job["progress"] = {
                        "percent": percent,
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "speed": progress_data.get("speed"),
                        "eta": progress_data.get("eta"),
                    }
                except (json.JSONDecodeError, ValueError):
                    pass
        except Exception:
            pass

    stderr_reader = threading.Thread(target=_read_stderr, daemon=True)
    stderr_reader.start()

    try:
        returncode = process.wait()
    except Exception:
        returncode = -1
    finally:
        deadline_timer.cancel()
        stderr_reader.join(timeout=5)
        # Reap any zombies if process.wait() was bypassed
        try:
            process.wait(timeout=1)
        except Exception:
            pass

    if timed_out.is_set():
        job["status"] = "error"
        job["error"] = f"Download timed out ({DOWNLOAD_TIMEOUT // 60} min limit)"
        return

    # H1 — sanitized stderr surface
    try:
        if returncode != 0:
            job["status"] = "error"
            job["error"] = _friendly_error(returncode, stderr_lines[-1] if stderr_lines else "")
            return

        files = _glob_job_files(job_id)
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        if chosen.endswith(".mp4"):
            try:
                codec_probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name",
                     "-of", "default=noprint_wrappers=1:nokey=1", chosen],
                    capture_output=True, text=True, timeout=10,
                )
                vcodec = (codec_probe.stdout or "").strip().lower()
            except Exception:
                vcodec = ""

            if vcodec in ("av1", "vp9", "vp8"):
                transcoded = chosen + ".h264.mp4"
                try:
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-i", chosen,
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                         "-c:a", "aac", "-b:a", "128k",
                         "-movflags", "+faststart",
                         transcoded],
                        capture_output=True, timeout=600,
                    )
                    if r.returncode == 0 and os.path.exists(transcoded) and os.path.getsize(transcoded) > 0:
                        os.replace(transcoded, chosen)
                    elif os.path.exists(transcoded):
                        os.remove(transcoded)
                except (subprocess.TimeoutExpired, OSError):
                    if os.path.exists(transcoded):
                        try:
                            os.remove(transcoded)
                        except OSError:
                            pass
            else:
                faststart_tmp = chosen + ".fs.mp4"
                try:
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", chosen, "-c", "copy",
                         "-movflags", "+faststart", faststart_tmp],
                        capture_output=True, timeout=120,
                    )
                    if os.path.exists(faststart_tmp) and os.path.getsize(faststart_tmp) > 0:
                        os.replace(faststart_tmp, chosen)
                    elif os.path.exists(faststart_tmp):
                        os.remove(faststart_tmp)
                except (subprocess.TimeoutExpired, OSError):
                    if os.path.exists(faststart_tmp):
                        try:
                            os.remove(faststart_tmp)
                        except OSError:
                            pass

        if chosen.endswith(".mp4"):
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height:format=duration",
                     "-of", "json", chosen],
                    capture_output=True, text=True, timeout=10,
                )
                info = json.loads(probe.stdout)
                stream = (info.get("streams") or [{}])[0]
                fmt = info.get("format") or {}
                job["width"] = stream.get("width")
                job["height"] = stream.get("height")
                dur = fmt.get("duration")
                job["duration"] = float(dur) if dur else None
            except Exception:
                pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        if title:
            safe_title = _sanitize_title(title)
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except (OSError, ValueError, KeyError, IndexError) as e:
        logger.exception("download processing error")
        job["status"] = "error"
        job["error"] = "internal error"
    except subprocess.SubprocessError as e:
        logger.exception("subprocess error during download")
        job["status"] = "error"
        job["error"] = "internal error"
    except Exception:
        logger.exception("unexpected error during download")
        job["status"] = "error"
        job["error"] = "internal error"


def _glob_job_files(job_id: str) -> list[str]:
    return glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.route("/")
@_require_token
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
@_require_token
def get_info():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        _validate_url(url)
    except ValueError as e:
        return jsonify({"error": "URL not allowed"}), 400

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": "Could not fetch video info"}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            safe_fid = _sanitize_format_id(f.get("format_id"))
            if not safe_fid:
                continue
            formats.append({
                "id": safe_fid,
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": _sanitize_title(info.get("title", "")),
            "thumbnail": _sanitize_thumbnail(info.get("thumbnail", "")),
            "duration": info.get("duration"),
            "uploader": (info.get("uploader") or "")[:200],
            "extractor": (info.get("extractor") or "")[:64],
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("info fetch failed: %s", e)
        return jsonify({"error": "internal error"}), 400
    except Exception:
        logger.exception("unexpected error in /api/info")
        return jsonify({"error": "internal error"}), 400


@app.route("/api/download", methods=["POST"])
@_require_token
def start_download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    format_choice = data.get("format", "video")
    format_id = _sanitize_format_id(data.get("format_id"))
    title = (data.get("title") or "")[:200]

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if format_choice not in ("video", "audio"):
        return jsonify({"error": "invalid format"}), 400

    try:
        _validate_url(url)
    except ValueError:
        return jsonify({"error": "URL not allowed"}), 400

    # M8 — reject when at concurrency cap
    if _active_download_count() >= MAX_CONCURRENT_DOWNLOADS:
        return jsonify({"error": "server busy, retry later"}), 429

    # H3 — disk precheck
    try:
        free_mb = shutil.disk_usage(DOWNLOAD_DIR).free // (1024 * 1024)
        if free_mb < MIN_FREE_DISK_MB:
            return jsonify({"error": "insufficient disk space"}), 507
    except OSError:
        logger.warning("disk_usage check failed; proceeding")

    job_id = uuid.uuid4().hex[:10]
    _register_job(job_id, {"status": "downloading", "url": url, "title": title})

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
@_require_token
def check_status(job_id):
    if not JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "Job not found"}), 404
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    response = {
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress"),
        "width": job.get("width"),
        "height": job.get("height"),
        "duration": job.get("duration"),
    }
    if job.get("file"):
        response["file"] = os.path.basename(job["file"])
    return jsonify(response)


@app.route("/api/file/<job_id>")
@_require_token
def download_file(job_id):
    if not JOB_ID_RE.match(job_id or ""):
        return jsonify({"error": "File not ready"}), 404
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    # M6 — defense-in-depth: ensure the resolved path stays inside DOWNLOAD_DIR
    try:
        resolved = Path(job["file"]).resolve()
    except (OSError, RuntimeError):
        return jsonify({"error": "File not ready"}), 404
    if not resolved.is_relative_to(DOWNLOAD_DIR_RESOLVED):
        return jsonify({"error": "Forbidden"}), 403
    return send_file(str(resolved), as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
