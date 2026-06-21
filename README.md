# Komeiji's Tavern

Komeiji's Tavern 是 AstrBot 的角色扮演提示词编排插件。它负责管理角色卡、用户设定、提示词预设、世界书和会话生命周期，并展示某次请求最终发送给模型的 `messages[]`。

当前版本：`0.3.1`。仅支持 AstrBot Chat Completion 管线。

## 版本更新记录

### 0.3.1

- 绑定管理与调试器的会话选择改为可输入 combobox，支持搜索已有会话或直接输入任意会话 ID 绑定。
- 补齐遗漏的版本号标注。

### 0.3.0

- 新增自动配图：每次 LLM 回复后可异步调用 `astrbot_plugin_omnidraw` 生成一张配图并补发到当前会话。
- 软依赖 omnidraw，未安装或未激活时自动跳过；不修改文本回复流程。
- 新增 7 个 `illustration_*` 配置项，支持开关、模式、尺寸、额度策略与提示词前缀。

### 0.2.6

- 支持将 `.txt` 和 `.md` 纯文本文件导入为提示词预设。
- 自动去掉纯文本提示词首尾匹配的单层引号。
- 导入后的文本作为 Main Prompt，并继续使用默认角色、世界书和 Persona 提示块。

### 0.2.5

- QQ 普通消息分片增加逐片日志和失败重试。
- 支持配置重试次数与重试等待时间。
- 当前测试参数调整为每片 1000 字、发送间隔 2000ms。

### 0.2.4

- 新增 QQ 长回复逐条普通消息发送模式，可通过插件配置与合并转发切换。
- 支持设置每条普通消息的最大字符数和发送间隔。
- 调整长回复发送钩子顺序，使 token 统计等附加文本一并参与分片。

### 0.2.3

- 新增非流式 QQ 长回复合并转发分片。
- 支持配置合并转发触发字数和每个 Node 的最大字符数。
- 分片优先使用段落、换行和句末边界，并保证正文字符不丢失。

### 0.2.2

- 调试器直接从绑定记录生成会话选项，不再完全依赖 AstrBot 会话目录响应。
- 更新页面缓存版本，确保 Dashboard 加载最新脚本。

### 0.2.1

- 修复已绑定会话未出现在调试器下拉框的问题。
- AstrBot 会话目录为空或读取失败时，使用插件绑定记录作为兜底。
- 调试器自动选择已有绑定的会话，并显示当前有效配置。
- 页面资源增加版本参数，避免 Dashboard 缓存旧脚本。

### 0.2.0

- 新增中文首次使用向导和配置状态概览。
- 新增角色卡、提示词预设、世界书和用户设定专用编辑器。
- 新增 AstrBot Persona、会话选择及完整绑定管理。
- 新增只读扫描模拟、最终 `messages[]` 调试和真实请求查看。
- 新增文档规范化、中文校验、复制以及未知导入字段合并导出。
- 数据库增加版本记录，首次升级自动创建 `0.1.0` 备份。
- 修复未绑定单选资料隐式使用资料库第一项的问题。
- 增加角色卡 Main Prompt 和 PHI 覆盖策略。

### 0.1.0

- 首个可用版本。
- 加入角色卡、角色组、提示词预设和世界书管理。
- 加入递归扫描、生命周期、概率和聊天深度注入。
- 加入创作素材、状态栏、特殊生成模式和最终请求预览。
- 加入 AstrBot 原生 Dashboard 管理页面。

更详细的版本变更同时记录在 `CHANGELOG.md`。

## QQ 长回复分片

插件设置提供“拆分 QQ 合并转发节点”“QQ 合并转发触发字数”和“每个 QQ 转发 Node 最大字数”。非流式纯文本回复超过触发值后，会在 AstrBot 自动包装前拆为多个 Node。默认每个 Node 最多 2500 个 Unicode 字符，中文、英文、标点和换行通常都各计 1 个字符。建议设置在 2000-3000；过高仍可能被 QQ 拒绝，内容审核导致的拒绝也无法靠分片解决。

开启“QQ 长回复逐条直接发送”后，该模式优先于合并转发。插件会按“QQ 直接发送每条最大字数”拆分正文，通过普通 QQ 消息接口逐条发送，并按配置的毫秒间隔限速。关闭此开关后恢复合并转发 Node 模式。

## 打开管理页

登录 AstrBot 管理面板后进入：

```text
插件 → Komeiji's Tavern → Pages → dashboard
```

管理页使用 AstrBot Dashboard 的登录状态，不提供匿名入口。插件或页面升级后，需要在插件管理中重载一次插件。

