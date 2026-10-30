"""Metadata-only scanning and deletion through anchored, non-symlink directories."""
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

MAX_DIRECTORY_ENTRIES = 100000


def identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def normalize(raw, *, keywords=False):
    values = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
    result, seen = [], set()
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.replace('，', ',') if keywords else value
        for item in text.replace(',', '\n').splitlines():
            item = item.strip()
            key = item.casefold() if keywords else item
            if item and key not in seen:
                seen.add(key)
                result.append(item)
    return result


def relative_parts(value):
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or '\x00' in value:
        raise ValueError('路径不在扫描目录内')
    return path.parts


@contextmanager
def directory(root, relative='', root_identity=None):
    """Every path component is opened without following symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        info = os.fstat(fd)
        if root_identity is not None and [info.st_dev, info.st_ino] != root_identity:
            raise ValueError('扫描目录已被替换，请重新扫描')
        for part in relative_parts(relative):
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    finally:
        os.close(fd)


def roots(values, temporary=''):
    candidates = [temporary] if temporary.strip() else normalize(values)
    result = []
    for value in candidates:
        try:
            path = Path(value).expanduser()
            if not path.is_absolute():
                raise ValueError('扫描路径必须为绝对路径')
            path = path.resolve(strict=True)
            with directory(path) as fd:
                info = os.fstat(fd)
            item = dict(path=str(path), identity=[info.st_dev, info.st_ino])
            if item not in result:
                result.append(item)
        except (OSError, ValueError) as exc:
            # An offline configured root must be visible rather than silently
            # making a partial scan look complete.
            raise ValueError(f'无法访问扫描目录 {value}：{exc}') from exc
    if not result:
        raise ValueError('未配置默认扫描路径，请先在插件设置中填写并保存')
    return [item for item in result if not any(
        item != other and Path(item['path']).is_relative_to(other['path']) for other in result)]


def list_directory(root, relative, keywords, remaining, max_entries=MAX_DIRECTORY_ENTRIES):
    folders, items, checked, truncated = [], [], 0, False
    with directory(root['path'], relative, root['identity']) as fd:
        with os.scandir(fd) as entries:
            for entry in entries:
                if checked >= min(MAX_DIRECTORY_ENTRIES, max_entries):
                    truncated = True
                    break
                checked += 1
                child = (PurePosixPath(relative) / entry.name).as_posix()
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    folders.append(child)
                    continue
                if not any(word in entry.name.casefold() for word in keywords):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                items.append(dict(root=root['path'], root_identity=root['identity'], relative_path=child,
                                  path=str(Path(root['path']) / child), name=entry.name, title=entry.name,
                                  size=info.st_size, identity=identity(info),
                                  modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                                  state='pending', error=''))
                if len(items) >= remaining:
                    truncated = True
                    break
    return folders, items, checked, truncated


def delete_candidate(item, keywords):
    parts = relative_parts(item['relative_path'])
    if not parts:
        raise ValueError('只允许删除扫描出的普通文件')
    name = parts[-1]
    if not keywords or not any(word in name.casefold() for word in keywords):
        raise ValueError('文件名不再匹配当前清理关键词')
    with directory(item['root'], '/'.join(parts[:-1]), item['root_identity']) as fd:
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('文件不是普通文件，或已被替换为符号链接')
        if identity(info) != item['identity']:
            raise ValueError('文件在扫描后发生变化，请重新扫描')
        os.unlink(name, dir_fd=fd)


def migrate_settings(path):
    """The host supplies a private migration snapshot, never the legacy file."""
    import json
    import tempfile
    snapshot = json.loads(path.read_text(encoding='utf-8'))
    old = snapshot['values']
    snapshot['values'] = {'scan_paths': normalize(old.get('scan_paths', [])),
                          'keywords': normalize(old.get('keywords', []), keywords=True)}
    descriptor, temporary = tempfile.mkstemp(prefix='.migration-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            json.dump(snapshot, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
