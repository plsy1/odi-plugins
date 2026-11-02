import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from kanojo_sdk import OperationFailed
from kanojo_sdk.testing import FakeContext

PLUGIN = Path(__file__).resolve().parents[1]/'cloud-strm'
package = types.ModuleType('cloud_strm_test'); package.__path__ = [str(PLUGIN)]
sys.modules['cloud_strm_test'] = package
spec = importlib.util.spec_from_file_location('cloud_strm_test.main',PLUGIN/'main.py')
module = importlib.util.module_from_spec(spec); sys.modules[spec.name] = module; spec.loader.exec_module(module)
Mapping, ScanInput = module.Mapping, module.ScanInput
MANIFEST = json.loads((PLUGIN/'plugin.json').read_text())


class CloudStrmTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source, self.output = self.root/'cloud', self.root/'strm'
        self.source.mkdir()
        self.fake = FakeContext(fields=MANIFEST['settings']['fields'])
        self.ctx = await self.fake.__aenter__()
        self.plugin = await self.ctx.start(module.Plugin())
        self.rule = Mapping(id='movies',name='115 电影',source_dir=str(self.source),target_dir=str(self.output),playback_root='/115/电影')
        await self.save_rule(self.rule)
        self.task = types.SimpleNamespace(task_id='test', progress=self.progress, check_cancelled=lambda:None)

    async def asyncTearDown(self):
        await self.fake.__aexit__(None,None,None)
        self.temp.cleanup()

    async def progress(self,*args): pass
    async def save_rule(self,rule): await self.ctx.store.put('rules',rule.id,rule.model_dump())
    async def scan(self,**values): return await self.plugin.scan(self.task,ScanInput(**values).model_dump())
    def video(self,name='电影/ABC-001.mkv'):
        path=self.source/name; path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'VIDEO')
        return path

    async def test_generate_preserves_structure_copies_sidecars_and_never_reads_video(self):
        movie=self.video()
        movie.with_suffix('.nfo').write_text('<movie/>')
        movie.with_suffix('.srt').write_text('subtitle')
        movie.with_suffix('.jpg').write_bytes(b'image')
        original=module.files.read_metadata
        def metadata(root,relative,maximum):
            self.assertNotEqual(Path(relative).suffix,'.mkv')
            return original(root,relative,maximum)
        with patch.object(module.files,'read_metadata',metadata): result=await self.scan()
        self.assertEqual(result['created'],3)
        self.assertEqual((self.output/'电影/ABC-001.strm').read_text(),'/115/电影/电影/ABC-001.mkv')
        self.assertTrue((self.output/'电影/ABC-001.nfo').exists())
        self.assertTrue((self.output/'电影/ABC-001.srt').exists())
        self.assertFalse((self.output/'电影/ABC-001.jpg').exists())
        self.assertEqual(movie.read_bytes(),b'VIDEO')

    async def test_incremental_repair_and_rule_change_update_our_files(self):
        self.video(); await self.scan()
        self.assertEqual((await self.scan())['skipped'],1)
        output=self.output/'电影/ABC-001.strm'; output.unlink()
        self.assertEqual((await self.scan())['created'],1)
        self.rule=self.rule.model_copy(update={'playback_root':'/new/115'})
        await self.save_rule(self.rule)
        self.assertEqual((await self.scan())['updated'],1)
        self.assertEqual(output.read_text(),'/new/115/电影/ABC-001.mkv')

    async def test_manual_change_is_preserved_and_failed_record_can_be_retried(self):
        self.video(); await self.scan()
        output=self.output/'电影/ABC-001.strm'; output.write_text('hand edited')
        with self.assertRaises(OperationFailed): await self.scan()
        self.assertEqual(output.read_text(),'hand edited')
        records=await self.ctx.store.scan('records')
        self.assertEqual(records.items[0].value['state'],'error')
        output.unlink()
        result=await self.scan(record_id=records.items[0].key,force=True)
        self.assertEqual(result['created'],1)

    async def test_source_offline_preserves_outputs_and_index(self):
        self.video(); await self.scan()
        before=(await self.ctx.store.scan('records')).items
        self.source.rename(self.root/'unmounted')
        with self.assertRaises(OperationFailed): await self.scan()
        self.assertTrue((self.output/'电影/ABC-001.strm').exists())
        self.assertEqual((await self.ctx.store.scan('records')).items,before)

    async def test_same_stem_collision_and_unowned_outputs_are_not_overwritten(self):
        self.video('same.mp4'); self.video('same.mkv')
        with self.assertRaises(OperationFailed): await self.scan()
        self.assertEqual((self.output/'same.strm').read_text(),'/115/电影/same.mkv')
        self.assertEqual(len((await self.ctx.store.scan('records')).items),2)

    async def test_failed_after_atomic_write_recovers_ownership_from_intent(self):
        self.video()
        original=module.files.atomic_write
        def write_then_fail(*args): original(*args);raise OSError('simulated interruption after rename')
        with patch.object(module.files,'atomic_write',write_then_fail):
            with self.assertRaises(OperationFailed): await self.scan()
        self.assertEqual(len((await self.ctx.store.scan('intents')).items),1)
        self.rule=self.rule.model_copy(update={'playback_root':'/changed'})
        await self.save_rule(self.rule)
        self.assertEqual((await self.scan())['updated'],1)
        self.assertEqual(len((await self.ctx.store.scan('intents')).items),0)

    async def test_refresh_failure_keeps_outbox_then_retries_without_regeneration(self):
        self.video()
        snapshot=await self.ctx.settings.get()
        await self.ctx.settings.patch({'refresh_emby':True},expected_revision=snapshot.revision)
        self.rule=self.rule.model_copy(update={'emby_root':'/emby/电影'})
        await self.save_rule(self.rule)
        self.ctx.library.refresh.side_effect=OSError('offline')
        with self.assertRaises(OSError): await self.scan()
        self.assertEqual(len((await self.ctx.store.scan('refresh')).items),1)
        self.ctx.library.refresh.side_effect=None
        result=await self.scan()
        self.assertEqual(result['skipped'],1)
        self.assertEqual(result['refreshed'],1)
        self.assertEqual(len((await self.ctx.store.scan('refresh')).items),0)
        updates=self.ctx.library.refresh.call_args.args[0]
        self.assertEqual(updates[0].path,'/emby/电影/电影/ABC-001.strm')

    async def test_symlinks_and_hidden_sources_are_skipped(self):
        self.video('visible.mp4'); self.video('.hidden.mp4'); self.video('@Recycle/deleted.mp4')
        (self.source/'escape').symlink_to(self.root,target_is_directory=True)
        result=await self.scan()
        self.assertEqual(result['created'],1)

    async def test_target_symlink_and_large_metadata_are_rejected(self):
        self.video(); self.output.mkdir()
        (self.output/'电影').symlink_to(self.source/'电影',target_is_directory=True)
        with self.assertRaises(OperationFailed): await self.scan()
        self.assertFalse((self.source/'电影/ABC-001.strm').exists())
        (self.output/'电影').unlink()
        settings=await self.ctx.settings.get()
        await self.ctx.settings.patch({'metadata_max_mb':1},expected_revision=settings.revision)
        self.video().with_suffix('.nfo').write_bytes(b'x'*(1024*1024+1))
        with self.assertRaises(OperationFailed): await self.scan()
        self.assertFalse((self.output/'电影/ABC-001.nfo').exists())

    async def test_output_root_changed_to_symlink_cannot_write_into_source(self):
        self.video()
        self.output.symlink_to(self.source,target_is_directory=True)
        with self.assertRaises(OperationFailed): await self.scan()
        self.assertFalse((self.source/'电影/ABC-001.strm').exists())

    async def test_enabling_refresh_reconciles_already_generated_strm(self):
        self.video(); await self.scan()
        settings=await self.ctx.settings.get()
        await self.ctx.settings.patch({'refresh_emby':True},expected_revision=settings.revision)
        result=await self.scan()
        self.assertEqual(result['skipped'],1)
        self.assertEqual(result['refreshed'],1)
        self.ctx.library.refresh.assert_awaited_once()

    async def test_cancelled_task_stops_between_files_and_preserves_source(self):
        for index in range(20): self.video(f'{index}.mkv')
        original=self.plugin.process
        async def slow(*args,**kwargs):
            result=await original(*args,**kwargs); await asyncio.sleep(.01); return result
        self.plugin.process=slow
        ref=await self.plugin.submit(ScanInput())
        while not list(self.output.glob('*.strm')): await asyncio.sleep(.001)
        await self.ctx.tasks.cancel(ref.task_id)
        count=len(list(self.output.glob('*.strm')))
        await asyncio.sleep(.03)
        self.assertEqual(len(list(self.output.glob('*.strm'))),count)
        self.assertEqual(len(list(self.source.glob('*.mkv'))),20)
        self.assertEqual((await self.ctx.tasks.get(ref.task_id))['status'],'cancelled')

    async def test_directory_batches_generate_before_later_directory_is_enumerated(self):
        self.video('first.mkv')
        self.video('later/second.mkv')
        original = module.files.list_folder
        def listing(root, relative, extensions):
            if relative == 'later':
                self.assertTrue((self.output/'first.strm').exists())
            return original(root, relative, extensions)
        with patch.object(module.files, 'list_folder', side_effect=listing):
            self.assertEqual((await self.scan())['created'], 2)

    async def test_later_mount_error_preserves_completed_outputs_and_existing_records(self):
        self.video('first.mkv'); self.video('later/second.mkv')
        await self.scan()
        before = {e.key: e.value for e in (await self.ctx.store.scan('records')).items}
        self.video('another.mkv')
        original = module.files.list_folder
        def listing(root, relative, extensions):
            if relative == 'later': raise OSError('mount unavailable')
            return original(root, relative, extensions)
        with patch.object(module.files, 'list_folder', side_effect=listing), self.assertRaises(OperationFailed):
            await self.scan()
        self.assertTrue((self.output/'another.strm').exists())
        self.assertTrue((self.output/'later/second.strm').exists())
        after = {e.key: e.value for e in (await self.ctx.store.scan('records')).items}
        for key in before: self.assertEqual(after[key], before[key])

    async def test_batches_bound_metadata_and_bulk_queries_and_filter_unused_files(self):
        for index in range(205): self.video(f'{index:03d}.mkv')
        self.video('ignored.txt'); self.video('ignored.jpg')
        original_stat = Path.lstat
        def lstat(path, *args, **kwargs):
            self.assertNotIn(path.name, {'ignored.txt', 'ignored.jpg'})
            return original_stat(path, *args, **kwargs)
        with patch.object(module.files, 'stat_batch', wraps=module.files.stat_batch) as batches, \
             patch.object(self.ctx.store, 'get_many', wraps=self.ctx.store.get_many) as queries, \
             patch.object(Path, 'lstat', lstat):
            self.assertEqual((await self.scan())['created'], 205)
            self.assertEqual([len(call.args[1]) for call in batches.call_args_list], [100, 100, 5])
            self.assertEqual(queries.await_count, 6)
            self.assertTrue(all(len(call.args[1]) <= 100 for call in queries.call_args_list))
        with patch.object(module.files, 'atomic_write') as write:
            self.assertEqual((await self.scan())['skipped'], 205)
            write.assert_not_called()

    async def test_folder_limit_counts_ignored_entries_too(self):
        for index in range(3): self.video(f'{index}.txt')
        with patch.object(module.files, 'MAX_FOLDER_ITEMS', 2), self.assertRaises(OperationFailed):
            await self.scan()


