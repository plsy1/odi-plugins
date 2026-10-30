# 云盘 STRM

Kanojo SDK1 插件，需宿主 **1.2.1 或更新版本**。将 CD2 的 115 目录挂载到 Kanojo，插件扫描目录并在独立位置生成 STRM，保留原有文件夹结构。STRM 内容按你现有 Nginx 规则生成；播放时继续由 Emby → Nginx → OpenList 获取 115 直链并返回 302。

## 三种路径分别填什么

例如，宿主机 CD2 的目录为 `/mnt/cd2/115`，STRM 输出到 `/mnt/strm`。Kanojo 容器增加挂载：

```yaml
volumes:
  - /mnt/cd2/115:/cloud/115:ro
  - /mnt/strm:/strm
```

Emby 容器可以把 `/mnt/strm` 挂载为 `/media`。

在插件中添加映射：

| 字段 | 示例 | 含义 |
|---|---|---|
| CD2 源目录（Kanojo 内） | `/cloud/115/电影` | Kanojo 扫描的挂载目录 |
| STRM 输出目录（Kanojo 内） | `/strm/电影` | 实际生成 STRM 的位置 |
| STRM 内容格式 | 路径 | 适用于已有 Nginx/OpenList 路径转换 |
| STRM 内容前缀 | `/115/电影` | Nginx 能识别的原视频路径前缀 |
| STRM 输出目录（Emby 内） | `/media/电影` | 用于可选的 Emby 刷新，与 STRM 内容分开 |

输入文件：`/cloud/115/电影/演员/ABC-001.mkv`。

输出文件：`/strm/电影/演员/ABC-001.strm`，内容只有一行：

```text
/115/电影/演员/ABC-001.mkv
```

导入 Emby 的是 STRM 输出目录。STRM 路径前缀按你已有、能正常播放的 Nginx 映射填写；它可以与 Kanojo 和 Emby 内的目录名不同。保存后目录表会显示内容示例。

## 设置和操作

- 支持多个目录映射，表格中可编辑、扫描、移除。移除映射会保留源文件及生成结果。
- “增量扫描”遍历目录，对未变更文件跳过生成；“重新检查全部文件”修复缺失输出并应用新的路径规则。
- 周期扫描默认关闭，默认间隔 300 秒，最短 60 秒；启用后执行一次也单独控制。
- 视频只读取名称、大小和修改时间，生成过程中不打开视频内容，也不移动源视频。
- 字幕和 NFO 默认复制，图片默认关闭；单个配套文件默认最大 20 MB。
- 隐藏目录、回收站、`@eaDir`、`extrafanart` 和符号链接跳过。
- 任务有进度、统计、取消和历史。生成记录中可以单独重试；源目录不可用时保留已有输出及索引。
- 路径规则变更会更新本插件拥有的输出；手工修改或其他程序生成的不同内容不会覆盖，记录中会说明冲突。
- 修改输出目录后，原输出保留。插件不会自动删除失去源文件的 STRM。

## URL 与模板

常见的 Nginx 转换链路使用默认“路径”模式即可。

“URL”模式：前缀可以是 `https://openlist.example/d/115/电影`，追加相对路径并编码中文和空格。

“模板”模式支持：

- `{path}`：保留原始中文和 `/` 的相对路径；例如 `/115/电影/{path}`。
- `{url_path}`：URL 编码相对路径，保留 `/`。
- `{encoded_path}`：包括 `/` 在内全部 URL 编码，适用于定制 CD2 等转发格式。

插件生成固定路径或固定 URL，临时 115 直链由已有 Nginx/OpenList 播放链路获取。

## Emby 刷新

默认关闭。开启后使用宿主配置的 Emby 服务，不在插件中重复填写 API Key。生成结果和待刷新记录在同一个插件事务中保存，刷新失败保留队列，可以点击“重试待刷新文件”，或由后续扫描重试。Emby 接受通知代表已提交刷新请求，完成入库取决于服务端处理。

目录映射中的 Emby 输出目录必须是 Emby 容器实际看到的 STRM 路径。

## 本地安装与测试

把整个 `cloud-strm` 目录复制到 Kanojo 数据目录的 `plugins/cloud-strm/`，在插件页启用，添加目录映射。首次启用不会扫描，直到手动点击扫描或打开定时/启动扫描设置。

使用宿主的 Python 环境测试：

```sh
PYTHONPATH=/path/to/kanojo/backend /path/to/kanojo/venv/bin/python -m unittest discover -s tests
PYTHONPATH=/path/to/kanojo/backend /path/to/kanojo/venv/bin/python tools/package_sdk.py cloud-strm
```

打包器生成 `dist/cloud-strm-1.0.0.zip`、SHA-256 和 v2 release 索引片段，结果可重复，不自动发布。正式发布时先上传固定版本的 Release 资产，验证后再合并 v2 索引；旧 index.json 保留固定版本的 Legacy 插件供旧宿主使用。

实现独立编写，功能设计参考 MoviePilot 云盘 STRM 插件；无需安装 MoviePilot。
