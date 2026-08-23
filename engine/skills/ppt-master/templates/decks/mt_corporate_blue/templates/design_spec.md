---
deck_id: mt_corporate_blue
kind: deck
category: brand
summary: 面向公司内部汇报、项目复盘、方案沟通和客户交流的 MT 蓝顶白底演示模板，帮助内容保持统一、清晰、可交付。
keywords: [MT, 公司模板, 蓝顶白底, 内部汇报, 项目复盘]
primary_color: "#004696"
canvas_format: ppt169
canvas_width: 1280
canvas_height: 720
canvas_viewbox: "0 0 1280 720"
source_canvas_width: 1280
source_canvas_height: 720
source_viewbox: "0 0 1280 720"
replication_mode: fidelity
native_structure_mode: structured
page_count: 11
placeholders:
  01_cover: ["{{TITLE}}", "{{SUBTITLE}}", "{{LOCATION}}", "{{DATE}}", "{{AUTHOR}}"]
  02_toc: ["{{PAGE_TITLE}}", "{{TOC_ITEM_1_TITLE}}", "{{TOC_ITEM_2_TITLE}}", "{{TOC_ITEM_3_TITLE}}", "{{TOC_ITEM_4_TITLE}}", "{{PAGE_NUM}}"]
  03a_content_standard: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{CONTENT_AREA}}", "{{PAGE_NUM}}"]
  03b_content_two_col: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{CONTENT_AREA}}", "{{SUPPORTING_AREA}}", "{{PAGE_NUM}}"]
  03c_content_image_right: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{CONTENT_AREA}}", "{{IMAGE_AREA}}", "{{PAGE_NUM}}"]
  03d_content_chart_table: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{DATA_VISUAL}}", "{{DATA_DETAIL}}", "{{PAGE_NUM}}"]
  03e_content_kpi: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{KPI_1}}", "{{KPI_2}}", "{{KPI_3}}", "{{KPI_4}}", "{{CONTENT_AREA}}", "{{PAGE_NUM}}"]
  03f_content_timeline: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{STAGE_1}}", "{{STAGE_2}}", "{{STAGE_3}}", "{{STAGE_4}}", "{{TIMELINE_NOTE}}", "{{PAGE_NUM}}"]
  03g_content_comparison: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{OPTION_A_TITLE}}", "{{OPTION_B_TITLE}}", "{{OPTION_A_CONTENT}}", "{{OPTION_B_CONTENT}}", "{{PAGE_NUM}}"]
  03h_content_three_card: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{CARD_1}}", "{{CARD_2}}", "{{CARD_3}}", "{{PAGE_NUM}}"]
  03i_content_data_story: ["{{PAGE_TITLE}}", "{{KEY_MESSAGE}}", "{{DATA_VISUAL}}", "{{INSIGHTS}}", "{{PAGE_NUM}}"]
---

# MT 公司蓝顶白底模板 — Design Specification

## I. Template Overview

| Application context | Definition |
|---|---|
| Recurring presentation family | 公司内部汇报、项目复盘、业务方案、阶段总结和客户沟通 |
| Intended audiences and outcomes | 供管理层、项目团队、跨部门协作方和客户阅读或听取；帮助其快速理解结论、证据与下一步 |
| Delivery and reading assumptions | 以现场讲解为主，同时支持会后独立阅读和文件交接 |
| Representative narrative/page roles | 品牌封面、目录、结论先行正文、双栏论证、图文说明、数据与表格 |

- 视觉基调：专业、克制、可信，浅色正文页面。
- 识别特征：蓝色横向渐变页眉、白色内容区、绿色结论强调、右上角白色品牌字标、右下角页码。
- **第 1、2 页（封面/目录）**：必须取自 `exports/mt_corporate_blue_template_preview.pptx`（该文件即公司原件《MT PPT模板_2023.pptx》前 3 页的逐字节副本）。版式、形状、配色、字体一律不动，但**必须填充真实内容**——封面填标题/副标题/地点/日期，目录页填真实议程条目；封面顶部保留原稿 `pic idx=10` 图片占位槽，未启用 AI 配图时透明留空，启用后由宿主系统用 `images/company_cover_hero.png` 自动替换并居中裁切；其余填充只允许改写既有占位文本，禁止 SVG 重绘或增删形状。
- **第 3 页起（正文）**：内容与版面自由发挥，但**页眉横条必须与公司模板逐项一致**，按下方「页眉契约」执行；页脚保留 `For internal use - Confidential`。

## I-b. 页眉契约（正文页强制，1280×720 坐标，已按公司原件 Master/Layout 核实）

