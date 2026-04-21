import base64
import glob
import logging
import os
import re
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

# Track download jobs: {job_id: {status, filepath, filename, error}}
jobs = {}


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

    cmd += ['--extractor-args', 'youtube:player_client=tv_embedded,mweb,ios']

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            jobs[job_id] = {'status': 'error', 'error': result.stderr[-500:]}
            return

        files = glob.glob(os.path.join(job_dir, '*'))
        if files:
            jobs[job_id] = {
                'status': 'done',
                'filepath': files[0],
                'filename': os.path.basename(files[0]),
            }
        else:
            jobs[job_id] = {'status': 'error', 'error': 'Nenhum arquivo gerado.'}
    except subprocess.TimeoutExpired:
        jobs[job_id] = {'status': 'error', 'error': 'Download excedeu o tempo limite (5 min).'}
    except Exception as e:
        jobs[job_id] = {'status': 'error', 'error': str(e)}


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
    jobs[job_id] = {'status': 'downloading'}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_type))
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/status/<job_id>')
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job nao encontrado'}), 404

    safe = {k: v for k, v in job.items() if k != 'filepath'}
    return jsonify(safe)


@app.route('/get/<job_id>')
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Arquivo nao disponivel'}), 404
    return send_file(job['filepath'], as_attachment=True, download_name=job['filename'])


