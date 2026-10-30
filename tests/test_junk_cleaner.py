import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from kanojo_sdk import Conflict, OperationFailed, ValidationError
from kanojo_sdk.manifest import Manifest
from kanojo_sdk.testing import FakeContext
from modules.plugins import PluginManager
from modules.plugins.transactions import install_staged

SOURCE = Path(__file__).resolve().parents[1] / 'junk-cleaner'
package = types.ModuleType('junk_cleaner_test')
package.__path__ = [str(SOURCE)]
sys.modules[package.__name__] = package
spec = importlib.util.spec_from_file_location('junk_cleaner_test.main', SOURCE / 'main.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
MANIFEST = json.loads((SOURCE / 'plugin.json').read_text())


class Task:
    task_id = 'test'
    def __init__(self): self.messages = []; self.saved = None
    def check_cancelled(self): pass
    async def progress(self, value, message=''): self.messages.append(message)
    async def checkpoint(self, value): self.saved = value


class JunkCleanerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.fake = FakeContext(fields=MANIFEST['settings']['fields'], settings={'scan_paths':[str(self.downloads)], 'keywords':['广告']})
        self.ctx = await self.fake.__aenter__()
        self.plugin = await self.ctx.start(module.Plugin())
        self.task = Task()

    async def asyncTearDown(self):
        await self.fake.__aexit__(None, None, None)
        self.temp.cleanup()

    def file(self, name, root=None, size=7):
        path = (root or self.downloads) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        return path

    async def settings(self, **values):
        snapshot = await self.ctx.settings.get()
        return await self.ctx.settings.patch(values, expected_revision=snapshot.revision)

    async def scan(self, path=''):
        return await self.plugin.scan(self.task, {'path':path})

    async def candidates(self):
        return await next(r.endpoint for r in self.ctx.router.routes if r.path == '/candidates')()

    async def identifiers(self):
        return [item['id'] for item in (await self.candidates())['items']]

    async def delete(self, items=None, task=None, generation=None):
        return await self.plugin.delete(task or self.task, {'items':items or await self.identifiers(), 'generation':generation or self.ctx.plugin.generation})

    async def test_scan_independent_keywords_name_only_and_size_order(self):
        await self.settings(keywords=['广告， AD,ad'])
        first = self.file('广告.txt', size=20)
        self.file('ad.url', size=5)
        self.file('广告目录/movie.mkv')
        self.file('movie.mkv')
        result = await self.scan()
        rows = (await self.candidates())['items']
        self.assertEqual(result['count'], 2)
        self.assertEqual([r['name'] for r in rows], ['广告.txt','ad.url'])
        self.assertTrue(first.exists(), 'Scanning must never delete')
        self.assertEqual(module.files.normalize(['广告， AD,ad'], keywords=True), ['广告','AD'])

    async def test_empty_keywords_prevent_scan_and_deletion(self):
        target = self.file('广告.txt')
        await self.scan(); ids = await self.identifiers()
        await self.settings(keywords=[])
        with self.assertRaises(ValidationError): await self.scan()
        with self.assertRaises(Conflict): await self.delete(ids)
        self.assertTrue(target.exists())

    async def test_nested_roots_are_deduplicated_and_paginated(self):
        for i in range(35): self.file(f'nested/广告-{i:02d}.txt')
        await self.settings(scan_paths=[str(self.downloads),str(self.downloads/'nested')])
        self.assertEqual((await self.scan())['count'],35)
        first = await self.candidates()
        endpoint = next(r.endpoint for r in self.ctx.router.routes if r.path == '/candidates')
        second = await endpoint(cursor=first['next_cursor'])
        self.assertEqual((len(first['items']),len(second['items'])), (30,5))
        self.assertTrue(first['has_more']); self.assertFalse(second['has_more'])

    async def test_delete_only_selected_and_reject_raw_or_outdated_ids(self):
        junk = self.file('广告.txt')
        keep = self.file('movie.mkv')
        outside = self.file('广告-outside.txt', root=self.root)
        await self.scan()
        for value in (str(outside), '../广告.txt', '0'*32+':000000'):
            with self.assertRaises(Conflict): await self.plugin.selected([value])
        ids = await self.identifiers()
        result = await self.delete(ids)
        self.assertEqual(result['deleted'],1)
        self.assertFalse(junk.exists()); self.assertTrue(keep.exists()); self.assertTrue(outside.exists())
        self.assertEqual((await self.candidates())['items'], [])
        with self.assertRaises(Conflict): await self.delete(ids)

    async def test_custom_root_authority_comes_from_completed_scan(self):
        other = self.root/'other'; other.mkdir()
        junk = self.file('广告.txt', root=other)
        self.file('广告-default.txt')
        await self.scan('  '+str(other)+'  ')
        self.assertEqual((await self.delete())['deleted'],1)
        self.assertFalse(junk.exists())
        self.assertTrue((self.downloads/'广告-default.txt').exists())

    async def test_changed_file_and_replaced_inode_are_preserved(self):
        target = self.file('广告.txt')
        await self.scan(); ids = await self.identifiers()
        target.write_text('changed file')
        with self.assertRaises(OperationFailed): await self.delete(ids)
        self.assertTrue(target.exists())
        self.assertIn('发生变化', (await self.candidates())['items'][0]['error'])
        await self.scan(); ids = await self.identifiers()
        replacement = self.file('replacement.txt', size=12)
        replacement.replace(target)
        with self.assertRaises(OperationFailed): await self.delete(ids)
        self.assertTrue(target.exists())

    async def test_scan_skips_symlinks_and_delete_rechecks_file_and_parent(self):
        outside = self.file('广告-outside.txt', root=self.root)
        (self.downloads/'广告-link.txt').symlink_to(outside)
        (self.downloads/'linked-folder').symlink_to(self.root, target_is_directory=True)
        self.assertEqual((await self.scan())['count'],0)
        target = self.file('folder/广告.txt')
        await self.scan(); ids = await self.identifiers()
        (self.downloads/'folder').rename(self.root/'moved')
        (self.downloads/'folder').symlink_to(self.root/'moved', target_is_directory=True)
        with self.assertRaises(OperationFailed): await self.delete(ids)
        self.assertTrue((self.root/'moved/广告.txt').exists())
        (self.downloads/'folder').unlink(); (self.root/'moved').rename(self.downloads/'folder')
        await self.scan(); ids = await self.identifiers()
        target.unlink(); target.symlink_to(outside)
        with self.assertRaises(OperationFailed): await self.delete(ids)
        self.assertTrue(outside.exists()); self.assertTrue(target.is_symlink())

    async def test_root_replacement_and_keyword_change_invalidate_deletion(self):
        self.file('广告.txt')
        await self.scan(); ids = await self.identifiers()
        self.downloads.rename(self.root/'old-root'); self.downloads.mkdir()
        replacement = self.file('广告.txt')
        with self.assertRaises(OperationFailed): await self.delete(ids)
        self.assertTrue(replacement.exists())
        await self.scan(); ids = await self.identifiers()
        await self.settings(keywords=['游戏大全'])
        with self.assertRaises(Conflict): await self.delete(ids)
        self.assertTrue(replacement.exists())

    async def test_new_scan_retires_old_ids_and_storage(self):
        self.file('广告.txt')
        first = await self.scan(); ids = await self.identifiers()
        await self.scan()
        with self.assertRaises(Conflict): await self.delete(ids)
        self.assertEqual((await self.ctx.store.scan('scan:'+first['id'])).items, [])

    async def test_limit_is_explicit_and_cancelled_scan_keeps_previous_results(self):
        for i in range(5): self.file(f'广告-{i}.txt')
        with patch.object(module, 'SCAN_LIMIT', 3): result = await self.scan()
        self.assertEqual(result['count'],3); self.assertTrue(result['truncated'])
        before = await self.identifiers()
        original = self.task.check_cancelled
        self.task.check_cancelled = lambda: (_ for _ in ()).throw(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError): await self.scan()
        self.task.check_cancelled = original
        self.assertEqual(await self.identifiers(), before)
        self.assertEqual(len(list(self.downloads.iterdir())), 5)

    async def test_entry_limit_does_not_overrun_within_a_large_directory(self):
        for number in range(5): self.file(f'广告-{number}.txt')
        with patch.object(module, 'ENTRY_LIMIT', 2): result=await self.scan()
        self.assertEqual(result['checked'],2)
        self.assertEqual(result['count'],2)
        self.assertTrue(result['truncated'])

    async def test_cancel_deletion_stops_remaining_files(self):
        self.file('广告-a.txt'); self.file('广告-b.txt')
        await self.scan()
        reached = asyncio.Event()
        async def progress(*_):
            reached.set()
            await asyncio.Event().wait()
        task = Task(); task.progress = progress
        deleting = asyncio.create_task(self.delete(task=task))
        await asyncio.wait_for(reached.wait(),1)
        deleting.cancel()
        with self.assertRaises(asyncio.CancelledError): await deleting
        self.assertEqual(len(list(self.downloads.iterdir())),1)
        self.assertEqual(len((await self.candidates())['items']),1)

    async def test_changed_settings_during_delete_stop_later_files(self):
        self.file('广告-a.txt'); self.file('广告-b.txt')
        await self.scan()
        async def progress(*_): await self.settings(keywords=['new'])
        task=Task(); task.progress=progress
        with self.assertRaises(Conflict): await self.delete(task=task)
        self.assertEqual(len(list(self.downloads.iterdir())),1)

    async def test_old_generation_never_replays_confirmed_deletion(self):
        target = self.file('广告.txt'); await self.scan()
        with self.assertRaises(Conflict): await self.delete(generation='old-generation')
        self.assertTrue(target.exists())

    async def test_sdk_managed_tasks_and_manifest(self):
        Manifest.model_validate(MANIFEST)
        self.file('广告.txt')
        ref = await self.ctx.tasks.submit('scan', {'path':''})
        result = await self.ctx.tasks.wait(ref.task_id)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['result']['count'],1)
        self.assertTrue((self.downloads/'广告.txt').exists())


class JunkMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_installer_migrates_legacy_text_and_lists_without_actions(self):
        for raw in ({'scan_paths':'/downloads\n/media', 'keywords':'广告， AD,ad'},
                    {'scan_paths':['/downloads','/downloads'], 'keywords':['广告','AD','ad']},
                    {'scan_paths':['/downloads']}):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                source=root/'plugins/junk-cleaner'; source.mkdir(parents=True)
                (source/'plugin.json').write_text(json.dumps(dict(id='junk-cleaner',name='旧清理插件',version='1.2.0')))
                (source/'main.py').write_text('class Plugin:\n def setup(self, ctx): pass\n')
                (source/'config.json').write_text(json.dumps(raw))
                manager=PluginManager(root/'plugins',root/'state.json',data_root=root,host_version='1.2.1')
                await manager.startup(FastAPI())
                try:
                    await manager.enable('junk-cleaner')
                    await install_staged(manager,SOURCE,MANIFEST)
                    record=manager.records['junk-cleaner']
                    saved=(await manager.settings_for(record).get()).values
                    self.assertTrue(record.loaded); self.assertEqual(record.api_version,1)
                    self.assertEqual(saved['scan_paths'],module.files.normalize(raw['scan_paths']))
                    self.assertEqual(saved['keywords'],module.files.normalize(raw.get('keywords',[]),keywords=True))
                    self.assertEqual(manager.task_host.list(record.id),[])
                    self.assertFalse((record.path/'config.json').exists())
                finally: await manager.shutdown()

    async def test_queued_delete_is_rejected_after_host_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); downloads=root/'downloads';downloads.mkdir()
            target=downloads/'广告.txt';target.write_text('keep until confirmed again')
            manager=PluginManager(root/'plugins',root/'state.json',data_root=root,host_version='1.2.1')
            await manager.startup(FastAPI())
            async def terminal(task_id):
                for _ in range(100):
                    item=manager.task_host.get('junk-cleaner',task_id)
                    if item['status'] in ('succeeded','failed','cancelled'): return item
                    await asyncio.sleep(.01)
                self.fail('Task did not finish')
            try:
                await install_staged(manager,SOURCE,MANIFEST)
                record=await manager.enable('junk-cleaner')
                await record.context.settings.patch({'scan_paths':[str(downloads)],'keywords':['广告']},expected_revision=0)
                scan=await record.context.tasks.submit('scan',{'path':''})
                self.assertEqual((await terminal(scan.task_id))['status'],'succeeded')
                current=await record.context.store.get('state','scan')
                entries=await record.context.store.scan('scan:'+current['id'])
                ids=[current['id']+':'+entry.key for entry in entries.items]
                started=asyncio.Event()
                async def blocking(task,payload):
                    started.set()
                    await asyncio.Event().wait()
                record.context.tasks.definitions['scan']=(blocking,None,600,'interrupt')
                await record.context.tasks.submit('scan',{'path':''})
                await asyncio.wait_for(started.wait(),1)
                queued=await record.context.tasks.submit('delete',{'items':ids,'generation':record.context.plugin.generation})
                await manager.shutdown()
                manager=PluginManager(root/'plugins',root/'state.json',data_root=root,host_version='1.2.1')
                await manager.startup(FastAPI())
                result=await terminal(queued.task_id)
                self.assertEqual(result['status'],'failed')
                self.assertIn('重新选择',result['error'])
                self.assertTrue(target.exists())
            finally: await manager.shutdown()

    async def test_deterministic_sdk_package_and_release_metadata(self):
        import hashlib
        import zipfile
        spec=importlib.util.spec_from_file_location('junk_packager',SOURCE.parent/'tools/package_sdk.py')
        packager=importlib.util.module_from_spec(spec);spec.loader.exec_module(packager)
        with tempfile.TemporaryDirectory() as temporary:
            archive,entry=packager.package('junk-cleaner',temporary)
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            second,metadata=packager.package('junk-cleaner',temporary)
            self.assertEqual(digest,hashlib.sha256(second.read_bytes()).hexdigest())
            self.assertEqual(metadata,entry)
            self.assertEqual(entry['releases'][0]['sha256'],digest)
            self.assertEqual(entry['releases'][0]['version'],'2.0.0')
            self.assertIn('SDK1',entry['releases'][0]['release_notes'])
            with zipfile.ZipFile(archive) as handle:
                self.assertIn('junk-cleaner/files.py',handle.namelist())
                self.assertFalse(any('config.json' in name or '__pycache__' in name for name in handle.namelist()))

    async def test_invalid_migration_restores_legacy_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); source=root/'plugins/junk-cleaner'; source.mkdir(parents=True)
            (source/'plugin.json').write_text(json.dumps(dict(id='junk-cleaner',name='旧清理插件',version='1.2.0')))
            (source/'main.py').write_text('class Plugin:\n def setup(self, ctx): pass\n')
            raw={'scan_paths':['/downloads'],'keywords':[str(i) for i in range(501)]}
            (source/'config.json').write_text(json.dumps(raw))
            manager=PluginManager(root/'plugins',root/'state.json',data_root=root,host_version='1.2.1')
            await manager.startup(FastAPI())
            try:
                await manager.enable('junk-cleaner')
                with self.assertRaises(ValidationError): await install_staged(manager,SOURCE,MANIFEST)
                self.assertEqual(manager.records['junk-cleaner'].api_version,0)
                self.assertTrue(manager.records['junk-cleaner'].loaded)
                self.assertEqual(json.loads((source/'config.json').read_text()),raw)
            finally: await manager.shutdown()


if __name__ == '__main__': unittest.main()