本机也可以在已经登录 Dashboard 的浏览器中直接访问：

```text
http://127.0.0.1:6185/api/plug/astrbot_plugin_komeiji_tavern/v1/panel
```

插件不单独监听网络端口。是否能从局域网或公网访问完全取决于 AstrBot Dashboard 的监听地址、防火墙和反向代理配置；所有页面和接口仍要求 Dashboard 身份认证，不提供匿名外部地址。

## 第一次使用

### 1. 准备角色

打开“角色卡”，点击“新建角色卡”，或在页面右上角导入 JSON、YAML、YML、带角色卡元数据的 PNG。纯文本 `.txt` 和 `.md` 文件会作为提示词预设导入。

角色卡可以设置：

- 角色名称、描述、性格、场景和开场白
- 示例对话
- 角色 Main Prompt
- 历史后指令（PHI）

导入操作只保存资料，不会立即影响聊天。导入完成后页面会进入绑定向导。

### 2. 检查提示词预设

“Default”预设会自动绑定到全局。提示词块可排序、启用或禁用，并可设置：

- 消息角色：`system`、`user`、`assistant`
- 注入位置：系统提示词、示例区、聊天深度
- 聊天深度和裁剪优先级
- 是否允许角色卡覆盖 Main Prompt 或 PHI

优先级数值越高，超过上下文预算时越早被裁剪。主提示词等核心块默认受到保护。

### 3. 创建世界书

世界书条目支持：

- 主关键词和次关键词的四种组合逻辑
- 常驻、禁用、概率和排列顺序
- 角色前、角色后、作者注、示例、聊天深度及 Outlet
- 递归扫描、扫描深度和消息角色
- Sticky、Cooldown、Delay 生命周期

`Sticky` 表示激活后继续保持的轮数。`Cooldown` 从保持结束后开始计算，在此期间不能再次触发。`Delay` 表示会话开始后的前若干轮不允许触发。

### 4. 绑定资料

资料只有绑定后才生效。绑定页会直接列出 AstrBot 当前 Persona 和会话，不需要手工查找会话 ID。

单选资源的覆盖顺序为：

```text
具体会话 > AstrBot Persona > 用户 > 群组 > 全局
```

角色卡、角色组、用户设定和提示词预设属于单选资源。世界书和创作素材会收集所有匹配作用域并叠加，同一文档只注入一次。

未绑定的角色卡或用户设定不会因为排在资料库第一位而意外生效。默认提示词预设仍保持全局绑定。

### 5. 调试最终请求

打开“调试器”，选择真实会话并输入一条测试消息：

- “只读模拟”使用固定随机种子构造请求，不写入会话状态。
- “最近真实请求”显示该会话上一次实际发送给模型的请求。
- 结果会列出最终 `messages[]`、每个提示词块、世界书激活原因、递归轮次、Outlet、裁剪项和警告。

当前 token 数标记为近似估算；上下文超限时会先按块的裁剪优先级移除非核心块，再从最旧的聊天历史开始裁剪。

## 特殊生成模式

```text
/tavern continue [补充要求]       续写上一条助手回复
/tavern impersonate [补充要求]    生成用户下一句话草稿
/tavern quiet [静默提示词]         执行 Quiet Prompt
```

管理页调试器也可以只读模拟这些模式。

## 自动配图

0.3.0 起，插件可在每次 LLM 回复后自动生成一张配图并补发到当前会话，适合角色扮演场景的"每句话配一张图"。

### 前置条件

