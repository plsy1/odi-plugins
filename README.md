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
| [junk-cleaner](junk-cleaner/) | 扫描数据目录中命中关键词过滤的无用文件，勾选后批量清理 |

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