@app.route('/extract-frames', methods=['POST'])
def extract_frames():
    data = request.get_json(silent=True) or {}

    # URL mode: extract specific frames from a remote URL via ffmpeg.
    # Used by Supabase video-track-person (no prior yt-dlp job).
    url = (data.get('url') or '').strip()
    timestamps = data.get('timestamps')
    if url and isinstance(timestamps, list) and len(timestamps) > 0:
        quality = int(data.get('quality') or 80)
        qscale = max(2, min(31, int(31 - (quality * 29 / 100))))  # 0–100 → 31–2
        logger.info('Extracting %d frames from URL (quality=%d, qscale=%d)',
                    len(timestamps), quality, qscale)

        frames_b64 = []
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                for i, t in enumerate(timestamps):
                    try:
                        ts = float(t)
                    except (TypeError, ValueError):
                        continue
                    frame_path = os.path.join(tmp_dir, 'frame_{:06d}.jpg'.format(i))
                    # -ss BEFORE -i = fast input seek (keyframe-aligned).
                    cmd = [
                        'ffmpeg',
                        '-hide_banner', '-loglevel', 'error',
                        '-ss', str(ts),
                        '-i', url,
                        '-frames:v', '1',
                        '-q:v', str(qscale),
                        '-f', 'image2',
                        frame_path,
                        '-y',
                    ]
                    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                    if result.returncode != 0:
                        logger.warning('Frame at t=%.2fs failed: %s',
                                       ts, result.stderr[-200:])
                        continue
                    if os.path.isfile(frame_path):
                        with open(frame_path, 'rb') as fh:
                            frames_b64.append(base64.b64encode(fh.read()).decode('utf-8'))

            logger.info('Extracted %d/%d frames from URL', len(frames_b64), len(timestamps))
            return jsonify({'frames': frames_b64})
        except subprocess.TimeoutExpired:
            logger.error('URL frame extraction timed out')
            return jsonify({'error': 'Frame extraction timed out'}), 500
        except Exception as exc:
            logger.exception('URL-mode extract-frames failed')
            return jsonify({'error': str(exc)}), 500

    # job_id mode: reuse a file previously downloaded via /download.
    job_id = data.get('job_id', '').strip()

    if not job_id:
        return jsonify({'error': 'job_id or url+timestamps is required'}), 400

    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    if job.get('status') != 'done':
        return jsonify({'error': 'Job is not complete (status: {})'.format(job.get('status'))}), 400

    video_path = job.get('filepath')
    if not video_path or not os.path.isfile(video_path):
        return jsonify({'error': 'Video file not available'}), 404

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            logger.info('Extracting frames for job %s from %s', job_id, video_path)
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

        logger.info('Extracted %d frames for job %s', len(frames_payload), job_id)
        return jsonify({'status': 'success', 'frames': frames_payload})

    except RuntimeError as exc:
        logger.error('Frame extraction failed for job %s: %s', job_id, exc)
        return jsonify({'error': 'Frame extraction failed: {}'.format(exc)}), 500
    except subprocess.TimeoutExpired:
        logger.error('ffmpeg timed out for job %s', job_id)
        return jsonify({'error': 'Frame extraction timed out'}), 500
    except Exception as exc:
        logger.exception('Unexpected error extracting frames for job %s', job_id)
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

    job = jobs.get(job_id)
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
    """Trim + reframe a remote video using FFmpeg, return MP4."""
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

    if not url:
        return jsonify({'error': 'url is required'}), 400
    if duration <= 0:
        return jsonify({'error': 'duration must be > 0'}), 400

    actual_start = max(0.0, start - padding_before)
    actual_duration = duration + padding_before + padding_after

    out_dir = os.path.join(DOWNLOAD_DIR, 'trims', uuid.uuid4().hex[:12])
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, 'trimmed.mp4')

    # No -loglevel; we want everything. ultrafast/CRF 26 are light on CPU+RAM
    # so we don't get OOM-killed on Railway's smaller instances.
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
        '-tune', 'zerolatency',    # desabilita lookahead/B-frames
        '-crf', '26',
        '-pix_fmt', 'yuv420p',
        # Cortar recursos do x264 que consomem RAM: 1 reference frame, sem
        # B-frames, sem weighted prediction, sem motion estimation hex/umh,
        # sem mbtree lookahead, sem trellis. Perde ~5-10% de eficiência de
        # compressão mas usa ~1/4 da RAM de um preset normal.
        '-x264-params', 'ref=1:bframes=0:weightp=0:me=dia:subme=1:no-mbtree=1:trellis=0:aq-mode=0',
        '-threads', '1',            # single-thread — evita overhead de paralelismo
        '-c:a', 'aac',
        '-b:a', '128k',
        '-movflags', '+faststart',
        '-max_muxing_queue_size', '1024',
        '-y',
        out_path,
    ]

    logger.info('Trim starting: start=%.2f duration=%.2f url=%s', actual_start, actual_duration, url[:120])
    logger.info('Trim vf (len=%d): %s', len(vf), vf[:500])

    try:
        # bytes mode so weird ffmpeg output doesn't silently disappear
        result = subprocess.run(cmd, capture_output=True, timeout=600)
        stderr_text = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
        stdout_text = result.stdout.decode('utf-8', errors='replace') if result.stdout else ''

        if result.returncode != 0:
            # Negative returncode on POSIX = killed by signal (|returncode| = signal number)
            signal_info = ''
            if result.returncode < 0:
                signal_info = f' (killed by signal {-result.returncode}, likely OOM or container restart)'
            logger.error('Trim ffmpeg failed rc=%d%s stderr=%s stdout=%s',
                         result.returncode, signal_info,
                         stderr_text[-2000:] or '<empty>',
                         stdout_text[-500:] or '<empty>')
            return jsonify({
                'error': f'ffmpeg failed (rc={result.returncode}){signal_info}',
                'stderr': stderr_text[-500:],
            }), 500

        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            logger.error('Trim produced empty file. stderr=%s', stderr_text[-1000:])
            return jsonify({'error': 'ffmpeg produced empty output', 'stderr': stderr_text[-500:]}), 500

        logger.info('Trim done: %d bytes (stderr lines=%d)', os.path.getsize(out_path), stderr_text.count('\n'))
        return send_file(out_path, mimetype='video/mp4', as_attachment=True, download_name='trimmed.mp4')
    except subprocess.TimeoutExpired:
        logger.error('Trim timed out after 10 min')
        return jsonify({'error': 'Trim timed out (10 min limit)'}), 500
    except Exception as exc:
        logger.exception('Trim failed unexpectedly')
        return jsonify({'error': str(exc)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
