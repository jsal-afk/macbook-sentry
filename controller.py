"""Fail-off presence state and ownership of one Away process group."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

import archive

ROOT = Path(__file__).resolve().parent
MAX_AGE = 120
FUTURE_SKEW = 10
STOP_GRACE = 3
SWEEP_SECONDS = 60
ISO = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})')


def event_time(value):
    if not isinstance(value, str) or not ISO.fullmatch(value):
        raise ValueError('Require an ISO 8601 device timestamp with timezone')
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def iso(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class Controller:
    def __init__(self, config, lock=None, *, test=False):
        self.config, self.lock, self.test = config, lock, test
        self.cv = threading.Condition()
        self.armed = False
        self.presence = None
        self.started = self.arm_after = time.time()
        self.last = {}
        self.away_until = self.away_wall_until = 0
        self.worker = None
        self.stop_by = None
        self.error = None
        self.closed = False
        self.retention_stop = threading.Event()
        self.retainer = None
        if not test:
            self.retainer = threading.Thread(target=self.retain, name='archive-retention')
            self.retainer.start()
        self.thread = threading.Thread(target=self.run, name='presence-control')
        self.thread.start()

    def status(self):
        with self.cv:
            return {'mode': 'test' if self.test else 'run', 'armed': self.armed,
                    'signal': self.presence, 'capture_worker': self.worker is not None,
                    'stopping_capture': self.stop_by is not None, 'error': self.error,
                    'lease_until': iso(self.away_wall_until) if self.away_wall_until else None,
                    'last_accepted': {name: iso(at) for name, at in self.last.items()}}

    def arm(self):
        with self.cv:
            if self.test or self.closed:
                return False
            if not self.armed:
                self.armed = True
                self.arm_after = time.time()
                self.error = None
                self.presence = None
            return True

    def pause(self):
        with self.cv:
            self.armed = False
            self.disarm(None, time.monotonic())
            self.cv.notify_all()

    def receive(self, name, event):
        with self.cv:
            wall, now = time.time(), time.monotonic()
            if self.closed or not self.test and name == 'away' and not self.armed:
                return 'not_armed'
            if not max(self.started, wall - MAX_AGE) <= event <= wall + FUTURE_SKEW:
                return 'timestamp_outside_window'
            if (event <= self.last.get(name, 0) or
                    name == 'away' and event <= max(self.arm_after, self.last.get('home', 0))):
                return 'duplicate_or_out_of_order'
            if name == 'home':
                self.disarm('home', now)
            elif self.test:
                self.presence = 'away'
            elif self.presence != 'away':
                remaining = self.config['away_lease_seconds'] - max(0, wall - event)
                if remaining <= 0:
                    return 'lease_already_expired'
                self.presence = 'away'
                self.away_until = now + remaining
                self.away_wall_until = wall + remaining
                self.error = None
            self.last[name] = event
            self.cv.notify_all()
            return None

    def disarm(self, presence, now):
        self.presence = presence
        self.away_until = self.away_wall_until = 0
        if self.worker and self.stop_by is None:
            self.stop_by = now + STOP_GRACE
            self.kill_group(signal.SIGTERM)

    def kill_group(self, sig):
        try:
            os.killpg(self.worker.pid, sig)
        except ProcessLookupError:
            pass

    def reconcile(self, now):
        if self.worker:
            if self.worker.poll() is not None:
                self.kill_group(signal.SIGKILL)
                assert self.worker.stdin is not None
                self.worker.stdin.close()
                if self.stop_by is None:
                    self.error = 'capture_worker_exited'
                    self.armed = False
                    self.disarm(None, now)
                self.worker = self.stop_by = None
            elif self.stop_by is not None:
                if now >= self.stop_by:
                    self.kill_group(signal.SIGKILL)
                return
        if self.test or self.presence != 'away':
            return
        if now >= self.away_until or time.time() >= self.away_wall_until:
            self.armed = False
            self.error = 'away_lease_expired'
            self.disarm(None, now)
            return
        if self.worker is None:
            if self.lock is None:
                raise OSError('Capture requires the service lock')
            payload = json.dumps(self.config | {'lease_wall': self.away_wall_until,
                                                'lease_mono': self.away_until}).encode() + b'\n'
            self.worker = subprocess.Popen(
                [sys.executable, '-B', str(ROOT/'sentry.py'), '--worker', uuid.uuid4().hex],
                stdin=subprocess.PIPE, start_new_session=True, pass_fds=(self.lock.fileno(),))
            assert self.worker.stdin is not None
            self.worker.stdin.write(payload)
            self.worker.stdin.flush()

    def run(self):
        with self.cv:
            while True:
                now = time.monotonic()
                try:
                    self.reconcile(now)
                except (OSError, ValueError) as exc:
                    self.error = 'worker_start_' + type(exc).__name__
                    self.armed = False
                    self.disarm(None, now)
                if self.closed and self.worker is None:
                    return
                self.cv.wait(.2 if self.worker or not self.test and self.presence == 'away' else None)

    def retain(self):
        while not self.retention_stop.is_set():
            if self.config.get('volume'):
                try:
                    archive.sweep(self.config)
                except FileNotFoundError:
                    pass  # No internal fallback.
                except (OSError, ValueError) as exc:
                    print('archive_retention:', type(exc).__name__, file=sys.stderr, flush=True)
            self.retention_stop.wait(SWEEP_SECONDS)

    def close(self):
        with self.cv:
            self.closed = True
            self.pause()
            self.retention_stop.set()
        self.thread.join()
        if self.retainer:
            self.retainer.join()