| 元素 | 参数 |
|---|---|
| 渐变横条 | (0,0) 1280×80，水平线性渐变 `#004696` → `#3399FF`（0°，左深右亮） |
| 页标题 | (55,20) 968×60，Arial **常规体**（非粗体）24pt（SVG 32px），白色，垂直居中 |
| 品牌字标 | (1084.4,51) 127.2×14.5，白色 METTLER TOLEDO 字标（原件 EMF 已提取为 `images/mt_header_logo.png`，SVG 原型直接引用该图，禁止用文字近似） |
| 分隔线 | x=1225.4 竖线，白色（bg1），0.5pt（SVG stroke-width 0.67），y 49→79.3 |
| 页码 | 原稿 OOXML 框 `(1224.57,34.41) 55.43×45.35`，Arial 14.2pt 白色，**不补零**（2、3、4…）；文本框底部锚定、段落左对齐，左内边距 7.56、下内边距 10.58。flat SVG 预览使用 `x≈1233, y≈63` 的文字基线，**禁止使用 y=74**；最终公司合并必须以原稿属性写入动态 `slidenum` 字段。 |
| 绿色副标（可选） | (55,107) 1171×34，Arial **粗体** 18pt（SVG 24px），`#56A410` |
| 页脚 | (55,698) 1171×16，Arial 8pt（SVG 10.67px）灰色，右对齐，固定文案 `For internal use - Confidential` |

## II. Color Scheme

| Role | Color | Usage |
|---|---|---|
| Primary | `#004696` | 标题、深蓝文字、页眉渐变起点 |
| Bright blue | `#3399FF` | 页眉渐变终点、辅助数据色 |
| Green accent | `#56A410` | 结论、重点、目录编号 |
| Light blue | `#97CBFF` | 辅助信息和浅色数据层 |
| Light green | `#BEF38E` | 浅色强调层 |
| Orange | `#E69400` | 需要区分的次要重点 |
| Background | `#FFFFFF` | 页面和正文区 |
| Body text | `#1F2937` | 正文 |
| Muted text | `#7A7A7A` | 页脚和说明 |

## III. Typography

- 英文和数字：Arial。
- 中文：黑体；导出环境不具备黑体时，以系统无衬线字体替代。
- 封面标题：32 pt。
- 封面副标题：24 pt。
- 地点、日期、作者：16 pt。
- 正文页标题：24 pt，单行。
- 结论或副标题：18 pt，加粗，绿色。
- 正文：16 pt。
- 页脚：8 pt。
- 图表标签建议 14 pt；紧凑图表可使用 12 pt，正文内容不低于 12 pt。

## IV. Signature Design Elements

- 正文页顶部 80 px 蓝色水平渐变条，从 `#004696` 过渡到 `#3399FF`。
- 页眉左侧为白色标题；右侧为白色品牌字标图、白色细分隔线和白色页码。
- 正文区保持白底，标题下方优先放置一行绿色结论，再进入正文或数据内容。
- 页脚右对齐显示 `For internal use - Confidential`；外部沟通时可按受众替换。
- 封面沿用源模板的左侧蓝色品牌区、右侧标题区和底部真实品牌图。
- 形状保持平直、几何和简洁；不使用卡通、圆润、3D 或过度装饰。
- 照片使用矩形干净边缘；图文版式右侧媒体区约占页面宽度 38%。

## V. Page Roster

| SVG | Layout key | PowerPoint picker name | Role and structural capacity |
|---|---|---|---|
| `01_cover.svg` | `mt_cover` | MT 封面 | 左侧品牌区、顶部 3.404:1 图片槽、右侧标题/副标题、地点/日期/作者和底部品牌图 |
| `02_toc.svg` | `mt_toc` | MT 目录 | 蓝色页眉、四项编号目录、页码和内部保密页脚 |
| `03a_content_standard.svg` | `mt_content_standard` | MT 正文 - 标准 | 单列正文，包含页面标题、绿色结论和大正文区 |
| `03b_content_two_col.svg` | `mt_content_two_col` | MT 正文 - 双栏 | 左右双栏内容区，中间细分隔线 |
| `03c_content_image_right.svg` | `mt_content_image_right` | MT 正文 - 右图 | 左侧文字区和右侧媒体/图片区 |
| `03d_content_chart_table.svg` | `mt_content_chart_table` | MT 正文 - 数据 | 上方大数据视觉区和下方数据明细区 |
| `03e_content_kpi.svg` | `mt_content_kpi` | MT 正文 - KPI 指标 | 四张浅蓝指标卡（大数字）+ 下方证据/图表面板；适合总览页、成果页、月度关键数字 |
| `03f_content_timeline.svg` | `mt_content_timeline` | MT 正文 - 时间线 | 横向时间轴（四节点，末节点绿色）+ 四张阶段卡 + 底部备注行；适合项目计划、里程碑、实施步骤 |
| `03g_content_comparison.svg` | `mt_content_comparison` | MT 正文 - 对比 | 左蓝右绿两个标题条 + 左右内容面板；适合方案 A/B、现状 vs 目标、竞品对照 |
| `03h_content_three_card.svg` | `mt_content_three_card` | MT 正文 - 三卡片 | 三张等宽卡片（顶缘蓝/亮蓝/绿色条区分）；适合三要点、三方案、三阶段并列 |
| `03i_content_data_story.svg` | `mt_content_data_story` | MT 正文 - 数据结论 | 左侧大数据视觉面板（约 60%）+ 右侧绿顶结论栏;适合图表主导、右侧列洞察的页面 |

