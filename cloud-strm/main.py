"""CD2 mount -> STRM exports, using only Kanojo's public SDK."""
import asyncio
import hashlib
import re
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4
from fastapi import status
from pydantic import BaseModel, ConfigDict, Field
from kanojo_sdk import BasePlugin, Conflict, LibraryUpdate, NotFound, OperationFailed, PluginBusy, ValidationError
from .config import Mapping, clean_relative, validate_boundaries
from . import files


def timestamp(): return datetime.now(timezone.utc).isoformat()
def key_for(*parts): return hashlib.sha256('\0'.join(parts).encode()).hexdigest()


class ScanInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    rule_id: str = ''
    record_id: str = ''
    force: bool = False
    refresh_only: bool = False


class Selected(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1, max_length=128)


class Plugin(BasePlugin):
    async def setup(self, ctx):
        self._run_lock = asyncio.Lock()
        self._first_poll = True
        ctx.tasks.register('scan', self.scan, input_schema=ScanInput, timeout=86400)
        ctx.jobs.interval('poll', self.poll, seconds_setting='poll_seconds', run_on_start=True)

        @ctx.router.get('/rules')
        async def rules(cursor: str | None = None):
            page = await ctx.store.scan('rules', cursor=cursor, limit=30)
            return dict(items=[dict(**entry.value, preview=Mapping.model_validate(entry.value).content('影片/ABC-001.mkv')) for entry in page.items], next_cursor=page.next_cursor, has_more=page.has_more)

        @ctx.router.post('/rules')
        async def save_rule(value: Mapping):
            if self._run_lock.locked(): raise PluginBusy('正在生成 STRM，请完成或取消后再修改目录映射')
            async with self._run_lock:
                rule = value.model_copy(update={'id': value.id or uuid4().hex})
                existing = await self.all_rules()
                if rule.id not in {r.id for r in existing} and len(existing) >= 50:
                    raise ValidationError('最多支持 50 个目录映射')
                try:
                    await ctx.run_io(validate_boundaries, rule, existing)
                except ValueError as exc:
                    raise ValidationError(str(exc)) from exc
                await ctx.store.put('rules', rule.id, rule.model_dump())
                return {'message': f'已保存目录映射「{rule.name}」', 'id': rule.id}

        @ctx.router.post('/rules/remove')
        async def remove_rule(value: Selected):
            if self._run_lock.locked(): raise PluginBusy('扫描期间不能移除目录映射')
            async with self._run_lock:
                if await ctx.store.get('rules', value.id) is None: raise NotFound('目录映射不存在')
                await ctx.store.delete('rules', value.id)
            return {'message': '已移除映射，生成的 STRM 和源文件均保留'}

        @ctx.router.post('/scan', status_code=status.HTTP_202_ACCEPTED)
        async def start(value: ScanInput): return await self.submit(value)

        @ctx.router.post('/rules/scan', status_code=status.HTTP_202_ACCEPTED)
        async def scan_rule(value: Selected): return await self.submit(ScanInput(rule_id=value.id))

        @ctx.router.post('/retry', status_code=status.HTTP_202_ACCEPTED)
        async def retry(value: Selected): return await self.submit(ScanInput(record_id=value.id, force=True))

        @ctx.router.post('/rebuild', status_code=status.HTTP_202_ACCEPTED)
        async def rebuild(): return await self.submit(ScanInput(force=True))

        @ctx.router.post('/refresh', status_code=status.HTTP_202_ACCEPTED)
        async def refresh(): return await self.submit(ScanInput(refresh_only=True))

        @ctx.router.get('/records')
        async def records(cursor: str | None = None): return await self.page('records', cursor)

        @ctx.router.get('/runs')
        async def runs(cursor: str | None = None): return await self.page('runs', cursor)

        @ctx.router.get('/refresh')
        async def pending(cursor: str | None = None): return await self.page('refresh', cursor)

    async def page(self, namespace, cursor):
        page = await self.ctx.store.scan(namespace, cursor=cursor, limit=30)
        return dict(items=[dict(entry.value, id=entry.key) for entry in page.items], next_cursor=page.next_cursor, has_more=page.has_more)

    async def all_rules(self):
        page = await self.ctx.store.scan('rules', limit=100)
        return [Mapping.model_validate(item.value) for item in page.items]

    async def submit(self, request):
        payload = request.model_dump()
        return await self.ctx.tasks.submit('scan', payload, dedupe_key='scan:' + key_for(request.model_dump_json()))

    async def poll(self):
        values = (await self.ctx.settings.get()).values
        initial, self._first_poll = self._first_poll, False
        if initial and not values['scan_on_start']: return
        if not initial and not values['auto_scan']: return
        if not self._run_lock.locked() and any(rule.enabled for rule in await self.all_rules()):
            await self.submit(ScanInput())

    async def inventory(self, rule, task, record=None):
        if record:
            relative = clean_relative(record['relative'])
            def stat_one():
                path = files.output_path(rule.source_dir, relative)
                info = path.stat()
                if not path.is_file(): raise ValueError('源文件不是普通文件')
                return files.Entry(relative, info.st_size, info.st_mtime_ns)
            return [await self.ctx.run_io(stat_one)]
        pending, items = deque(['']), []
        while pending:
            task.check_cancelled()
            folders, entries = await self.ctx.run_io(files.list_folder, rule.source_dir, pending.popleft())
            pending.extend(folders); items.extend(entries)
            if len(items) + len(pending) > 200000:
                raise ValueError('目录项目超过 200000，请拆分映射范围')
        return items

    @staticmethod
    def file_kind(relative, settings):
        suffix = PurePosixPath(relative).suffix.casefold()
        if suffix in {'.' + ext.strip().lstrip('.').casefold() for ext in settings['video_extensions']}:
            return 'video'
        if settings['copy_subtitles'] and suffix in {'.srt','.ass','.ssa','.sub','.idx','.vtt','.sup'}: return 'metadata'
        if settings['copy_nfo'] and suffix == '.nfo': return 'metadata'
        if settings['copy_images'] and suffix in {'.jpg','.jpeg','.png','.webp'}: return 'metadata'
        return None

    async def process(self, rule, entry, settings, force=False):
        relative, kind = entry.relative, self.file_kind(entry.relative, settings)
        if not kind: return 'ignored'
        key = key_for(rule.id, relative)
        previous = await self.ctx.store.get('records', key) or {}
        intent = await self.ctx.store.get('intents', key) or {}
        destination = str(PurePosixPath(relative).with_suffix('.strm')) if kind == 'video' else relative
        maximum = settings['metadata_max_mb'] * 1024 * 1024 if kind != 'video' else 16384
        record = dict(rule_id=rule.id, rule=rule.name, relative=relative, title=relative, destination=str(Path(rule.target_dir)/destination), observed_at=timestamp(), state='completed', error='', fingerprint=entry.fingerprint, rule_digest=rule.digest, owned=False)
        try:
            output = await self.ctx.run_io(files.output_path, rule.target_dir, destination)
            old_hash = await self.ctx.run_io(files.digest, output, maximum)
            if kind == 'metadata' and not force and not intent and previous.get('fingerprint') == entry.fingerprint and previous.get('rule_digest') == rule.digest and previous.get('content_hash') == old_hash and old_hash and (not settings['refresh_emby'] or previous.get('refresh_requested')):
                return 'skipped'
            if kind == 'video':
                data = rule.content(relative).encode('utf-8')
                if len(data) > maximum: raise ValueError('STRM 内容超出大小限制')
            else:
                data = await self.ctx.run_io(files.read_metadata, rule.source_dir, relative, maximum)
            new_hash = hashlib.sha256(data).hexdigest()
            if not force and not intent and previous.get('state') == 'completed' and previous.get('destination') == record['destination'] and previous.get('content_hash') == new_hash == old_hash and previous.get('fingerprint') == entry.fingerprint and previous.get('rule_digest') == rule.digest and previous.get('refresh_requested') == settings['refresh_emby']:
                return 'skipped'
            owned = any(item.get('owned') and item.get('destination') == record['destination'] and item.get('content_hash') == old_hash for item in (previous, intent))
            if old_hash is not None and old_hash != new_hash and not owned:
                raise Conflict('目标文件已存在且不属于本插件，已保留原文件')
            record.update(content_hash=new_hash, owned=old_hash is None or owned)
            changed = old_hash != new_hash
            emby_path = rule.emby_path(destination)
            parent_type = PureWindowsPath if re.match(r'^[A-Za-z]:[\\/]', emby_path) else PurePosixPath
            refresh_path = emby_path if kind == 'video' else str(parent_type(emby_path).parent)
            pending_refresh = dict(path=refresh_path, title=refresh_path, rule_id=rule.id, update_type='Created' if old_hash is None else 'Modified', state='queued', error='')
            needs_refresh = settings['refresh_emby'] and (changed or force or previous.get('rule_digest') != rule.digest or not previous.get('refresh_requested'))
            record['refresh_requested'] = settings['refresh_emby']
            if needs_refresh: record['pending_refresh'] = pending_refresh
            if changed:
                # Persist intent before writing. A crash after rename can then
                # identify our output and recover both ownership and refresh.
                record['pending_refresh'] = pending_refresh if settings['refresh_emby'] else None
                await self.ctx.store.put('intents', key, record)
                await self.ctx.run_io(files.atomic_write, rule.target_dir, destination, data, old_hash)
            async with self.ctx.store.transaction() as tx:
                pending = record.pop('pending_refresh', None) or intent.get('pending_refresh')
                if pending: await tx.put('refresh', key_for(pending['path']), pending)
                await tx.put('records', key, record)
                await tx.delete('intents', key)
            return ('created' if old_hash is None else 'updated') if changed else 'skipped'
        except Exception as exc:
            record.update(state='error', error=str(exc))
            # Keep the last known hash so a retry can recognise an output
            # that this plugin wrote before a later operation failed.
            if previous.get('owned') and previous.get('destination') == record['destination']:
                for field in ('owned','content_hash'): record[field] = previous.get(field)
            await self.ctx.store.put('records', key, record)
            self.ctx.log.warning('STRM 文件处理失败', rule_id=rule.id, relative=relative, error=str(exc))
            return 'failed'

    async def flush_refresh(self, task):
        refreshed, cursor = 0, None
        while True:
            task.check_cancelled()
            page = await self.ctx.store.scan('refresh', cursor=cursor, limit=100)
            if not page.items: break
            try:
                await self.ctx.library.refresh([LibraryUpdate(path=entry.value['path'], update_type=entry.value['update_type']) for entry in page.items])
            except Exception as exc:
                async with self.ctx.store.transaction() as tx:
                    for entry in page.items: await tx.put('refresh', entry.key, dict(entry.value, state='error', error=str(exc)))
                raise
            async with self.ctx.store.transaction() as tx:
                for entry in page.items: await tx.delete('refresh', entry.key)
            refreshed += len(page.items)
            if not page.has_more: break
            cursor = page.next_cursor
        return refreshed

    async def scan(self, task, payload):
        request = ScanInput.model_validate(payload)
        async with self._run_lock:
            settings = (await self.ctx.settings.get()).values
            run = dict(id=f'{9999999999999999999-time.time_ns():019d}:'+task.task_id, started_at=timestamp(), state='running', checked=0, created=0, updated=0, skipped=0, failed=0, refreshed=0, error='')
            record = None
            try:
                if not request.refresh_only:
                    rules = await self.all_rules()
                    if request.record_id:
                        record = await self.ctx.store.get('records', request.record_id)
                        if not record: raise NotFound('生成记录不存在')
                        request = request.model_copy(update={'rule_id':record['rule_id']})
                    rules = [rule for rule in rules if rule.enabled and (not request.rule_id or rule.id == request.rule_id)]
                    if not rules: raise ValidationError('请先添加并启用目录映射')
                    for index, rule in enumerate(rules):
                        await task.progress(.9*index/max(1,len(rules)), f'正在扫描目录：{rule.name}')
                        try: entries = await self.inventory(rule, task, record)
                        except Exception as exc:
                            run['failed'] += 1; run['error'] = f'{rule.name}：{exc}'
                            self.ctx.log.warning('源目录扫描失败，保留已有 STRM 和索引', rule_id=rule.id, error=str(exc))
                            continue
                        for offset, entry in enumerate(entries):
                            task.check_cancelled()
                            outcome = await self.process(rule, entry, settings, request.force)
                            if outcome != 'ignored':
                                run['checked'] += 1; run[outcome] += 1
                            if offset % 20 == 0 or offset+1 == len(entries):
                                await task.progress(.9*(index+(offset+1)/max(1,len(entries)))/len(rules), f"已检查 {run['checked']}，新增 {run['created']}，更新 {run['updated']}，跳过 {run['skipped']}，失败 {run['failed']}")
                if settings['refresh_emby'] or request.refresh_only:
                    await task.progress(.95, '正在通知 Emby 刷新已生成的文件')
                    run['refreshed'] = await self.flush_refresh(task)
                if run['failed']: raise OperationFailed(run['error'] or f"有 {run['failed']} 项处理失败，可在生成记录中重试")
                run['state'] = 'succeeded'
                await task.progress(1, f"完成：新增 {run['created']}，更新 {run['updated']}，跳过 {run['skipped']}")
                return run
            except asyncio.CancelledError:
                run['state'] = 'cancelled'
                raise
            except Exception as exc:
                run.update(state='failed', error=str(exc))
                raise
            finally:
                run['finished_at'] = timestamp()
                try:
                    await self.ctx.store.put('runs', run['id'], run)
                    # Bound summaries; generated-file ownership is retained.
                    history = await self.ctx.store.scan('runs', limit=101)
                    if len(history.items)>100: await self.ctx.store.delete('runs', history.items[-1].key)
                except Exception:
                    if run['state'] != 'cancelled': raise
