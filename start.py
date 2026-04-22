import os
import sys


def main():
    port = os.environ.get('PORT', '5000').strip() or '5000'
    args = [
        'gunicorn',
        '--bind', f'0.0.0.0:{port}',
        '--timeout', '300',
        '--workers', '4',
        '--worker-class', 'gthread',
        '--threads', '2',
        'app:app',
    ]
    os.execvp('gunicorn', args)


if __name__ == '__main__':
    main()
