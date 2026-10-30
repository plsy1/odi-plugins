"""Lifecycle smoke for an obfuscated Kanojo runtime; uses disposable files only."""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from fastapi import FastAPI
from modules.plugins import PluginManager


class Scheduler:
    def add_job(self,*args,**kwargs): pass
    def remove_job(self,*args,**kwargs): pass
    def queue_job(self,*args,**kwargs): pass


async def check(source):
    with tempfile.TemporaryDirectory(prefix='cloud-strm-image-') as temporary:
        root=Path(temporary)
        shutil.copytree(source,root/'plugins/cloud-strm',ignore=shutil.ignore_patterns('__pycache__'))
        cloud=root/'cloud/演员';cloud.mkdir(parents=True)
        (cloud/'ABC-001.mkv').write_bytes(b'video')
        manager=PluginManager(root/'plugins',root/'state.json',data_root=root,scheduler=Scheduler())
        await manager.startup(FastAPI())
        try:
            record=await manager.enable('cloud-strm')
            await record.context.store.put('rules','movies',dict(id='movies',name='movies',source_dir=str(root/'cloud'),target_dir=str(root/'strm'),playback_root='/115',mode='路径',emby_root='',enabled=True))
            task=await record.context.tasks.submit('scan',{})
            for _ in range(200):
                status=manager.task_host.get(record.id,task.task_id)
                if status['status'] not in ('queued','running'): break
                await asyncio.sleep(.025)
            assert status['status']=='succeeded',status
            assert (root/'strm/演员/ABC-001.strm').read_text()=='/115/演员/ABC-001.mkv'
            assert (cloud/'ABC-001.mkv').read_bytes()==b'video'
            await manager.disable(record.id)
            assert not record.loaded
        finally: await manager.shutdown()


if __name__=='__main__':
    asyncio.run(check(Path(sys.argv[1])))
    print('Cloud STRM image lifecycle passed')
