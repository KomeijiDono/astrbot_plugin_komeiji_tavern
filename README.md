<div align="center">

# Komeiji's Tavern

[![Version](https://img.shields.io/badge/version-0.5.0-7c5cff?style=for-the-badge)](CHANGELOG.md)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.25%2B-4f9cff?style=for-the-badge)](https://github.com/AstrBotDevs/AstrBot)
[![License](https://img.shields.io/badge/license-AGPL--3.0-42b883?style=for-the-badge)](LICENSE)

面向 AstrBot Chat Completion 的角色扮演提示词编排、世界书和请求调试插件。

</div>

## 功能特色

- 可排序 Prompt Manager：角色卡、Persona、世界书、示例、作者注、摘要、记忆、PHI、Bias 和自定义块。
- 世界书扫描：主次关键词、正则、递归、概率、Sticky、Cooldown、Delay、Outlet 和深度注入。
- 会话级自动摘要：使用可选小模型压缩旧历史，将滚动摘要注入 Summary 块。
- 上下文管理：固定历史条数、token 预算、近期消息保护和核心提示块保护。
- 完整调试器：查看最终 `messages[]`、提示块、世界书激活原因、裁剪项、摘要状态和警告。
- 资料管理：角色卡、提示词预设、世界书、用户设定、创作素材及会话绑定。
- 备份导出：单份资料 JSON、分类或全部资料 ZIP、最终 `messages[]` 和重置前会话备份。
- QQ 长回复：普通消息分片、分包合并转发、失败重试及自动降级。
- 会话生命周期：自动清理过期插件状态和真实请求预览，不影响 AstrBot 聊天和资料。

完整版本记录见 [CHANGELOG.md](CHANGELOG.md)。

## 快速开始

1. 在 AstrBot 插件管理中安装并启用本插件。
2. 打开管理页，创建或导入角色卡、提示词预设和世界书。
3. 在“绑定管理”中把资料绑定到具体会话、Persona、用户、群组或全局。
4. 使用调试器执行只读模拟，确认最终 `messages[]` 和世界书激活结果。
5. 返回对应会话正常发送消息。

管理页地址：

```text
http://127.0.0.1:6185/api/plug/astrbot_plugin_komeiji_tavern/v1/panel
```

端口以 AstrBot Dashboard 实际配置为准。管理接口复用 Dashboard 鉴权，不建议直接暴露到公网。

## 管理页工作流

- **角色卡**：编辑名称、描述、性格、场景、开场白、示例、Main Prompt 和 PHI。
- **提示词预设**：调整块顺序、角色、注入位置、深度、裁剪优先级及覆盖规则。
- **世界书**：配置关键词、扫描深度、递归、概率、生命周期和注入位置。
- **用户设定**：维护 Persona 内容并映射 AstrBot Persona。
- **绑定管理**：单选资料按“会话 > Persona > 用户 > 群组 > 全局”覆盖，世界书和素材叠加去重。
- **调试器**：只读模拟不会推进轮次或生命周期；“最近真实请求”展示实际发送结果。

## 自动摘要

自动摘要默认关闭。建议先在插件配置中选择成本较低、上下文足够的 Chat Completion Provider，再开启该功能。

- 默认累计到 18 条未压缩历史时触发。
- 生成摘要后保留最近 12 条历史，其余内容合并进会话滚动摘要。
- 摘要与预设中的静态 Summary 内容合并，注入 Prompt Manager 的 `summary` 块。
- 摘要 Provider 留空时使用当前会话 Provider；指定 Provider 不可用时不会静默换模型。
- 摘要失败不会阻断聊天，也不会推进摘要边界；本轮退回普通历史裁剪。
- 只读模拟不会调用摘要模型，只显示已有摘要并提示真实请求是否将触发摘要。

如果预设禁用了 `summary` 块，摘要仍可保存，但不会发送给模型。可在调试器查看“已注入”状态。

## 特殊生成模式

```text
/tavern continue [补充要求]
/tavern impersonate [补充要求]
/tavern quiet [静默提示词]
```

- `continue`：续写上一条 assistant 回复，不重复已有内容。
- `impersonate`：以用户口吻草拟下一条用户消息。
- `quiet`：在深度 0 注入临时提示词，不改变长期预设。

状态与调试命令：

```text
/tavern status
/tavern preview
/tavern reset
```

`reset` 只清除当前会话的插件生命周期、状态变量、滚动摘要和请求预览，不删除 AstrBot 聊天或绑定资料。

## 插件配置

配置页按功能分组：

- **基础功能**：插件开关、发送工具引导。普通聊天建议关闭发送工具引导。
- **上下文与裁剪**：上下文预算、输出预留、历史条数与裁剪顺序。
- **自动摘要与历史压缩**：Provider、触发条数、输出上限、超时和提示词。
- **会话数据生命周期**：自动清理、状态保留天数、预览保留天数和检查间隔。
- **世界书与向量**：扫描深度、递归和 Embedding Provider。
- **QQ 普通消息分片 / 合并转发**：长回复发送策略。
- **状态栏 / 自动配图**：可选创作扩展。

默认生命周期策略：会话状态保留 30 天，请求预览保留 7 天，每 24 小时检查一次。

## 导入与数据

- 支持 JSON、YAML、PNG 角色卡以及纯文本提示词预设。
- 导入时保留未知原始字段，导出时与已编辑字段合并。
- 资料编辑页可分别导出当前资料 JSON，也可按类别或一键导出全部资料 ZIP。
- 调试器可导出当前显示的纯 `messages[]` JSON；“重置前备份 ZIP”还会保存完整请求预览与插件会话状态。
- 会话备份不包含 AstrBot 原始聊天记录；`/tavern reset` 本身也不会删除这些聊天记录。
- SQLite 文件位于 `data/astrbot_plugin_komeiji_tavern/tavern.db`。
- 自动清理仅删除 `sessions` 和 `previews` 中的过期行，不删除 documents、bindings 或聊天数据。
- SQLite 页面会被后续数据复用，插件不会在运行时自动执行 `VACUUM`。

## 常见问题

### 最终请求只有用户消息

检查会话是否绑定了提示词预设和角色卡，并在调试器中确认“有效绑定”。未绑定时插件不会任意选择资料库第一项。

### 模型不遵守尾部格式

先检查调试器中的 system 指令是否存在，再减少历史消息条数或启用自动摘要。上下文未超限也可能因注意力稀释而漏遵循格式。

### 自动摘要没有运行

确认已开启功能、未压缩历史达到触发条数，并且摘要 Provider 可用。调试器会显示是否将触发及最近错误。

### QQ 长回复发送失败

降低每个 Node 或普通消息分片字符数；合并转发失败时可启用自动降级，或改用逐条普通消息发送。

## 开发与验证

```powershell
$env:PYTHONPATH='E:\AstrBot_plugin;E:\AstrBot\backend\app'
E:\AstrBot\backend\python\python.exe -m unittest discover -s tests -v

cd web
npm ci
npm run build
```

发布前应校验 Python 语法、配置 JSON、前端构建、版本一致性和完整测试。

## 许可证

本项目使用 [GNU AGPL v3](LICENSE)。
