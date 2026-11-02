"""Bounded metadata-only reads and atomic writes. Source videos are never opened."""
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from .config import clean_relative

SKIP_DIRS = {'@Recycle','#recycle','@eaDir','extrafanart','$RECYCLE.BIN'}


@dataclass(frozen=True)
class Entry:
    relative: str
    size: int
    modified: int

    @property
    def fingerprint(self): return f'{self.size}:{self.modified}'


MAX_FOLDER_ITEMS = 100_000
MAX_SCAN_ITEMS = 200_000
SCAN_BATCH_SIZE = 100


def source_folder(root: str, relative: str):
    base = Path(root)
    folder = base / relative if relative else base
    if folder.is_symlink() or not folder.resolve().is_relative_to(base.resolve()):
        raise ValueError('跳过源目录外的符号链接')
    return folder


def list_folder(root: str, relative: str, extensions):
    folder = source_folder(root, relative)
    folders, names, examined = [], [], 0
    with os.scandir(folder) as iterator:
        for item in iterator:
            examined += 1
            if examined > MAX_FOLDER_ITEMS:
                raise ValueError('单个目录超过 100000 项，请拆分目录映射')
            if item.name.startswith('.') or item.name in SKIP_DIRS or item.is_symlink(): continue
            name = '/'.join(filter(None, (relative, item.name)))
            clean_relative(name)
            if item.is_dir(follow_symlinks=False):
                folders.append(name)
            elif PurePosixPath(item.name).suffix.casefold() in extensions and item.is_file(follow_symlinks=False):
                # Keep only names here. Unused files never need size/mtime, and
                # metadata is read in small batches just before processing.
                names.append(name)
    return sorted(folders), sorted(names), examined


def stat_batch(root: str, names):
    if not names: return []
    parent = str(PurePosixPath(names[0]).parent)
    folder = source_folder(root, '' if parent == '.' else parent)
    entries = []
    for name in names:
        path = PurePosixPath(clean_relative(name))
        if str(path.parent) != parent:
            raise ValueError('扫描批次包含不同目录')
        info = (folder / path.name).lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('源文件不是普通文件')
        entries.append(Entry(name, info.st_size, info.st_mtime_ns))
    return entries


def output_path(root: str, relative: str):
    base = Path(root)
    if base.is_symlink(): raise ValueError('源/输出根目录不能是符号链接')
    target = base.joinpath(*PurePosixPath(clean_relative(relative)).parts)
    # Every descendant is checked, including directories that already exist.
    for part in (target, *target.parents):
        if part == base: break
        if part.is_symlink(): raise ValueError('输出路径包含符号链接，已停止写入')
    if not target.parent.resolve().is_relative_to(base.resolve()):
        raise ValueError('输出路径超出配置的目标目录')
    return target


def digest(path: Path, max_bytes: int):
    if not path.exists(): return None
    if path.is_symlink() or not path.is_file(): raise ValueError('目标不是普通文件')
    if path.stat().st_size > max_bytes: return 'oversized'
    hasher = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(128*1024), b''): hasher.update(chunk)
    return hasher.hexdigest()


def read_metadata(root: str, relative: str, maximum: int):
    source = output_path(root, relative)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError('元数据文件超出大小限制或不是普通文件')
        data = handle.read(maximum + 1)
        if len(data) > maximum: raise ValueError('元数据文件超出大小限制')
        return data


def atomic_write(root: str, relative: str, data: bytes, expected: str | None):
    target = output_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = output_path(root, relative)
    if digest(target, max(len(data), 32*1024*1024)) != expected:
        raise ValueError('目标文件在处理期间发生变化，已保留原文件')
    fd, name = tempfile.mkstemp(prefix='.cloud-strm-', dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        # Exclusive create protects a file that appears after the initial check.
        if expected is None:
            os.link(temporary, target)
        else:
            if digest(target, max(len(data), 32*1024*1024)) != expected:
                raise ValueError('目标文件已被其他程序修改')
            os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
