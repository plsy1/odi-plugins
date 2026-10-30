"""Prepare a deterministic SDK archive and v2 release entry; does not publish."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def package(plugin_id='cloud-strm', output=None, release_notes=None):
    from kanojo_sdk.manifest import Manifest
    root=Path(__file__).resolve().parents[1]
    if not plugin_id or '/' in plugin_id or '..' in plugin_id: raise ValueError('Invalid plugin id')
    source=root/plugin_id
    manifest=Manifest.model_validate(json.loads((source/'plugin.json').read_text()))
    destination=Path(output) if output else root/'dist'
    destination.mkdir(parents=True,exist_ok=True)
    archive=destination/f'{manifest.id}-{manifest.version}.zip'
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as handle:
        for file in sorted(source.rglob('*')):
            if not file.is_file() or file.is_symlink() or '__pycache__' in file.parts: continue
            if file.suffix not in ('.py','.json','.md') and file.name != 'LICENSE': continue
            if file.name == 'config.json': continue
            info=zipfile.ZipInfo(f'{manifest.id}/{file.relative_to(source).as_posix()}',date_time=(2026,1,1,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED; info.external_attr=0o100644<<16
            handle.writestr(info,file.read_bytes(),compresslevel=9)
    checksum=hashlib.sha256(archive.read_bytes()).hexdigest()
    entry=dict(id=manifest.id,name=manifest.name,description=manifest.description,author=manifest.author,releases=[dict(
        version=manifest.version,api_version=manifest.api_version,requires_host=manifest.requires_host,
        download_url=f'https://github.com/plsy1/odi-plugins/releases/download/{manifest.id}-v{manifest.version}/{archive.name}',
        sha256=checksum,release_notes=release_notes or {
            'cloud-strm': 'CD2 挂载目录生成 STRM，支持路径映射、增量生成、配套文件和 Emby 刷新。',
            'junk-cleaner': '迁移至 SDK1：旧配置迁移、可取消的后台扫描与清理、分页候选和删除前文件校验。',
        }.get(manifest.id, 'SDK1 插件更新。'))])
    (destination/f'{manifest.id}-{manifest.version}.release.json').write_text(json.dumps(entry,ensure_ascii=False,indent=2)+'\n')
    (destination/f'{archive.name}.sha256').write_text(f'{checksum}  {archive.name}\n')
    return archive,entry


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plugin_id',nargs='?',default='cloud-strm')
    parser.add_argument('--output')
    parser.add_argument('--release-notes')
    args=parser.parse_args()
    artifact,entry=package(args.plugin_id,args.output,args.release_notes)
    print(json.dumps({'archive':str(artifact),'sha256':entry['releases'][0]['sha256']},ensure_ascii=False))
