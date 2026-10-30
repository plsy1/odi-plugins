# odi-plugins

[opendata-insight](https://github.com/plsy1/opendata-insight) 的官方插件仓库。

## 使用

在应用的 **设置 → 插件** 中，把插件仓库地址填为：

```
plsy1/odi-plugins
```

保存并刷新后即可在列表中安装插件。

## 插件列表

| 插件 | 说明 |
|---|---|
| [cloud-strm](cloud-strm/) | CD2 挂载目录生成 STRM，支持 Nginx 路径映射、配套文件与 Emby 刷新（需 Kanojo 1.2.1；当前可本地安装） |
| [junk-cleaner](junk-cleaner/) | SDK1 后台扫描、分页确认与清理，独立关键词，支持旧配置迁移（需 Kanojo 1.2.1） |

垃圾文件清理 2.0.0 使用 SDK1，需要 Kanojo 1.2.1 或更新版本。路径和关键词在插件的「清理设置」中独立保存，与 qBittorrent 的下载过滤互不影响。升级时迁移旧插件配置；缺失的清理关键词保持为空，不读取下载器过滤词。扫描与删除都有后台进度和取消功能，候选文件按页展示，勾选确认后才会删除。详情见 [垃圾文件清理](junk-cleaner/README.md)。

旧 `index.json` 固定提供兼容旧宿主的 Legacy 1.2.0 安装包。SDK1 的固定版本安装包和索引条目由打包工具生成，发布到 Release 后再加入 `index.v2.json`。

## 目录结构

```
odi-plugins/
├── index.json        # 插件索引（应用读取这个文件）
└── <插件id>/
    ├── plugin.json   # 插件清单
    └── main.py       # 插件入口
```

新增插件时：创建插件目录，并在 `index.json` 的 `plugins` 数组里加一条同 `id` 的记录即可（`download_url` 可省略，安装时会自动下载本仓库的 archive zip 并按 id 定位目录）。

插件开发规范见主仓库的 [PLUGINS.md](https://github.com/plsy1/opendata-insight/blob/main/PLUGINS.md)。

SDK1 插件用固定版本 ZIP、SHA-256 和 `index.v2.json` 发行，不能直接加到旧 `index.json`。发布步骤和本地安装说明见 [云盘 STRM](cloud-strm/README.md)。
