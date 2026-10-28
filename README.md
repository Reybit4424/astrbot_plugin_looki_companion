# Looki 日程管理 · AstrBot 插件

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.23.1-2f6f5e)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.5.2-176b56)](https://github.com/RabbitZheng4424/astrbot_plugin_looki_companion/releases)
[![License](https://img.shields.io/badge/license-MIT-e0a908)](LICENSE)
[![QQ Group](https://img.shields.io/badge/QQ%E7%BE%A4-637906820-4a90d9)](https://qm.qq.com/q/637906820)

把 Looki 生成的结构化日程同步到 AstrBot 本地，提供日历页面查看与编辑，并让 Bot 在需要时查阅近期日程与生活线索。

插件不依赖其他插件，不会主动发消息，也不会定时推送。

---

## 目录

- [功能特性](#功能特性)
- [界面布局](#界面布局)
- [快速开始](#快速开始)
- [配置项](#配置项)
- [使用说明](#使用说明)
- [数据存储与去重策略](#数据存储与去重策略)
- [隐私说明](#隐私说明)
- [常见问题](#常见问题)
- [开发与测试](#开发与测试)
- [目录结构](#目录结构)
- [兼容性](#兼容性)
- [更新日志](#更新日志)
- [许可证](#许可证)

---

## 功能特性

- **自动同步**：按设定间隔拉取 Looki Moments 中已结构化的时间片段，向前回填指定天数。
- **本地持久化**：SQLite 存储，WAL 模式，按日期与 Looki ID 建立索引，查询快、断电友好。
- **日历页面**：月视图（黄点标示当日记录数，最多 5 个）、单日时间轴（小标题 + 关键词 + 详细描述 + 音频摘要）、当天目录一键跳转。
- **手动补录与编辑**：支持新增、修改、删除；删除采用墓碑标记，后续同步不会把已删记录复活。
- **冲突保护**：同步不会覆盖你手工编辑过的记录，也不会删除手工录入的数据。
- **Bot 查阅**：LLM 工具 `query_looki_schedule` 支持按日期范围与关键词查询，最多 366 天、100 条。
- **近期线索注入**：可选在 owner 私聊的对话上下文中注入压缩后的近期生活线索，群聊不注入。
- **隐私友好**：原始 JSON 默认不落库，任何接口都不返回该字段。

## 界面布局

插件页面（AstrBot 控制台 → 插件 → Looki 日程管理）采用三区块布局：

```
┌────────────────────────────────────────────────────────────┐
│  ◔ Looki 日程   同步状态              [日程] [设置]  立即同步 │
├──────────────────────────────────┬─────────────────────────┤
│  时间轴（当天）                   │  月视图                 │
│  ┌────────────────────────────┐  │  ‹  2026 年 10 月  •  › │
│  │ 09:00  ─ 10:30             │  │  一 二 三 四 五 六 日   │
│  │   小标题                    │  │         · 黄的圆点表示  │
│  │   关键词 chips             │  ├─────────────────────────┤
│  │   详细描述 · 仅作线索        │  │  当天时间轴             │
│  │   ▍音频摘要 · Looki 捕捉     │  │  09:00  小标题          │
│  └────────────────────────────┘  │  14:00  小标题          │
│  ┌────────────────────────────┐  │  20:30  小标题          │
│  │ 14:00  ─ 15:00  小标题 手动 │  │  单击跳转到对应时段      │
│  └────────────────────────────┘  │                         │
└──────────────────────────────────┴─────────────────────────┘
```

- **顶部**：品牌与同步状态、`日程 / 设置` 标签页切换、`立即同步` 按钮（同步中防连点）。
- **左侧**：当天时间轴，按时间段排列；每条显示起止时间、小标题、「关键词」标签与关键词 chips、「详细描述 · 仅作线索」与完整描述、「音频摘要 · Looki 捕捉」块；仅手动补录的条目带「手动」徽标；悬停出现编辑按钮，点击选中。
- **右上**：月视图，可切换上月 / 下月 / 回到本月；每天下方以黄点表示记录数（封顶 5 个）。
- **右下**：当天时间轴目录（时间 + 小标题），单击平滑滚动到左侧对应时段并高亮，等同页内锚点。
- **设置页**：Looki 连接参数与行为开关，API Key 掩码显示、可手动查看明文。
- 窄屏（≤960px）自动折叠为单栏，侧栏取消吸顶。

## 快速开始

1. 将本仓库放入 AstrBot 的 `data/plugins/` 目录，或在 AstrBot 插件市场安装。
2. 在 AstrBot 控制台启用插件。
3. 打开插件配置，填写 `looki_api_key`（Looki 开放 API Key，请求头为 `x-api-key`）。
4. 保持 `auto_sync` 开启即可；也可以到插件页面点「立即同步」。
5. 打开插件页面确认数据已落库。

依赖仅两项，安装时 AstrBot 会自动处理：

```
httpx>=0.27.0
pydantic>=2.0.0
```

## 配置项

| 配置键 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `looki_api_key` | string | 空 | Looki 开放 API Key，请求使用 `x-api-key` 头。 |
| `looki_base_url` | string | `https://open.looki.tech/api/v1` | Looki API Base URL。 |
| `auto_sync` | bool | `true` | 启用后台自动同步。 |
| `sync_interval_minutes` | int | `5` | 自动同步间隔（分钟），最小 1 分钟。 |
| `backfill_days` | int | `7` | 每次同步向前回填的天数。 |
| `request_timeout_seconds` | int | `20` | Looki 请求超时秒数。 |
| `timezone` | string | `Asia/Shanghai` | 日程展示与同步使用的 IANA 时区。 |
| `clue_scope` | string | `仅对管理成员开放` | 近期线索注入范围：`仅对管理成员开放` / `关闭`。仅私聊生效。 |
| `allow_group_query` | bool | `false` | 是否允许在群聊使用日程查询工具。 |
| `store_raw_json` | bool | `false` | 是否把 Looki 原始响应写入 `raw_json` 列（仅调试用）。 |
| `clue_days` | int | `7` | 线索回看天数，最大 31。 |
| `max_clues` | int | `3` | 最多注入的线索条数，最大 10。 |

> 高频轮询只代表插件的请求频率，不能使 Looki 尚未生成的数据提前可用。

## 使用说明

### 日历页面

- 月视图黄点 = 当天记录数，封顶 5 个，无数据则不显示圆点。
- 点击日期切换左侧时间轴；左侧完整展示每条的小标题、「关键词」、「详细描述 · 仅作线索」与「音频摘要 · Looki 捕捉」。
- 右下「当天时间轴」列出全天小标题，单击即滚动到左侧对应时段并高亮。
- 支持补录、编辑、删除、立即同步。
- 当天最后一段在 21:00 前结束时会提示「正在记录.ing」。

### LLM 工具 `query_looki_schedule`

- 参数：日期范围、可选查询词、返回条数上限（最多 366 天、100 条）。
- 标题与关键词会先做大小写、标点、空白归一化再匹配。
- 关键词无命中时会降级返回该日期范围的全部记录，并在结果中附带 `notice` 与 `keyword_matched: false`，提示 LLM 如实说明而不是当作确定事实。
- 无数据时明确返回「无记录」。

**权限分两层**：

1. 插件层 `allow_group_query` 决定群聊是否开放（默认关闭）；私聊查询不受身份限制。
2. AstrBot 工具权限决定谁能触发：控制台「扩展 → 组件」中的 `query_looki_schedule` 可设为管理员或成员，也可整体停用。

### 近期线索注入

`clue_scope` 为 `仅对管理成员开放`（默认）时，只有 AstrBot 管理员（`admins_id`）的私聊会话会在对话前获得少量近期结构化线索；设为 `关闭` 则不注入。群聊永不注入。

线索由记录中的日期、结束时间和关键词压缩生成，数量受 `clue_days` 与 `max_clues` 控制。描述保守，例如较晚结束的活动只会表述为「晚间仍有活动记录」，不会推断睡眠或健康情况。

## 数据存储与去重策略

数据库位于 `data/plugin_data/astrbot_plugin_looki_companion/schedules.sqlite3`，WAL 模式。

主要字段：

- 本地 ID、Looki 原始 ID（如有）
- 日期、带时区的 ISO 8601 开始 / 结束时间
- 标题、关键词、描述、音频摘要（Looki 的 `audio_summary`）
- 来源（Looki / 手动）、用户编辑标记
- 创建 / 更新时间
- 可选 `raw_json`（仅 `store_raw_json` 开启时写入，任何接口都不返回）

**去重**：存在稳定 Looki ID 时按该 ID 幂等去重；缺少稳定 ID 时使用「日期 + 开始 + 结束 + 标题」组成复合键，字段会去除首尾空白并忽略大小写。

**冲突**：同步不会静默覆盖用户编辑过的记录，不会删除手工录入数据；用户删除写入墓碑标记，重复同步不会复活同一条远端记录；未被编辑的 Looki 记录可以随远端数据更新。

**备份**：请复制整个 `data/plugin_data/astrbot_plugin_looki_companion/` 目录，不要只复制主库文件而忽略 WAL。

**Schema 版本**：使用 `PRAGMA user_version` 记录，当前 `SCHEMA_VERSION = 2`（v2 新增 `audio_summary` 列，旧库启动时自动 `ALTER TABLE` 增量迁移）；`day` 与 `looki_id` 各自拥有独立索引（`idx_schedules_day`、`idx_schedules_looki`）。

## 隐私说明

- 插件不主动发消息、不定时推送，不读取或调用其他插件。
- `raw_json` 默认不写入，开启后也只在本地数据库中，任何 Web API 都不返回该字段。
- WebUI 读写 API 通过 AstrBot 插件页面的认证 bridge 调用，匿名请求返回 401。
- 线索注入仅发生在管理员私聊，群聊不注入。
- 其他插件可在兼容的 AstrBot 进程中导入公开的 `ScheduleStore` 类读取本地日程。

## 常见问题

**Q：同步成功但没有新数据？**
Looki 侧尚未生成该日期的结构化片段，或记录缺少开始 / 结束时间、标题为空。插件只导入具备 `id`、`date`、起止时间与标题的记录。

**Q：我改了条目标题，再同步会变回去吗？**
不会。用户编辑过的记录会打上编辑标记，后续同步不会静默覆盖。

**Q：删掉的记录为什么又出现了？**
正常情况下不会：删除写入墓碑标记。若出现，请确认不是手动重新补录的同名记录。

**Q：群里问日程没反应？**
默认群聊关闭查询。请开启 `allow_group_query`，并在「扩展 → 组件」中为该工具配置权限。

**Q：`store_raw_json` 要不要开？**
除非排查字段映射问题，建议保持关闭，减少本地隐私原文。

**Q：可以只用页面不用 LLM 工具吗？**
可以。工具与页面互不依赖，均可单独使用。

## 开发与测试

测试仅覆盖 `schedule_store.py`，使用标准库 `unittest`，不依赖 AstrBot 运行时的网络请求。

```bash
# 在仓库根目录执行（Windows 用 ; 分隔，Linux / macOS 用 :）
set PYTHONPATH=<插件目录>;<AstrBot 目录>
python tests/test_schedule_store.py -v
```

当前共 15 个用例，覆盖：

- 请求头与字段映射（`description` / `audio_summary` 不进入日程事实）
- 远端 upsert 幂等、用户编辑保护
- 手工记录在同步后保留
- 时区与日期范围校验
- 关键词降级与空结果
- 线索不推断睡眠
- 手动更新 / 删除持久化
- 手动编辑不覆盖已同步的音频摘要
- `search()` 关键词命中 / 未命中降级 / 无关键词
- `store_raw_json` 开关写库行为
- `PRAGMA user_version` 与 schema 版本一致
- `clues()` 使用传入时区

## 目录结构

```
astrbot_plugin_looki_companion/
├── main.py                  # 插件入口：同步调度、Web API、LLM 工具、线索注入
├── schedule_store.py        # SQLite 存储层：schema、去重、搜索、线索
├── _conf_schema.json        # 配置项定义
├── metadata.yaml            # 插件元信息
├── requirements.txt         # httpx / pydantic
├── pages/schedule/index.html# 日历页面（标签页 + 时间轴 + 月视图 + 当天目录）
├── tests/test_schedule_store.py
├── LICENSE                  # MIT
└── README.md
```

## 兼容性

- 按 AstrBot 4.26.4 的插件接口实现，市场声明 `astrbot_version >= 4.23.1`。
- LLM 工具使用 `FunctionTool` 与 `context.add_llm_tools()`。
- 线索注入使用 `on_llm_request` 与 `ProviderRequest.extra_user_content_parts`。
- 页面使用插件 Pages 与 `context.register_web_api()`，仅通过页面 bridge 的 GET / POST 交互。
- 已在本地 AstrBot 4.26.4 完成部署验证：插件加载、自动同步、手动同步幂等、页面资产与 bridge 协议、扩展 API 认证（匿名 401）、参数校验（400）、路由匹配（404）、增删改全生命周期（含删除后同步不复活）、config 保存回路、月度聚合接口。
- 未在其他最低版本逐一运行验证。

## 更新日志

### 0.5.2

- 线索注入权限由「填写特定人 QQ 号」改为下拉选择：`仅对管理成员开放` / `关闭`，判定使用 AstrBot 管理员身份（`admins_id`）。
- 移除 `owner_id` 与 `inject_clues` 配置项，统一为 `clue_scope`。

### 0.5.1

- 时间轴条目去掉来源「Looki」徽标，只保留手动补录的「手动」徽标。
- 左侧时间轴改为完整展示：小标题 + 「关键词」chips + 「详细描述 · 仅作线索」+ 「音频摘要 · Looki 捕捉」（`audio_summary`），不再两行截断。
- 右下改为「当天时间轴」目录（时间 + 小标题），单击平滑滚动到左侧对应时段并高亮。
- 数据库新增 `audio_summary` 列，`SCHEMA_VERSION` 升至 2，旧库自动迁移；手动编辑不会覆盖音频摘要。

### 0.5.0

- 页面改为「标签页 + 左侧时间轴 + 右侧月视图与当日详情」布局，删除了设置弹窗，改为独立设置页。
- 时间轴条目支持点击选中、来源徽标、关键词标签与摘要截断。
- 月视图黄点密度接口与当日详情联动。
- 新增 `store_raw_json` 配置项，默认关闭。
- 查询工具关键词无命中时返回 `notice` 与 `keyword_matched`，避免误导。
- 数据库补充 `PRAGMA user_version` 与日期 / Looki ID 索引。
- `clues()` 支持传入时区。
- 群聊查询改为由 `allow_group_query` 控制，私聊不再受 `owner_id` 限制。
- 修复日历页黄点统计与部分并发同步问题。

## 许可证

本项目使用 [MIT License](LICENSE)。

测试交流群：QQ `637906820`
