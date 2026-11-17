# Looki 日程管理 · AstrBot 插件

<p align="center">
  <strong>QQ群</strong>&nbsp;&nbsp;
  <img src="assets/aniclock/6.gif" width="36" height="80" alt="6" /><img src="assets/aniclock/3.gif" width="36" height="80" alt="3" /><img src="assets/aniclock/7.gif" width="36" height="80" alt="7" /><img src="assets/aniclock/9.gif" width="36" height="80" alt="9" /><img src="assets/aniclock/0.gif" width="36" height="80" alt="0" /><img src="assets/aniclock/6.gif" width="36" height="80" alt="6" /><img src="assets/aniclock/8.gif" width="36" height="80" alt="8" /><img src="assets/aniclock/2.gif" width="36" height="80" alt="2" /><img src="assets/aniclock/0.gif" width="36" height="80" alt="0" />
</p>

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.23.1-2f6f5e "AstrBot")](https://github.com/AstrBotDevs/AstrBot) [![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab "Python")](https://www.python.org/) [![Version](https://img.shields.io/badge/version-0.8.0-176b56 "Version")](https://github.com/Reybit4424/astrbot_plugin_looki_companion/releases) [![License](https://img.shields.io/badge/license-MIT-e0a908 "License")](LICENSE)

> 以前，是虚拟伴侣看着时间轴给你发自拍；现在，换你把一天分享给它，让它看看你的二次元分身都经历了什么

同步 Looki 记录的日常片段，让 AstrBot 中的虚拟伴侣了解你的一天。还可以按日程查找照片，让它把你的生活片段变成一张二次元风格的图（大人，时代变了：现在轮到虚拟伴侣看你的时间轴了）

在网页中查看日历和时间轴，也可以直接问 Bot 某天做了什么。记录有误时，还能手动补充、修改或通过对话校正


***

## 目录

[功能特性](#功能特性) · [快速开始](#快速开始) · [配置项](#配置项) · [对话工具与权限](#对话工具与权限) · [近期线索注入与对话校正](#近期线索注入与对话校正) · [图片与视频](#图片与视频) · [数据存储](#数据存储) · [隐私说明](#隐私说明) · [常见问题](#常见问题) · [开发与测试](#开发与测试) · [目录结构](#目录结构) · [兼容性](#兼容性) · [更新日志](#更新日志) · [许可证](#许可证)

***

## 功能特性

### 日程同步与保护

- 按设定间隔拉取 Looki Moments 中已经结构化的时间片段，并向前回填指定天数。
- SQLite 本地持久化，WAL 模式；同步带互斥锁，避免重复任务同时写入。
- 保存标题、关键词、描述、音频摘要、媒体类型、采集模式和起止时间；兼容 UTC 时间并转换到配置时区。
- 有稳定 Looki ID 时幂等去重；缺少稳定 ID 时使用内容键辅助识别。
- 手工补录、手工编辑和对话校正独立保存，后续同步不会静默覆盖。
- 删除采用墓碑标记，远端再次同步不会把已删除记录复活。
- Looki 尚未生成结构化数据时，提高轮询频率也不会让数据提前出现。

<br />

### 媒体证据

- 默认同步每个 Looki 时段的媒体列表，保存照片、视频、音频、转写、文本摘要及未知类型的元数据。
- 页面可以查看数量、媒体类型、尺寸、时长、可用状态和文本摘录，并筛选有证据的日程。
- 媒体证据只描述“这个时段有什么材料”，不会在后台下载所有原图、视频或音频。
- 不保存签名 URL；图片工具需要发图时才按需获取媒体。
- 单个时段的媒体同步失败不会阻断基础日程同步，可在后续同步重试。

### 对话与图片

- `query_looki_schedule`：按日期范围和关键词查询原始日程、媒体摘要及校正历史。
- `add_looki_correction`：管理员为日程追加对话校正，不修改 Looki 原始内容。
- `send_looki_image`：按严格时段或活动语义找图，由多模态模型挑选一张，再发送原图或图生图结果。
- 支持白名单成员和白名单群；对话校正与近期线索注入始终只面向 AstrBot 管理员。

<br />

## 快速开始

1. 将本仓库放入 AstrBot 的 `data/plugins/` 目录，或通过支持的插件安装方式安装。
2. 在 AstrBot 控制台启用插件。
3. 填写 `looki_api_key`。插件使用 `x-api-key` 请求头访问 Looki 开放 API。首次连接时，插件页面会询问是否导入历史日程；选择“暂不导入”后也可随时手动同步。
4. 保持 `auto_sync` 开启，或在插件页面点击“立即同步”。
5. 打开插件页面，确认日程已经落库。
6. 如需图片功能，配置支持图像输入的挑图模型；只发原图时开启 `image_send_original`，无需配置图生图模型。

Python 依赖：

```text
httpx>=0.27.0
pydantic>=2.0.0
```

视频抽帧还需要系统提供 `ffmpeg` 和 `ffprobe` 命令。只同步日程、查看媒体证据或处理图片时不需要它们；官方 AstrBot Docker 镜像通常已包含这两个命令。

## 配置项

### Looki 与同步

| 配置键                       | 默认值                              | 说明                                     |
| :------------------------ | :------------------------------- | :------------------------------------- |
| `looki_api_key`           | 空                                | Looki 开放 API Key，使用 `x-api-key` 请求头。   |
| `looki_base_url`          | `https://open.looki.tech/api/v1` | Looki API Base URL。                    |
| `auto_sync`               | `true`                           | 是否启用后台自动同步。                            |
| `sync_interval_minutes`   | `5`                              | 同步间隔，范围 1–1440 分钟。                     |
| `backfill_days`           | `7`                              | 每次同步向前回填天数，范围 1–366。                   |
| `request_timeout_seconds` | `20`                             | Looki 请求超时，范围 5–120 秒。                 |
| `timezone`                | `Asia/Shanghai`                  | 同步、展示与查询使用的 IANA 时区。                   |
| `store_raw_json`          | `false`                          | 是否保存 Looki 原始 JSON，仅供调试；页面和查询接口不返回该字段。 |

### 媒体证据

| 配置键                           | 默认值    | 说明                                            |
| :---------------------------- | :----- | :-------------------------------------------- |
| `enable_media_evidence`       | `true` | 是否同步媒体列表和文本摘要证据。                              |
| `media_evidence_max_moments` | `6`    | 每次同步最多补抓媒体证据的 Looki 时段数，范围 1–30。 |

### 对话、权限与近期线索

| 配置键                   | 默认值        | 说明                              |
| :-------------------- | :--------- | :------------------------------ |
| `clue_scope`          | `仅对管理成员开放` | 近期线索注入范围，可设为“关闭”。               |
| `clue_days`           | `7`        | 近期线索回看天数，最大 31 天。               |
| `max_clues`           | `3`        | 最多注入的近期线索条数，最大 10；设为 0 表示不注入。   |
| `allow_group_query`   | `false`    | 是否在群聊开放日程和图片工具；白名单独立放行。         |
| `whitelist_user_ids`  | 空          | 白名单成员 ID，每行一个或用逗号分隔；在私聊和任意群聊生效。 |
| `whitelist_group_ids` | 空          | 白名单群 ID，每行一个或用逗号分隔；群内成员均可在该群使用。 |

### 图片与图生图

| 配置键                            | 默认值          | 说明                                                            |
| :----------------------------- | :----------- | :------------------------------------------------------------ |
| `image_candidate_count`        | `6`          | 交给挑图模型的候选图数量，范围 1–9；最终只发送 1 张。                                |
| `image_send_original`          | `false`      | 开启后发送选中的原图；关闭时进行图生图。                                          |
| `image_style_prompt`           | 日系二次元插画提示词   | 默认图生图风格，可在单次调用中追加调整。                                          |
| `image_selection_provider_id`  | 空            | 挑图多模态 Provider ID；留空跟随当前对话模型。                                 |
| `image_generation_mode`        | `multimodal` | `multimodal` 使用 AstrBot 多模态模型，`openai_compatible` 使用兼容图生图接口。  |
| `image_generation_provider_id` | 空            | 图生图多模态 Provider ID；留空跟随挑图模型。                                  |
| `image_api_base_url`           | 空            | 兼容接口地址，可填写到 `/v1`、完整 `/images/edits` 或 `/images/generations`。 |
| `image_api_model`              | 空            | 兼容接口的图生图模型名称。                                                 |
| `image_api_key`                | 空            | 兼容接口 API Key；留空不发送 Authorization 头。                           |

## 对话工具与权限

| 工具                     | 作用                         | 写入权限          |
| :--------------------- | :------------------------- | :------------ |
| `query_looki_schedule` | 按日期范围和关键词查询原始日程、媒体摘要与校正历史。 | 只读            |
| `send_looki_image`     | 按严格时段或活动语义找图、选图并发送。        | 只读日程，按需发送图片   |
| `add_looki_correction` | 为唯一确认的日程追加对话校正。            | 仅 AstrBot 管理员 |

权限分两层：

1. **插件访问层**：私聊查询默认开放；群聊由 `allow_group_query`、成员白名单或群白名单任一条件放行。白名单独立生效，无需打开群聊总开关。
2. **AstrBot 工具权限层**：控制台“扩展 → 组件”中的工具仍可分别设置为管理员、成员或停用，实际可用范围取两层权限的交集。

`add_looki_correction` 和近期线索注入不随白名单开放，始终只面向 AstrBot 管理员。

### 查询日程

- 支持日期范围、可选关键词和返回上限。
- 关键词会匹配标题、关键词、描述、音频摘要和对话校正，并进行大小写、标点与空白归一化。
- 原始内容与校正冲突时，应以最新校正为准；描述仍只是线索，不应被解释为确定的健康或行为结论。

### 对话校正

管理员可以先查询并定位唯一日程，再追加校正。例如：

```text
实际地点是教室，不是宿舍。
```

校正按时间保留多条历史，不修改 Looki 原始描述。无法确定唯一记录时，不应写入。

## 近期线索注入与对话校正

`clue_scope` 为“仅对管理成员开放”时，插件会在管理员对话请求前提供少量近期背景，私聊与群聊均生效；非管理员不注入，设为“关闭”则完全不注入。

近期线索优先包含对话校正，再由日程日期、结束时间和关键词生成，数量由 `clue_days` 与 `max_clues` 控制。描述保持保守，例如较晚结束的活动只会写“晚间仍有活动记录”，不会推断睡眠或健康情况。

## 图片与视频

### 两种找图方式

1. **明确时段**：提供 `date`、`start_time`、`end_time`，先只查询与该时段相交的 Looki 日程。
2. **活动回忆**：没有明确日期时只提供 `query`，例如“之前一起玩 Switch 的照片”，由插件在本地日程中定位最近匹配的活动。

明确时段的候选不足时，图片服务可以补充同日相邻活动的候选，并在返回结果中明确说明，不会静默改变查询范围。活动模式没有匹配时不会发送无关照片，也不会为了找图自动扩大远端同步范围。

### 候选、抽帧与发送

- 图片直接成为候选；每个视频最多保留 1 张 JPEG 帧。
- 可探测时长时，按视频长度选择最多 3 个时刻抽帧，经过清晰度与异常帧过滤后择优保留 1 张；无法探测时长时依次尝试第 3 秒、第 1 秒和第 0 秒。
- 视频流式下载上限为 50 MiB，单个媒体失败只跳过该候选。
- 默认准备 6 张有效候选，可配置 1–9 张；图片与视频交错分批处理，每批最多 2 路并发。
- 所有有效候选交给多模态模型，模型必须返回一个有效编号；编号无效时明确报错，不随机选图。
- 无论原图还是图生图模式，最终只发送选中的 1 张。原图模式下，视频来源发送抽帧 JPEG，不发送 MP4。

### 图生图

- 默认使用日系二次元插画风格，可修改默认提示词或在单次调用中追加调整。
- 多模态模式使用 AstrBot Provider，要求模型支持图像输入与图片输出。
- 兼容接口支持 `/images/edits`，以及使用 JSON base64 data URL 的完整 `/images/generations`，后者适用于火山 Seedream 等接口。
- 兼容接口支持返回 `b64_json` 或 `url`；图生图失败不会静默退化为无关的文生图。
- 增加候选数量、使用较慢模型或提高输出分辨率时，可能还需要调整 AstrBot 的全局工具调用超时；该设置会影响其他工具，应谨慎修改。

## 数据存储

数据库位于：

```text
data/plugin_data/astrbot_plugin_looki_companion/schedules.sqlite3
```

主要数据：

- `schedules`：原始日程、手工补录、编辑标记、软删除、采集模式和媒体类型。
- `schedule_corrections`：通过 `schedule_id` 关联的对话校正历史。
- `media_evidence`：照片、视频、音频、转写、摘要和未知类型的证据元数据，不保存签名 URL。
- `sync_meta`：最近同步时间、错误、媒体同步进度等元数据。

**备份**：复制整个 `data/plugin_data/astrbot_plugin_looki_companion/` 目录。不要只复制主库而忽略可能存在的 `-wal` 文件。

**日志脱敏**：Looki API Key 和第三方图生图密钥不应写入日志；媒体列表接口不返回或持久化签名 URL。

## 隐私说明

- 插件不主动发消息、不定时推送，不读取或调用其他插件的私有数据。
- 原始 JSON 默认不写入数据库；开启 `store_raw_json` 后也仅供本地调试，Web API 不返回该字段。
- WebUI 读写通过 AstrBot 插件页面认证 bridge 调用，匿名请求返回 401。
- 媒体证据后台同步只保存元数据和文本摘录，不批量下载原始媒体。
- 使用图片工具时，候选图片或视频帧会发送给挑图模型；图生图时，选中的参考图和提示词会发送给图生图服务。
- 图片和视频不写入 SQLite。候选图、下载的视频和生成图使用独立临时目录，固定保留 3 天，并在插件启动及图片调用时清理过期文件。
- 白名单只放开读取与图片工具，不开放对话校正写入或管理员近期线索注入。

## 常见问题

### 同步成功但没有新数据

Looki 可能尚未生成对应日期的结构化片段，或记录缺少有效 ID、日期、起止时间、标题。插件不会用猜测内容填补缺失日程。

### 我修改过的日程会被远端同步覆盖吗

不会。用户编辑有独立标记，远端同步会保留手工修改。手工补录和软删除也有相应保护。

### 媒体证据里有照片，为什么页面不直接显示原图

媒体证据用于说明“有哪些材料”，默认只保存元数据，不保存签名 URL，也不批量下载原图。需要发图时，图片工具会按时段或活动重新获取媒体。

### 群里无法查询日程

默认关闭群聊访问。可开启 `allow_group_query`，或配置成员 / 群白名单；同时检查 AstrBot“扩展 → 组件”中的工具权限。

### 视频为什么不能抽帧

请确认运行环境中 `ffmpeg` 和 `ffprobe` 可直接执行，并检查视频是否超过 50 MiB、下载地址是否已失效。单个媒体失败不会阻断其他候选。

## 开发与测试

在能够导入 AstrBot 的 Python 环境中，于插件根目录运行：

```bash
python -m unittest discover -s tests -v
```

测试使用 `unittest` 和 Mock，不依赖真实 Looki API Key、在线模型或真实媒体下载。覆盖：

- Looki 真实字段形状解析、时区转换和媒体类型归一化
- SQLite 去重、编辑保护、软删除与对话校正
- 媒体证据分页、摘要、音频与未知类型保留
- 严格时段与活动找图、视频抽帧、候选清理和图生图接口
- 管理员、群聊开关、成员白名单和群白名单
- 空任务行过滤及 AstrBot 事件权限适配

请不要把真实 API Key、个人日程或未脱敏的原始媒体写入测试夹具。

## 目录结构

```text
astrbot_plugin_looki_companion/
├─ main.py                  # 插件入口、同步、对话工具与 Web API
├─ schedule_store.py        # SQLite、迁移、日程与媒体证据
├─ media_service.py         # Looki 媒体列表解析与证据归一化
├─ image_service.py         # 找图、视频抽帧、多模态选图与图生图
├─ _conf_schema.json        # AstrBot 配置定义
├─ metadata.yaml            # 插件元信息
├─ requirements.txt         # Python 依赖
├─ pages/
│  └─ schedule/
│     └─ index.html         # 日程、设置与媒体证据页面
└─ tests/
   ├─ fixtures/             # 脱敏的 Looki 字段形状样例
   ├─ test_looki_parse.py
   ├─ test_schedule_store.py
   └─ test_image_service.py
```

## 兼容性

- 元信息声明 AstrBot `>=4.23.1`。
- Python `>=3.10`。
- 当前声明支持平台：`aiocqhttp`；其他平台未在元信息中声明，也未逐一验证。
- 视频功能额外依赖系统 `ffmpeg` / `ffprobe`，其余核心日程功能不依赖它们。

## 更新日志

### v0.8.0

- 新增媒体证据同步、数量摘要、类型筛选和日 / 周 / 月聚合视图。
- 桌面端右侧栏支持收起和展开；手机端改为右侧抽屉。
- 扩充 Looki 真实字段、媒体证据、图片与权限回归测试。

### v0.7.0

- 新增对话校正、音频摘要与严格时段 / 活动回忆找图。
- 新增独立白名单、多模态选图、原图 / 图生图和兼容图生图接口。
- 改进手动编辑、软删除、权限校验和临时媒体清理。

更早版本的变更请查看 Git 提交历史与 Releases。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。
