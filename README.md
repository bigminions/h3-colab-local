# LStudio

**LocalStudio · 本地创作工作台，按需使用云端 GPU。**

LStudio 在 Windows 本机管理应用、素材与任务，把模型下载和推理交给远程 GPU。素材和作品保存在本机，不要求本机配备 NVIDIA 显卡。

工作台分为三个部分：

- **应用**：左侧选择应用，中间显示应用内容。当前提供 H3 视频应用（创作与角色管理）与 Qwen-Image-2.1 生图应用（文生图与图生图）。
- **算力**：左下角、设置上方的算力卡片显示提供商、GPU、运行状态、剩余计算单元与预估可用时长，点击进入配置。
- **素材库**：图片、视频、音频统一保存，跨应用复用。设置位于左侧导航底部。

切换页面和浏览素材不会申请 GPU；提交生成任务后才连接算力。

## 当前支持

| 项目 | 状态 |
| --- | --- |
| MiniMax H3 视频应用 | 可用：文生视频、图生视频、首尾帧、角色和声音参考 |
| Google Colab 算力 | 可用：账号授权、按需连接、运行时复用与释放 |
| 本机素材库 | 可用：导入、筛选、搜索、预览、下载，图片可用于 H3 或 Qwen-Image |
| Qwen-Image-2.1 生图应用 | 可用：文生图、图生图、批量生成，使用社区量化模型 (`abenzerps/Qwen-Image-2.1-Turbo-Quantized`) |
| RunPod / Modal | 尚未接入，算力页仅展示其状态 |

任务串行执行。应用和算力的扩展接口已拆出，详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 环境要求

- Windows 10 / 11、64 位 Python 3.12。凭据使用当前 Windows 用户的 DPAPI 加密。
- 可使用 Colab 的 Google 账号，以及可分配所选 GPU 的计算单元或配额。GPU 列表包含 G4、A100、H100、L4、T4，并显示检测到的在线运行时；实际供应以账号返回为准。
- 本机可以访问 Google OAuth、Colab 和可选的 Drive；远程运行时可以访问 GitHub、Hugging Face 等代码及模型来源。
- 本机有足够空间保存素材、图片和视频。远程模型可能占用数十 GB 磁盘；启用 Drive 模型缓存还需要相应的 Drive 空间。

首次使用或释放后重新申请运行时，需要准备环境和模型。准备过程也会消耗运行时资源。

## 安装与启动

