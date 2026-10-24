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
| [junk-cleaner](junk-cleaner/) | 按插件独立配置的路径和关键词扫描文件，勾选确认后批量清理 |

垃圾文件清理的扫描路径和清理关键词都在插件的「清理设置」中填写，保存在插件自己的 `config.json` 中，与 qBittorrent 下载文件过滤互不影响。关键词每行一个，也支持中英文逗号分隔；文件名包含任意关键词即命中，不区分大小写。留空时不会匹配或删除文件。

从 1.1.x 升级后，原有扫描路径保留，清理关键词需要在插件内单独填写，不会自动沿用下载器的过滤词。扫描仅列出候选文件，勾选并确认后才删除。

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
