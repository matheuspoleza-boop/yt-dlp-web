import base64
import glob
import logging
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Download jobs live in SQLite so all gunicorn workers see the same state.
# In-memory dicts are per-worker, so a /download on worker A is invisible
# to /status on worker B; a single file on /tmp fixes that.
JOBS_DB_PATH = '/tmp/jobs.db'

# DIAGNÓSTICO TEMPORÁRIO — confirma que `node` está no PATH do container
# após o commit que adicionou nodejs ao apt install. Remover junto do fix
# definitivo de n-sig challenge.
try:
    _node_v = subprocess.run(
        ['node', '--version'], capture_output=True, text=True, timeout=5,
    )
    logger.info(
        'Node runtime detected: %s',
        _node_v.stdout.strip() or _node_v.stderr.strip() or '(empty output)',
    )
except FileNotFoundError:
    logger.error('Node runtime NOT found on PATH — n-sig challenge will fail')
except Exception as _exc:
    logger.error('Node runtime check failed: %s', _exc)

# bgutil-ytdlp-pot-provider sidecar. Override via Railway env var if the
# sidecar service is renamed; default matches the service name agreed for
# this project's Railway setup.
BGUTIL_POT_BASE_URL = os.environ.get(
    'BGUTIL_POT_BASE_URL',
    'http://bgutil-pot-provider.railway.internal:4416',
)
logger.info('bgutil POT provider base URL: %s', BGUTIL_POT_BASE_URL)

# YouTube cookies file. If present at this path, yt-dlp uses it to bypass
# the "Sign in to confirm you're not a bot" check that fires on Railway's
# datacenter IPs. Each user-remixer must provide their own cookies.txt
# (extracted from a disposable YouTube account in their browser). bgutil
# remains as defense-in-depth — both can run in parallel.
COOKIES_PATH = os.environ.get('YTDLP_COOKIES_PATH', '/app/cookies.txt')

# Convenience for Railway-style deploys without shell access: if the
# cookies file content is provided base64-encoded in YTDLP_COOKIES_BASE64,
# decode it to COOKIES_PATH at startup. Lets the user-remixer paste the
# base64 string into the Railway dashboard's Variables panel instead of
# mounting a volume and uploading the file via CLI. Decode failures are
# logged but non-fatal — the worker still boots.
_cookies_b64 = os.environ.get('YTDLP_COOKIES_BASE64', '').strip()
if _cookies_b64:
    try:
        _decoded = base64.b64decode(_cookies_b64)
        _parent = os.path.dirname(COOKIES_PATH)
        if _parent:
            os.makedirs(_parent, exist_ok=True)
        with open(COOKIES_PATH, 'wb') as _fh:
            _fh.write(_decoded)
        os.chmod(COOKIES_PATH, 0o600)
        logger.info(
            'YouTube cookies decoded from YTDLP_COOKIES_BASE64 to %s (%d bytes)',
            COOKIES_PATH, len(_decoded),
        )
    except Exception as _exc:
        logger.error('Failed to decode YTDLP_COOKIES_BASE64: %s', _exc)

if os.path.isfile(COOKIES_PATH):
    logger.info('YouTube cookies file found at %s', COOKIES_PATH)
else:
    logger.info(
        'YouTube cookies file NOT found at %s — downloads will likely '
        'fail with bot check. See README for setup.',
        COOKIES_PATH,
    )