在项目目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe launch.py
```

安装完成后也可双击 `start.cmd`。启动器会打开 [LStudio 本机工作台](http://127.0.0.1:7865/)，已有 LStudio 服务时复用该服务。

`requirements.txt` 与 `requirements-cached.txt` 需要放在一起，分别声明本机依赖和已验证的版本约束。远程 ComfyUI、模型和推理依赖由应用准备。

启动失败时，可直接运行后台查看错误：

```powershell
.\.venv\Scripts\python.exe -u app.py
```

启动器日志位于 `log/server.log` 和 `log/server-error.log`。更新代码后，正常退出后台并重新启动。

## 开始创作

1. 点击左下角算力卡片，连接 Google 账号。按页面提示完成授权，将授权码粘贴回本机工作台。
2. 在侧边栏选择“H3 视频”或“Qwen-Image-2.1”，在应用内选择 GPU，也可进入算力页选择运行时和释放方式。选择运行时不会立即申请 GPU。
3. 输入提示词并调整生成参数；需要参考图时，可上传本机图片，或通过素材选择弹窗引用已有图片。
4. 提交生成后，在当前应用的任务区查看进度与错误，完成后预览、下载作品或打开保存目录。

### H3 视频

进入“H3 视频 → 创作”，输入画面、动作与声音描述，选择比例、画质、时长和数量。首次验证可以使用 5 秒、480p。网页初始 GPU 为 G4，选择的模型与参数仍需匹配运行时显存。

图生视频可直接上传图片，也可从素材库选图。图片可设置为一般参考图、首帧或尾帧；首尾帧各限一张。素材库图片卡片上的“用于 H3”会把图片加入当前创作。

点击 H3 创作区的“角色管理”，在弹窗中保存角色外观、参考图和声音样本，随后在提示词中输入 `@` 选择图片或角色。

- **素材上限**：角色图、其他参考图和首尾帧合计最多 9 张。声音参考最多 3 段，每段 2–15 秒，合计不超过 15 秒。
- **批量生成**：一次 1–8 条，顺序执行并复用运行时，各条使用独立输出编号和种子。
- **Qwen 提示词整理**：理解剧本并整理成 H3 使用的英文区段，保留原文对白和画面文字。可查看整理日志，或关闭整理直接传入提示词。
- **性能加速**：编码器、主模型和视频 VAE 量化、PDD 8 步、实验性 Veda 注意力加速可分别调整。关闭总开关会使用原版权重、常规 20 步和完整注意力，资源占用增加。

加速组合可能影响画面和声音。模型版本、校验和加载来源记录在代码及任务日志中。

### Qwen-Image 生图

进入“Qwen-Image-2.1”，选择“文生图”或“图生图”。文生图输入画面描述后生成；图生图先上传或选择参考图，再填写提示词并调整参数。

- **画幅**：支持 1:1、16:9、9:16、4:3、3:4、3:2、2:3，具体输出尺寸随比例显示。
- **模型与采样**：可选择 GGUF 或 safetensors 量化模型，调整编码器、采样步数、CFG、种子和图生图重绘强度。
- **默认配置**：Q4_K_M 模型、INT8 编码器、8 步、CFG 1.0；网页和 API 默认 GPU 为 T4。实际显存需求随模型和画幅变化。
- **批量生成**：一次 1–8 张，顺序执行并使用独立种子；生成图片可放大查看、下载或再次引用。

H3 与 Qwen-Image 分别保留创作草稿、GPU 选择和作品视图，生成任务共用串行队列。

## 素材与本机数据

所有媒体保存在同一个 `materials/` 根目录，按“应用 / 用途 / 类型”存放，以素材编号引用，原始名称保存在元数据中：

```text
materials/
├── shared/imports/          通用导入素材
│   ├── images/
│   ├── videos/
│   └── audios/
├── h3/
│   ├── references/images/  H3 创作参考图与首尾帧
│   ├── characters/
│   │   ├── images/         角色图片
│   │   └── audios/         角色声音样本
│   └── outputs/videos/     H3 生成和恢复的作品
└── qwen_image_2_1/
    ├── references/images/  Qwen-Image 创作参考图
    └── outputs/images/     Qwen-Image 生成图片
