# LStudio 设计原则与交互规范

本文档定义 LStudio 工作台及各个应用模块（H3 视频创作、Qwen-Image 生图等）的前端设计哲学、视觉标准与交互规范。所有后续功能开发、页面重构与 UI 优化必须严格遵循以下五大核心原则。

---

## 一、核心设计原则

### 1. 简洁 (Concise & Focused)
- **拒绝过度嵌套**：杜绝冗余的容器层级与“框中框”。输入框内嵌图标时采用单层 input + 内部绝对定位图标，严禁嵌套带边框的 label 与 input 制造双重边框。
- **排版克制紧凑**：内容自然包裹，避免无意义的垂直或水平空白。卡片、弹窗与列表应当“高度随内容而定（height: auto）”，严禁无内容支撑时通过 `flex: 1` 纵向硬撑产生大面积空洞死区。
- **信息层级分明**：界面第一视觉聚焦于核心创作区或素材内容，次要提示、统计信息与辅助选项低饱和度弱化呈现。

### 2. 易用 (Intuitive & Efficient)
- **符合直觉的操作动线**：
  - 弹窗统一遵循标准现代 Web/桌面模式：左上角放置返回/关闭按钮并与标题对齐同行，右上角放置核心操作（如新建、保存）。
  - 支持全键盘无障碍操作：ESC 键与点击背景遮罩（Backdrop）均可直接平滑关闭弹窗。
- **就近闭环与上下文直达**：
  - 素材选择与引用采用弹窗原位完成（Modal Picker），不打断主创作流程，选中确认后即刻生效，不作全局跳转。
  - 在剧本或提示词输入框中输入 `@` 时，菜单跟随光标当前坐标实时弹出，提供缩略图+名称即选即填。
- **安全可逆与兜底机制**：
  - 高风险操作（如删除角色、清除历史）提供明确的一键恢复或回收机制（如“显示已删除角色”中随时恢复），降低操作心理负担。

### 3. 界面美观 (Aesthetic & Polished)
- **视觉比例严谨考究**：
  - 角色卡片与素材预览图采用标准人像/画面比例（如 3:4 或 4:5），避免随意拉伸变形；搭配 `object-cover` 与居中顶部对齐（`object-position: top center`），完美呈现人物主体。
  - 按钮与小组件严格对齐基线，高度与内边距统一（微型按钮 24px 高度，小按钮 32px 高度，标准按钮 40px 高度）。
- **统一暗色调光影层次**：
  - 基础画板底色为深灰黑 `#101218`，通过不同层级的灰度（`bg-base-100`, `bg-base-200`, `bg-base-300`）营造清晰的空间景深，搭配细腻的 1px 细微半透明边框（`border-base-content/10`）与柔和阴影，界面现代深邃。

### 4. 用 DaisyUI 统一风格 (Unified DaisyUI Design System)
- **全面拥抱 DaisyUI + Tailwind 语义化体系**：
  - **主题与环境**：全局根节点声明 `<html lang="zh-CN" data-theme="dark">`，所有组件颜色必须走 DaisyUI 语义色（`bg-base-*`, `text-base-content`, `btn-primary`, `badge-*`, `border-base-*`），保证全局暗色风格 100% 一致。
  - **弹窗组件**：统一采用 `<dialog class="modal">` + `<div class="modal-box">` + `<form method="dialog" class="modal-backdrop">` 架构。
  - **卡片组件**：统一采用 `card card-compact bg-base-100 border border-base-300`，内部分为 `figure`（封面）与 `card-body`（信息与操作），卡片必须设置 `align-self: start`，Grid 容器添加 `content-start`（`align-content: flex-start`）。
  - **按钮规范**：所有可点击动作必须是真正的 DaisyUI `btn` 实体按钮（如 `btn btn-xs btn-outline`、`btn btn-primary`、`join` 组合），**严禁使用裸露的彩色文字伪装成按钮**。
  - **表单控件**：输入框采用 `input input-sm input-bordered`；复选框采用 `checkbox checkbox-sm checkbox-primary`；徽标采用 `badge badge-sm badge-info`。
- **严禁滥用冲突的全局 CSS 与 `!important`**：
  - 严禁在外部 CSS 中对通用标签或 ID 编写带 `!important` 的尺寸、边框或 flex 规则（如滥用 `flex: 1 1 auto !important` 或硬写全局 `#characterSearch { border: ... !important; }`）。
  - 自定义样式仅作为微调补充，不得破坏 DaisyUI 组件内置的盒模型与弹性布局。

### 5. 交互友好细腻 (Delicate & Responsive Interactions)
- **状态即时感知与优雅占位**：
  - 异步加载图片时，必须显示 `avatar placeholder` 或骨架屏，图片就绪（`onload`）后平滑显现，并**从 DOM 树中物理清理占位节点**，彻底杜绝占位文字或圆形初始覆盖在图片之上的瑕疵。
- **生动的微交互动效**：
  - 卡片 Hover 提供平滑微上浮（`-translate-y-0.5`）与阴影加深（`shadow-xl`）。
  - 封面图片在鼠标悬停时轻微缩放（`scale-105`），产生呼吸感。
  - 按钮与图标拥有明确的悬停背景色反馈与点击按压（`active:scale-95`）质感。
- **清晰的操作反馈**：
  - 提交、保存、切换或删除时，即时提供文本状态提示（如“正在保存…”、“已恢复 男主”），操作结束提供短暂成功反馈，发生异常时呈现温和明确的错误警告，引导用户排查。

---

## 二、开发自查清单 (Checklist)

每次新增或修改界面组件时，务必核对以下清单：
- [ ] 是否直接使用了 DaisyUI 标准语义类，而不是随意手写非标 CSS？
- [ ] 弹窗内卡片是否尺寸紧凑自然，是否存在垂直方向空洞拉伸？
- [ ] 搜索框是否为单层结构，是否存在双重边框、框中框？
- [ ] 操作项是否为具备点击范围与边框的实体按钮，是否在窄屏或多按钮时发生意外折行？
- [ ] 异步加载图片后，占位状态（如“读取中…”、“骨架屏”）是否被物理清理？
- [ ] 弹窗是否支持左上角退出、ESC 按键与点击遮罩平滑退出？
