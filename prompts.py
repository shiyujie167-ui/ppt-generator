"""构建交给无头 agent 的一次性委托提示词。"""
import json
from pathlib import Path

import config

# mock 模式下「AI 推荐风格」返回的演示数据(真实模式由 agent 根据材料定制)。
MOCK_RECOMMENDATIONS = [
    {"name": "公司蓝模板", "description": "沿用公司 mt_corporate_blue 模板:蓝白主调、原生封面与目录。正式汇报的默认安全牌。(演示数据,配 key 后按你的材料定制推荐)"},
    {"name": "瑞士极简", "description": "白底大留白、克制的公司蓝点缀,信息密度低、阅读负担小,适合管理层快速过目。"},
    {"name": "数据仪表盘", "description": "深浅分区 + 大数字卡片 + 图表优先,适合报价对比、费用构成这类数字密集的材料。"},
    {"name": "深色高对比", "description": "深蓝黑背景、亮色强调,投影环境显眼,适合会议室大屏演示场景。"},
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


# 上传表单里的风格选项。key 与前端 <select> 的 value 一致。
STYLES = {
    "company": {
        "label": "公司蓝模板(上午同款,推荐)",
        "brief": (
            "使用模板工作区 {template} 生成(显式模板工作区根目录,走 Generate 路线 Step 3)。"
            "封面、目录与内容页均沿用该公司模板的版式与配色。"
            "成品的第 1、2 页最终由系统替换为公司模板原稿的原生封面与目录页。"
        ),
    },
    "company_free": {
        "label": "公司蓝 · 自由版(色板/字体/页眉固定,其余全自由)",
        "brief": (
            "使用模板工作区 {template} 生成(显式模板工作区根目录,走 Generate 路线 Step 3)。"
            "成品的第 1、2 页最终由系统替换为公司模板原稿的原生封面与目录页。"
            "正文页(第 3 页起)只有三项硬约束——"
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
    "swiss": {
        "label": "瑞士极简(白底,自由设计)",
        "brief": "不使用模板工作区,自由设计,视觉风格 swiss-minimal:白底、大量留白、公司蓝 #004696 作为主色。",
    },
    "dark": {
        "label": "深色商务(自由设计)",
        "brief": "不使用模板工作区,自由设计,深色商务风格:深蓝黑背景、亮色强调、适合投影汇报。",
    },
    "custom": {
        "label": "自定义(在补充要求里描述)",
        "brief": "不使用模板工作区,风格按下方「补充要求」中的描述执行;若描述不足,按稳重的商务风格补全。",
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
    """轻量任务:读材料 → 推荐若干风格方向,JSON 输出。不生成任何页面。"""
    return f"""请快速完成一个轻量任务,不要生成 PPT,不要初始化项目,不要进入 ppt-master 的生成流程。

【材料】
{_material_block(upload_dir, files, topic)}

【任务】
快速浏览材料(转换或抽样阅读即可),判断内容气质与受众,然后提出 4 个适合这份材料的 PPT 视觉风格方向。其中第 1 个固定为「公司蓝模板(沿用 {config.COMPANY_TEMPLATE} 模板工作区)」,其余 3 个自由发挥(配色、版式气质、适用理由都要具体到这份材料,不要泛泛而谈)。

【输出格式】
最终回复只输出一个 JSON 数组,不要其他文字,形如:
[{{"name": "风格名(8字内)", "description": "两三句话:配色/版式/为什么适合这份材料"}}, ...]
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
按用户意见调整上一版方案:意见没有涉及的页面尽量保持原样(标题与要点原文保留),只改动意见涉及的部分;需要增删页时同步调整目录页要点与总页数。styles 保持上一版不变。"""
    else:
        revision_block = f"""
【任务】
1. 快速浏览材料(转换或抽样阅读即可),判断内容气质、受众与信息结构。
2. 提出 4 个适合这份材料的视觉风格方向,第 1 个固定为「公司蓝模板(沿用 {config.COMPANY_TEMPLATE} 模板工作区)」,其余 3 个自由发挥(配色、版式气质、适用理由要具体到这份材料)。
3. 规划每一页的内容分布:{page_req}第 1 页固定为封面、第 2 页固定为目录;每页给出页标题和 2-4 条内容要点(要点要具体到会放什么信息,不要写「介绍一下」这类空话)。"""

    return f"""请快速完成一个轻量规划任务,不要生成 PPT 页面,不要初始化项目,不要进入生成流程。

【材料】
{_material_block(upload_dir, files, topic)}{note_block}
{revision_block}

【输出格式】
最终回复只输出一个 JSON 对象,不要其他文字,形如:
{{"styles": [{{"name": "风格名(8字内)", "description": "两三句话:配色/版式/为什么适合"}}, ...共4项],
 "pages": 总页数,
 "outline": [{{"title": "页标题", "points": ["要点1", "要点2"]}}, ...每页一项,与 pages 数量一致],
 "notes": "一句话说明规划思路(可选)"}}
"""


def build_prompt(*, style: str, pages: str, note: str, upload_dir: Path, files: list[str], topic: str,
                 style_brief: str = "", outline: list[dict] | None = None,
                 ai_images: bool = False) -> str:
    if style_brief:
        style_text = (
            f"采用用户选定的风格:{style_brief}\n"
            f"若该风格提到公司蓝模板,使用模板工作区 {config.COMPANY_TEMPLATE}(显式模板工作区根目录,走 Generate 路线 Step 3);否则自由设计并严格贴合上述描述。"
        )
    else:
        style_conf = STYLES.get(style, STYLES["company"])
        style_text = style_conf["brief"].format(template=config.COMPANY_TEMPLATE)

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
