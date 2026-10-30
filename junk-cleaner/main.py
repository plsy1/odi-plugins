"""SDK1 junk cleaner: explicit scans, confirmed selection, no automatic deletion."""
import asyncio
import hashlib
from collections import deque
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import status
from pydantic import BaseModel, ConfigDict, Field
from kanojo_sdk import BasePlugin, ValidationError, Conflict, OperationFailed, MigrationRequired
from . import files

SCAN_LIMIT = 2000
ENTRY_LIMIT = 200000
PAGE_SIZE = 30


class ScanInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    path: str = Field(default='', max_length=4096)


class Selected(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    items: list[str] = Field(min_length=1, max_length=SCAN_LIMIT)


class DeleteInput(Selected):
    generation: str


class Plugin(BasePlugin):
    async def setup(self, ctx):
        self._lock = asyncio.Lock()
        ctx.tasks.register('scan', self.scan, input_schema=ScanInput, timeout=86400)
        ctx.tasks.register('delete', self.delete, input_schema=DeleteInput, timeout=3600, restart_policy='interrupt')

        @ctx.router.post('/scan', status_code=status.HTTP_202_ACCEPTED)
        async def scan(value: ScanInput):
            value = value.model_copy(update={'path': value.path.strip()})
            key = hashlib.sha256(value.path.encode()).hexdigest()
            return await ctx.tasks.submit('scan', value.model_dump(), dedupe_key='scan:' + key)

        @ctx.router.get('/candidates')
        async def candidates(cursor: str | None = None):
            current = await ctx.store.get('state', 'scan')
            if not current:
                return dict(items=[], next_cursor=None, has_more=False)
            page = await ctx.store.scan('scan:' + current['id'], cursor=cursor, limit=PAGE_SIZE)
            return dict(items=[dict(entry.value, id=current['id'] + ':' + entry.key) for entry in page.items],
                        next_cursor=page.next_cursor, has_more=page.has_more)

        @ctx.router.post('/delete', status_code=status.HTTP_202_ACCEPTED)
        async def delete(value: Selected):
            items = sorted(set(value.items))
            # Only server-issued IDs from the currently published scan are accepted.
            await self.selected(items)
            payload = DeleteInput(items=items, generation=ctx.plugin.generation)
            key = hashlib.sha256('\n'.join(items).encode()).hexdigest()
            return await ctx.tasks.submit('delete', payload.model_dump(), dedupe_key='delete:' + key)

    async def migrate(self, ctx, from_version, to_version):
        if (from_version, to_version) != (0, 1):
            raise MigrationRequired(from_version, to_version)
        await ctx.run_io(files.migrate_settings, ctx.paths.data('config.json'))
        ctx.log.info('垃圾清理插件配置已迁移至 SDK1')

    async def all_keys(self, namespace):
        keys, cursor = [], None
        while True:
            page = await self.ctx.store.scan(namespace, cursor=cursor, limit=200)
            keys.extend(entry.key for entry in page.items)
            if not page.has_more:
                return keys
            cursor = page.next_cursor

    async def scan(self, task, payload):
        request = ScanInput.model_validate(payload)
        async with self._lock:
            snapshot = await self.ctx.settings.get()
            keywords = [word.casefold() for word in files.normalize(snapshot.values.get('keywords', []), keywords=True)]
            if not keywords:
                raise ValidationError('未配置清理关键词，请先在本插件的清理设置中填写并保存')
            roots = await self.ctx.run_io(files.roots, snapshot.values.get('scan_paths', []), request.path.strip())
            pending = deque((root, '') for root in roots)
            items, checked, skipped, truncated, progress = [], 0, 0, False, 0
            self.ctx.log.info('开始扫描垃圾文件', roots=[root['path'] for root in roots])
            while pending and len(items) < SCAN_LIMIT and checked < ENTRY_LIMIT:
                task.check_cancelled()
                root, relative = pending.popleft()
                try:
                    folders, entries, count, limited = await self.ctx.run_io(files.list_directory, root, relative, keywords, SCAN_LIMIT - len(items), ENTRY_LIMIT - checked)
                except (OSError, ValueError) as exc:
                    skipped += 1
                    self.ctx.log.warning('垃圾文件目录扫描失败', root=root['path'], relative=relative, error=str(exc))
                    continue
                items.extend(entries)
                checked += count
                truncated |= limited
                pending.extend((root, folder) for folder in folders)
                if len(pending) > ENTRY_LIMIT:
                    truncated = True
                    break
                progress = max(progress, min(.85, .85 * checked / (checked + len(pending) * 256 + 1)))
                await task.progress(progress, f'已检查 {checked} 项，发现 {len(items)} 个候选文件')
            truncated |= bool(pending) or checked >= ENTRY_LIMIT
            items.sort(key=lambda item: (-item['size'], item['path']))
            previous = await self.ctx.store.get('state', 'scan')
            old_namespace = 'scan:' + previous['id'] if previous else None
            old_keys = await self.all_keys(old_namespace) if old_namespace else []
            scan_id = uuid4().hex
            meta = dict(id=scan_id, revision=snapshot.revision, roots=roots, temporary=bool(request.path.strip()),
                        count=len(items), checked=checked, skipped=skipped, truncated=truncated,
                        completed_at=datetime.now(timezone.utc).isoformat())
            # Publish a complete snapshot. Cancellation before commit leaves the
            # previous scan available and never authorises a partial result set.
            async with self.ctx.store.transaction() as tx:
                for index, item in enumerate(items):
                    task.check_cancelled()
                    await tx.put('scan:' + scan_id, f'{index:06d}', item)
                for key in old_keys:
                    task.check_cancelled()
                    await tx.delete(old_namespace, key)
                await tx.put('state', 'scan', meta)
            message = f'扫描完成，发现 {len(items)} 个候选文件'
            if truncated:
                message += f'；扫描达到限制（最多 {SCAN_LIMIT} 个候选、{ENTRY_LIMIT} 项），请缩小范围后重扫'
            if skipped:
                message += f'；{skipped} 个目录未能读取，详见日志'
            await task.progress(1, message)
            self.ctx.log.info('垃圾文件扫描完成', candidates=len(items), checked=checked, skipped=skipped, truncated=truncated)
            return dict(meta, message=message)

    async def selected(self, identifiers):
        current = await self.ctx.store.get('state', 'scan')
        if not current:
            raise Conflict('请先扫描，再选择需要清理的文件')
        snapshot = await self.ctx.settings.get()
        if current['revision'] != snapshot.revision:
            raise Conflict('清理设置已变化，请重新扫描后再确认删除')
        items = []
        for identifier in identifiers:
            parts = identifier.split(':')
            if len(parts) != 2 or parts[0] != current['id'] or len(parts[1]) != 6 or not parts[1].isascii() or not parts[1].isdigit():
                raise Conflict('扫描结果已过期或选择无效，请重新扫描')
            item = await self.ctx.store.get('scan:' + current['id'], parts[1])
            if item is None:
                raise Conflict('所选文件已清理或扫描结果已过期，请刷新列表')
            items.append((parts[1], item))
        return current, snapshot, items

    async def delete(self, task, payload):
        request = DeleteInput.model_validate(payload)
        if request.generation != self.ctx.plugin.generation:
            raise Conflict('插件已重载或重启，请重新选择文件并确认删除')
        async with self._lock:
            current, snapshot, selected = await self.selected(list(dict.fromkeys(request.items)))
            keywords = [word.casefold() for word in files.normalize(snapshot.values.get('keywords', []), keywords=True)]
            if not keywords:
                raise ValidationError('未配置本插件的清理关键词，无法删除')
            allowed = current['roots'] if current['temporary'] else await self.ctx.run_io(files.roots, snapshot.values.get('scan_paths', []))
            deleted, failed, released = 0, 0, 0
            namespace = 'scan:' + current['id']
            for index, (key, item) in enumerate(selected):
                task.check_cancelled()
                latest = await self.ctx.settings.get()
                if latest.revision != snapshot.revision:
                    raise Conflict(f'清理设置已变化，已停止；此前已删除 {deleted} 个文件')
                try:
                    if not any(root['path'] == item['root'] and root['identity'] == item['root_identity'] for root in allowed):
                        raise ValueError('文件不在当前允许的扫描目录内')
                    await self.ctx.run_io(files.delete_candidate, item, keywords)
                except (OSError, ValueError) as exc:
                    failed += 1
                    await self.ctx.store.put(namespace, key, dict(item, state='failed', error=str(exc)))
                    self.ctx.log.warning('垃圾文件清理已跳过', path=item['path'], error=str(exc))
                else:
                    deleted += 1
                    released += item['size']
                    await self.ctx.store.delete(namespace, key)
                await task.checkpoint(dict(deleted=deleted, failed=failed, released_bytes=released))
                await task.progress((index + 1) / len(selected), f'已删除 {deleted} 个文件，跳过 {failed} 个')
            self.ctx.log.info('垃圾文件清理完成', deleted=deleted, failed=failed, released_bytes=released)
            if failed:
                raise OperationFailed(f'已删除 {deleted} 个文件，跳过 {failed} 个；原因保留在候选列表中，请检查后重新扫描')
            return dict(deleted=deleted, failed=failed, released_bytes=released)
