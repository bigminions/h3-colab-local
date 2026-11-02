# H3 本地视频工作台

在 Windows 本机打开网页，使用自己的 Google Colab 运行时生成 MiniMax H3 视频。本机负责素材、任务和作品管理，模型下载与推理在 Colab 上完成，不要求本机有 NVIDIA 显卡。

支持文生视频、参考图与首尾帧、角色库和声音参考、批量生成、Qwen 提示词整理，以及可选的 Google Drive 模型缓存和成片备份。

## 环境要求

- Windows 10 / 11。凭据使用 Windows DPAPI 加密，当前不支持直接在 Linux、macOS 或 WSL 中运行本地控制台。
- Python 3.12（64 位），安装时勾选 Python Launcher。
- 可以使用 Colab 的 Google 账号，以及可分配所选 GPU 的配额或计算单元。网页提供 G4、A100 选择，实际可用性以账号返回为准。
- 本机能访问 Google OAuth、Colab 和 Drive；Colab 运行时能访问 GitHub、Hugging Face 等模型和代码来源。
- 本机需要保存视频和上传素材的空间。模型在远程下载，可能需要数十 GB 的运行时磁盘；使用 Drive 缓存时还需相应的 Drive 空间。

GPU 运行时会消耗自己的 Colab 额度。首次使用或释放后重新分配运行时，需要重新准备环境和模型，耗时取决于模型大小与网络。

## 安装与启动

