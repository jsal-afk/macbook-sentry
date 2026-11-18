"""Retention for the versioned, explicitly provisioned Sentry archive only."""
from contextlib import contextmanager
import csv
import json
import math
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import time
import uuid

import state

NS = 1_000_000_000
DAY = 86_400 * NS
SEGMENT_SECONDS = 10
MARKER = '.sentry-archive-v1'
DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
BINDING_FIELDS = {'volume', 'volume_uuid', 'archive_name', 'archive_owner'}
INTERNAL_NAME = 'archive'
EXTERNAL_NAME = 'Mac-Sentry'
SESSION = re.compile(r'[0-9a-f]{32}')
SEGMENT = re.compile(r'[1-9][0-9]{8,10}\.ts')
EVENT = re.compile(r'([1-9][0-9]{17,19})-([0-9a-f]{32})')
EVENT_FILES = ('event.json', 'receipt.json', 'clean.mp4', 'telegram.mp4',
               'event.tmp', 'receipt.tmp', 'clean.part.mp4', 'telegram.part.mp4', 'clip.concat')


def disk_identity(mount):
    try:
        data = plistlib.loads(subprocess.check_output(
            ['/usr/sbin/diskutil', 'info', '-plist', str(mount)], stderr=subprocess.DEVNULL, timeout=3))
        if (data.get('MountPoint') != str(mount) or data.get('Internal') is not False
                or data.get('BusProtocol') == 'Disk Image'):
            raise ValueError
        return str(uuid.UUID(data['VolumeUUID']))
    except (subprocess.SubprocessError, KeyError, TypeError, ValueError, plistlib.InvalidFileException) as exc:
        raise OSError('Select a mounted external drive with a volume UUID') from exc


@contextmanager
def open_volume(mount, *, is_mount=os.path.ismount):
    if mount.parent != Path('/Volumes') or mount.resolve() != mount:
        raise ValueError('Select one mounted volume directly under /Volumes')
    if not is_mount(mount):
        raise FileNotFoundError('Owned recording volume is not mounted')
    volume = os.open(mount, DIR)
    try:
        info = os.fstat(volume)
        if not is_mount(mount) or not os.path.samestat(info, mount.stat()):
            raise FileNotFoundError('Owned recording volume changed')
        yield volume
    finally:
        os.close(volume)


def create_owned(parent, name):
    validate_name(name)
    owner = uuid.uuid4().hex
    os.mkdir(name, mode=0o700, dir_fd=parent)  # Never adopt an existing name.
    with child(parent, name) as root:
        for folder in ('recordings', 'events'):
            os.mkdir(folder, mode=0o700, dir_fd=root)
        with os.fdopen(os.open(MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=root), 'w') as marker:
            marker.write(owner + '\n')
    return {'archive_name': name, 'archive_owner': owner}


@contextmanager
def open_internal():
    fd = os.open(state.directory(), DIR)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError('Internal archive parent must be owner-only')
        yield fd
    finally:
        os.close(fd)


def provision_internal(name=INTERNAL_NAME):
    with open_internal() as parent:
        return create_owned(parent, name)


def provision(mount, name):
    with open_volume(mount) as volume:
        identity = disk_identity(mount)
        if not os.path.samestat(os.fstat(volume), mount.stat()):
            raise FileNotFoundError('Selected volume changed')
        return create_owned(volume, name) | {'volume': str(mount), 'volume_uuid': identity}


