#!/usr/bin/env python3
"""Owned Away worker: one audiovisual capture, local detection and clip delivery."""
import argparse
from collections import deque
import datetime as dt
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import stat
import sys
import threading
import time
import uuid

import archive

WIDTH, HEIGHT, DETECT_FPS = 960, 540, 2
FRAME_BYTES = WIDTH * HEIGHT * 3
FREE_BYTES = 5 * 1024**3
AAC = ['-c:a', 'aac', '-profile:a', 'aac_low', '-b:a', '96k', '-ar', '48000', '-ac', '1']
VT = ['-c:v', 'h264_videotoolbox', '-realtime', '1', '-allow_sw', '0']


def atomic(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    temp.replace(path)


def timestamp():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def log(kind, **kw):
    print(json.dumps({'time': timestamp(), 'kind': kind, **kw}, ensure_ascii=False), flush=True)


def camera_command(config, directory, duration, audio_fd=None, *, audio_rate=48000, source=None):
    """The generated-media source is for offline tests, not a worker CLI option."""
    inp = (['-re', '-i', str(source)] if source else
           ['-thread_queue_size', '16', '-f', 'avfoundation', '-pixel_format', 'nv12',
            '-framerate', '30', '-video_size', '1280x720', '-i', config['camera_input'],
            '-thread_queue_size', '32', '-f', 'f32le', '-ar', str(audio_rate), '-ac', '1',
            '-i', f'pipe:{audio_fd}'])
    audio = ('[0:a]asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0' if source else
             '[1:a]asetpts=N/SR/TB,highpass=f=80,lowpass=f=8000,volume=2,alimiter=limit=0.95:latency=1')
    graph = '[0:v]setpts=PTS-STARTPTS,fps=15'
    detection = config.get('detection', False)
    if detection:
        graph += f',split=2[rec][view];[view]fps={DETECT_FPS},scale={WIDTH}:{HEIGHT},format=rgb24[det];'
    else:
        graph += '[rec];'
    graph += audio + '[arec]'
    limit = ['-t', str(duration)]
    cmd = [config['ffmpeg'], '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y', *inp,
           '-filter_complex_threads', '1', '-filter_complex', graph,
           '-map', '[rec]', '-map', '[arec]', *limit, *AAC, *VT, '-pix_fmt', 'nv12',
           '-b:v', '1500k', '-maxrate', '1800k', '-bufsize', '3600k', '-g', '30', '-color_range', 'tv',
           '-f', 'segment', '-segment_time', str(archive.SEGMENT_SECONDS), '-segment_format', 'mpegts',
           '-reset_timestamps', '1', '-segment_list', str(directory/'segments.csv'),
           '-segment_list_type', 'csv', '-strftime', '1', str(directory/'%s.ts')]
    if detection:
        cmd += ['-map', '[det]', *limit, '-an', '-pix_fmt', 'rgb24', '-c:v', 'rawvideo',
                '-threads:v', '1', '-f', 'rawvideo', 'pipe:1']
    return cmd


class Frames:
    def __init__(self):
        self.queue = deque(maxlen=3)
        self.cv = threading.Condition()
        self.last = 0

    def read(self, pipe, capture):
        start = 0
        with pipe:
            for index in range(sys.maxsize):
                frame = pipe.read(FRAME_BYTES)
                if len(frame) != FRAME_BYTES:
                    break
                if index == 0:
                    start = time.time_ns()
                with self.cv:
                    self.queue.append((capture, index / DETECT_FPS,
                                       start + index * (archive.NS // DETECT_FPS), frame))
                    self.last = time.monotonic()
                    self.cv.notify()

    def next(self, timeout):
        with self.cv:
            if not self.queue:
                self.cv.wait(timeout)
            return self.queue.popleft() if self.queue else None


def read_event(directory):
    try:
        return json.loads((directory/'event.json').read_text())
    except (FileNotFoundError, ValueError):
        return None


def event_dirs():
    return (p for p in Path('events').iterdir() if archive.EVENT.fullmatch(p.name) and not p.is_symlink())


def probe(config, path):
    data = json.loads(subprocess.check_output([config['ffprobe'], '-v', 'error', '-show_entries',
        'format=duration:stream=codec_name,codec_type,pix_fmt,profile', '-of', 'json', str(path)], timeout=10))
    if (float(data['format']['duration']) <= 0
            or not any(s.get('codec_name') == 'h264' and s.get('pix_fmt') == 'yuv420p' for s in data['streams'])
            or not any(s.get('codec_name') == 'aac' and s.get('profile') == 'LC' for s in data['streams'])):
        raise RuntimeError('Clip requires playable H.264 and AAC-LC')


def clip_event(config, directory, event, *, final=False):
    capture = event['capture']
    if not archive.SESSION.fullmatch(capture):
        raise ValueError('Invalid capture identity')
    recording = Path('recordings')/capture
    if recording.is_symlink() or (recording/'segments.csv').is_symlink():
        raise ValueError('Unexpected link in recording')
    try:
        with (recording/'segments.csv').open() as manifest:
            rows = list(archive.segment_rows(manifest))
    except FileNotFoundError:
        return False
    # The segment CSV uses the mux clock, which starts at the native AAC
    # encoder's priming packet, 1024 samples before capture/video PTS zero.
    priming = 1024 / 48000
    start = max(0, event['pts']-5) + priming
    end = event['pts']+15 + priming
    if not rows or (not final and rows[-1][2] < end):
        return False
    # A clip that starts on a video cut can still need the previous AAC packet.
    chosen = [(p, a, b) for p, a, b in rows if b > start-priming and a < end]
    if not chosen or min(end, chosen[-1][2]) <= start or not all((recording/p).is_file() and not (recording/p).is_symlink() for p, _, _ in chosen):
        return False
    last, a, b = chosen[-1]
    clipped_end = min(end, b)
    capture_end = archive.segment_end_ns(last, clipped_end - a)
    if capture_end <= time.time_ns() - archive.retention_ns(config):
        return False
    manifest = directory/'clip.concat'
    output = directory/'clean.part.mp4'
    manifest.write_text(''.join(f"file '../../recordings/{recording.name}/{p}'\nduration {b-a:.6f}\n"
                                for p, a, b in chosen))
    origin = chosen[0][1]
    window = f'start={start-origin:.6f}:end={clipped_end-origin:.6f}'
    try:
        subprocess.run([config['ffmpeg'], '-v', 'error', '-nostdin', '-y', '-f', 'concat', '-safe', '0',
                        '-i', str(manifest), '-map', '0:v:0', '-map', '0:a:0',
                        '-vf', f'trim={window},setpts=PTS-STARTPTS', '-fps_mode', 'passthrough',
                        '-af', f'atrim={window},asetpts=PTS-STARTPTS',
                        *AAC, '-map_metadata', '-1', '-c:v', 'libx264', '-threads:v', '2',
                        '-preset', 'veryfast', '-crf', '24', '-pix_fmt', 'yuv420p',
                        '-movflags', '+faststart', str(output)], check=True, timeout=90)
        probe(config, output)
        if capture_end <= time.time_ns() - archive.retention_ns(config):
            output.unlink(missing_ok=True)
            return False
        output.replace(directory/'clean.mp4')
        return capture_end
    finally:
        manifest.unlink(missing_ok=True)


def delivery_worker(config, binding, stop, wake):
    from telegram_bot import send
    retry = {}
    wake.set()
    while not stop.is_set():
        wake.clear()
        for directory in sorted(event_dirs()):
            if stop.is_set():
                return
            event = read_event(directory)
            if not event or event.get('delivery_generation') != binding['generation'] or archive.event_end_ns(directory.name, event) <= time.time_ns() - archive.retention_ns(config):
                retry.pop(directory, None)
                continue
            try:
                receipt_path = directory/'receipt.json'
                try:
                    receipt = json.loads(receipt_path.read_text())
                except FileNotFoundError:
                    receipt = {}
                if time.monotonic() < retry.get(directory, 0):
                    continue
                text = f"Person detected · {dt.datetime.fromisoformat(event['time']):%m/%d %H:%M:%S}"
                if not receipt.get('alert') and not stop.is_set():
                    receipt['alert'] = send(binding, text)
                    atomic(receipt_path, receipt)
                if (event.get('clip_ready') and not receipt.get('video') and not stop.is_set()
                        and archive.event_end_ns(directory.name, event) > time.time_ns() - archive.retention_ns(config)):
                    video = directory/'telegram.mp4'
                    receipt['video'] = send(binding, text, video)
                    atomic(receipt_path, receipt)
                    video.unlink(missing_ok=True)
                retry.pop(directory, None)
            except Exception as exc:
                log('delivery_retry', error_type=type(exc).__name__)
                retry[directory] = time.monotonic() + 60
        retry = {p: at for p, at in retry.items() if p.exists()}
        delay = max(0, min(retry.values())-time.monotonic()) if retry else None
        wake.wait(delay)


def detect(config, frames, stop, wake, active, generation):
    from person_detector import PersonGate, PersonModel
    model = PersonModel(config)
    pending = {p: e for p in event_dirs() if (e := read_event(p)) and e.get('delivery_generation') == generation and not e.get('clip_ready')}
    gate = PersonGate()
    last_capture = None
    next_clip = 0
    while not stop.is_set():
        item = frames.next(.5)
        if stop.is_set():
            return
        if item:
            capture, pts, wall, frame = item
            if capture != last_capture:
                gate = PersonGate()
                last_capture = capture
            boxes = model.detect_rgb(frame, WIDTH, HEIGHT)
            if not stop.is_set() and gate.update(pts, bool(boxes)):
                directory = Path('events')/f'{wall}-{uuid.uuid4().hex}'
                directory.mkdir()
                event = {'time': dt.datetime.fromtimestamp(wall/archive.NS).astimezone().isoformat(),
                         'capture': capture, 'pts': pts, 'clip_ready': False,
                         'delivery_generation': generation}
                atomic(directory/'event.json', event)
                pending[directory] = event
                wake.set()
        if time.monotonic() < next_clip:
            continue
        next_clip = time.monotonic() + 1
        for directory, event in list(pending.items()):
            if stop.is_set():
                return
            if archive.event_end_ns(directory.name, event) <= time.time_ns() - archive.retention_ns(config):
                del pending[directory]
                continue
            try:
                capture_end = event.get('capture_end_ns') or clip_event(
                    config, directory, event, final=active[0] != event['capture'])
                if capture_end and not stop.is_set():
                    event['capture_end_ns'] = capture_end
                    atomic(directory/'event.json', event)
                    part = directory/'telegram.part.mp4'
                    model.annotate(directory/'clean.mp4', part)
                    probe(config, part)
                    if stop.is_set() or capture_end <= time.time_ns() - archive.retention_ns(config):
                        part.unlink(missing_ok=True)
                        continue
                    part.replace(directory/'telegram.mp4')
                    event['clip_ready'] = True
                    atomic(directory/'event.json', event)
                    del pending[directory]
                    wake.set()
            except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as exc:
                log('clip_failed', error_type=type(exc).__name__)
                del pending[directory]


def finish(proc, timeout=1):
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def check_space(path):
    fs = os.fstatvfs(path) if isinstance(path, int) else os.statvfs(path)
    if fs.f_bavail * fs.f_frsize < FREE_BYTES:
        raise OSError('Less than 5 GiB free on recording volume')


def microphone_rate(fd):
    # Read only the header; leave every PCM byte for FFmpeg.
    header = b''
    until = time.monotonic() + 4
    while len(header) < 7 and select.select([fd], [], [], max(0, until - time.monotonic()))[0]:
        b = os.read(fd, 1)
        if not b:
            break
        if b == b'\n' and header.isdigit() and 8000 <= int(header) <= 192000:
            return int(header)
        header += b
    raise OSError('Microphone did not provide PCM; check macOS permission and input')


def closed_segment(directory):
    try:
        with (directory/'segments.csv').open() as stream:
            return max((end for _, _, end in archive.segment_rows(stream)), default=0)
    except FileNotFoundError:
        return 0


def worker(config, session, state_dir):
    if os.getpgrp() != os.getpid() or not stat.S_ISFIFO(os.fstat(0).st_mode):
        raise RuntimeError('Capture must be launched by the presence controller')
    if not archive.SESSION.fullmatch(session):
        raise ValueError('Invalid Away session')
    stop = threading.Event()
    wake = threading.Event()
    frames = Frames() if config['detection'] else None
    active = [None]
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def parent_lost():
        os.read(0, 1)
        stop.set()
        os.killpg(os.getpgrp(), signal.SIGTERM)
        # If the parent disappeared, it cannot enforce the group grace period.
        time.sleep(3)
        os.killpg(os.getpgrp(), signal.SIGKILL)

    def remaining():
        return min(config['lease_wall'] - time.time(), config['lease_mono'] - time.monotonic())

    threading.Thread(target=parent_lost, daemon=True).start()
    if stop.is_set() or remaining() <= 0:
        return
    with archive.open_archive(config) as root:
        check_space(root)
        for name in ('recordings', 'events'):
            with archive.child(root, name):
                pass
        os.fchdir(root)  # Relative output stays bound to the selected archive.
    if frames is not None:
        from state import read
        from telegram_bot import validate_binding
        binding = validate_binding(read(state_dir/'bot.json'))
        threading.Thread(target=delivery_worker, args=(config, binding, stop, wake), daemon=True).start()

        def detection():
            try:
                detect(config, frames, stop, wake, active, binding['generation'])
            except Exception as exc:
                # A model failure must not interrupt clean recording.
                log('person_worker_failed', error_type=type(exc).__name__)

        threading.Thread(target=detection, daemon=True).start()
    if stop.is_set() or remaining() <= 0:
        return
    awake = subprocess.Popen(['/usr/bin/caffeinate', '-i', '-s', '-w', str(os.getpid())])
    try:
        capture = uuid.uuid4().hex
        directory = Path('recordings')/capture
        directory.mkdir()
        active[0] = capture
        mic = camera = None
        try:
            mic = subprocess.Popen([config['microphone_capture'], str(max(0, remaining()) + 5)],
                                   stdout=subprocess.PIPE)
            assert mic.stdout is not None
            fd = mic.stdout.fileno()
            rate = microphone_rate(fd)
            if stop.is_set() or remaining() <= 0:
                return
            camera = subprocess.Popen(camera_command(config, directory, remaining(), fd, audio_rate=rate),
                                      stdout=subprocess.PIPE if frames is not None else subprocess.DEVNULL,
                                      pass_fds=(fd,))
            mic.stdout.close()
            if frames is not None:
                threading.Thread(target=frames.read, args=(camera.stdout, capture), daemon=True).start()
            checked = last_progress = time.monotonic()
            previous = 0
            while camera.poll() is None and mic.poll() is None and not stop.wait(.2) and remaining() > 0:
                now = time.monotonic()
                if now - checked >= 2:
                    with archive.open_archive(config, check_identity=False) as root:
                        if not os.path.samestat(os.fstat(root), os.stat('.')):
                            raise OSError('Owned volume changed during capture')
                        check_space(root)
                    media_time = closed_segment(directory)
                    if media_time > previous:
                        last_progress, previous = now, media_time
                    if now - last_progress > 30:
                        raise OSError('Recording stalled: no new closed segment')
                    checked = now
            if not stop.is_set() and remaining() > 1:
                raise OSError('Camera or microphone stopped early; check permissions and devices')
        finally:
            finish(camera, 1.5)
            finish(mic)
            if mic and mic.stdout:
                mic.stdout.close()
            active[0] = None
    finally:
        stop.set()
        wake.set()
        finish(awake)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', required=True, metavar='SESSION')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if os.getpgrp() != os.getpid() or not stat.S_ISFIFO(os.fstat(0).st_mode):
            raise ValueError('Internal worker requires a controller pipe')
        # Unbuffered: parent-death watcher must see the actual descriptor's EOF.
        with os.fdopen(os.dup(0), 'rb', buffering=0) as control:
            config = json.loads(control.readline(32768))
        worker(config, args.worker, Path.cwd())
    except (OSError, KeyError, ValueError, RuntimeError) as exc:
        log('worker_failed', error_type=type(exc).__name__, detail=str(exc) if isinstance(exc, OSError) and exc.errno is None else 'Check volume, permissions and device settings')
        raise SystemExit(1)