下载或克隆代码后，在项目目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch.py
```

也可在安装依赖后双击 `start.cmd`。浏览器会打开 <http://127.0.0.1:7865/>；已有工作台服务时，启动器复用该服务。

`requirements.txt` 声明本地代码直接使用的依赖，并用 `requirements-cached.txt` 约束已验证的依赖版本，两个文件需要放在一起。Colab 上的 ComfyUI、模型与推理依赖由程序准备，无需安装到本机。

如果窗口未打开，可以运行以下命令查看启动错误：

```powershell
.\.venv\Scripts\python.exe -u app.py
```

## 第一次生成

1. 打开“配置”，连接自己的 Google 账号。按页面提示完成 Google 授权，将授权码粘贴回本机工作台。
2. 回到创作页，输入提示词，选择视频比例、画质、时长和生成数量。第一次可用 5 秒、480p 验证完整流程。
3. 如需图生视频，上传参考图并设置为首帧、尾帧或一般参考图。角色库可保存角色外观、图片和声音样本，在提示词中用 `@` 选择角色。
4. 在高级设置确认 GPU 和释放方式，再开始生成。进度和错误原因可在运行日志中查看。
5. 完成后在作品区播放或下载视频。最近作品支持应用配置、重新下载和移入系统回收箱。

无需授权 Drive 即可生成并保存到本机。Drive 是独立授权，用于可选的成片备份及模型缓存。

### 提示词与性能选项

- **Qwen 整理**：先理解输入剧本，再将描述整理为 H3 使用的英文区段；对白和画面文字保留原文。整理结果可能有语义偏差，可查看日志并调整输入，或关闭整理直接传入提示词。
- **性能加速**：提供编码器选择、主模型和视频 VAE 量化、PDD 8 步与实验性 Veda 注意力加速。关闭总开关会使用官方 BF16 主模型和编码器、FP16 视频 VAE、常规 20 步与完整注意力，资源占用会增加。
- **角色声音**：每次任务的声音参考合计最多 3 段、15 秒；角色图、参考图和首尾帧合计最多 9 张。
- **批量生成**：一次 1–8 条，顺序执行并复用同一运行时，各条使用独立输出编号和种子。

部分加速组合会影响画面和声音。模型、编码器、PDD、Veda 的具体版本与校验信息记录在代码和任务日志中。

### GPU 释放

高级设置提供整批生成后释放、闲置后释放、手动释放。保留运行时期间仍可能消耗额度。

释放前程序检查自己管理的运行时是否空闲、结果是否已取回或完成备份。网络异常或结果未保存可能阻止释放，应查看日志并处理。取消任务后也应检查释放状态。

关闭浏览器不会关闭后台服务；关闭后台程序后无法继续执行定时释放。退出前请在工作台确认显卡释放，必要时在自己的 Colab 中核对运行时。

## Google Drive（可选）

在“配置”中单独完成 Drive 授权：

- 成片备份保存在私有的 `H3-Videos` 文件夹；本机副本缺失时可从 Drive 恢复并校验。
- 模型缓存使用 `H3-Model-Cache`，校验来源版本、大小与 SHA-256；缓存未命中时按原模型来源下载。
- 缓存备份会申请运行时，复制模型并校验，结束后尝试释放所申请的运行时。它会消耗时间、运行时资源和 Drive 空间，不保证比直接从 Hugging Face 下载更快。

如提示 `SERVICE_DISABLED` 或配额项目错误，需要在自己的 Google Cloud 配额项目启用 Drive API，并确认项目权限。可用配置接口设置自己的项目 ID；不要使用他人的项目名：

```powershell
$headers = @{ 'X-H3-Key' = '从本机配置页复制的Key' }
$body = @{ enabled = $false; backend = 'api'; quota_project = 'your-project-id' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:7865/api/drive-cache -Headers $headers -ContentType application/json -Body $body
```

保存配额项目后，在页面重新检查并启用 Drive 缓存。普通生成不依赖 Drive，也不需要预先运行备份脚本。

`seed_drive_cache.py` 是缓存备份和测速工具，操作报告写入 `.private/drive-cache-reports/`，按用户数据管理，不属于安装依赖。

## 本机数据

程序按需创建以下目录，均不应提交到 Git：

| 目录 | 内容 |
| --- | --- |
| `.private/` | 加密凭据、API Key、会话、任务记录、角色库、上传的图片和声音 |
| `videos/` | 下载到本机的视频 |
| `log/` | 启动器的服务日志和错误日志 |
| `.venv/` | 本机 Python 虚拟环境 |

网页草稿和部分偏好保存在当前浏览器的本地存储中。Windows DPAPI 凭据绑定当前 Windows 用户，不能把别人的 `.private/` 当成可复用配置。

用户素材和生成视频保存在上述数据目录中。测试使用程序生成的合成素材，仓库不携带个人作品或历史实测结果。

## 本地 API

在“配置 → 开发者 · 本地 API”复制本机 Key，请求携带 `X-H3-Key`。服务只接受本机访问。

以下示例会提交真实生成任务并使用 Colab 额度：

```python
import time
import requests

base = "http://127.0.0.1:7865"
headers = {"X-H3-Key": "从本机配置页复制的Key"}
response = requests.post(base + "/api/jobs", headers=headers, json={
    "prompt": "A quiet garden in soft morning light, leaves moving in the breeze.",
    "prompt_processing": "direct",
    "gpu": "G4",
    "resolution": "480p",
    "duration": 5,
    "count": 1,
    "release_mode": "always",
}, timeout=30)
response.raise_for_status()
job_id = response.json()["id"]

while True:
    response = requests.get(base + f"/api/jobs/{job_id}", headers=headers, timeout=30)
    response.raise_for_status()
    job = response.json()
    print(job["status"])
    if job["status"] in {"done", "warning", "error", "interrupted", "cancelled"}:
        print(job)
        break
    time.sleep(3)
```

| 接口 | 用途 |
| --- | --- |
| `GET /api/status` | 当前任务、作品及连接状态 |
| `POST /api/images` | 上传参考图，multipart 字段名为 `file` |
| `POST /api/jobs` | 提交生成任务 |
| `GET /api/jobs/{id}` | 查询任务 |
| `POST /api/jobs/{id}/cancel` | 取消任务 |
| `POST /api/jobs/{id}/retry` | 恢复或取回已有结果，视任务状态而定 |
| `GET /video/{id}.mp4` | 播放本机作品，支持 Range；加 `?download=true` 下载 |
| `GET` / `PUT /api/release-policy` | 查询或修改释放策略 |
| `POST /api/release` | 检查并释放工作台管理的运行时 |

图生任务先上传图片，取得 `image_id`，再在生成请求中传入 `references`，例如：

```json
{"generation_type":"image","references":[{"slot":1,"image_id":"上传返回的图片编号","frame":"first","role":"character","description":"保持人物外观"}]}
```

`frame` 可选 `first`、`last`、`reference`。API 省略 `gpu` 时默认 A100，网页默认选择与 API 默认值可能不同，建议调用方显式指定 GPU、种子、性能和释放参数。

## 开发与提交范围

运行代码包括 `app.py`、`remote_h3.py`、提示词模块、角色与历史模块、Drive 模块、运行时恢复模块、`studio.html` 和 `static/`。`seed_drive_cache.py` 被网页的备份接口调用，也需要提交。

安装入口、依赖声明和 `tests/` 中的离线回归测试一起提交。用户数据与运行产物由程序保存在数据目录中；历史实测结果和一次性诊断文件已从项目目录移除。具体文件边界和验证方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

本项目使用的主要上游项目包括 [ComfyUI](https://github.com/Comfy-Org/ComfyUI)、[MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3)、[社区 Qwen 编码器](https://huggingface.co/ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot) 和 [Veda-on-ComfyUI](https://github.com/veda-sparse/Veda-on-ComfyUI)。模型权重与上游组件各自的许可不因本项目公开而改变；仓库不包含模型权重。项目自身的许可证尚待维护者确定。