### V-b. 正文原型选择指引（策略师排版时遵循）

- 原型仅在页面内容**天然匹配**时选用；拿不准或内容形态不典型时，一律回退 `03a`/`03b`
  自由构图。**禁止为追求版式多样性而硬套原型**——原型多样性不是目标，页面充实才是。
- 页面内容以 3-4 个关键数字/指标为主 → `03e` KPI 指标。
- 内容是时间顺序、阶段推进、里程碑 → `03f` 时间线。
- 内容是两个对象的对照（方案/前后/竞品）→ `03g` 对比。
- 内容是三个并列要点/模块/方案 → `03h` 三卡片。
- 一张主图表配文字洞察 → `03i` 数据结论；图表+明细表上下结构 → `03d` 数据。
- **填充下限（硬性）**：选用任何原型后，大面积内容区（`{{CONTENT_AREA}}`、`{{INSIGHTS}}`、
  `{{CARD_x}}`、`{{OPTION_x_CONTENT}}` 等）必须完整构图——多条要点、每条含图标/序号 +
  加粗小标题 + 说明文字，或图表/表格等实体内容；**禁止在大面板中只放一行文字交卷**，
  禁止出现无文字配套的孤立装饰元素（圆点/图标）。内容不足以填满该区时，换用更紧凑的
  原型、回退 `03a`/`03b` 自由构图，或与相邻页合并。
- 宿主任务可显式声明「品牌壳固定、版面自由」模式（company_free）：此时正文页**不套 03x 原型**，
  仅 §I-b 页眉契约、保密页脚、§II 色板与 §III 字体为强制，内容区每页自由构图
  （可参考 `templates/layouts/presentation_core` 的 20 种结构），本节选型指引不适用。

## VI. Assets

| File | Usage |
|---|---|
| `images/mt_cover_brand.png` | 从公司模板提取的封面品牌图，用于封面底部 |
| `images/mt_header_logo.png` | 从公司原件页眉提取的品牌字标（白字透明底），目录页与正文页页眉引用 |
| `icons/official/*.png` | 公司原件图标页提取的 84 个官方图标（深蓝主版本 + 浅蓝弱强调版本），语义索引见 `icons/official/INDEX.md` |

**图标使用纪律**：正文页需要图标时**优先从 `icons/official/` 选用**（先读 INDEX.md 按语义挑选，
必要时可 view_image 确认图形），官方集确无对应语义时才允许使用引擎通用图标库；同一页内
深浅版本不混用于同级要点。

### VI-b. AI 配图纪律（启用 AI 配图的任务强制）

- **渲染画风锁定 `vector-illustration`**（干净扁平矢量插画）：写图片提示词前必须读
  `references/image-renderings/vector-illustration.md`，按其风格段落与 fewshot 片段组装提示词；
  本模板任务不现场提案、不自创画风，`deck_rendering` 记 `vector-illustration`。
- **颜色继承 §II 色板角色**：Primary `#004696` 主形体与轮廓，Bright blue `#3399FF` 次级信息层，
  Light blue `#97CBFF` 大面积浅底，Green `#56A410` 仅单点正向强调，Orange `#E69400` 仅单点
  风险/待办强调；白底，不引入色板外颜色。
- **主体必须是可辨认的实体场景**：人物协作、设备与工位、实物与流程隐喻、概念场景等。
  **禁止以"软件界面 / 仪表盘 / 报表截图"为图片主体**——`text_policy: none` 之下这类主体必然
  退化为空线框方块。界面、数据看板、图表一律用原生 SVG 绘制（`03d`/`03e`/`03i` 原型或自由构图），
  不交给生图。
- 结构类图示（流程/框架/漏斗等）默认原生 SVG；确需 AI 生成时按
  `references/image-type-templates/_index.md` 选型并读对应模板文件后再写提示词。
- 默认 `text_policy: none`（图内不得出现任何文字）；插画置入媒体区沿用 §IV「矩形干净边缘」。

## VII. Placeholder Overrides

- 封面增加 `{{LOCATION}}`，以分别承载地点、日期和作者。
- 双栏、图文和数据版式分别使用 `{{SUPPORTING_AREA}}`、`{{IMAGE_AREA}}`、`{{DATA_VISUAL}}` 与 `{{DATA_DETAIL}}` 描述其真实结构。