class MappingTests(unittest.TestCase):
    def rule(self,**values): return Mapping(name='demo',source_dir='/mnt/115',target_dir='/strm',**values)
    def test_paths_urls_templates_and_windows_paths(self):
        self.assertEqual(self.rule(playback_root='/115/电影').content('演员/影片 01.mp4'),'/115/电影/演员/影片 01.mp4')
        self.assertEqual(self.rule(mode='URL',playback_root='https://open.example/d/115').content('电影/影片 01.mkv'),'https://open.example/d/115/%E7%94%B5%E5%BD%B1/%E5%BD%B1%E7%89%87%2001.mkv')
        self.assertEqual(self.rule(mode='模板',playback_root='/115/{path}').content('ABC.mp4'),'/115/ABC.mp4')
        self.assertEqual(self.rule(playback_root='D:\\115').content('演员/ABC.mkv'),'D:\\115\\演员\\ABC.mkv')
    def test_overlap_and_template_expressions_are_rejected(self):
        for values in [dict(target_dir='/mnt/115/output'),dict(source_dir='/strm/sub'),dict(mode='模板',playback_root='{path.__class__}'),dict(mode='URL',playback_root='javascript:alert(1)')]:
            with self.subTest(values=values),self.assertRaises(ValueError): Mapping(name='demo',**{'source_dir':'/mnt/115','target_dir':'/strm',**values})
    def test_manifest_matches_sdk_contract(self):
        from kanojo_sdk.manifest import Manifest
        Manifest.model_validate(MANIFEST).check_host('1.2.2')


class PackagingTests(unittest.TestCase):
    def test_archive_is_reproducible_and_does_not_bundle_runtime_data(self):
        import hashlib,zipfile
        spec=importlib.util.spec_from_file_location('sdk_packager',PLUGIN.parent/'tools/package_sdk.py')
        packager=importlib.util.module_from_spec(spec);spec.loader.exec_module(packager)
        with tempfile.TemporaryDirectory() as tmp:
            archive,entry=packager.package('cloud-strm',tmp)
            first=archive.read_bytes()
            archive,second_entry=packager.package('cloud-strm',tmp)
            self.assertEqual(first,archive.read_bytes())
            self.assertEqual(hashlib.sha256(first).hexdigest(),entry['releases'][0]['sha256'])
            with zipfile.ZipFile(archive) as contents:
                self.assertIn('cloud-strm/main.py',contents.namelist())
                self.assertFalse(any('__pycache__' in name or name.endswith('config.json') for name in contents.namelist()))
