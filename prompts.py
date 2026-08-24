"""构建交给无头 agent 的一次性委托提示词。"""
import json
from pathlib import Path

import config

# 产品现在只提供一套最终 MT 模板。旧任务/旧客户端的 style 值只在边界处
# 归一化，不能再让它们重新暴露成可选风格。
FINAL_STYLE = "company_free"
FINAL_TEMPLATE_ID = "__company__"
FINAL_TEMPLATE_LABEL = "MT 公司最终模板"
_LEGACY_STYLE_ALIASES = {
    "company": FINAL_STYLE,
    "company_free": FINAL_STYLE,
    "mt_corporate_blue": FINAL_STYLE,
    "swiss": FINAL_STYLE,
    "dark": FINAL_STYLE,
    "custom": FINAL_STYLE,
}
_LEGACY_TEMPLATE_ALIASES = {
    "__company__": FINAL_STYLE,
    "__company_free__": FINAL_STYLE,
}


def normalize_style(value: str | None, *, default: str = FINAL_STYLE) -> str | None:
    """把历史 style 归一化为唯一模板；未知值返回 None。"""
    raw = str(value or "").strip()
    if not raw:
        return default
    return _LEGACY_STYLE_ALIASES.get(raw)


def style_for_template_id(value: str | None) -> str | None:
    """把历史模板卡 ID 归一化；示例画廊不再是模板入口。"""
    raw = str(value or "").strip()
    if not raw:
        return FINAL_STYLE
    return _LEGACY_TEMPLATE_ALIASES.get(raw)

# mock 模式下「AI 推荐风格」返回的演示数据(真实模式由 agent 根据材料定制)。
MOCK_RECOMMENDATIONS = [
    {
        "name": FINAL_TEMPLATE_LABEL,
        "description": "MT 原生封面与目录；第 3 页起固定公司页眉、Logo、页码和页脚，正文由 AI 自由排版。",
    },
]

# mock 模式下「规划确认」返回的演示数据(真实模式由 agent 根据材料定制)。
MOCK_PLAN = {
    "styles": MOCK_RECOMMENDATIONS,
    "pages": 10,
    "outline": [
        {"title": "封面", "points": ["报价评审:LEHY-III-S 与 LEHY-Pro 对比", "面向管理层的决策材料"]},
        {"title": "目录", "points": ["自动生成,与正文页一一对应"]},
        {"title": "评审范围说明", "points": ["报价书来源与版本", "对比口径与前提假设"]},
        {"title": "产品定位对比", "points": ["两款机型的目标场景", "配置档次与价格区间"]},
        {"title": "核心规格适配", "points": ["载重/速度/行程覆盖", "土建条件适配性"]},
        {"title": "安全体系", "points": ["安全等级与冗余设计", "关键安全配置差异"]},
        {"title": "智能与舒适配置", "points": ["群控与目的层派梯", "轿厢体验差异"]},
        {"title": "节能表现", "points": ["能耗等级对比", "再生制动与待机策略"]},
        {"title": "商务条款核验", "points": ["价格构成与税费", "交期与质保边界"]},
        {"title": "结论与建议", "points": ["选型建议", "需正式报价书确认的事项"]},
    ],
    "notes": "演示数据:配好 API key 后会按你的材料定制大纲。",
}