def _jobs_db():
    conn = sqlite3.connect(JOBS_DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _init_jobs_db():
    conn = _jobs_db()
    try:
        # Switching journal_mode to WAL needs an exclusive DB lock. With
        # 4 gunicorn workers calling _init_jobs_db concurrently on boot,
        # all but one race losers get "database is locked" and the worker
        # dies, which crashes the master and triggers a full gunicorn
        # restart. WAL is persistent across connections, so the winner's
        # change sticks for everyone — losing the race is harmless and
        # we tolerate it explicitly. CREATE TABLE IF NOT EXISTS is
        # concurrency-safe and stays outside this guard.
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except sqlite3.OperationalError as exc:
            logger.warning(
                'pid=%d journal_mode=WAL race during init (%s); continuing — '
                'another worker is converting the DB',
                os.getpid(), exc,
            )
        conn.execute('''
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                filepath TEXT,
                filename TEXT,
                error TEXT,
                created_at REAL DEFAULT (strftime('%s','now'))
            )
        ''')
        conn.commit()
    finally:
        conn.close()


_init_jobs_db()


def set_job(job_id, status, filepath=None, filename=None, error=None):
    conn = _jobs_db()
    try:
        conn.execute(
            'INSERT OR REPLACE INTO jobs (id, status, filepath, filename, error) '
            'VALUES (?, ?, ?, ?, ?)',
            (job_id, status, filepath, filename, error),
        )
        conn.commit()
    finally:
        conn.close()


def get_job(job_id):
    conn = _jobs_db()
    try:
        row = conn.execute(
            'SELECT status, filepath, filename, error FROM jobs WHERE id = ?',
            (job_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {k: row[k] for k in ('status', 'filepath', 'filename', 'error') if row[k] is not None}


def cleanup_old_files():
    """Remove files older than 1 hour to save disk."""
    while True:
        time.sleep(300)
        now = time.time()
        for dirpath in glob.glob(os.path.join(DOWNLOAD_DIR, '*')):
            if os.path.isdir(dirpath):
                for f in glob.glob(os.path.join(dirpath, '*')):
                    if now - os.path.getmtime(f) > 3600:
                        os.remove(f)
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)


threading.Thread(target=cleanup_old_files, daemon=True).start()


def run_download(job_id, url, format_type):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    cmd = [
        'yt-dlp',
        '--no-playlist',
        '--restrict-filenames',
        '-o', os.path.join(job_dir, '%(title)s.%(ext)s'),
    ]

    if format_type == 'audio':
        cmd += ['-x', '--audio-format', 'mp3', '--audio-quality', '0']
    else:
        cmd += ['-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '--merge-output-format', 'mp4']

    cmd += ['--extractor-args', 'youtube:player_client=web']
    cmd += ['--extractor-args', f'youtubepot-bgutilhttp:base_url={BGUTIL_POT_BASE_URL}']

    if os.path.isfile(COOKIES_PATH):
        cmd += ['--cookies', COOKIES_PATH]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            set_job(job_id, status='error', error=result.stderr[-500:])
            return

        files = glob.glob(os.path.join(job_dir, '*'))
        if files:
            set_job(job_id, status='done', filepath=files[0],
                    filename=os.path.basename(files[0]))
        else:
            set_job(job_id, status='error', error='Nenhum arquivo gerado.')
    except subprocess.TimeoutExpired:
        set_job(job_id, status='error', error='Download excedeu o tempo limite (5 min).')
    except Exception as e:
        set_job(job_id, status='error', error=str(e))


def extract_frames_from_video(video_path, output_dir):
    """Use ffmpeg to extract 1 frame per second from a video file."""
    output_pattern = os.path.join(output_dir, 'frame_%06d.png')
    cmd = [
        'ffmpeg',
        '-i', video_path,
        '-vf', 'fps=1',
        '-vsync', 'vfr',
        '-f', 'image2',
        output_pattern,
        '-y',
    ]
    logger.info('Running ffmpeg: %s', ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        logger.error('ffmpeg stderr: %s', result.stderr[-1000:])
        raise RuntimeError(
            'ffmpeg exited with code {}: {}'.format(result.returncode, result.stderr[-500:])
        )

    frame_files = sorted(glob.glob(os.path.join(output_dir, 'frame_*.png')))
    frames = []
    for path in frame_files:
        basename = os.path.basename(path)
        match = re.search(r'frame_(\d+)\.png$', basename)
        index = int(match.group(1)) if match else 1
        timestamp = float(index - 1)
        frames.append((timestamp, path))
    return frames


def png_to_base64(file_path):
    """Read a PNG file and return a data-URI base64 string."""
    with open(file_path, 'rb') as fh:
        encoded = base64.b64encode(fh.read()).decode('utf-8')
    return 'data:image/png;base64,' + encoded


def extract_audio_from_video(video_path, output_path):
    """Use ffmpeg to extract audio from a video file and encode it as MP3."""
    cmd = [
        'ffmpeg',
        '-i', video_path,
        '-q:a', '0',
        '-map', 'a',
        output_path,
        '-y',
    ]
    logger.info('Running ffmpeg audio extraction: %s', ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        logger.error('ffmpeg stderr: %s', result.stderr[-1000:])
        raise RuntimeError(
            'ffmpeg exited with code {}: {}'.format(result.returncode, result.stderr[-500:])
        )


@app.route('/health')
def health():
    return jsonify({'status': 'ok'})


@app.route('/download', methods=['POST'])
def download():
    data = request.get_json(silent=True) or {}
    url = data.get('url', '').strip()
    format_type = data.get('format', 'video')

    if not url:
        return jsonify({'error': 'URL obrigatoria'}), 400

    if not any(domain in url for domain in ['youtube.com', 'youtu.be']):
        return jsonify({'error': 'Apenas URLs do YouTube sao aceitas.'}), 400

    job_id = uuid.uuid4().hex[:12]
    set_job(job_id, status='downloading')

    thread = threading.Thread(target=run_download, args=(job_id, url, format_type))
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def status(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job nao encontrado'}), 404

    safe = {k: v for k, v in job.items() if k != 'filepath'}
    return jsonify(safe)


@app.route('/get/<job_id>')
def get_file(job_id):
    job = get_job(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Arquivo nao disponivel'}), 404
    filepath = job.get('filepath')
    if not filepath or not os.path.isfile(filepath):
        return jsonify({'error': 'Arquivo nao disponivel'}), 404
    return send_file(filepath, as_attachment=True, download_name=job['filename'])


@app.route('/extract-frames', methods=['POST'])
def extract_frames():
    data = request.get_json(silent=True) or {}

    url = (data.get('url') or '').strip()
    timestamps = data.get('timestamps')
    if url and isinstance(timestamps, list) and len(timestamps) > 0:
        try:
            ts_list = sorted([float(t) for t in timestamps])
        except (TypeError, ValueError):
            return jsonify({'error': 'timestamps must be numbers'}), 400

        if len(ts_list) == 0:
            return jsonify({'frames': []})

        quality = int(data.get('quality') or 80)
        qscale = max(2, min(31, int(31 - (quality * 29 / 100))))

        t_start = ts_list[0]
        t_end = ts_list[-1]

        # Detect evenly-spaced timestamps (video-track-person always sends these)
        use_fps = False
        avg_delta = 1.0
        if len(ts_list) > 1:
            deltas = [ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)]
            avg_delta = sum(deltas) / len(deltas)
            if avg_delta > 0 and all(abs(d - avg_delta) < 0.15 for d in deltas):
                use_fps = True

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                if use_fps:
                    # FAST PATH: one ffmpeg call with fps filter
                    fps_rate = 1.0 / avg_delta
                    duration = (t_end - t_start) + avg_delta * 0.5
                    output_pattern = os.path.join(tmp_dir, 'frame_%06d.jpg')
                    cmd = [
                        'ffmpeg',
                        '-hide_banner', '-loglevel', 'error',
                        '-ss', str(t_start),
                        '-i', url,
                        '-t', str(duration),
                        '-an',
                        '-vf', f'fps={fps_rate:.6f},scale=640:-2',
                        '-q:v', str(qscale),
                        '-f', 'image2',
                        output_pattern,
                        '-y',
                    ]
                    logger.info(
                        'Extracting %d frames in ONE call (fps=%.3f, duration=%.1fs)',
                        len(ts_list), fps_rate, duration,
                    )
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
                    if result.returncode == 0:
                        frame_files = sorted(glob.glob(os.path.join(tmp_dir, 'frame_*.jpg')))
                        frames_b64 = []
                        for path in frame_files[:len(ts_list)]:
                            with open(path, 'rb') as fh:
                                frames_b64.append(base64.b64encode(fh.read()).decode('utf-8'))
                        logger.info('Got %d/%d frames via single call', len(frames_b64), len(ts_list))
                        return jsonify({'frames': frames_b64})
                    else:
                        logger.warning(
                            'Single-call fps extraction failed (%s), falling back to per-frame',
                            result.stderr[-300:],
                        )

                # FALLBACK: one ffmpeg call per timestamp
                frames_b64 = []
                for i, t in enumerate(ts_list):
                    frame_path = os.path.join(tmp_dir, f'frame_{i:06d}.jpg')
                    cmd = [
                        'ffmpeg',
                        '-hide_banner', '-loglevel', 'error',
                        '-ss', str(t),
                        '-i', url,
                        '-frames:v', '1',
                        '-an',
                        '-vf', 'scale=640:-2',
                        '-q:v', str(qscale),
                        '-f', 'image2',
                        frame_path,
                        '-y',
                    ]
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    except subprocess.TimeoutExpired:
                        logger.warning('Frame at t=%.2fs timed out', t)
                        continue
                    if result.returncode != 0:
                        logger.warning('Frame at t=%.2fs failed: %s', t, result.stderr[-200:])
                        continue
                    if os.path.isfile(frame_path):
                        with open(frame_path, 'rb') as fh:
                            frames_b64.append(base64.b64encode(fh.read()).decode('utf-8'))
                logger.info('Per-frame fallback: got %d/%d', len(frames_b64), len(ts_list))
                return jsonify({'frames': frames_b64})

        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Frame extraction timed out'}), 500
        except Exception as exc:
            logger.exception('URL-mode extract-frames failed')
            return jsonify({'error': str(exc)}), 500

    # ---- job_id mode (unchanged, keeps YouTube flow working) ----
    job_id = data.get('job_id', '').strip()
    if not job_id:
        return jsonify({'error': 'job_id or url+timestamps is required'}), 400
    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job.get('status') != 'done':
        return jsonify({'error': 'Job is not complete (status: {})'.format(job.get('status'))}), 400
    video_path = job.get('filepath')
    if not video_path or not os.path.isfile(video_path):
        return jsonify({'error': 'Video file not available'}), 404

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            frame_list = extract_frames_from_video(video_path, tmp_dir)
            if not frame_list:
                return jsonify({'error': 'No frames extracted from video'}), 500
            frames_payload = []
            for timestamp, frame_path in frame_list:
                try:
                    data_uri = png_to_base64(frame_path)
                    frames_payload.append({'timestamp': timestamp, 'frame': data_uri})
                finally:
                    try:
                        os.remove(frame_path)
                    except OSError:
                        pass
        return jsonify({'status': 'success', 'frames': frames_payload})
    except RuntimeError as exc:
        return jsonify({'error': 'Frame extraction failed: {}'.format(exc)}), 500
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Frame extraction timed out'}), 500
    except Exception as exc:
        return jsonify({'error': 'Unexpected error: {}'.format(exc)}), 500


@app.route('/extract-audio', methods=['POST'])
def extract_audio():
    data = request.get_json(silent=True) or {}

    # URL mode: stream audio directly from a remote URL via ffmpeg, without
    # any prior yt-dlp download. Used for files uploaded directly to Supabase
    # Storage, which have no job_id.
    url = (data.get('url') or '').strip()
    if url and not data.get('job_id'):
        cmd = [
            'ffmpeg',
            '-hide_banner', '-loglevel', 'error',
            '-i', url,
            '-vn',
            '-acodec', 'libmp3lame',
            '-ab', '128k',
            '-ar', '44100',
            '-f', 'mp3',
            'pipe:1',
        ]
        logger.info('Streaming audio extraction from URL (length=%d)', len(url))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def generate():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    yield chunk
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                if proc.returncode and proc.returncode != 0:
                    err = proc.stderr.read().decode('utf-8', errors='ignore')[:500]
                    logger.error('[extract-audio] ffmpeg failed: %s', err)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

        return Response(generate(), mimetype='audio/mpeg')

    # job_id mode: reuse a file previously downloaded via /download.
    job_id = data.get('job_id', '').strip()

    if not job_id:
        return jsonify({'error': 'job_id or url is required'}), 400

    job = get_job(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job.get('status') != 'done':
        return jsonify({'error': 'Job is not complete (status: {})'.format(job.get('status'))}), 400

    video_path = job.get('filepath')
    if not video_path or not os.path.isfile(video_path):
        return jsonify({'error': 'Video file not available'}), 404

    audio_path = os.path.splitext(video_path)[0] + '_audio.mp3'

    try:
        if not os.path.isfile(audio_path):
            logger.info('Extracting audio for job %s from %s', job_id, video_path)
            extract_audio_from_video(video_path, audio_path)
            logger.info('Audio extraction complete for job %s -> %s', job_id, audio_path)
        else:
            logger.info('Reusing cached audio for job %s: %s', job_id, audio_path)

        download_name = os.path.splitext(job.get('filename', 'audio'))[0] + '.mp3'
        return send_file(
            audio_path,
            mimetype='audio/mpeg',
            as_attachment=True,
            download_name=download_name,
        )

    except RuntimeError as exc:
        logger.error('Audio extraction failed for job %s: %s', job_id, exc)
        return jsonify({'error': 'Audio extraction failed: {}'.format(exc)}), 500
    except subprocess.TimeoutExpired:
        logger.error('ffmpeg timed out during audio extraction for job %s', job_id)
        return jsonify({'error': 'Audio extraction timed out'}), 500
    except Exception as exc:
        logger.exception('Unexpected error extracting audio for job %s', job_id)
        return jsonify({'error': 'Unexpected error: {}'.format(exc)}), 500


@app.route('/trim', methods=['POST'])
def trim():
    """Trim + reframe a remote video using FFmpeg, return MP4 (sync)."""
    import urllib.request

    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    try:
        start = float(data.get('start') or 0)
        duration = float(data.get('duration') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'start and duration must be numbers'}), 400

    vf = (data.get('vf') or '').strip()
    padding_before = float(data.get('padding_before') or 0)
    padding_after = float(data.get('padding_after') or 0)
    subtitle_url = (data.get('subtitle_url') or '').strip()
    subtitle_style = data.get('subtitle_style') or ''

    if not url:
        return jsonify({'error': 'url is required'}), 400
    if duration <= 0:
        return jsonify({'error': 'duration must be > 0'}), 400

    actual_start = max(0.0, start - padding_before)
    actual_duration = duration + padding_before + padding_after

    out_dir = os.path.join(DOWNLOAD_DIR, 'trims', uuid.uuid4().hex[:12])
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'trimmed.mp4')
    ass_path = None

    if subtitle_url:
        try:
            ass_path = os.path.join(out_dir, 'subs.ass')
            logger.info('Downloading subtitles (style=%s) from %s', subtitle_style, subtitle_url[:120])
            with urllib.request.urlopen(subtitle_url, timeout=30) as resp:
                with open(ass_path, 'wb') as fh:
                    fh.write(resp.read())
            if os.path.getsize(ass_path) == 0:
                ass_path = None
            else:
                escaped = ass_path.replace('\\', '/').replace(':', '\\:').replace("'", "\\'")
                subs_filter = f"subtitles='{escaped}'"
                vf = f"{vf},{subs_filter}" if vf else subs_filter
        except Exception as exc:
            logger.warning('Subtitle download failed, continuing without burn-in: %s', exc)
            ass_path = None

    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-ss', str(actual_start),
        '-i', url,
        '-t', str(actual_duration),
    ]
    if vf:
        cmd += ['-vf', vf]
    cmd += [
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-tune', 'zerolatency',
        '-crf', '26',
        '-pix_fmt', 'yuv420p',
        '-x264-params', 'ref=1:bframes=0:weightp=0:me=dia:subme=1:no-mbtree=1:trellis=0:aq-mode=0',
        '-threads', '1',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-movflags', '+faststart',
        '-max_muxing_queue_size', '1024',
        '-y',
        out_path,
    ]

    logger.info('Sync trim: start=%.2f duration=%.2f url=%s', actual_start, actual_duration, url[:120])
    logger.info('Sync trim vf (len=%d): %s', len(vf), vf[:400])

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''

        if result.returncode != 0:
            signal_info = ''
            if result.returncode < 0:
                signal_info = f' (killed by signal {-result.returncode}, likely OOM)'
            logger.error('Sync trim failed rc=%d%s stderr=%s', result.returncode, signal_info, stderr_text[-2000:])
            return jsonify({
                'error': f'ffmpeg failed (rc={result.returncode}){signal_info}',
                'stderr': stderr_text[-500:],
            }), 500

        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            logger.error('Sync trim empty output. stderr=%s', stderr_text[-1000:])
            return jsonify({'error': 'ffmpeg produced empty output'}), 500

        size_mb = os.path.getsize(out_path) / 1024 / 1024
        logger.info('Sync trim done: %.1f MB', size_mb)
        return send_file(out_path, mimetype='video/mp4', as_attachment=True, download_name='trimmed.mp4')
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Trim timed out (10 min limit)'}), 500
    except Exception as exc:
        logger.exception('Sync trim failed unexpectedly')
        return jsonify({'error': str(exc)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
