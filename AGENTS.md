# Komeiji's Tavern 开发说明

## 目录与运行环境

- 开发工作区：`E:\GitHub\astrbot_plugin_komeiji_tavern`
- AstrBot 插件运行目录：`C:\Users\KomeijiDono\.astrbot\data\plugins\astrbot_plugin_komeiji_tavern`
- AstrBot 安装目录：`E:\AstrBot`
- AstrBot 后端源码：`E:\AstrBot\backend\app`
- AstrBot 自带 Python：`E:\AstrBot\backend\python\python.exe`
- 插件运行数据库：`C:\Users\KomeijiDono\.astrbot\data\astrbot_plugin_komeiji_tavern\tavern.db`

开发工作区是代码修改的唯一来源。完成修改和验证后，再把文件增量同步到运行目录。不要直接把运行目录中的临时改动当作源代码。

## 修改约定

- 当前工作区可能存在用户尚未提交的改动；修改前先查看 `git status --short`，不得重置、覆盖或清理无关改动。
- 优先增量修改现有实现，保持旧配置和旧 SQLite 数据库可以迁移。
- 数据库新增字段时，旧表已经存在的情况必须先补列，再创建依赖新列的索引。
- 主聊天的调试预览、分支存档、备份和 AstrBot conversation 应保存明文逻辑消息；临时密文、网址 token 和工具中间消息不得持久化。
- URL 请求、密文、普通请求、网页驾驶台和 QQ 命令共用的行为应尽量下沉到 `service.py`，避免多条链路产生不同结果。
- 用户可见回复进入状态栏、分支、配图、战役状态和会话保存前，应先完成解密及思维标签清理。

## 前端

- 前端源码：`web/src`
- 构建目录：`web/dist`
- AstrBot Dashboard 镜像：`pages/dashboard`
- 构建命令：

  ```powershell
  Set-Location E:\GitHub\astrbot_plugin_komeiji_tavern\web
  npm install
  npm run build
  ```

`web/scripts/fix-index.mjs` 会处理构建产物并同步 Dashboard 页面。修改前端后必须重新构建，不要只手工编辑压缩后的 `app.js`。

## 验证命令

在开发工作区进行基础检查：

```powershell
Set-Location E:\GitHub\astrbot_plugin_komeiji_tavern
python -m py_compile main.py service.py storage.py web.py
python -m json.tool _conf_schema.json > $null
git diff --check
```

使用 AstrBot 运行环境执行完整测试：

```powershell
$env:PYTHONPATH='E:\GitHub;E:\AstrBot\backend\app'
E:\AstrBot\backend\python\python.exe -m unittest astrbot_plugin_komeiji_tavern.tests.test_core
```

涉及数据库迁移时，至少增加一个从旧结构初始化的测试；涉及命令时，验证完整命令、`/tv` 简写和实际传给模型的 prompt。

## 同步到运行目录

同步采用非镜像式增量覆盖，保留运行目录中的 `data`、缓存和目标端专有文件：

```powershell
$source = 'E:\GitHub\astrbot_plugin_komeiji_tavern'
$target = 'C:\Users\KomeijiDono\.astrbot\data\plugins\astrbot_plugin_komeiji_tavern'
robocopy $source $target /E /R:2 /W:1 `
  /XD .git node_modules data __pycache__ .pytest_cache .opencode `
  /XF *.pyc
if ($LASTEXITCODE -gt 7) { exit $LASTEXITCODE }
```

- 不使用 `/MIR`，避免删除运行目录中的数据或专有文件。
- 同步后比较 `main.py`、`service.py`、`storage.py`、`web.py`、`_conf_schema.json` 和前端构建产物的 SHA-256。
- 使用 AstrBot Python 对运行目录核心文件执行 `py_compile`。
- 代码同步不等于插件已经重载；若 AstrBot 没有自动热重载，需要在插件管理页重载插件。

## 文档与发布

- 新功能同步更新 `_conf_schema.json`、`README.md` 和 `CHANGELOG.md`。
- 版本号由 `constants.py`、`metadata.yaml` 和前端包信息共同体现；正式发布时保持一致。
- 最终报告应说明修改内容、测试结果、同步目标以及是否仍需重载 AstrBot。