# 上传表单里的唯一模板选项。key 与任务内部 style 一致。
STYLES = {
    FINAL_STYLE: {
        "label": FINAL_TEMPLATE_LABEL,
        "brief": (
            "只使用 MT 公司最终模板工作区 {template}，不要选择或创建其他模板。"
            "第 1 页使用公司原生封面，第 2 页使用公司原生目录；第 3 页起固定公司品牌壳，"
            "包括蓝渐变页眉、白色标题、Logo、分隔线、不补零页码和保密页脚，正文区域自由设计。"
            "本任务是品牌壳引用,不是结构化模板复刻:spec_lock.md 的 pptx_structure "
            "必须使用 mode: flat 和 template_reuse_scope: style,禁止写 structured 或 page_layouts;"
            "SVG 中也不得写 data-pptx-master/layout/layer 等 Master/Layout 结构元数据;"
            "【颜色】配色锚点锁定为模板 design_spec §II 九色板,不引入其他色相;"
            "【字体】字体字号按 §III(英文数字 Arial、中文黑体);"
            "【页眉页脚】每页完整复刻 §I-b 页眉契约(蓝渐变横条、白色页标题、"
            "images/mt_header_logo.png 品牌字标、白色分隔线、不补零页码)与保密页脚。"
            "【自由构图】页眉以下、页脚以上的内容区不强制套用 03x 正文原型、不受固定槽位限制,"
            "按材料内容现场构图;可从 templates/layouts/presentation_core 的 20 种结构取意,"
            "也可像无模板任务一样自创版式,模板 §V 原型只作参考而非硬约束。"
            "【质量下限】每页必须有明确的核心结论、信息层级和充实的视觉主体,"
            "禁止只摆少量文本框或在大面积面板中放一两行文字;根据内容优先使用图表、表格、"
            "流程图、时间线、关系图、信息图或图片形成主要视觉,内容不足时改用更紧凑的构图。"
            "鼓励整版数据视觉、大数字、非对称构图等更有表现力的版面,绿色结论行可用可不用;"
            "图标优先使用 icons/official/ 官方图标集(见其 INDEX.md),官方集没有对应语义时再使用通用图标。"
            "视觉表达可以自由变化,但整体保持公司模板「平直、几何、克制、专业」的气质,"
            "不使用卡通、3D 或过度装饰。"
        ),
    },
}


def _material_block(upload_dir: Path, files: list[str], topic: str) -> str:
    if files:
        material = (
            f"源材料在目录 {upload_dir} 下,共 {len(files)} 个文件:"
            + "、".join(files)
            + "。请把它们作为生成的源材料导入。"
        )
    else:
        material = "本次没有上传文件。"
    if topic:
        material += f"\n主题/额外说明:{topic}"
    return material


def build_recommend_prompt(*, upload_dir: Path, files: list[str], topic: str) -> str:
    """兼容旧接口:确认唯一 MT 模板，不再推荐多个风格。"""
    return f"""请快速完成一个轻量任务,不要生成 PPT,不要初始化项目,不要进入 ppt-master 的生成流程。

【材料】
{_material_block(upload_dir, files, topic)}

【任务】
快速浏览材料(转换或抽样阅读即可),确认唯一的「{FINAL_TEMPLATE_LABEL}」适合本次内容。
不要提出其他风格、其他模板或自由设计方案。

【输出格式】
最终回复只输出一个 JSON 数组,不要其他文字，只能包含一个模板:
[{{"name": "{FINAL_TEMPLATE_LABEL}", "description": "MT 原生封面与目录；第 3 页起固定公司品牌壳，正文自由排版"}}]
"""


def build_plan_prompt(*, upload_dir: Path, files: list[str], topic: str, pages: str, note: str,
                      current_plan: dict | None = None, feedback: str = "") -> str:
    """轻量任务:读材料 → 一次产出风格建议 + 页数 + 每页内容大纲,JSON 输出。不生成任何页面。

    首轮 current_plan 为空;重规划轮把上一版方案与用户意见一并给出,要求增量调整。
    """
    page_req = "页数由你根据内容决定(通常 8-12 页,含封面与目录)。" if pages == "auto" else f"目标页数约 {pages} 页(含封面与目录)。"
    note_block = f"\n用户补充要求:{note}" if note else ""

    if current_plan and feedback:
        revision_block = f"""
【上一版方案】
{json.dumps(current_plan, ensure_ascii=False, indent=1)}

【用户调整意见】
{feedback}

【任务】
按用户意见调整上一版方案:意见没有涉及的页面尽量保持原样(标题与要点原文保留),只改动意见涉及的部分;需要增删页时同步调整目录页要点与总页数。styles 必须保持唯一的 MT 公司最终模板。"""
    else:
        revision_block = f"""
【任务】
1. 快速浏览材料(转换或抽样阅读即可),判断内容气质、受众与信息结构。
2. 模板固定为「{FINAL_TEMPLATE_LABEL}」，不要推荐其他视觉方向。
3. 规划每一页的内容分布:{page_req}第 1 页固定为封面、第 2 页固定为目录;每页给出页标题和 2-4 条内容要点(要点要具体到会放什么信息,不要写「介绍一下」这类空话)。"""

    return f"""请快速完成一个轻量规划任务,不要生成 PPT 页面,不要初始化项目,不要进入生成流程。

【材料】
{_material_block(upload_dir, files, topic)}{note_block}
{revision_block}

【输出格式】
最终回复只输出一个 JSON 对象,不要其他文字,形如:
{{"styles": [{{"name": "{FINAL_TEMPLATE_LABEL}", "description": "MT 原生封面与目录；第 3 页起固定公司品牌壳，正文自由排版"}}],
 "pages": 总页数,
 "outline": [{{"title": "页标题", "points": ["要点1", "要点2"]}}, ...每页一项,与 pages 数量一致],
 "notes": "一句话说明规划思路(可选)"}}
"""


