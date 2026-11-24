"""操作系统文件锁，进程退出后自动释放，避免两个采集任务同时改写分片。"""
import os
from contextlib import contextmanager
from functools import wraps
from ..contracts import ConfigError


class LockBusy(ConfigError):
    """另一个进程已经持有写锁。"""


@contextmanager
def exclusive_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockBusy('已有任务正在写入数据，请先停止它') from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def collection_write_lock(settings):
    return exclusive_file(settings.paths.tasks_dir / '.writer.lock')


def exclusive_collection(function):
    @wraps(function)
    def wrapped(settings, *args, **kwargs):
        with collection_write_lock(settings):
            return function(settings, *args, **kwargs)
    return wrapped
