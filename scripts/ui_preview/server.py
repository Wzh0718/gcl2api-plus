#!/usr/bin/env python3
"""Local UI-only preview: serves front/ assets and browser-only demo data, no backend."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import mimetypes

ROOT = Path(__file__).resolve().parents[2]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        route = urlsplit(self.path).path
        if route in ('/', '/mobile'):
            name = 'control_panel_mobile.html' if route == '/mobile' else 'control_panel.html'
            body = (ROOT / 'front' / name).read_text()
            body = body.replace('</body>', '<script src="/__preview.js"></script></body>')
            self.send(body.encode(), 'text/html; charset=utf-8')
            return
        if route == '/__preview.js':
            self.send((Path(__file__).parent / 'preview.js').read_bytes(), 'text/javascript; charset=utf-8')
            return
        if route.startswith('/front/'):
            file = (ROOT / route.lstrip('/')).resolve()
            if file.is_relative_to(ROOT / 'front') and file.is_file() and file.suffix in {'.js','.css','.svg','.png','.ico'}:
                self.send(file.read_bytes(), mimetypes.guess_type(file)[0] or 'application/octet-stream')
                return
        self.send_error(404)

    def send(self, body, kind):
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18761)
    args = parser.parse_args()
    print(f'UI preview: http://127.0.0.1:{args.port} (desktop), /mobile (mobile)', flush=True)
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