def build_prompt(*, style: str, pages: str, note: str, upload_dir: Path, files: list[str], topic: str,
                 style_brief: str = "", outline: list[dict] | None = None,
                 ai_images: bool = False) -> str:
    # style_brief 仅为历史任务字段；产品现在始终执行唯一 MT 模板，
    # 防止旧示例/旧推荐把生成重新带回其他风格。
    canonical_style = normalize_style(style) or FINAL_STYLE
    style_text = STYLES[canonical_style]["brief"].format(template=config.COMPANY_TEMPLATE)

    material = _material_block(upload_dir, files, topic)
    if outline:
        page_req = f"页数与每页内容严格按下方已确认大纲执行(共 {len(outline)} 页)。"
        outline_lines = "\n".join(
            f"{i + 1}. {p.get('title', '')}:{';'.join(p.get('points', []))}"
            for i, p in enumerate(outline)
        )
        outline_block = (
            "\n【已确认大纲】\n"
            "用户已在生成前逐页确认以下内容分布,请将其作为最终页面计划(final page plan)对待:"
            "页数、页序和每页主题是权威约束,写入 design_spec 的页面 roster,不得增删、合并、拆分或重排页面;"
            "每页要点是该页必须覆盖的内容,可在此基础上按源材料补充细节与数据。\n"
            f"{outline_lines}\n"
        )
    else:
        page_req = "页数由你根据内容决定(通常 8–12 页)。" if pages == "auto" else f"目标页数约 {pages} 页。"
        outline_block = ""
    note_block = f"\n补充要求:{note}" if note else ""
    if ai_images:
        image_line = (
            "- 已启用 AI 配图:本机已配置 IMAGE_BACKEND(openai 兼容),按 image-generator.md 执行,"
            "design_spec §I 的 AI Image Acquisition Path 记为 api。本机未随附 image_search.py 与 slice_images.py,"
            "资源行不得使用 Acquire Via: web/slice;ai/user/formula/placeholder 照文档正常使用。"
            "写提示词前先读 references/image-renderings/_index.md 为整套 deck 锁定一种渲染画风"
            "(公司蓝模板固定 vector-illustration,见模板 design_spec §VI-b),再按所选画风文件的风格段落组装提示词。"
            "图片主体必须是可辨认的实体场景/概念插画;禁止无文字的软件界面/仪表盘/报表截图式主体"
            "(text_policy: none 下必然退化为空线框方块),界面与数据看板一律原生 SVG 绘制。"
            "AI 配图控制在 3-6 张,优先用于封面/章节页/概念场景图;先写 images/image_prompts.json,"
            "跑 image_gen.py --render-md,再跑 image_gen.py --manifest(timeout_seconds 传 900)。"
            "仍为 Failed 的行重跑一次 --manifest;再失败就把该行 status 改为 Needs-Manual(api 路径不换供应商),"
            "该页改用纯矢量替代不得留空缺,并在结束报告中列出这些行与失败原因。"
        )
    else:
        image_line = "- 本机未配置 AI 图像生成后端,不要调用 image_gen.py;配图一律使用源材料自带的图片或纯矢量设计。"

    return f"""请用本仓库的 ppt-master 技能生成一份 PPTX。本次运行完全无人值守,请一次性跑完全部流程,中途不要向我提问、不要等待任何确认。

【源材料】
{material}

【风格】
{style_text}
{outline_block}
【要求】
- {page_req}
- 输出语言与源材料一致(默认中文)。
- 三阶段策略确认按「显式委托」处理:由你代为决策,在聊天输出中留下完整的三阶段摘要即可;不要启动 confirm_ui 服务器,不要执行任何 --wait-only 等待。
{image_line}
- 必须完成 Step 7 导出与最终质量检查,确保项目 exports/ 目录下存在最终 PPTX 文件。
- 结束时用一段话报告:项目目录、最终 PPTX 的完整路径、页数与核心结论。{note_block}
"""
