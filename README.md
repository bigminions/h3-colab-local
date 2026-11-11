# LStudio (LocalStudio)

**本地创作工作台，按需使用云端 GPU。**

LStudio 在 Windows 本机管理应用、素材与任务，将模型下载与重度推理交给远程 GPU（如 Google Colab）。生成的素材和作品全部保存在本机，无需本机配备高性能 NVIDIA 显卡。

工作台主要包含三个部分：
- **应用中心**：支持 MiniMax H3 视频生成、Qwen-Image-2.1 生图以及大模型对话。
- **算力管理**：按需连接远程算力，实时显示运行状态、剩余计算单元与预估时长，支持自动闲置释放。
- **统一素材库**：图片、视频、音频统一管理，跨应用随时引用与复用。

---

## 功能特性

| 功能模块 | 说明 |
| --- | --- |
| **MiniMax H3 视频** | 支持文生视频、图生视频、首尾帧控制、角色外观与声音参考 |
| **Qwen-Image-2.1 生图** | 支持文生图、图生图、多种画幅比例与量化模型，支持批量生成 |
| **大模型对话** | 支持 Gemma 4 31B（支持图文多模态视觉输入）及 GPT-OSS 120B，多会话管理与流式打字回复 |
| **Google Colab 算力** | 账号快速授权、按需连接、运行时复用与闲置自动释放 |
| **本机统一素材库** | 本地导入、筛选、搜索、预览，跨应用无缝复用素材 |
| **Google Drive（可选）** | 可选用于 H3 视频成片云端备份与模型缓存加速 |

---

## 环境要求

- **操作系统**：Windows 10 / 11（64 位）。
- **Python 环境**：Python 3.12（推荐）。
- **Google 账号**：拥有 Google Colab 访问权限，并具备可用 GPU 配额（支持 T4、L4、A100、H100 等）。
- **网络与存储**：本机需可顺畅访问 Google 服务；预留足够的本地磁盘空间保存生成素材与作品。

---

## 安装与启动

从 Git 仓库获取源码后的完整安装与运行步骤：

### 1. 克隆仓库与准备目录

打开 PowerShell 或终端：

```powershell
git clone https://github.com/bigminions/h3-colab-local.git
cd h3-colab-local
```

### 2. 创建虚拟环境并安装依赖

在项目根目录下执行：

```powershell
# 创建虚拟环境（确保使用 Python 3.12）
python -m venv .venv

# 安装项目依赖
.\.venv\Scripts\pip install -r requirements.txt
```

> **提示**：若系统安装了多个 Python 版本，推荐使用 `py -3.12 -m venv .venv` 创建环境。

### 3. 启动工作台

- **日常使用（推荐）**：  
  直接在文件管理器中**双击 `start.cmd`**（或在终端运行 `.\start.cmd`）。启动器会自动拉起服务并在默认浏览器打开工作台：[http://127.0.0.1:7865/](http://127.0.0.1:7865/)。
- **开发者 / 排查启动问题**：  
  可在终端前台运行以查看实时控制台日志：
  ```powershell
  .\.venv\Scripts\python.exe -u app.py
  ```

日常日志保存在 `log/server.log` 与 `log/server-error.log`。

---

## 快速上手

1. **连接算力**：  
   打开工作台后，点击左下角的算力卡片，点击连接 Google 账号。按页面指引完成授权，将授权码粘贴回工作台完成连接。
2. **开始创作**：  
   - **H3 视频**：输入画面及运动描述，选择画质与时长。支持上传参考图或指定首尾帧；亦可在“角色管理”中录入角色与声音，并在提示词中通过 `@角色名` 快速引用。
   - **Qwen-Image**：选择文生图或图生图，调整画幅比例、采样步数与模型量化版本，支持批量连续生成。
   - **大模型对话**：选择 GPU 与模型后点击“开启应用”，模型就绪后即可开展多会话对话，支持拖拽或粘贴图片进行多模态视觉问答。
3. **查看与管理作品**：  
   生成任务在后台串行队列中有序执行。生成完成后可直接在线预览、下载作品或在本地素材库中查看。

---

## 算力管理与释放

- **释放策略**：算力页提供“队列完成后释放”、“闲置后释放（默认 10 分钟）”及“手动释放”三种策略，有效避免长时间空置消耗 Colab 计算单元。
- **会话退出**：大模型对话使用完毕后，点击“关闭应用”即可及时归还 GPU 资源。
- **Google Drive（可选加速）**：可在“算力 → 连接与缓存”中单独绑定 Drive，用于自动备份 H3 视频作品与缓存常用模型权重，加速下一次远程启动。

---

## 本地数据说明

工作台产生的所有数据均保存在本机，且已加入 `.gitignore`，不会提交至代码仓库：

- `materials/`：统一保存所有导入的媒体素材与生成的图片、视频作品。
- `.private/`：保存加密凭据（DPAPI 加密）、本地会话记录与任务数据库。
- `log/`：运行日志。

---

## 开发与贡献

- 架构设计与模块分层请参考 [ARCHITECTURE.md](ARCHITECTURE.md)。
- 源码规范与测试要求请参考 [CONTRIBUTING.md](CONTRIBUTING.md)。
- 本地离线回归测试命令：
  ```powershell
  .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -t . -q
  ```

---

## 上游与致谢

核心上游项目包括 [ComfyUI](https://github.com/Comfy-Org/ComfyUI)、[MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3)、[Qwen-Image-2.1 社区模型](https://huggingface.co/abenzerps/Qwen-Image-2.1-Turbo-Quantized)、[Veda-on-ComfyUI](https://github.com/veda-sparse/Veda-on-ComfyUI) 与 [ComfyUI-GGUF](https://github.com/leejet/ComfyUI-GGUF)。
