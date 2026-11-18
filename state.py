"""Owner-private, out-of-tree installation state."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile

ROOT = Path(__file__).resolve().parent
NOFOLLOW = os.O_NOFOLLOW | os.O_NONBLOCK


def directory(create=False):
    home = Path.home()
    if not home.is_absolute() or home.resolve() != home:
        raise ValueError('Use an absolute, non-symlink HOME')
    path = home/'.macbook-sentry'
    if path.is_relative_to(ROOT):
        raise ValueError('Sentry state must be outside the source directory')
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError('Sentry state must be an owner-only 0700 directory, not a symlink')
    return path


def private(fd):
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise PermissionError('Sentry state file must be owner-only 0600, not a link')


def read(path):
    with os.fdopen(os.open(path, os.O_RDONLY | NOFOLLOW), 'rb') as stream:
        private(stream.fileno())
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError('Sentry state file is too large')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Expected a Sentry state object')
    return data


def write(path, data):
    fd, name = tempfile.mkstemp(prefix='.sentry-state-', dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)  # Replace the entry, never follow old links.
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def locked(path):
    with os.fdopen(os.open(path/'service.lock', os.O_RDWR | os.O_CREAT | NOFOLLOW, 0o600), 'rb+') as stream:
        private(stream.fileno())
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Sentry is running or being configured; stop it first') from None
        yield stream
        # Close, not LOCK_UN: the capture worker holds this lease during cleanup.