def validate_name(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('Archive name must be 1–64 letters, digits, dashes or underscores')


@contextmanager
def open_archive(config, *, check_identity=True):
    if not BINDING_FIELDS.intersection(config):
        raise FileNotFoundError('No recording archive configured')
    name, owner = config.get('archive_name'), config.get('archive_owner')
    external = 'volume' in config or 'volume_uuid' in config
    required = BINDING_FIELDS if external else {'archive_name', 'archive_owner'}
    if not all(isinstance(config.get(key), str) for key in required):
        raise ValueError('Incomplete archive binding')
    validate_name(name)
    if not re.fullmatch(r'[0-9a-f]{32}', owner):
        raise ValueError('Invalid archive owner')
    mount = Path(config['volume']) if external else None
    with (open_volume(mount) if mount is not None else open_internal()) as parent:
        if mount is not None and check_identity and (disk_identity(mount) != config['volume_uuid']
                                                    or not os.path.samestat(os.fstat(parent), mount.stat())):
            raise PermissionError('Wrong recording volume UUID')
        with child(parent, name) as root:
            with read_file(root, MARKER) as marker:
                if marker.read(64).strip() != owner:
                    raise PermissionError('Sentry archive ownership marker mismatch')
            yield root


@contextmanager
def child(parent, name):
    fd = os.open(name, DIR, dir_fd=parent)
    try:
        if os.fstat(fd).st_dev != os.fstat(parent).st_dev:
            raise OSError('Nested filesystems are not part of the Sentry archive')
        yield fd
    finally:
        os.close(fd)


@contextmanager
def read_file(parent, name):
    fd = os.open(name, FILE, dir_fd=parent)
    with os.fdopen(fd, encoding='utf-8') as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError('Not a regular archive file')
        yield stream


def segment_rows(stream):
    for row in csv.reader(stream):
        if len(row) != 3 or not SEGMENT.fullmatch(row[0]):
            continue
        try:
            start, end = float(row[1]), float(row[2])
        except ValueError:
            continue
        if math.isfinite(end) and 0 <= start < end:
            yield row[0], start, end


def segment_end_ns(name, duration):
    return int(name[:-3]) * NS + math.ceil(duration * NS)


def event_end_ns(name, data):
    match = EVENT.fullmatch(name)
    if not match:
        raise ValueError('Not a Sentry event')
    trigger = int(match[1])
    end = data.get('capture_end_ns') if isinstance(data, dict) else None
    if type(end) is int and end > 0:
        return end
    # An interrupted producer can leave an unfinished event. Keep the possible
    # post-roll for the duration of its own retention window, not its write time.
    return trigger + 15 * NS


def remove_file(parent, name):
    try:
        if stat.S_ISREG(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
            os.unlink(name, dir_fd=parent)
            return 1
    except FileNotFoundError:
        pass
    return 0


def retention_ns(config):
    return int(config.get('retention_hours', 24) * 3600 * NS)


def sweep(config, *, now_ns=None):
    """Expire complete media units by capture time, never filesystem mtime."""
    cutoff = (time.time_ns() if now_ns is None else now_ns) - retention_ns(config)
    removed = 0
    with open_archive(config) as root:
        for folder, pattern in (('recordings', SESSION), ('events', EVENT)):
            try:
                with child(root, folder) as entries:
                    for name in os.listdir(entries):
                        if not pattern.fullmatch(name):
                            continue
                        try:
                            before = removed
                            with child(entries, name) as entry:
                                if folder == 'recordings':
                                    try:
                                        with read_file(entry, 'segments.csv') as manifest:
                                            ends = {p: segment_end_ns(p, b-a) for p, a, b in segment_rows(manifest)}
                                    except (OSError, UnicodeError, csv.Error):
                                        ends = {}
                                    # An unindexed tail after abrupt termination was opened at
                                    # its filename time and is at most one segment plus GOP slack.
                                    for item in os.listdir(entry):
                                        if SEGMENT.fullmatch(item) and ends.get(
                                                item, segment_end_ns(item, SEGMENT_SECONDS + 2)) <= cutoff:
                                            removed += remove_file(entry, item)
                                    if removed > before and set(os.listdir(entry)) == {'segments.csv'}:
                                        removed += remove_file(entry, 'segments.csv')
                                else:
                                    try:
                                        with read_file(entry, 'event.json') as metadata:
                                            event = json.load(metadata)
                                    except (OSError, ValueError):
                                        event = None
                                    if event_end_ns(name, event) <= cutoff:
                                        for item in EVENT_FILES:
                                            removed += remove_file(entry, item)
                            if removed > before:
                                try:
                                    os.rmdir(name, dir_fd=entries)
                                except OSError:  # Nonempty, including unrelated content.
                                    pass
                        except OSError:  # Symlink, nested filesystem, or disappeared entry.
                            continue
            except FileNotFoundError:
                continue
    return removed
