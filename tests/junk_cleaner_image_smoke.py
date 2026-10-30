"""SDK migration, scanning and confirmed deletion in a disposable runtime."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from fastapi import FastAPI
from modules.plugins import PluginManager
from modules.plugins.transactions import install_staged


async def terminal(manager, ref):
    for _ in range(200):
        value = manager.task_host.get('junk-cleaner', ref.task_id)
        if value['status'] not in ('queued', 'running'):
            assert value['status'] == 'succeeded', value
            return value
        await asyncio.sleep(.025)
    raise AssertionError('Timed out waiting for task')


async def check(source):
    with tempfile.TemporaryDirectory(prefix='junk-sdk-image-') as temporary:
        root = Path(temporary)
        downloads = root / 'downloads'; downloads.mkdir()
        junk, keep = downloads / '广告.txt', downloads / 'movie.mkv'
        junk.write_text('disposable junk'); keep.write_text('keep this')
        legacy = root / 'plugins/junk-cleaner'; legacy.mkdir(parents=True)
        (legacy/'plugin.json').write_text(json.dumps(dict(id='junk-cleaner',name='Legacy cleaner',version='1.2.0')))
        (legacy/'main.py').write_text('class Plugin:\n def setup(self, ctx): pass\n')
        (legacy/'config.json').write_text(json.dumps(dict(scan_paths=str(downloads),keywords='广告，AD,ad')))
        manager = PluginManager(root/'plugins', root/'state.json', data_root=root)
        await manager.startup(FastAPI())
        try:
            await manager.enable('junk-cleaner')
            manifest = json.loads((source/'plugin.json').read_text())
            await install_staged(manager, source, manifest)
            record = manager.records['junk-cleaner']
            assert record.api_version == 1 and record.loaded
            assert (await record.context.settings.get()).values['keywords'] == ['广告', 'AD']
            await terminal(manager, await record.context.tasks.submit('scan', {'path':''}))
            assert junk.exists() and keep.exists()
            scan = await record.context.store.get('state','scan')
            page = await record.context.store.scan('scan:'+scan['id'])
            assert len(page.items) == 1
            identifier = scan['id'] + ':' + page.items[0].key
            await terminal(manager, await record.context.tasks.submit('delete', {'items':[identifier], 'generation':record.context.plugin.generation}))
            assert not junk.exists() and keep.read_text() == 'keep this'
            await manager.disable(record.id)
            assert not record.loaded
        finally:
            await manager.shutdown()


if __name__ == '__main__':
    asyncio.run(check(Path(sys.argv[1])))
    print('Junk cleaner SDK migration and lifecycle passed')
