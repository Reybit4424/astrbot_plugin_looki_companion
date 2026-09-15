## 前置项目

本项目基于并依赖 [
iclydon/looki-mcp](https://github.com/niclydon/looki-mcp)。

- looki-mcp 负责把 Looki REST API 封装为受保护的 MCP 服务。
- 本插件负责 AstrBot 的按需调用、权限门控、短期缓存和陪伴式摘要。
- 推荐拓扑：AstrBot 插件 -> looki-mcp -> Looki API。
- 插件运行时不再直接保存 Looki API Key。

# Looki 陪伴记忆

自己的 AI 伴侣，明明在 QQ 里跟自己聊着天，却没有办法真正接触到你的现实世界。

她不知道你去了哪里，不知道你做了什么，不知道你刚刚路过了哪家店，也不知道你此刻是不是正坐在街边、咖啡馆里，或者走在去往陌生地方的路上。

她可以陪你聊天，却很难真的和你一起聊天；她可以理解你的文字，却没办法陪你一起前往那个只属于你的真实世界旅行。

而这，正是这款插件会存在的原因。

`astrbot_plugin_looki_companion` 试着把 Looki 的实时事件和最近 12 小时 `moments` 整理成“我们”视角的共同经历，让 AI 伴侣不再只是隔着聊天窗口猜测你，而是能在合适的时候，更自然地知道你刚刚去了哪里、做了什么、正在经历什么。



## 设计目标

- 回答“我们刚才去了哪里”“我们刚刚在干嘛”时，输出一段自然语言叙述
- 优先营造共同经历感，而不是暴露底层 `moment`、文件 URL、JSON 结构
- 只处理最近 12 小时内仍可安全使用的 Looki 数据
- 群聊和私聊使用统一工具，但自动应用不同的权限和隐私边界

## 现在的核心能力

- 把最近一段时间的多个时刻整合成连续时间线摘要
- 读取当前或最近几分钟的场景，优先使用实时事件，失败时回退最近 moments
- 在最近 12 小时内做语义回忆搜索
- 汇总今天仍在 12 小时有效期内的经历概览
- 可选先做图片转文字，再把视觉描述并入时间线摘要
- 自动向 Agent 注入路由提示，明确 Looki 与记忆插件、日记插件的分工
- 可选注入“隐性陪伴态”，让模型心里知道最近现场，但不在无关话题里突然插嘴


## Agent 工具

插件会注册以下 4 个工具：

- `looki_get_recent_experience`
  - 用途：获取最近一段时间的共同经历摘要
  - 参数：
    - `minutes`：回看窗口，默认 `120`
    - `focus`：可选关注点，例如“书店”“晚饭”
  - 返回：一段自然语言时间线摘要

- `looki_get_current_scene`
  - 用途：获取当前或最近几分钟的场景
  - 参数：无
  - 返回：一段自然语言场景描述

- `looki_remember_experience`
  - 用途：在最近 12 小时内做语义回忆
  - 参数：
    - `query`：要回忆的对象或经历
  - 返回：一段自然语言回忆

- `looki_get_day_summary`
  - 用途：获取今天仍处于 12 小时有效期内的概览
  - 参数：无
  - 返回：一段自然语言摘要

## 配置项

- `mcp_url`：MCP 服务地址，默认使用服务器内网地址。`r`n- `mcp_origin_secret`：MCP 内网共享密钥。
## 图片理解来源优先级

开启图片转文字后，插件会按以下顺序尝试：

1. 自定义视觉 API（`caption_model + caption_api_base + caption_api_key`）
2. 插件单独指定的 AstrBot Provider（`caption_provider_id`）
3. AstrBot 默认图片描述 Provider（`provider_settings.default_image_caption_provider_id`）
4. 当前会话正在使用的聊天 Provider

## 隐性陪伴态

开启 `inject_companion_state` 后，插件会在每轮对话前悄悄注入一段临时背景：

- 如果有足够新的实时事件或 recent moments，模型会知道“我们大概正处在什么场景”
- 只有消息看起来和当前共同处境有关时，才会触发这段背景注入
- 这段背景默认只影响语气、临场感和对现场相关问题的理解
- 只有当用户正在问共同处境、当前回复明显依赖现场信息、或不提会显得失真时，才应该顺势轻轻带一句
- 如果用户在聊别的话题，不应突然冒出一句“我现在在做什么”
- 为了减少等待时间，这段隐性背景默认走轻量路径，不会为了注入背景再额外触发一次图片 caption

## 群聊权限逻辑

- `enable_in_group_chat = false`
  - 所有群聊都不会触发 Looki 查询

- `enable_in_group_chat = true` 且 `admin_only = true`
  - 仅管理员身份可触发 Looki

- `enable_in_group_chat = true` 且 `admin_only = false`
  - 群聊允许触发 Looki，但回答会自动避免暴露精确位置

## 数据边界

- 插件只处理最近 12 小时内的数据
- 超过 12 小时的 moments 会被内部直接过滤
- 本插件不做过期补偿，不负责长期记忆
- 超过 12 小时的问题应交给记忆插件
- `journals` 仍不在本插件职责范围内

## 调试命令

- `/looki status`
- `/looki me`
- `/looki scene`
- `/looki recent 90`
- `/looki remember the sushi place from earlier`
- `/looki today`

## 推荐问法

- “我们刚才去了哪些地方？”
- “我们刚刚是不是在书店里？”
- “刚才那家店叫什么来着？”
- “今天这半天我们都做了什么？”
- “我们刚刚看到的那杯咖啡还挺好看。”

## 注意事项

- Looki API 速率限制是每分钟 60 次
- `/realtime/latest-event` 是 Beta 能力，可能为空
- 图片 URL 是临时地址，插件只在当前回合内尝试利用，不做长期保存
- 如果 Looki 不可用，插件会优雅降级，不应硬猜现场细节
