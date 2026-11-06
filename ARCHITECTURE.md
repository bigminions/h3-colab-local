# LStudio 架构：应用、算力与素材库

工作台在本机管理导航、任务和素材，远程资源只负责运行应用。当前交付的应用是 H3 视频与 Qwen-Image-2.1 生图，已接入的算力是 Colab；RunPod 和 Modal 需要后续各自的实现与实测。

## 应用

工作台采用应用模块化架构，所有应用独立维护在 `apps/<app_id>/` 目录下，互不影响：

- **H3 视频应用 (`apps/h3/`)**：
  - `application.py`：拥有 H3 应用规范 (`ApplicationSpec`)、`H3JobConfig` 参数模型、素材校验钩子 (`validate_assets`)、远程程序打包 (`source()`)、输入素材清单、执行入口与空闲探测。
  - `characters.py`：角色素材管理、外观特征、声音样本与 `@` 提及展开。
  - `remote_h3.py`：Colab 远程 ComfyUI 工作流构建、模型加载与推理代码。
  - `h3_prompt.py`、`h3_prompt_worker.py`：Qwen 剧本理解、提示词整理与节点运行。
  - `seed_drive_cache.py`：H3 模型备份及缓存基准工具，网页备份接口从此应用目录加载；命令行使用 `python -m apps.h3.seed_drive_cache`。
  - `web/`：前端专属目录，包含 `content.html`（创作与角色管理面板）、`workbench.css`、`workbench.js`、`characters.js`。

- **Qwen-Image-2.1 生图应用 (`apps/qwen_image/`)**：
  - `application.py`：拥有 Qwen-Image-2.1 应用规范 (`ApplicationSpec`)、`QwenImageJobConfig` 参数模型、素材校验钩子 (`validate_assets`)、远程程序打包 (`source()`)、输入素材清单、执行入口与空闲探测。
  - `remote_qwen_image.py`：对接社区开源模型库 `abenzerps/Qwen-Image-2.1-Turbo-Quantized`，构建 ComfyUI 工作流（支持 `leejet/ComfyUI-GGUF` 的 UnetLoaderGGUF 以及标准 UNETLoader、Qwen3-VL 文本编码器与 VAE），完成远端环境准备、采样执行与图片生成。
  - `web/`：前端专属目录，包含 `content.html`（文生图与图生图面板）、`qwen-image.css`、`qwen-image.js`。
  - 素材用途声明 `references`（创作参考图）与 `outputs`（生成图片），物理保存于 `materials/qwen_image_2_1/` 下。

`workspace_runtime.py` 定义应用协议 (`ApplicationProtocol`)、应用描述和注册表。任务提交统一由目标应用的 `config_model` 与 `validate_assets` 独立校验，由提供商注册表检查算力配置。

应用专属 Python 模块统一通过 `apps.h3` 或 `apps.qwen_image` 导入，根目录不保留旧模块的兼容转发文件。应用脚本与样式仅在各自的 `web/` 目录维护，公共 `static/` 不保存应用副本。H3 提示词工具会打包到 Colab 的独立目录，远端独立运行时使用的同目录导入不依赖本机根目录文件。

后续应用在 `apps/` 目录下新增独立子目录，实现参数模型、远程适配器和中心内容页，并注册其输入输出能力与支持的提供商。统一队列通过应用标签和显卡选择任务，不依赖应用专属参数。不同应用的依赖不要求安装进同一个进程；切换应用或显卡时，由消费者确认并释放原运行时，再准备目标环境。Drive 成片存储目前用于视频，新输出类型需要相应的存储与恢复实现。

## 算力

`workspace_runtime.ComputeProvider` 定义连接、执行、下载、取消运行与释放的基本接口，`ProviderRegistry` 管理提供商描述、兼容性和延迟创建。`compute_colab.py` 负责 Colab 授权后的资源连接、文件传输、内核执行、恢复与释放；它向所选应用取得程序和空闲探测，不选择 H3 模型。

提供商注册表直接创建 `ColabProvider`，并注入所需的本机服务。`Controller` 保持单消费者串行执行，所有提交、重试与重新提交先进入统一等待队列，只有消费者领取任务后才连接资源；导航、读取注册表和查看素材不申请 GPU。

`job_queue.py` 负责应用标签亲和排序及 SQLite 持久化，数据位于 `.private/studio.sqlite3`。任务、事件与释放/消费者状态保存在同一个数据库，不再读写 `jobs.json` 或 `release-state.json`，不提供旧文件迁移。常用查询字段独立建列，应用参数和结果保留 JSON。数据库连接按操作关闭，写入在短事务内完成，网络调用不持有数据库事务。

