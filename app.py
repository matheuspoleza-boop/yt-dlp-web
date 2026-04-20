import os
import subprocess
import uuid
import glob
import threading
import time

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/downloads'
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Track download jobs: {job_id: {status, filepath, filename, error}}
jobs = {}


def cleanup_old_files():
    """Remove files older than 10 minutes to save disk."""
    while True:
        time.sleep(300)
        now = time.time()
        for dirpath in glob.glob(os.path.join(DOWNLOAD_DIR, '*')):
            if os.path.isdir(dirpath):
                for f in glob.glob(os.path.join(dirpath, '*')):
                    if now - os.path.getmtime(f) > 600:
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
        '--extractor-args', 'youtube:player_client=web,android,ios',
        '-o', os.path.join(job_dir, '%(title)s.%(ext)s'),
    ]

    if format_type == 'audio':
        cmd += ['-x', '--audio-format', 'mp3', '--audio-quality', '0']
    else:
        cmd += ['-f', 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                '--merge-output-format', 'mp4']

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

    # Don't expose filepath to the client
    safe = {k: v for k, v in job.items() if k != 'filepath'}
    return jsonify(safe)


@app.route('/get/<job_id>')
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get('status') != 'done':
        return jsonify({'error': 'Arquivo nao disponivel'}), 404
    return send_file(job['filepath'], as_attachment=True, download_name=job['filename'])


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