```

素材库可按应用、用途和类型筛选。导入前会显示保存位置；直接从应用创作区或 H3 角色管理上传的素材自动归入对应用途。从其他用途引用素材时保留其原有位置，跨应用复用无需复制文件。

素材库支持 PNG、JPEG、WebP 图片，MP4 视频，以及 WAV、MP3、M4A、OGG、FLAC、AAC 音频。图片最大 20MB；单个视频或音频最大 512MB。通用音频导入不受 H3 声音时长限制，引用到 H3 时会按应用规则校验。

| 目录 | 内容 |
| --- | --- |
| `materials/` | 统一媒体素材和作品 |
| `.private/` | 加密凭据、本地 API Key、会话、任务记录和角色配置 |
| `log/` | 启动器日志 |
| `.venv/` | 本机 Python 虚拟环境 |

上述目录均为本机数据，不应提交到 Git。草稿、算力选择和部分偏好保存在浏览器中；DPAPI 凭据绑定当前 Windows 用户。

作品区的移除操作使用系统回收箱。需要恢复时，在系统回收箱还原对应作品，工作台会检查恢复包并重建记录。

## 算力释放

算力页提供三种策略：整批完成后释放、闲置后释放、手动释放。默认在闲置 10 分钟后检查释放。

保留运行时期间仍可能消耗额度。释放前会检查运行时是否空闲，以及结果是否已保存和校验。取消任务或网络中断后，可在算力页检查实际状态。

关闭浏览器不会关闭后台；退出后台后，定时释放也会停止。结束使用前请确认 GPU 已释放，必要时在自己的 Colab 中核对运行时。

## Google Drive（可选）

无需授权 Drive 即可生成和保存到本机。在“算力 → 连接与缓存”中可独立授权 Drive，用于：

- **H3 成片备份**：保存到 `H3-Videos`，本机副本缺失时可恢复并校验。
- **H3 模型缓存**：保存到 `H3-Model-Cache`，校验来源版本、大小和 SHA-256，作为模型下载的备用来源。

当前 Drive 备份与缓存用于 H3；Qwen-Image 的生成图片下载并校验后保存到本机素材库。

自动备份模型会申请运行时、复制和校验模型，并在结束后尝试释放资源。它会消耗运行时和存储空间，不保证比直接下载更快。备份报告位于 `.private/drive-cache-reports/`。

遇到 `SERVICE_DISABLED` 或配额项目错误时，请在自己的 Google Cloud 配额项目启用 Drive API，并确认项目权限。可通过本地配置接口保存项目 ID，再在页面重新检查和启用缓存：

```powershell
$headers = @{ 'X-LStudio-Key' = '从本机设置页复制的 Key' }
$body = @{ enabled = $false; backend = 'api'; quota_project = 'your-project-id' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://127.0.0.1:7865/api/drive-cache -Headers $headers -ContentType application/json -Body $body
```

## 本地 API

在“设置 → 开发者 · 本地 API”复制 Key，请求携带 `X-LStudio-Key`。服务仅接受本机访问。

| 接口 | 用途 |
| --- | --- |
| `GET /api/workspace` | 应用、提供商和素材目录 |
| `GET /api/status` | 任务、作品和连接状态 |
| `GET /api/compute` | 本机已知的算力状态，不探测云端 |
| `GET /api/assets` | 素材列表，支持 `app_id`、`purpose`、`kind` 筛选 |
| `POST /api/assets` | 导入素材，multipart 字段为 `file`、`app_id`、`purpose`，默认 `shared/imports` |
| `GET /api/assets/{id}/file` | 读取素材；加 `?download=true` 下载 |
| `POST /api/images` | 上传应用图片，multipart 字段为 `file`、`app_id`、`purpose`，默认 `h3/references`，返回 `image_id` |
| `POST /api/jobs` | 提交生成任务，通过 `app_id` 选择 `h3` 或 `qwen_image_2_1` |
| `GET /api/jobs/{id}` | 查询任务 |
| `POST /api/jobs/{id}/cancel` | 取消任务 |
| `POST /api/jobs/{id}/retry` | 恢复任务或取回已有结果 |
| `GET /video/{id}.mp4` | 播放本机视频，支持 Range；加 `?download=true` 下载 |
| `GET /image/{id}.png` | 预览生成图片；加 `?download=true` 下载 |
| `GET` / `PUT /api/release-policy` | 查询或修改释放策略 |
| `POST /api/release` | 检查并释放工作台管理的运行时 |

以下示例会提交真实生成任务并使用 Colab 额度：

```python
import time
import requests

base = "http://127.0.0.1:7865"
headers = {"X-LStudio-Key": "从本机设置页复制的 Key"}
response = requests.post(base + "/api/jobs", headers=headers, json={
    "app_id": "h3",
    "provider_id": "colab",
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
        break
    time.sleep(3)
```

H3 图生视频使用 `references`。上传图片后，将返回的 `image_id` 放入对应引用：

```json
{
  "generation_type": "image",
  "references": [
    {"slot": 1, "image_id": "上传返回的图片编号", "frame": "first"}
  ]
}
```

H3 的 `frame` 可选 `first`、`last`、`reference`。H3 API 默认 GPU 为 A100，网页初始选择 G4。

Qwen-Image 文生图可向同一 `POST /api/jobs` 接口提交以下请求体：

```json
{
  "app_id": "qwen_image_2_1",
  "provider_id": "colab",
  "prompt": "清晨湖边的小木屋，柔和自然光，写实摄影风格",
  "gpu": "T4",
  "aspect_ratio": "1:1",
  "model_file": "qwen-image-2.1-turbo-Q4_K_M.gguf",
  "steps": 8,
  "cfg": 1.0,
  "count": 1,
  "seed": 42,
  "release_mode": "always"
}
```

上传 Qwen-Image 参考图时指定 `app_id=qwen_image_2_1`、`purpose=references`，生成请求再携带 `references`（如 `[{"slot": 1, "image_id": "上传返回的图片编号"}]`）。各应用使用自己的参数模型；调用方应显式指定应用、GPU、种子和释放配置。

## 开发与验证

应用、算力和素材库的代码边界见 [ARCHITECTURE.md](ARCHITECTURE.md)，源码与数据的提交范围见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```text
apps/
├── h3/
│   ├── application.py      H3 配置、校验与远端程序打包
│   ├── characters.py       角色素材与提示词引用
│   ├── remote_h3.py        H3 远端环境与推理
│   ├── h3_prompt.py        提示词处理
│   ├── h3_prompt_worker.py 提示词工作进程
│   ├── seed_drive_cache.py H3 模型备份工具
│   └── web/                H3 页面、样式与脚本
└── qwen_image/
    ├── application.py      生图配置、校验与远端程序打包
    ├── remote_qwen_image.py 生图远端环境与推理
    └── web/                生图页面、样式与脚本
```

应用专属代码和前端资源统一放在各自应用目录，通过 `apps.h3`、`apps.qwen_image` 导入；根目录不再保留应用兼容文件。`studio.html` 为工作台壳层，公共 `static/` 保存共享资源，服务端自动加载各应用的 `web/` 内容。

H3 模型备份工具的命令行入口为：

```powershell
.\.venv\Scripts\python.exe -m apps.h3.seed_drive_cache --help
```

隔离验证可指定独立数据目录和本机端口：

```powershell
$env:LSTUDIO_DATA_ROOT = Join-Path $PWD '.private/dev-workspace'
$env:LSTUDIO_PORT = '7866'
.\.venv\Scripts\python.exe -u app.py
```

源码仍从项目目录读取，账号、任务、素材和后台数据写入指定目录。启动器默认使用项目数据目录与 7865 端口，自定义运行参数请直接启动 `app.py`。

离线检查使用临时目录、模拟传输和合成素材。请在干净的检出目录运行，或先设置独立的 `LSTUDIO_DATA_ROOT`，避免读取现有账号和任务数据：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t . -q
Get-ChildItem -File tests/test_*.cjs | ForEach-Object { node $_.FullName; if ($LASTEXITCODE -ne 0) { throw "Frontend check failed: $_" } }
Get-ChildItem -Path static, apps -Recurse -Filter *.js -File | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed: $_" } }
git diff --check
```

真实授权、GPU 分配、模型下载和生成需要单独实测，会消耗账户资源。

## 上游与许可

主要上游包括 [ComfyUI](https://github.com/Comfy-Org/ComfyUI)、[MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3)、[社区 Qwen 编码器](https://huggingface.co/ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot)、[Veda-on-ComfyUI](https://github.com/veda-sparse/Veda-on-ComfyUI)、[Qwen-Image-2.1 社区量化模型](https://huggingface.co/abenzerps/Qwen-Image-2.1-Turbo-Quantized) 和 [ComfyUI-GGUF](https://github.com/leejet/ComfyUI-GGUF)。

仓库不包含模型权重。上游组件与模型各自的许可保持有效；LStudio 自身的许可证尚待维护者确定。
