"""Mappings from a mounted CD2 directory to STRM output and playback paths."""
import hashlib
import json
import re
import string
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import quote, urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Mapping(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(default='', pattern=r'^(?:[a-zA-Z0-9_-]{1,64})?$')
    name: str = Field(min_length=1, max_length=80)
    source_dir: str = Field(min_length=1, max_length=4096)
    target_dir: str = Field(min_length=1, max_length=4096)
    mode: Literal['路径', 'URL', '模板'] = '路径'
    playback_root: str = Field(default='/115', min_length=1, max_length=4096)
    emby_root: str = Field(default='', max_length=4096)
    enabled: bool = True

    @model_validator(mode='after')
    def valid(self):
        for value in (self.source_dir, self.target_dir, self.playback_root, self.emby_root):
            if any(c in value for c in ('\x00', '\r', '\n')):
                raise ValueError('路径不能包含换行或空字符')
        source, target = Path(self.source_dir), Path(self.target_dir)
        for path in (source, target):
            if not path.is_absolute() or path == Path('/') or '..' in path.parts:
                raise ValueError('源目录和输出目录必须是明确的绝对目录，不能使用根目录或 ..')
        self.source_dir, self.target_dir = str(source), str(target)
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError('源目录和输出目录不能相同或互相包含')
        if self.emby_root and not absolute(self.emby_root):
            raise ValueError('Emby 输出目录必须是绝对路径')
        if self.mode == '路径' and not absolute(self.playback_root):
            raise ValueError('STRM 路径前缀必须是绝对路径，例如 /115/电影')
        if self.mode == 'URL':
            parsed = urlsplit(self.playback_root)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError('URL 前缀需要完整的 http/https 地址，不包含查询参数或登录凭证')
            parsed.port
        if self.mode == '模板':
            parts = list(string.Formatter().parse(self.playback_root))
            if not any(field for _, field, _, _ in parts):
                raise ValueError('模板需要 {path}、{url_path} 或 {encoded_path} 占位符')
            if any(field not in (None, 'path', 'url_path', 'encoded_path') or spec or conversion for _, field, spec, conversion in parts):
                raise ValueError('模板只支持 {path}、{url_path}、{encoded_path}，不支持表达式')
        return self

    @property
    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(exclude={'name','enabled'}), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def content(self, relative):
        relative = clean_relative(relative)
        if self.mode == '路径':
            return join_playback(self.playback_root, relative)
        if self.mode == 'URL':
            return self.playback_root.rstrip('/') + '/' + quote(relative, safe='/')
        return self.playback_root.format(path=relative, url_path=quote(relative, safe='/'), encoded_path=quote(relative, safe=''))

    def emby_path(self, relative):
        return join_playback(self.emby_root or self.target_dir, relative)


def absolute(value):
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def join_playback(root, relative):
    path = PureWindowsPath(root) if re.match(r'^[A-Za-z]:[\\/]', root) else PurePosixPath(root)
    return str(path.joinpath(*PurePosixPath(clean_relative(relative)).parts))


def clean_relative(value):
    if not value or value.startswith('/') or '\\' in value or any(c in value for c in ('\x00','\r','\n')) or any(part in ('','.','..') for part in value.split('/')):
        raise ValueError('文件路径必须位于配置的源目录内')
    return value


def validate_boundaries(rule, others):
    """Resolve mount points off the event loop, including cross-rule overlap."""
    source, target = Path(rule.source_dir).resolve(), Path(rule.target_dir).resolve()
    def overlaps(a, b): return a == b or a.is_relative_to(b) or b.is_relative_to(a)
    if overlaps(source, target):
        raise ValueError('源目录和输出目录不能指向同一个位置或互相包含')
    for other in others:
        if other.id == rule.id: continue
        other_source, other_target = Path(other.source_dir).resolve(), Path(other.target_dir).resolve()
        if overlaps(target, other_source) or overlaps(other_target, source):
            raise ValueError('输出目录不能与其他映射的源目录重叠')
        if overlaps(target, other_target):
            raise ValueError('不同映射的输出目录不能重叠')
