"""Read-only HLS of the current Away session, behind private Tailscale Serve."""
import os
import re
import stat
from typing import cast

import archive
from presence_receiver import BoundedServer, Handler as ResponseHandler

PREFIX = '/sentry-live'
WINDOW = 6
MAX_MANIFEST = 2 * 1024 * 1024
MAX_SEGMENT = 8 * 1024 * 1024
TARGET_SECONDS = archive.SEGMENT_SECONDS + 2
MEDIA = re.compile(r'/([0-9a-f]{32})/([1-9][0-9]{8,10}\.ts)')
PAGE = b'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Mac Sentry Live</title><h1>Mac Sentry Live</h1><video controls playsinline autoplay muted width="720" src="./index.m3u8"></video><p>Unmute for audio. If no recording is active, reload after Away. Open in Safari or another browser with native HLS.</p></html>'''


class Viewer(BoundedServer):
    def __init__(self, port, owner, controller, config):
        self.owner, self.controller, self.config = owner, controller, config
        super().__init__(port, Handler)


class Handler(ResponseHandler):
    @property
    def viewer(self):
        return cast(Viewer, self.server)

    def send_data(self, code, content_type, size):
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'private, no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'none'; media-src 'self'; frame-ancestors 'none'")
        self.end_headers()

    def do_POST(self):
        self.send_data(405, 'text/plain', 0)

    def do_GET(self):
        self.view(False)

    def do_HEAD(self):
        self.view(True)

    def view(self, head):
        s = self.viewer
        if self.headers.get_all('Tailscale-User-Login') != [s.owner]:
            return self.send_data(403, 'text/plain', 0)
        path = self.path.removeprefix(PREFIX) if self.path.startswith(PREFIX + '/') or self.path == PREFIX else self.path
        if path == '':
            self.send_response(302)
            self.send_header('Location', PREFIX+'/')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            return
        if path == '/':
            self.send_data(200, 'text/html; charset=utf-8', len(PAGE))
            if not head:
                self.wfile.write(PAGE)
            return
        segment = MEDIA.fullmatch(path)
        if path != '/index.m3u8' and segment is None:
            return self.send_data(404, 'text/plain', 0)
        session = s.controller.viewing_session()
        if session is None:
            return self.send_data(410, 'text/plain', 0)
        if segment and segment[1] != session:
            return self.send_data(404, 'text/plain', 0)
        sent = False
        try:
            with (archive.open_archive(s.config, check_identity=False) as root,
                  archive.child(root, 'recordings') as recordings,
                  archive.child(recordings, session) as capture):
                with archive.read_file(capture, 'segments.csv') as manifest:
                    lines = manifest.read(MAX_MANIFEST+1)
                if len(lines) > MAX_MANIFEST:
                    return self.send_data(503, 'text/plain', 0)
                rows = list(archive.segment_rows(lines.splitlines()))
                recent = rows[-WINDOW:]
                if not recent:
                    return self.send_data(404, 'text/plain', 0)
                if path == '/index.m3u8':
                    seq = len(rows) - len(recent)
                    if any(b-a > TARGET_SECONDS for _,a,b in recent):
                        return self.send_data(503, 'text/plain', 0)
                    parts = ['#EXTM3U', '#EXT-X-VERSION:3', f'#EXT-X-TARGETDURATION:{TARGET_SECONDS}',
                             f'#EXT-X-MEDIA-SEQUENCE:{seq}', f'#EXT-X-DISCONTINUITY-SEQUENCE:{seq}']
                    for n,(name,a,b) in enumerate(recent):
                        if n:
                            parts.append('#EXT-X-DISCONTINUITY')
                        parts.extend((f'#EXTINF:{b-a:.3f},', f'./{session}/{name}'))
                    payload = ('\n'.join(parts)+'\n').encode()
                    if s.controller.viewing_session() != session:
                        return self.send_data(410, 'text/plain', 0)
                    sent = True
                    self.send_data(200, 'application/vnd.apple.mpegurl', len(payload))
                    if not head:
                        self.wfile.write(payload)
                    return
                if segment is None or segment[2] not in {r[0] for r in recent}:
                    return self.send_data(404, 'text/plain', 0)
                fd = os.open(segment[2], archive.FILE, dir_fd=capture)
                with os.fdopen(fd, 'rb') as fh:
                    info = os.fstat(fh.fileno())
                    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_SEGMENT:
                        return self.send_data(404, 'text/plain', 0)
                    if s.controller.viewing_session() != session:
                        return self.send_data(410, 'text/plain', 0)
                    sent = True
                    self.send_data(200, 'video/mp2t', info.st_size)
                    remaining = info.st_size
                    while not head and remaining and s.controller.viewing_session() == session:
                        data = fh.read(min(65536, remaining))
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
        except (OSError, ValueError):
            if not sent:
                self.send_data(404, 'text/plain', 0)