1. 安装并配置好 [`astrbot_plugin_omnidraw`](https://github.com/diaomin66/astrbot_plugin_omnidraw)（万象画卷）。
2. 在 omnidraw 中至少配置一个可用的图像 Provider 节点，并确认 `/画 一只猫` 能正常出图。
3. 建议开启 omnidraw 的副脑（`enable_optimizer`），它会把中文回复全文优化成更适合画图模型的英文提示词。

### 工作方式

- 文本回复照常走原 pipeline 发送（含 QQ 长回复分片）。
- 同时在后台异步调用 omnidraw 的 `generate_images_for_plugin()` 生图，不阻塞文本。
- 生图完成（几秒到两分钟）后，用 `event.send()` 单独补发一张图片，自然落在所有文本之后。
- 配图失败时静默记日志，不打扰聊天。

### 配置项

| 配置项 | 默认 | 说明 |
| :--- | :--- | :--- |
| `illustration_enabled` | `false` | 启用自动配图。默认关闭，需显式开启。 |
| `illustration_plugin_id` | `astrbot_plugin_omnidraw` | 要调用的生图插件 ID。 |
| `illustration_mode` | `text2img` | 配图模式：`text2img` 文生图 / `selfie` 人设自拍。 |
| `illustration_max_text_chars` | `600` | 传给生图的回复文本最大字符数，过长会被截断。 |
| `illustration_size` | `""` | 配图默认尺寸，留空用 omnidraw 节点默认。 |
| `illustration_consume_quota` | `false` | 是否走 omnidraw 权限检查并消耗用户每日额度。默认关闭，配图作为 bot 自动行为不扣额度。 |
| `illustration_prompt_prefix` | `""` | 可选提示词前缀，会拼接到回复文本前再交给 omnidraw 副脑。 |

### 注意事项

- 每轮都生图会显著增加 API 调用量与成本，建议配合较便宜的图像模型。
- `illustration_consume_quota=false` 时绕过 omnidraw 白名单与额度，适合 bot 自动行为；若希望统一计数则改为 `true`。
- `selfie` 模式会使用 omnidraw 当前激活人设的参考图，适合固定角色的日常场景配图。
- 配图输入为去掉状态栏标记后的纯叙事文本，按 `illustration_max_text_chars` 截断后送入副脑。

## 状态和排错命令

```text
/tavern status     查看当前会话轮次和生命周期记录
/tavern preview    查看最近一次最终 messages[]
/tavern reset      清除当前会话生命周期和预览状态
```

如果 `preview` 为空，请先确认：

1. 插件已经启用并重载。
2. 资料已经绑定到当前会话、Persona 或全局。
3. 当前会话已实际触发过一次 LLM 请求。

## 高级 JSON

各编辑器底部保留折叠的“高级 JSON”区域，用于查看和修改未出现在普通表单中的扩展字段。

点击“应用 JSON”只会把内容应用到当前表单，仍需点击“保存”。导出时插件以原始导入数据为基础合并已编辑字段，未知字段不会被普通表单主动删除。

## 数据与备份

数据库位于：

```text
data/astrbot_plugin_komeiji_tavern/tavern.db
```

从 `0.1.0` 首次升级时会在同目录生成：

```text
tavern.db.v0.1.0.bak
```

数据库保存资料、绑定、会话生命周期和请求预览。导入操作不会修改源文件，也不会读取其他插件目录。

## 插件配置

- `enabled`：启用提示词编排。
- `context_budget`：无法取得模型上下文上限时使用的预算。
- `output_reserve`：为模型回复预留的 token。
- `scan_depth`：默认世界书扫描消息深度。
- `max_recursion_steps`：世界书最大递归次数。
- `vector_enabled`：启用向量条目，默认关闭。
- `embedding_provider_id`：向量条目使用的 AstrBot Embedding Provider。
- `tool_delivery_enabled`：临时调整发送工具说明，引导模型通过工具发送正文。
- `qq_direct_split_enabled`：将 QQ 长回复拆成多条普通消息直接发送；开启时优先于合并转发。
- `qq_direct_message_chars`：每条普通 QQ 消息的最大 Unicode 字符数。
- `qq_direct_send_interval_ms`：相邻普通 QQ 消息的发送间隔，单位为毫秒。
- `qq_direct_retry_count`：单片普通消息发送失败后的重试次数。
- `qq_direct_retry_delay_ms`：发送失败后的重试等待时间，单位为毫秒。
- `qq_forward_split_enabled`：启用非流式 QQ 长回复的合并转发节点分片。
- `qq_forward_trigger_chars`：回复超过多少 Unicode 字符后改用合并转发。
- `qq_forward_node_chars`：每个 QQ 转发 Node 的最大 Unicode 字符数，建议设置为 2000-3000。
- `status_bar_enabled`：解析回复中的状态变量。
- `status_bar_template`：状态栏显示格式，使用 `{content}` 放置内容。
- `illustration_enabled`：启用每次 LLM 回复后的自动配图（需安装 omnidraw）。
- `illustration_plugin_id`：生图插件 ID，默认 `astrbot_plugin_omnidraw`。
- `illustration_mode`：配图模式，`text2img` 或 `selfie`。
- `illustration_max_text_chars`：传入生图的回复最大字符数，默认 600。
- `illustration_size`：配图默认尺寸，留空用 omnidraw 节点默认。
- `illustration_consume_quota`：配图是否消耗 omnidraw 用户额度，默认 false。
- `illustration_prompt_prefix`：配图提示词前缀，可选。

向量条目未配置有效 Embedding Provider 时会跳过，并在请求调试结果中显示警告。
