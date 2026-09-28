"""The same nonblocking process lock for the server and offline maintenance."""
import errno
import os


def lock_file(fd):
    """Hold an exclusive lock until fd is closed; never wait for another owner."""
    if os.name == 'nt':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BlockingIOError('База уже используется другим процессом.') from exc
            raise
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def check_private_file(path):
    # Windows access is controlled by NTFS ACLs, not POSIX mode bits.
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise ValueError('Доступ к .env слишком широкий; выполните chmod 600 для этого файла.')


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