普通任务追加队尾；插队任务进入等待队首，不中断当前任务，已有任务相对顺序保留。显式队首优先，其后优先选择与当前运行时应用标签和显卡一致的任务，否则按入队顺序执行。不比较模型等配置指纹。批次保留展示/取消分组，但每个子任务完成后重新调度，允许插队任务在子任务之间执行。新任务入队不会修改当前任务的释放策略。

取消等待任务只改队列；取消执行任务通过独立控制通道请求远端停止，确认后保留 GPU 和环境继续调度。取消未确认、未安全保存的结果或环境释放失败会暂停消费者，保留队列和运行时记录。释放 GPU 是独立操作；有活动任务时拒绝强制释放。空闲释放仍遵循用户选择的策略。

`remote_consumer.py` 随应用注入远端，记录任务编号、执行标识、应用、阶段、ComfyUI prompt ID 和心跳。消费者经 Jupyter Contents HTTP 接口读取状态/写入带任务标识的取消请求，忙碌内核不再接收探测代码。远端只删除/中断对应的 ComfyUI prompt，并中断当前 notebook 执行，保留内核和生成服务。内核恢复空闲后，再实时检查 ComfyUI 队列，避免把通信断开或 notebook 结束误判为生成停止。`/api/status` 返回缓存消费者状态和后端预计队序，`/api/consumer` 可主动核对远端状态。启动时保留等待任务，原运行任务标记为中断待核对，不盲目重复生成。

RunPod、Modal 在注册表中明确标记为不可用，不能提交任务。未来需各自实现凭据保护、资源状态与释放确认，不能把停机、销毁或自动缩容视为同一种操作。实例 ID 和结果必须绑定具体提供商，恢复时不跨提供商取回旧运行时中的临时文件。

GPU 选择作为工作台算力偏好保存；释放策略继续保存在本机后台。重置 H3 创作参数不会重置算力配置。

## 素材

`asset_library.py` 负责素材编号、物理目录、元数据和列表。统一布局为 `materials/{app_id}/{purpose}/{kind}`。共享导入为 `shared/imports`；H3 声明 `references`、`characters`、`outputs` 三类用途，并限制各自允许的媒体类型。创作上传、角色上传和生成结果分别进入其用途目录。角色配置和任务通过编号引用素材，预览与传输都通过素材库解析路径。

素材库合并本机文件和历史中的 Drive 副本。图片通过带本机 Key 的请求读取；视频沿用本机播放接口和 Range 支持。通用音频可以长于 H3 的声音参考上限，引用到 H3 时仍按应用规则校验。

## 界面与验证

工作台与各应用界面严格遵循 [PRINCIPLES.md](PRINCIPLES.md) 中定义的五大核心设计原则（简洁、易用、界面美观、用 DaisyUI 统一风格、交互友好细腻）。前端基于全局声明的 DaisyUI 暗色主题（`data-theme="dark"`）与 Tailwind CSS 语义化组件构建，严禁在外部样式中编写全局冲突的 `!important` 标签覆盖层，保证所有卡片、弹窗与操作按钮具有统一精细的触感与规范的盒模型。

`static/workspace.js` 与 `static/workspace.css` 管理全局壳层、侧边栏导航、算力入口和素材库等公共组件，不侵入各应用业务逻辑；各应用的前端（`content.html`、专属 CSS 和 JS）存放在各自的 `apps/<app_id>/web/` 目录下。启动时，服务端自动挂载各应用 web 静态路径，并在 `studio.html` 中注入对应内容与样式脚本。

顶栏应用导航根据当前激活的应用展示其内部 tab（如 H3 的“创作/角色管理”，Qwen-Image 的“生图创作”），各应用面板在切换时严格隔离显隐。左下角设置上方的算力卡片显示常用状态并打开算力页，顶部只显示页面位置。素材库按应用、用途和类型筛选，并显示导入目标。左侧“设置”存放通知、素材路径和开发者接口。

离线测试见 `tests/test_workspace.py`，覆盖应用用途目录边界、角色引用、按用途导入、非法文件清理、注册表不可用状态及应用与提供商解耦。已有 H3 测试继续验证执行、恢复、校验和释放。

需要隔离本机验证时，可设置 `LSTUDIO_DATA_ROOT` 指向独立数据目录，并通过 `LSTUDIO_PORT` 使用另一个本机端口（默认 7865）。源码和静态页面仍从项目目录读取；账号、任务、素材和日志均进入指定数据目录。请勿将生产 `.private/` 复制到验证目录。
