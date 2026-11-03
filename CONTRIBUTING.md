# LStudio 开发与公开源码范围

## 应提交的文件

| 分类 | 文件 |
| --- | --- |
| 文档和依赖 | `README.md`、`CONTRIBUTING.md`、`.gitignore`、`requirements.txt`、`requirements-cached.txt` |
| 启动 | `start.cmd`、`launch.py`、`app.py` |
| 应用模块 (`apps/`) | `apps/h3/`（`application.py`、`remote_h3.py`、`characters.py`、`h3_prompt.py`、`h3_prompt_worker.py`）及根目录兼容垫片 |
| 历史与回收 | `history_recycle.py` |
| Drive | `drive_storage.py`、`drive_cache.py`、`drive_cache_remote.py`、`mounted_drive_cache.py`、`native_drive_mount.py`、`seed_drive_cache.py` |
| 运行时恢复 | `runtime_recovery.py`、`runtime_watchdog.py` |
| 工作台架构 | `workspace_runtime.py`、`compute_colab.py`、`asset_library.py`、`ARCHITECTURE.md` |
| 网页 | `studio.html`、`static/` 下的 CSS 和 JavaScript |
| 本地回归验证 | `tests/`：离线 Python 和前端回归测试 |

`seed_drive_cache.py` 被网页备份接口动态导入；远程缓存、挂载及提示词模块会按文件内容发送到 Colab，不能仅检查顶层 Python import 后省略它们。

## 用户数据与生成文件

- `.private/`、`materials/`、`log/`、虚拟环境和 `.env`：用户资料、凭据、会话、本机配置和产物。
- 新的运行产物应写入对应的用户数据目录，避免散落在源码根目录。
- 历史截图、音视频、转录、实测报告、一次性诊断脚本和上游研究副本已移除，不为这些旧文件保留忽略规则。

正式 UI 图片和经过检查的测试夹具可以正常提交，不按图片、音视频或 JSON 扩展名统一忽略。自动化测试使用临时数据和模拟传输，不依赖个人作品或历史生成结果。

## 本地验证

在全新的检出目录安装 README 中的依赖后，从项目根目录执行，避免读取现有用户的 `.private/`。以下 Python 回归检查使用临时目录和模拟传输，不要求启动工作台服务或连接 Google 账号。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t . -q
# 单独运行一个模块：
.\.venv\Scripts\python.exe -X utf8 -m unittest -q tests.test_app
```

有 Node.js 时运行不依赖 npm 包的前端回归与语法检查：

```powershell
Get-ChildItem -File tests/test_*.cjs | ForEach-Object { node $_.FullName; if ($LASTEXITCODE -ne 0) { throw "Frontend check failed: $_" } }
Get-ChildItem -File static/*.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed: $_" } }
git diff --check
```

真实 Google 授权、申请 GPU、模型下载、生成和云端上传需要单独验证，会消耗账户资源。本地模拟检查不能替代这些链路的实测。

## 公开前

提交前用 `git diff --cached --name-only` 核对文件范围。删除工作区文件并提交，不会清除旧提交中的副本。

首次公开提交前，应检查暂存内容，确保没有用户资料、运行产物或个人凭据。

维护者还需确定项目自身的许可证。依赖、模型和上游组件保留各自许可；不要把个人开发环境、第三方模型权重或凭据作为发布包的一部分。
