"""Standalone Responses API agent for PPT Master.

The model decides the PPT workflow through function calls. This module owns the
local tool loop and constrains mutations to the current Web job's project.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable

import certifi

import config
import jobs
import prompts

LogFn = Callable[[str, str], None]

MAX_FILE_CHARS = 1_500_000
MAX_READ_CHARS = 120_000
MAX_COMMAND_OUTPUT_CHARS = 160_000

# view_image:可查看的格式、缩图阈值与上下文中保留的图片数
VIEW_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIEW_DOWNSCALE_BYTES = 1_200_000   # 超过则用引擎 Pillow 缩成 ≤1568px JPEG
VIEW_RAW_MAX_BYTES = 2_000_000     # 缩图不可用时的原图直传上限
VIEW_KEEP_IMAGES = 2               # 对话里保留最近 N 张图,更早的替换为占位文本
VIEW_IMAGE_MAX = int(os.environ.get("PPT_VIEW_IMAGE_MAX", "12"))  # 全任务看图次数上限(控 token)

COMPANY_COVER_IMAGE_NAME = "company_cover_hero.png"
COMPANY_COVER_IMAGE_RELATIVE = f"images/{COMPANY_COVER_IMAGE_NAME}"

# Web 生成流程允许调用的引擎脚本(内置 engine/ 只随附这一闭包)。
ALLOWED_SCRIPTS = {
    "source_to_md.py",
    "project_manager.py",
    "analyze_images.py",
    "svg_quality_checker.py",
    "total_md_split.py",
    "finalize_svg.py",
    "svg_to_pptx.py",
    "preset_shape_svg.py",
    "shape_boolean_svg.py",
    "image_gen.py",  # 仅 ai_images 任务放行(tool_run_ppt_script 内二次把关)
}

AGENT_INSTRUCTIONS = """你是 PPT Generator 的独立后端 Agent，通过 Responses API 运行，不依赖 Codex CLI、Claude CLI 或任何桌面会话。

执行纪律:
1. 工作目录是 PPT Master 仓库。开始 PPT 生成前，必须依次完整阅读 AGENTS.md、skills/ppt-master/SKILL.md、路由文件和所选 Generate PPTX 路由；再按路由的条件触发读取相应文件。
2. 严格遵守仓库文档。当前 Web 请求已经明确把本次三阶段 Strategist 确认委托给你，所以按 Generate 路由的 explicit delegation 分支生成一份完整三阶段摘要，不启动 confirm_ui，不等待用户。
3. 你是本次运行的唯一主 Agent。SVG 必须由你逐页直接创作，禁止用脚本批量生成 svg_output，也禁止虚构检查或导出成功。
4. 只能修改本任务自己的 projects/web_<job-id>... 工作区。不得修改 skills/、templates 库、AGENTS.md 或其他已有项目。
5. 先读取再行动；每个脚本一次只运行一个命令。所有质量检查必须处理完整输出。最终只有在 Step 7 成功、postflight 合格且 exports 中确有新 PPTX 时才宣布完成。
{image_rule}
7. 不要索取用户确认或额外输入。遇到可恢复错误时根据 owning source 修复并继续；只有材料确实缺失导致无法完成时才停止并清楚说明。
8. 套用模板原型时保留固定公司边框、页眉、页脚和品牌元素，成品不得残留可见的样例文字、空编号圆点或多余连接线。结构化 strict/layout 页面不得删除或改写固定 Layout 原子；优先选择槽位数量匹配的原型，确实没有兼容原型时，用带 data-pptx-bounds 的 Slide-local 顶层逻辑组遮住未使用的固定标记，同时保留原占位拓扑。
9. 不要启动任何常驻或后台服务（包括 svg_editor 的 --live/--daemon 实时预览）。Web 无人值守运行不需要它们，遗留进程会占用端口影响后续任务。
"""

IMAGE_GEN_OFF_RULE = "6. 不调用 image_gen.py。没有可用图片时采用纯矢量页面；不得把缺图伪装成已完成。"
IMAGE_GEN_ON_RULE = (
    "6. 本任务已启用 AI 配图（IMAGE_BACKEND 已配置，Path A=api）。需要 AI 生成的配图按 "
    "image-generator.md 的清单契约执行：先写 images/image_prompts.json，跑 image_gen.py --render-md "
    "渲染 sidecar，再跑 image_gen.py --manifest 生成（run_ppt_script 传 timeout_seconds=900）；"
    "design_spec §I 的 AI Image Acquisition Path 记为 api。本机未随附 image_search.py 与 slice_images.py，"
    "资源行不得使用 Acquire Via: web/slice；ai/user/formula/placeholder 照文档正常使用。"
    "写提示词前先读 references/image-renderings/_index.md 锁定整套 deck 的渲染画风（公司蓝模板固定 "
    "vector-illustration，见模板 design_spec §VI-b）；图片主体必须是可辨认的实体场景/概念插画，"
    "禁止无文字的软件界面/仪表盘/报表截图式主体，界面与数据看板一律原生 SVG 绘制。"
    "仍为 Failed 的行重跑一次 --manifest，再失败就把该行 status 改为 Needs-Manual（api 路径不换供应商）并改用纯矢量替代。"
    "成品只引用状态为 Generated 的图片；不得把缺图伪装成已完成。"
)

# 按模型 vision 能力二选一追加(对应 image-searcher.md 的 Multimodal / without-vision 两分支)
VISION_INSTRUCTIONS = """10. 你具备视觉能力（image-searcher.md 所述 Multimodal 分支）：材料里有图片时，在撰写 design_spec §VIII 图片安置行之前，用 view_image 逐张查看候选图片，确认主体内容、朝向、构图重心与裁切安全后再决定用在哪一页；禁止凭文件名臆断图片内容，禁止在成品中引用从未查看过的图片。查看结论直接用于决策，不据此向用户索取确认。全任务 view_image 有次数上限（默认 12 次）：PDF 正文已转成 Markdown，不要为核对文字逐页看图，只看将要放进成品或需要判断构图裁切的图片。
"""
NO_VISION_INSTRUCTIONS = """10. 当前模型不具备图片输入能力（image-searcher.md 所述 without-vision 分支）：不要尝试查看图片内容，只依据文件名、材料上下文位置与 analyze_images 的客观参数（尺寸/比例/透明度）决定图片安置，无法判断内容的图片宁可不用，不得臆造其内容。
"""

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "read_text_file",
        "description": (
            "Read a UTF-8 text file from the PPT Master repository or this job's upload directory. "
            "Use start_line/max_lines to continue large files until EOF."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path or path relative to the PPT Master repo."},
                "start_line": {"type": "integer", "description": "1-based first line; default 1."},
                "max_lines": {"type": "integer", "description": "Maximum lines to return; default 400."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "list_files",
        "description": "List files under an allowed directory without changing anything.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path, absolute or repo-relative."},
                "pattern": {"type": "string", "description": "Glob pattern such as *.md or **/*.svg."},
                "max_entries": {"type": "integer", "description": "Maximum entries; default 300."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "write_text_file",
        "description": (
            "Create or replace one UTF-8 text artifact inside this job's projects/web_<job-id>... directory. "
            "Use this to hand-author design_spec.md, spec_lock.md, SVG pages, JSON, CSV, and notes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "replace_text_file",
        "description": "Replace an exact text fragment in one text file owned by this job.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
                "count": {"type": "integer", "description": "Expected replacement count; default 1."},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "install_template_workspace",
        "description": (
            "Install one explicit template workspace into this job's initialized project. "
            "It performs a complete collision preflight, then copies only templates/, images/, and icons/; "
            "exports/ is ignored. Read and validate the workspace contract before calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_root": {"type": "string"},
                "project_path": {"type": "string"},
            },
            "required": ["source_root", "project_path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "view_image",
        "description": (
            "View one image (png/jpg/jpeg/webp/gif) from the repo or this job's upload directory. "
            "The image is attached to the conversation as the next user message so you can see its "
            "actual content: subject, orientation, focal region, crop safety. View each candidate "
            "material image before authoring design_spec §VIII placement rows. One image per call; "
            "only the most recent viewed images stay in context, re-view if needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Image path, absolute or repo-relative."},
                "purpose": {"type": "string", "description": "Why you are viewing it (logged)."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "run_ppt_script",
        "description": (
            "Run one documented Python script under skills/ppt-master/scripts with argv (no shell). "
            "Only these scripts are available: source_to_md.py, project_manager.py, analyze_images.py, "
            "svg_quality_checker.py, total_md_split.py, finalize_svg.py, svg_to_pptx.py, "
            "preset_shape_svg.py, shape_boolean_svg.py and image_gen.py "
            "(the last one only when this job enables AI images). "
            "Commands that mutate a project are restricted to this job's web_<job-id> project. "
            "Run quality and export commands serially and inspect the complete returned output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": "Path relative to skills/ppt-master/scripts, e.g. project_manager.py.",
                },
                "args": {"type": "array", "items": {"type": "string"}},
                "timeout_seconds": {"type": "integer", "description": "Per-command timeout, 1-900; default 300."},
            },
            "required": ["script", "args"],
            "additionalProperties": False,
        },
    },
]


class AgentError(RuntimeError):
    """User-safe execution failure."""


# ── Chat Completions 协议适配(DeepSeek 官方等 OpenAI 兼容端点)─────────────
# 内部统一使用 Responses 消息格式;wire=chat 的模型在收发时做双向翻译。

CHAT_TRIM_THRESHOLD = 100_000  # chat 协议无服务端 compaction,超过即截断早期工具输出/调用参数
CHAT_TRIM_KEEP = 10            # 最近 N 条工具输出不截断
CHAT_TRIM_KEEP_ARGS = 3        # 最近 N 条调用参数不截断(写页内容大,文件在磁盘可重读,窗口给小)


def _chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {
            "name": t.get("name", ""),
            "description": t.get("description", ""),
            "parameters": t.get("parameters") or {},
        }}
        for t in tools
    ]


def _chat_messages(instructions: str, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Responses 输入条目 → chat messages。连续的 message/function_call 合并为一条 assistant。"""
    msgs: list[dict[str, Any]] = [{"role": "system", "content": instructions}]
    pending: dict[str, Any] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            msgs.append(pending)
            pending = None

    for item in history:
        kind = item.get("type")
        if kind is None and item.get("role"):
            flush()
            content = item.get("content", "")
            if isinstance(content, list):
                parts = []
                for p in content:
                    ptype = p.get("type")
                    if ptype in ("input_text", "text"):
                        parts.append({"type": "text", "text": p.get("text", "")})
                    elif ptype == "input_image":
                        parts.append({"type": "image_url", "image_url": {"url": p.get("image_url", "")}})
                msgs.append({"role": item["role"], "content": parts})
            else:
                msgs.append({"role": item["role"], "content": content})
        elif kind == "message":
            text = "".join(
                c.get("text", "") for c in item.get("content", [])
                if c.get("type") in ("output_text", "text")
            )
            if item.get("role", "assistant") == "assistant":
                flush()
                pending = {"role": "assistant", "content": text or None}
            else:
                flush()
                msgs.append({"role": item.get("role", "user"), "content": text})
        elif kind == "function_call":
            if pending is None:
                pending = {"role": "assistant", "content": None}
            pending.setdefault("tool_calls", []).append({
                "id": item.get("call_id") or "",
                "type": "function",
                "function": {"name": item.get("name", ""), "arguments": item.get("arguments") or "{}"},
            })
        elif kind == "function_call_output":
            flush()
            msgs.append({"role": "tool", "tool_call_id": item.get("call_id") or "",
                         "content": item.get("output") or ""})
        # reasoning / compaction 等条目对 chat 协议无意义,跳过
    flush()
    return msgs


def _chat_payload(payload: dict[str, Any], max_tokens_cap: int) -> dict[str, Any]:
    return {
        "model": payload["model"],
        "messages": _chat_messages(payload.get("instructions", ""), payload.get("input") or []),
        "tools": _chat_tools(payload.get("tools") or []),
        "parallel_tool_calls": payload.get("parallel_tool_calls", False),
        "max_tokens": min(int(payload.get("max_output_tokens") or max_tokens_cap), max_tokens_cap),
    }


def _from_chat_response(data: dict[str, Any]) -> dict[str, Any]:
    """chat completion → Responses 形状,供统一的 run() 循环消费。"""
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output: list[dict[str, Any]] = []
    if message.get("content"):
        output.append({"type": "message", "role": "assistant",
                       "content": [{"type": "output_text", "text": message["content"]}]})
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({"type": "function_call", "name": fn.get("name", ""),
                       "arguments": fn.get("arguments") or "{}", "call_id": tc.get("id") or ""})
    usage = data.get("usage") or {}
    cached = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
              or usage.get("prompt_cache_hit_tokens") or 0)
    finish = choice.get("finish_reason")
    has_calls = any(i.get("type") == "function_call" for i in output)
    return {
        "output": output,
        "status": "incomplete" if (finish == "length" and not has_calls) else "completed",
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "input_tokens_details": {"cached_tokens": cached},
        },
    }


def _is_image_message(item: dict[str, Any]) -> bool:
    content = item.get("content")
    return (item.get("role") == "user" and isinstance(content, list)
            and any(p.get("type") == "input_image" for p in content))


def _approx_item_len(item: dict[str, Any]) -> int:
    """上下文体量估算:图片按固定小额计,避免 base64 字符数冲爆截断阈值。"""
    if _is_image_message(item):
        return 2_000
    return len(str(item.get("output") or item.get("content") or item.get("arguments") or ""))


def _trim_call_arguments(item: dict[str, Any]) -> bool:
    """截断早期 function_call 的大参数(写整页 SVG 的 content 等),保持 arguments 合法 JSON。

    文件内容以磁盘为准,模型如需回看可 read_text_file;只截参数里的大字符串,
    保留 path 等小字段,避免破坏模型对既往操作的记忆。
    """
    raw = item.get("arguments") or ""
    if len(raw) <= 800:
        return False
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = None
    if not isinstance(args, dict):
        item["arguments"] = json.dumps(
            {"_truncated": f"早期工具参数已截断(原 {len(raw)} 字符)"}, ensure_ascii=False)
        return True
    changed = False
    for key, value in args.items():
        if isinstance(value, str) and len(value) > 400:
            args[key] = value[:200] + f"…[已截断,原 {len(value)} 字符;文件在磁盘上,可 read_text_file 重看]"
            changed = True
    if changed:
        item["arguments"] = json.dumps(args, ensure_ascii=False)
    return changed


class _RetryableAPIError(RuntimeError):
    """Transient API failure worth retrying (5xx/429/network/timeout)."""


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _trim_command_output(text: str) -> str:
    if len(text) <= MAX_COMMAND_OUTPUT_CHARS:
        return text
    half = MAX_COMMAND_OUTPUT_CHARS // 2
    omitted = len(text) - MAX_COMMAND_OUTPUT_CHARS
    return f"{text[:half]}\n... [{omitted} chars omitted] ...\n{text[-half:]}"


class ToolBox:
    def __init__(self, job: jobs.Job, log: LogFn, deadline: float):
        self.job = job
        self.log = log
        self.deadline = deadline
        self.repo = config.PPT_MASTER_REPO.resolve()
        self.projects = (self.repo / "projects").resolve()
        self.upload = (config.UPLOADS_DIR / (job.upload_id or job.id)).resolve()
        self.project_prefix = f"web_{job.id}"
        resume_name = str(getattr(job, "resume_project", "") or "")
        self.resume_project = (self.projects / resume_name).resolve() if resume_name else None
        self.scripts = (self.repo / "skills/ppt-master/scripts").resolve()
        self.pending_image: dict[str, str] | None = None  # view_image 待注入对话的图片
        self.viewed_images = 0  # 已查看图片次数(VIEW_IMAGE_MAX 上限)

    def _remaining(self) -> int:
        seconds = int(self.deadline - time.monotonic())
        if seconds <= 0:
            raise AgentError("任务超过总超时时间")
        return seconds

    def _resolve_read(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.repo / path
        path = path.resolve()
        if _within(path, self.projects) and not self._owned_project_arg(str(path)):
            raise AgentError(f"拒绝读取其他任务的项目:{raw}")
        if not (
            _within(path, self.repo)
            or _within(path, self.upload)
            or self._owned_project_arg(str(path))
        ):
            raise AgentError(f"拒绝读取允许范围外的路径:{raw}")
        return path

    def _resolve_owned(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.repo / path
        path = path.resolve()
        try:
            relative = path.relative_to(self.projects)
        except ValueError as exc:
            raise AgentError(f"拒绝写入 projects 之外的路径:{raw}") from exc
        if not relative.parts or not relative.parts[0].startswith(self.project_prefix):
            raise AgentError(f"拒绝写入其他任务的项目:{raw}")
        if self.resume_project is not None and not _within(path, self.resume_project):
            raise AgentError("续跑只能写入已绑定的检查点项目")
        return path

    def _owned_project_arg(self, raw: str) -> bool:
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.repo / path
            path = path.resolve()
            relative = path.relative_to(self.projects)
        except (ValueError, OSError):
            return False
        if not relative.parts or not relative.parts[0].startswith(self.project_prefix):
            return False
        if self.resume_project is not None:
            return _within(path, self.resume_project)
        return True

    def _owned_project_root_from_arg(self, raw: str) -> Path | None:
        """Return the owning project root for a project, subdir, or file arg."""
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = self.repo / path
            relative = path.resolve().relative_to(self.projects)
        except (ValueError, OSError):
            return None
        if not relative.parts or not relative.parts[0].startswith(self.project_prefix):
            return None
        return (self.projects / relative.parts[0]).resolve()

    def _validate_script_args(self, script_name: str, args: list[str]) -> None:
        for arg in args:
            if "\x00" in arg or ".." in Path(arg).parts:
                raise AgentError(f"脚本参数包含非法路径:{arg}")
            if arg.startswith("projects/") and not self._owned_project_arg(arg):
                raise AgentError(f"脚本不得操作其他项目:{arg}")
            if Path(arg).is_absolute():
                path = Path(arg).expanduser().resolve()
                owned_project = self._owned_project_arg(arg)
                if not (
                    _within(path, self.repo)
                    or _within(path, self.upload)
                    or owned_project
                ):
                    raise AgentError(f"脚本参数超出允许路径:{arg}")
                if _within(path, self.projects) and not owned_project:
                    raise AgentError(f"脚本不得操作其他项目:{arg}")

        output_flags = {"-o", "--output", "--output-dir", "--output_dir"}
        for index, arg in enumerate(args[:-1]):
            if arg not in output_flags:
                continue
            output = Path(args[index + 1]).expanduser()
            if not output.is_absolute():
                output = self.repo / output
            output = output.resolve()
            if not (_within(output, self.upload) or self._owned_project_arg(str(output))):
                raise AgentError(f"脚本输出路径必须属于本任务:{args[index + 1]}")

        if script_name == "project_manager.py" and args[:1] == ["init"]:
            if self.resume_project is not None:
                raise AgentError("续跑任务禁止重新初始化项目")
            if len(args) < 2 or not args[1].startswith(self.project_prefix):
                raise AgentError(f"项目名必须以 {self.project_prefix} 开头")
            return

        if script_name == "source_to_md.py":
            if self.resume_project is not None:
                raise AgentError("续跑任务禁止重新导入或转换材料")
            skip_next = False
            for index, arg in enumerate(args):
                if skip_next:
                    skip_next = False
                    continue
                if arg in output_flags or arg in {"-t", "--type"}:
                    skip_next = True
                    continue
                if arg.startswith("-") or "://" in arg:
                    continue
                source = Path(arg).expanduser()
                if not source.is_absolute():
                    source = self.repo / source
                source = source.resolve()
                if not (_within(source, self.upload) or self._owned_project_arg(str(source))):
                    raise AgentError(f"source_to_md 只能转换本任务材料:{arg}")

        read_or_fragment_only = {
            "source_to_md.py",
            "preset_shape_svg.py",
            "shape_boolean_svg.py",
        }
        if script_name not in read_or_fragment_only and not any(self._owned_project_arg(arg) for arg in args):
            raise AgentError("该脚本必须显式传入本任务自己的项目路径")

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            handler = getattr(self, f"tool_{name}", None)
            if handler is None:
                raise AgentError(f"未知工具:{name}")
            result = handler(**arguments)
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - tool errors go back to the model
            message = str(exc) or type(exc).__name__
            self.log(self.job.id, f"工具 {name} 失败:{message[:500]}")
            return json.dumps({"ok": False, "error": message[:2000]}, ensure_ascii=False)

    def tool_read_text_file(self, path: str, start_line: int = 1, max_lines: int = 400) -> dict[str, Any]:
        resolved = self._resolve_read(path)
        if not resolved.is_file():
            raise AgentError(f"文件不存在:{path}")
        start = max(1, int(start_line))
        limit = max(1, min(int(max_lines), 2000))
        text = resolved.read_text(encoding="utf-8")
        lines = text.splitlines()
        selected = lines[start - 1:start - 1 + limit]
        content = "\n".join(f"{index}: {line}" for index, line in enumerate(selected, start=start))
        if len(content) > MAX_READ_CHARS:
            content = content[:MAX_READ_CHARS] + "\n[本次读取达到字符上限，请缩小行数继续]"
        next_line = start + len(selected)
        self.log(self.job.id, f"读取 {resolved.name}:第 {start}-{next_line - 1} 行")
        return {
            "path": str(resolved),
            "content": content,
            "next_line": next_line if next_line <= len(lines) else None,
            "total_lines": len(lines),
        }

    def tool_list_files(self, path: str, pattern: str = "*", max_entries: int = 300) -> dict[str, Any]:
        resolved = self._resolve_read(path)
        if not resolved.is_dir():
            raise AgentError(f"目录不存在:{path}")
        if ".." in Path(pattern).parts:
            raise AgentError("glob pattern 不得包含 ..")
        limit = max(1, min(int(max_entries), 1000))
        entries = []
        for item in sorted(resolved.glob(pattern)):
            item_resolved = item.resolve()
            if not (
                _within(item_resolved, self.repo)
                or _within(item_resolved, self.upload)
                or self._owned_project_arg(str(item_resolved))
            ):
                continue
            kind = "dir" if item.is_dir() else "file"
            size = item.stat().st_size if item.is_file() else None
            entries.append({"path": str(item), "type": kind, "size": size})
            if len(entries) >= limit:
                break
        self.log(self.job.id, f"列出 {resolved.name}: {len(entries)} 项")
        return {"entries": entries, "truncated": len(entries) >= limit}

    def tool_write_text_file(self, path: str, content: str) -> dict[str, Any]:
        if len(content) > MAX_FILE_CHARS:
            raise AgentError(f"单文件超过 {MAX_FILE_CHARS} 字符限制")
        resolved = self._resolve_owned(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        self.log(self.job.id, f"写入 {resolved.name} ({len(content)} 字符)")
        return {"path": str(resolved), "chars": len(content)}

    def tool_replace_text_file(
        self,
        path: str,
        old: str,
        new: str,
        count: int = 1,
    ) -> dict[str, Any]:
        resolved = self._resolve_owned(path)
        text = resolved.read_text(encoding="utf-8")
        expected = max(1, int(count))
        actual = text.count(old)
        if actual != expected:
            raise AgentError(f"期望匹配 {expected} 次，实际 {actual} 次；未修改")
        updated = text.replace(old, new, expected)
        if len(updated) > MAX_FILE_CHARS:
            raise AgentError(f"修改后文件超过 {MAX_FILE_CHARS} 字符限制")
        resolved.write_text(updated, encoding="utf-8")
        self.log(self.job.id, f"更新 {resolved.name}:替换 {expected} 处")
        return {"path": str(resolved), "replacements": expected}

    def tool_install_template_workspace(self, source_root: str, project_path: str) -> dict[str, Any]:
        if self.resume_project is not None:
            raise AgentError("续跑任务禁止重新安装模板工作区")
        source = self._resolve_read(source_root)
        destination = self._resolve_owned(project_path)
        if destination.parent != self.projects:
            raise AgentError("模板只能安装到本任务的项目根目录")
        if not source.is_dir() or not (source / "templates/design_spec.md").is_file():
            raise AgentError("模板工作区缺少 templates/design_spec.md")
        if not destination.is_dir():
            raise AgentError("目标项目尚未初始化")

        mappings: list[tuple[Path, Path]] = []
        for root_name in ("templates", "images", "icons"):
            source_dir = source / root_name
            if not source_dir.is_dir():
                continue
            for source_file in sorted(source_dir.rglob("*")):
                if not source_file.is_file():
                    continue
                source_resolved = source_file.resolve()
                if not _within(source_resolved, source.resolve()):
                    raise AgentError(f"模板包含越界链接:{source_file}")
                target = destination / root_name / source_file.relative_to(source_dir)
                mappings.append((source_file, target))

        collisions = [str(target) for _, target in mappings if target.exists()]
        if collisions:
            shown = "、".join(collisions[:8])
            raise AgentError(f"模板安装目标存在冲突:{shown}")

        copied: list[Path] = []
        try:
            for source_file, target in mappings:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target)
                copied.append(target)
        except Exception:
            for target in reversed(copied):
                target.unlink(missing_ok=True)
            raise

        counts = {
            name: sum(1 for _, target in mappings if target.relative_to(destination).parts[0] == name)
            for name in ("templates", "images", "icons")
        }
        self.log(self.job.id, f"安装模板工作区:共 {len(mappings)} 个文件")
        return {
            "source_root": str(source),
            "project_path": str(destination),
            "files": len(mappings),
            "counts": counts,
        }

    def _downscale_image(self, source: Path) -> tuple[bytes, str] | None:
        """用引擎解释器的 Pillow 把大图缩成 ≤1568px JPEG;失败返回 None。"""
        code = (
            "import sys\n"
            "from PIL import Image\n"
            "im = Image.open(sys.argv[1])\n"
            "im = im.convert('RGB')\n"
            "im.thumbnail((1568, 1568))\n"
            "im.save(sys.argv[2], 'JPEG', quality=85)\n"
        )
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        try:
            completed = subprocess.run(
                [os.environ.get("PPT_PYTHON_BIN", "python3"), "-c", code, str(source), tmp.name],
                capture_output=True, timeout=60,
            )
            if completed.returncode == 0:
                data = Path(tmp.name).read_bytes()
                if data:
                    return data, "image/jpeg"
            return None
        except Exception:
            return None
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def tool_view_image(self, path: str, purpose: str = "") -> dict[str, Any]:
        if not config.model_conf(self.job.model).get("vision"):
            raise AgentError(
                "当前驱动模型不支持图片输入,view_image 不可用;"
                "请按 without-vision 分支降级:依据文件名、材料上下文与 analyze_images 客观参数决策"
            )
        if self.viewed_images >= VIEW_IMAGE_MAX:
            raise AgentError(
                f"view_image 已达本任务上限({VIEW_IMAGE_MAX} 次)。材料的文字内容已转换为 Markdown,"
                "请依据 Markdown、文件名与 analyze_images 客观参数完成后续决策,不要再逐张看图"
            )
        resolved = self._resolve_read(path)
        if not resolved.is_file():
            raise AgentError(f"图片不存在:{path}")
        suffix = resolved.suffix.lower()
        if suffix not in VIEW_IMAGE_EXTS:
            raise AgentError(f"不支持查看该格式:{suffix}(仅 {', '.join(sorted(VIEW_IMAGE_EXTS))})")
        raw = resolved.read_bytes()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif"}[suffix.lstrip(".")]
        note = "原图"
        if len(raw) > VIEW_DOWNSCALE_BYTES:
            scaled = self._downscale_image(resolved)
            if scaled is not None:
                raw, mime = scaled
                note = "已缩样(≤1568px JPEG)"
            elif len(raw) > VIEW_RAW_MAX_BYTES:
                raise AgentError(f"图片过大({len(raw)} 字节)且缩图不可用,无法查看")
        self.viewed_images += 1  # 校验全部通过才计数,路径/格式错误不烧配额
        self.pending_image = {
            "path": str(resolved),
            "mime": mime,
            "b64": base64.b64encode(raw).decode("ascii"),
        }
        self.log(self.job.id, f"查看图片 {resolved.name}({note},{len(raw)} 字节)"
                              + (f":{purpose[:80]}" if purpose else ""))
        return {"path": str(resolved), "bytes": len(raw), "mode": note,
                "note": "图片已附在紧随其后的用户消息中,请直接观察其内容再继续"}

    def tool_run_ppt_script(
        self,
        script: str,
        args: list[str],
        timeout_seconds: int = 300,
    ) -> dict[str, Any]:
        """Run one allow-listed engine script.

        For AI-enabled company jobs the generated cover becomes a required
        resource as soon as image_gen.py finishes.  Later checker/finalize/
        export calls sync that file into the fixed cover picture slot before
        the engine reads svg_output.
        """
        script_path = (self.scripts / script).resolve()
        if not _within(script_path, self.scripts) or not script_path.is_file() or script_path.suffix != ".py":
            raise AgentError(f"不是允许的 PPT Master Python 脚本:{script}")
        clean_args = [str(arg) for arg in args]
        script_name = script_path.relative_to(self.scripts).as_posix()
        if script_name not in ALLOWED_SCRIPTS:
            raise AgentError(
                f"脚本不在白名单内:{script_name}。可用脚本:{', '.join(sorted(ALLOWED_SCRIPTS))}"
            )
        if script_name == "image_gen.py":
            image_ready, image_reason = config.image_gen_ready()
            if not (getattr(self.job, "ai_images", False) and image_ready):
                raise AgentError(
                    "本任务未启用 AI 配图,image_gen.py 不可用"
                    + (f"(后端:{image_reason})" if not image_ready else "")
                    + ";缺图页面请用材料图片或纯矢量设计"
                )
        if script_name == "svg_to_pptx.py" and self.job.style == "company_free":
            found_structure_flag = False
            normalized_args: list[str] = []
            index = 0
            while index < len(clean_args):
                arg = clean_args[index]
                if arg == "--pptx-structure":
                    if index + 1 >= len(clean_args):
                        raise AgentError("--pptx-structure 缺少参数")
                    normalized_args.extend([arg, "flat"])
                    found_structure_flag = True
                    index += 2
                    continue
                if arg.startswith("--pptx-structure="):
                    normalized_args.append("--pptx-structure=flat")
                    found_structure_flag = True
                    index += 1
                    continue
                normalized_args.append(arg)
                index += 1
            clean_args = normalized_args
            if not found_structure_flag:
                clean_args.extend(["--pptx-structure", "flat"])
        self._validate_script_args(script_name, clean_args)

        if (
            getattr(self.job, "ai_images", False)
            and self.job.style in ("company", "company_free")
            and script_name in {"svg_quality_checker.py", "finalize_svg.py", "svg_to_pptx.py"}
        ):
            project = next(
                (
                    project_root
                    for arg in clean_args
                    if (project_root := self._owned_project_root_from_arg(arg)) is not None
                ),
                None,
            )
            cover_file = project / COMPANY_COVER_IMAGE_RELATIVE if project is not None else None
            if cover_file is not None and cover_file.is_file():
                fill: dict = {}
                fill_path = project / "native_fill.json"
                if fill_path.is_file():
                    try:
                        parsed_fill = json.loads(fill_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        parsed_fill = None
                    if isinstance(parsed_fill, dict):
                        fill = parsed_fill
                fill.setdefault("cover_image", COMPANY_COVER_IMAGE_RELATIVE)
                cover = _resolve_company_cover_image(project, fill, required=True)
                if cover is not None:
                    if self.job.style == "company_free":
                        _strip_flat_structure_metadata(project, self.log, self.job.id)
                    _sync_company_cover_preview(
                        project,
                        cover,
                        structured=self.job.style == "company",
                    )

        timeout = max(1, min(int(timeout_seconds), 900, self._remaining()))
        command = [os.environ.get("PPT_PYTHON_BIN", "python3"), str(script_path), *clean_args]
        environment = dict(os.environ)
        for key in list(environment):
            upper = key.upper()
            if any(marker in upper for marker in
                   ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET", "PASSWORD")):
                environment.pop(key, None)
        if script_name == "image_gen.py":
            # 定向注入图像后端凭证:只进这个脚本的进程,不进其他脚本
            environment.update(config.image_gen_env())

        self.log(self.job.id, f"运行 {script_name} {' '.join(clean_args)}")
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=self.repo,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            elapsed = round(time.monotonic() - started, 2)
            self.log(self.job.id, f"{script_name} 结束:exit={completed.returncode}, {elapsed}s")
            return {
                "command": [script_name, *clean_args],
                "exit_code": completed.returncode,
                "stdout": _trim_command_output(stdout),
                "stderr": _trim_command_output(stderr),
                "duration_seconds": elapsed,
            }
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            self.log(self.job.id, f"{script_name} 超时:{timeout}s")
            return {
                "command": [script_name, *clean_args],
                "exit_code": None,
                "stdout": _trim_command_output(stdout),
                "stderr": _trim_command_output(stderr),
                "timed_out": True,
            }


class ResponsesAgent:
    def __init__(self, job: jobs.Job, log: LogFn, toolbox: ToolBox):
        self.job = job
        self.log = log
        self.toolbox = toolbox
        self.conf = config.model_conf(job.model)
        self.wire = self.conf.get("wire", "responses")
        self.vision = bool(self.conf.get("vision"))
        self.url = config.wire_url(self.conf)
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_input_tokens = 0

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """带瞬时错误重试的请求:5xx/429/网络中断/超时重试,4xx 直接失败。"""
        attempts = max(1, config.PPT_API_RETRIES)
        for attempt in range(1, attempts + 1):
            try:
                return self._post_once(payload)
            except _RetryableAPIError as exc:
                if attempt >= attempts:
                    raise AgentError(str(exc)) from exc
                wait = min(60, 5 * 2 ** (attempt - 1))  # 5/10/20/40/60s 指数退避,扛住网关连环断连
                self.log(self.job.id, f"API 瞬时错误,{wait}s 后重试({attempt}/{attempts - 1}):{str(exc)[:200]}")
                time.sleep(wait)
        raise AgentError("API 重试逻辑异常")  # 不可达,防御性兜底

    def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        remaining = self.toolbox._remaining()
        timeout = max(1, min(config.PPT_API_REQUEST_TIMEOUT_SECONDS, remaining))
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.conf.get('api_key') or config.PPT_API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": "ppt-web/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=self.ssl_context) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read(20_000).decode("utf-8", errors="replace")
            content_type = exc.headers.get("content-type", "")
            if body.lstrip().startswith("<") or "html" in content_type.lower():
                detail = f"API 网关返回 HTML 错误页(HTTP {exc.code})"
            else:
                try:
                    detail = f"API HTTP {exc.code}:{str(json.loads(body).get('error', body))[:1500]}"
                except json.JSONDecodeError:
                    detail = f"API HTTP {exc.code}:{body[:1500]}"
            if exc.code == 429 or exc.code >= 500:
                raise _RetryableAPIError(detail) from exc
            raise AgentError(detail) from exc
        except urllib.error.URLError as exc:
            raise _RetryableAPIError(f"API 连接失败:{exc.reason}") from exc
        except TimeoutError as exc:
            raise _RetryableAPIError(f"API 请求超过 {timeout} 秒") from exc
        except (http.client.HTTPException, OSError) as exc:
            # RemoteDisconnected/BadStatusLine/连接被重置等不会包成 URLError,单独兜住
            raise _RetryableAPIError(f"API 连接中断:{type(exc).__name__}: {exc}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise AgentError(f"API 返回非 JSON 内容:{body[:300]}") from exc
        if data.get("error"):
            raise AgentError(f"API 错误:{str(data['error'])[:1500]}")
        return data

    @staticmethod
    def _output_text(response: dict[str, Any]) -> str:
        text_parts = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"}:
                    text_parts.append(content.get("text", ""))
        return "\n".join(part for part in text_parts if part)

    def _chat_trim(self, history: list[dict[str, Any]]) -> None:
        """chat 协议没有服务端 compaction:上下文过大时就地截断较早的工具输出与调用参数。"""
        approx = sum(_approx_item_len(item) for item in history)
        if approx <= CHAT_TRIM_THRESHOLD:
            return
        trimmed = 0
        outputs = [item for item in history if item.get("type") == "function_call_output"]
        for item in outputs[:-CHAT_TRIM_KEEP]:
            out = item.get("output") or ""
            if len(out) > 400:
                item["output"] = out[:200] + "\n…[早期工具输出已截断,如仍需请重新调用]"
                trimmed += 1
        calls = [item for item in history if item.get("type") == "function_call"]
        for item in calls[:-CHAT_TRIM_KEEP_ARGS]:
            trimmed += _trim_call_arguments(item)
        if trimmed:
            self.log(self.job.id, f"上下文过长,截断 {trimmed} 条早期工具输出/参数")

    def _prune_images(self, history: list[dict[str, Any]]) -> None:
        """只保留最近 N 张已查看图片,更早的替换为占位文本(两种协议都要控制体量)。"""
        indexes = [i for i, item in enumerate(history) if _is_image_message(item)]
        for i in indexes[:-VIEW_KEEP_IMAGES] if len(indexes) > VIEW_KEEP_IMAGES else []:
            label = ""
            for p in history[i].get("content", []):
                if p.get("type") == "input_text":
                    label = p.get("text", "")
                    break
            history[i] = {"role": "user",
                          "content": f"{label}\n[该图片已从上下文移除以控制体量;如需重看请再次调用 view_image]"}

    def run(self, prompt: str) -> str:
        history: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        image_rule = (IMAGE_GEN_ON_RULE if getattr(self.job, "ai_images", False) and config.image_gen_ready()[0]
                      else IMAGE_GEN_OFF_RULE)
        instructions = (AGENT_INSTRUCTIONS.format(image_rule=image_rule)
                        + (VISION_INSTRUCTIONS if self.vision else NO_VISION_INSTRUCTIONS))
        tools = (TOOL_SCHEMAS if self.vision
                 else [t for t in TOOL_SCHEMAS if t.get("name") != "view_image"])
        for turn in range(1, config.PPT_AGENT_MAX_TURNS + 1):
            self.toolbox._remaining()
            self.log(self.job.id, f"API Agent 第 {turn} 轮")
            self._prune_images(history)
            payload = {
                "model": self.conf["model"],
                "instructions": instructions,
                "input": history,
                "tools": tools,
                "parallel_tool_calls": False,
                "max_output_tokens": config.PPT_MAX_OUTPUT_TOKENS,
                "store": False,
                "include": ["reasoning.encrypted_content"],
                "prompt_cache_key": f"ppt-web-{self.job.id}",
            }
            if config.PPT_COMPACT_THRESHOLD > 0:
                payload["context_management"] = [
                    {"type": "compaction", "compact_threshold": config.PPT_COMPACT_THRESHOLD}
                ]
            if self.wire == "chat":
                self._chat_trim(history)
                response = _from_chat_response(self._post(_chat_payload(payload, config.PPT_MAX_OUTPUT_TOKENS)))
            else:
                response = self._post(payload)
            usage = response.get("usage") or {}
            self.input_tokens += int(usage.get("input_tokens") or 0)
            self.output_tokens += int(usage.get("output_tokens") or 0)
            details = usage.get("input_tokens_details") or {}
            self.cached_input_tokens += int(details.get("cached_tokens") or 0)

            output = response.get("output") or []
            history.extend(output)
            compact_indexes = [
                index for index, item in enumerate(history) if item.get("type") == "compaction"
            ]
            if compact_indexes:
                history = history[compact_indexes[-1]:]
            calls = [item for item in output if item.get("type") == "function_call"]
            if not calls:
                text = self._output_text(response)
                status = response.get("status")
                if status not in (None, "completed"):
                    raise AgentError(f"API 响应未完成:status={status}")
                self.log(
                    self.job.id,
                    "Agent 完成;累计 tokens "
                    f"in={self.input_tokens}, cached={self.cached_input_tokens}, out={self.output_tokens}",
                )
                return text

            for call in calls:
                name = call.get("name", "")
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError:
                    result = json.dumps({"ok": False, "error": "工具参数不是合法 JSON"}, ensure_ascii=False)
                else:
                    self.log(self.job.id, f"Agent 调用工具:{name}")
                    result = self.toolbox.call(name, arguments)
                history.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": result,
                    }
                )
                pending = self.toolbox.pending_image
                if pending is not None:
                    self.toolbox.pending_image = None
                    history.append({
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": f"[view_image] {pending['path']} 的内容如下:"},
                            {"type": "input_image",
                             "image_url": f"data:{pending['mime']};base64,{pending['b64']}"},
                        ],
                    })
        raise AgentError(f"Agent 超过最大轮数 {config.PPT_AGENT_MAX_TURNS}")


def _cost_usd(agent: ResponsesAgent) -> float | None:
    input_rate = agent.conf.get("input_usd_per_m")
    output_rate = agent.conf.get("output_usd_per_m")
    if input_rate is None or output_rate is None:
        try:
            input_rate = float(os.environ["PPT_INPUT_USD_PER_M"])
            output_rate = float(os.environ["PPT_OUTPUT_USD_PER_M"])
        except (KeyError, ValueError):
            return None
    # 缓存命中按缓存价计费(未配置缓存价时退回旧行为:全部按输入价)
    cached_rate = agent.conf.get("cached_usd_per_m")
    if cached_rate is None:
        cached_rate = _env_float_or(os.environ.get("PPT_CACHED_USD_PER_M"), input_rate)
    cached = min(agent.cached_input_tokens, agent.input_tokens)
    fresh = agent.input_tokens - cached
    return round((fresh * input_rate + cached * cached_rate
                  + agent.output_tokens * output_rate) / 1_000_000, 6)


def _env_float_or(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw is not None else default
    except ValueError:
        return default


def _parse_recommendations(text: str) -> list[dict[str, str]]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    start, end = candidate.find("["), candidate.rfind("]")
    if start < 0 or end <= start:
        raise AgentError("风格推荐未返回 JSON 数组")
    try:
        data = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AgentError(f"风格推荐 JSON 无法解析:{exc}") from exc
    if not isinstance(data, list) or len(data) != 4:
        raise AgentError("风格推荐必须恰好包含 4 项")
    result = []
    for item in data:
        if not isinstance(item, dict):
            raise AgentError("风格推荐项格式错误")
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if not name or not description:
            raise AgentError("风格推荐缺少 name 或 description")
        result.append({"name": name[:40], "description": description[:800]})
    return result


def _parse_plan(text: str, fallback_styles: list[dict] | None = None) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        candidate = "\n".join(lines[1:-1]).strip()
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        raise AgentError("内容规划未返回 JSON 对象")
    try:
        data = json.loads(candidate[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AgentError(f"内容规划 JSON 无法解析:{exc}") from exc
    if not isinstance(data, dict):
        raise AgentError("内容规划结果格式错误")

    styles = []
    for item in data.get("styles") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        description = str(item.get("description", "")).strip()
        if name and description:
            styles.append({"name": name[:40], "description": description[:800]})
    if not styles:
        # 修订轮允许模型省略 styles,沿用上一版
        styles = list(fallback_styles or [])
    if not styles:
        raise AgentError("内容规划缺少可用的风格建议")

    raw_outline = data.get("outline")
    if not isinstance(raw_outline, list) or not raw_outline:
        raise AgentError("内容规划缺少 outline 页面列表")
    if len(raw_outline) > 40:
        raise AgentError("内容规划页数超过 40 页上限")
    outline = []
    for page in raw_outline:
        if not isinstance(page, dict):
            raise AgentError("outline 页面项格式错误")
        title = str(page.get("title", "")).strip()
        if not title:
            raise AgentError("outline 存在缺少标题的页面")
        points = [str(p).strip()[:300] for p in (page.get("points") or []) if str(p).strip()]
        outline.append({"title": title[:60], "points": points[:8]})

    return {
        "styles": styles[:4],
        "pages": len(outline),
        "outline": outline,
        "notes": str(data.get("notes", "")).strip()[:300],
    }


def _fail(job: jobs.Job, log: LogFn, exc: Exception) -> None:
    message = str(exc) or type(exc).__name__
    log(job.id, f"任务失败:{message[:1200]}")
    jobs.update(job, status="failed", error=message[:2000], finished_at=time.time())


def _new_agent(job: jobs.Job, log: LogFn) -> tuple[ResponsesAgent, ToolBox]:
    deadline = time.monotonic() + config.JOB_TIMEOUT_SECONDS
    toolbox = ToolBox(job, log, deadline)
    return ResponsesAgent(job, log, toolbox), toolbox


def is_transient_api_failure(error: str) -> bool:
    """Whether a failed job stopped on a retryable upstream/API transport error."""
    message = str(error or "").lower()
    markers = (
        "api http 429",
        "api http 5",
        "api 网关返回 html 错误页(http 5",
        "api 连接失败",
        "api 连接中断",
        "api 请求超过",
    )
    return any(marker in message for marker in markers)


def find_resume_checkpoint(job: jobs.Job) -> Path | None:
    """Return the only valid project checkpoint owned by this generation job."""
    projects = (config.PPT_MASTER_REPO / "projects").resolve()
    name_re = re.compile(rf"web_{re.escape(job.id)}_ppt169_\d{{8}}")
    valid: list[Path] = []
    try:
        entries = list(projects.iterdir())
    except OSError:
        return None
    for path in entries:
        try:
            if (
                not name_re.fullmatch(path.name)
                or path.is_symlink()
                or not path.is_dir()
                or path.parent.resolve() != projects
            ):
                continue
            required = (path / "design_spec.md", path / "spec_lock.md")
            if not all(item.is_file() and item.stat().st_size > 0 for item in required):
                continue
            for item in required:
                item.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        valid.append(path.resolve())
    return valid[0] if len(valid) == 1 else None


def _resume_prompt(project: Path) -> str:
    svg_names = [path.name for path in sorted((project / "svg_output").glob("*.svg"))]
    generated_images: list[str] = []
    manifest = project / "images" / "image_prompts.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        generated_images = [
            str(item.get("filename"))
            for item in (data.get("items") or [])
            if isinstance(item, dict)
            and item.get("status") == "Generated"
            and (project / "images" / str(item.get("filename") or "")).is_file()
        ]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return (
        "\n\n【磁盘检查点续跑】\n"
        f"- 已绑定项目: `{project}`。该项目已经初始化、导入材料并安装模板；"
        "严禁再次调用 project_manager.py init、source_to_md.py 或 install_template_workspace。\n"
        "- 这不是恢复旧 API 对话，而是以磁盘为准重建上下文。先读取项目根目录的 "
        "design_spec.md、spec_lock.md，再列出 images/、svg_output/、svg_final/、exports/、validation/。\n"
        f"- 已有 svg_output 页面: {', '.join(svg_names) if svg_names else '无（从第 1 页开始生成）'}。"
        "保留正确页面，只从第一个缺失页继续逐页创作；如已有全套页面则进入检查和导出。\n"
        f"- 已生成且必须复用的 AI 图片: {', '.join(generated_images) if generated_images else '无'}。"
        "清单中 status=Generated 且文件存在的图片禁止重新生成；只处理确实缺失或失败的资源。\n"
        "- 完成剩余页面后仍须完整执行质量检查、finalize、PPTX 导出和 postflight。"
        "最终导出必须在本次续跑中重新写入，不能把旧 exports 直接当作成功。\n"
    )


def _sparse_slot_pages(project: Path) -> list[str]:
    """找出「大槽位仅单行填充」的正文页——执行器把原型当表单填的退化产出。"""
    bad: list[str] = []
    for page in sorted(project.glob("svg_final/*.svg")):
        if page.name.startswith(("01_", "02_")):
            continue
        try:
            src = page.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in re.finditer(r'<g\b[^>]*data-pptx-bounds="([0-9. ]+)"[^>]*>(.*?)</g>', src, re.S):
            head = m.group(0).split(">", 1)[0]
            if any(k in head for k in ('"title"', '"subtitle"', '"slide-number"')):
                continue
            try:
                _, _, w, h = (float(v) for v in m.group(1).split())
            except ValueError:
                continue
            if w * h < 60_000:          # 只查大面板(约 300×200 以上)
                continue
            inner = m.group(2)
            if "<g" in inner:           # 有嵌套分组 = 认真排过版
                continue
            texts = len(re.findall(r"<text\b", inner))
            tspans = len(re.findall(r"<tspan\b", inner))
            shapes = len(re.findall(r"<(rect|line|circle|ellipse|path|polyline|polygon|image)\b", inner))
            if shapes == 0 and texts <= 1 and tspans <= 1:
                bad.append(page.name)
                break
    return bad


_FLAT_STRUCTURE_ATTR_RE = re.compile(
    r'\s+(?:data-pptx-layer|data-pptx-layout|data-pptx-layout-kind|'
    r'data-pptx-layout-name|data-pptx-master|data-pptx-master-name|'
    r'data-pptx-show-inherited-shapes|data-pptx-show-master-shapes|'
    r'data-pptx-placeholder|data-pptx-binding|data-pptx-carrier|'
    r'data-pptx-idx)(?![A-Za-z0-9_-])'
    r'(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+))?'
)


def _strip_flat_structure_metadata(project: Path, log: LogFn, job_id: str) -> None:
    """Remove structured Master/Layout markers before a company-free export.

    company_free keeps the brand skin but intentionally exports slide-local SVGs.
    Agents may copy the deck template's structured markers into those SVGs;
    removing only metadata leaves the authored visual content unchanged.
    """
    changed: list[str] = []
    for directory_name in ("svg_output", "svg_final"):
        directory = project / directory_name
        if not directory.is_dir():
            continue
        for svg in sorted(directory.glob("*.svg")):
            try:
                source = svg.read_text(encoding="utf-8")
                updated = _FLAT_STRUCTURE_ATTR_RE.sub("", source)
                if updated != source:
                    svg.write_text(updated, encoding="utf-8")
                    changed.append(f"{directory_name}/{svg.name}")
            except OSError as exc:
                raise AgentError(f"无法准备 flat SVG:{svg}:{exc}") from exc
    if changed:
        log(job_id, f"公司自由版清理结构元数据:{len(changed)} 个 SVG")


def _resolve_company_cover_image(
    project: Path,
    fill: dict,
    *,
    required: bool,
) -> Path | None:
    """Resolve the declared cover image inside this project's images directory.

    AI-enabled company jobs use a fixed manifest filename so post-processing is
    deterministic and never guesses among several hero images.
    """
    raw = str(fill.get("cover_image") or "").strip()
    if not raw:
        if required:
            raise AgentError(
                f"AI 公司封面缺少 native_fill.cover_image;"
                f"应为 {COMPANY_COVER_IMAGE_RELATIVE}"
            )
        return None
    if not required:
        raise AgentError("本任务未启用 AI 配图，native_fill.json 不得声明 cover_image")
    declared = Path(raw)
    if declared.is_absolute() or ".." in declared.parts:
        raise AgentError("封面图路径必须是项目 images/ 下的相对路径")
    images_root = (project / "images").resolve()
    cover = (project / declared).resolve()
    if cover.parent != images_root:
        raise AgentError("封面图路径必须直接位于项目 images/ 目录")
    if required and cover.name != COMPANY_COVER_IMAGE_NAME:
        raise AgentError(
            f"AI 公司封面文件名必须为 {COMPANY_COVER_IMAGE_NAME}"
        )
    if not cover.is_file():
        raise AgentError(f"封面图文件不存在:{declared.as_posix()}")

    if required:
        manifest_path = images_root / "image_prompts.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(f"AI 封面图清单缺失或损坏:{exc}") from exc
        if not isinstance(manifest, dict):
            raise AgentError("AI 封面图清单根节点必须是 JSON 对象")
        item = next(
            (
                row for row in (manifest.get("items") or [])
                if isinstance(row, dict) and str(row.get("filename") or "") == cover.name
            ),
            None,
        )
        if item is None:
            raise AgentError(f"AI 图片清单未登记封面图:{cover.name}")
        if item.get("status") != "Generated":
            raise AgentError(
                f"AI 封面图未生成成功:{cover.name}({item.get('status') or 'Pending'})"
            )
        expected_contract = {
            "page_role": "hero_page",
            "text_policy": "none",
            "aspect_ratio": "3.4:1",
        }
        mismatches = [
            f"{field} 应为 {expected},实为 {str(item.get(field) or '未填写').strip()}"
            for field, expected in expected_contract.items()
            if str(item.get(field) or "").strip() != expected
        ]
        if mismatches:
            raise AgentError("AI 封面图清单契约不匹配:" + ";".join(mismatches))
    fill["cover_image"] = str(cover)
    return cover


def _sync_company_cover_preview(
    project: Path,
    cover: Path,
    *,
    structured: bool = True,
) -> None:
    """Keep the authored cover SVG and Web preview on the same generated image.

    The final company deck uses the native slide-1 placeholder, while PPT
    Master's validation/preview pipeline reads svg_output.  Injecting the same
    project-local image there makes the resource contract, task-card preview,
    and downloaded PPTX agree without generating a second asset.
    """
    candidates = sorted((project / "svg_output").glob("*.svg"))
    if not candidates:
        raise AgentError("AI 公司封面无法同步预览:svg_output 中没有封面 SVG")
    cover_svg = candidates[0]
    try:
        source = cover_svg.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentError(f"AI 公司封面无法读取预览 SVG:{exc}") from exc

    href = f"../images/{cover.name}"
    image = (
        (
            '  <g id="company-cover-hero-slot" data-pptx-placeholder="picture" '
            'data-pptx-idx="10"\n    data-pptx-bounds="338 0 942 277">\n'
            '    <image id="company-cover-hero-carrier" data-pptx-carrier="true"\n'
            f'      href="{href}" x="338" y="0" width="942" height="277"\n'
            '      preserveAspectRatio="xMidYMid slice"/>\n  </g>'
        )
        if structured else
        (
            '  <image id="company-cover-hero-carrier" '
            f'href="{href}" x="338" y="0" width="942" height="277"\n'
            '    preserveAspectRatio="xMidYMid slice"/>'
        )
    )
    existing = re.compile(
        r'(?:<g\b[^>]*\bid="company-cover-hero-slot"[^>]*>.*?</g>|'
        r'<image\b[^>]*\bid="company-cover-hero-carrier"[^>]*/>)',
        re.S,
    )
    if existing.search(source):
        updated = existing.sub(image, source, count=1)
    else:
        anchor = re.search(
            r'<rect\b[^>]*\bid="cover-white-panel"[^>]*/>',
            source,
            re.S,
        )
        if anchor is None:
            raise AgentError("AI 公司封面无法同步预览:封面 SVG 缺少图片槽")
        updated = source[:anchor.end()] + "\n" + image + source[anchor.end():]
    try:
        ET.fromstring(updated)
        cover_svg.write_text(updated, encoding="utf-8")
    except (ET.ParseError, OSError) as exc:
        raise AgentError(f"AI 公司封面预览 SVG 写入失败:{exc}") from exc


def _verify_company_cover_preview(project: Path) -> None:
    """Require the task-card cover preview to be a self-contained SVG."""
    candidates = sorted((project / "svg_final").glob("*.svg"))
    if not candidates:
        raise AgentError("AI 公司封面预览缺失:svg_final 中没有封面 SVG")
    try:
        root = ET.parse(candidates[0]).getroot()
    except (OSError, ET.ParseError) as exc:
        raise AgentError(f"AI 公司封面预览损坏:{exc}") from exc
    image = next(
        (
            element
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "image"
            and element.get("id") == "company-cover-hero-carrier"
        ),
        None,
    )
    href = "" if image is None else str(
        image.get("href")
        or image.get("{http://www.w3.org/1999/xlink}href")
        or ""
    )
    if not href.startswith("data:image/"):
        raise AgentError("AI 公司封面预览未嵌入图片数据")


def _company_postprocess(job: jobs.Job, log: LogFn, toolbox: ToolBox) -> None:
    """公司模板任务收尾:以模板原稿为底做原生合并,并做母版保真度硬校验。"""
    import native_company  # noqa: PLC0415 — ppt-web 本地模块

    template = config.company_template_source()
    if template is None:
        raise AgentError("未找到公司模板原稿(检查 COMPANY_TEMPLATE_SOURCE 或模板 exports/ 预览稿)")
    resume_project = getattr(toolbox, "resume_project", None)
    if resume_project is not None:
        project = resume_project
    else:
        projects = sorted(
            toolbox.projects.glob(f"{toolbox.project_prefix}*"),
            key=lambda p: p.stat().st_mtime,
        )
        if not projects:
            raise AgentError("找不到本任务的项目目录,无法执行原生合并")
        project = projects[-1]

    if job.style == "company_free":
        _strip_flat_structure_metadata(project, log, job.id)

    if job.style == "company":  # 自由版不走槽位,不适用此检查
        # 仅提示不拦截:该启发式按「槽位代码内部」判稀疏,而产线常把内容画在槽位上方
        # (兄弟分组),对正常成品有已知误报;成品质量交由规划确认 + 人工验收把关。
        sparse = _sparse_slot_pages(project)
        if sparse:
            log(job.id, f"提示:{len(sparse)} 页疑似大面板单行填充(可能误报),建议下载后人工过目:"
                        f"{'、'.join(sparse[:6])}{'…' if len(sparse) > 6 else ''}")
    bases = sorted(
        (
            p for p in project.glob("exports/*.pptx")
            if not p.stem.endswith("_company")
            and (not getattr(job, "resume_existing", False) or p.stat().st_mtime >= job.started_at - 1)
        ),
        key=lambda p: p.stat().st_mtime,
    )
    if not bases and job.style == "company_free":
        log(job.id, "Agent 未产出基础 PPTX,系统按公司自由版 flat 契约自动导出")
        result = toolbox.tool_run_ppt_script(
            "svg_to_pptx.py",
            [f"projects/{project.name}", "--no-notes"],
        )
        if int(result.get("exit_code", 1)) != 0:
            details = str(result.get("stderr") or result.get("stdout") or "").strip()
            suffix = f":{details[:1200]}" if details else ""
            raise AgentError(f"公司自由版基础 PPTX 自动导出失败{suffix}")
        bases = sorted(
            (
                p for p in project.glob("exports/*.pptx")
                if not p.stem.endswith("_company")
                and (not getattr(job, "resume_existing", False) or p.stat().st_mtime >= job.started_at - 1)
            ),
            key=lambda p: p.stat().st_mtime,
        )
    if not bases:
        raise AgentError("项目 exports/ 中没有基础 PPTX,无法执行原生合并")
    base = bases[-1]

    fill: dict = {}
    fill_file = project / "native_fill.json"
    if fill_file.is_file():
        try:
            fill = json.loads(fill_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log(job.id, f"native_fill.json 解析失败({exc}),改用回退文案")
    else:
        log(job.id, "Agent 未写 native_fill.json,改用回退文案")
    if not str(fill.get("title") or "").strip():
        topic_line = job.topic.strip().splitlines()[0][:40] if job.topic.strip() else ""
        fill["title"] = topic_line or "演示文稿"
    fill.setdefault("date", time.strftime("%Y年%m月%d日"))
    cover_path = _resolve_company_cover_image(
        project,
        fill,
        required=bool(getattr(job, "ai_images", False)),
    )
    if cover_path is not None:
        _sync_company_cover_preview(
            project,
            cover_path,
            structured=job.style == "company",
        )
        if (project / "svg_final").is_dir():
            preview_result = toolbox.tool_run_ppt_script(
                "finalize_svg.py",
                [f"projects/{project.name}"],
            )
            if int(preview_result.get("exit_code", 1)) != 0:
                details = str(
                    preview_result.get("stderr") or preview_result.get("stdout") or ""
                ).strip()
                suffix = f":{details[:1200]}" if details else ""
                raise AgentError(f"AI 公司封面预览同步失败{suffix}")
            _verify_company_cover_preview(project)

    merged = base.with_name(f"{base.stem}_company.pptx")
    try:
        stats = native_company.merge(template, base, merged, fill)
    except native_company.MergeError as exc:
        raise AgentError(f"公司模板原生合并失败:{exc}") from exc
    problems = native_company.verify(
        merged,
        template,
        expect_cover_image=cover_path is not None,
    )
    if problems:
        raise AgentError("公司模板保真度校验未通过:" + ";".join(problems))
    log(
        job.id,
        f"原生合并完成:{merged.name}(原稿封面/目录 + 正文 {stats['body_slides']} 页,"
        f"目录填充 {stats['toc_rows_filled']} 行,页码按原稿归一化 {stats['slide_numbers_normalized']} 页,"
        f"封面图 {'已自动裁切填入' if stats.get('cover_image_filled') else '未启用'})",
    )


def _prepare_project_attempt(job: jobs.Job, log: LogFn, toolbox: ToolBox) -> Path | None:
    """Preserve an explicit checkpoint; otherwise keep the existing fresh-run cleanup."""
    if getattr(job, "resume_existing", False):
        project = find_resume_checkpoint(job)
        if (
            project is None
            or not job.resume_project
            or project.name != job.resume_project
            or toolbox.resume_project != project
        ):
            raise AgentError("续跑检查点已缺失、冲突或与入队时绑定的项目不一致")
        log(job.id, f"从磁盘检查点继续:{project.name}")
        return project
    for stale in toolbox.projects.glob(f"{toolbox.project_prefix}_*"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
            log(job.id, f"清理中断残留工作区:{stale.name}")
    return None


def run(job: jobs.Job, log: LogFn) -> None:
    """Generate a deck through the standalone API tool loop."""
    import runner  # Local import avoids runner -> runner_agent import cycle.

    started = time.time()
    agent: ResponsesAgent | None = None
    try:
        if bool(getattr(job, "ai_images", False)):
            image_ready, image_reason = config.image_gen_ready()
            if not image_ready:
                raise AgentError(f"任务已启用 AI 配图，但图像后端未就绪:{image_reason}")
        agent, toolbox = _new_agent(job, log)
        resume_project = _prepare_project_attempt(job, log, toolbox)
        upload_dir = config.UPLOADS_DIR / (job.upload_id or job.id)
        prompt = prompts.build_prompt(
            style=job.style,
            pages=job.pages,
            note=job.note,
            upload_dir=upload_dir,
            files=job.files,
            topic=job.topic,
            style_brief=job.style_brief,
            outline=job.outline or None,
            ai_images=bool(job.ai_images) and config.image_gen_ready()[0],
        )
        prompt += (
            "\n\n【Web 运行约束】\n"
            f"- 初始化项目时项目名必须使用 `{toolbox.project_prefix}`，不得改名；后缀由 project_manager 自动添加。\n"
            "- 这是独立 API 执行，不要调用或提及 Codex/Claude CLI。\n"
            "- 不要修改模板源工作区；需要模板时使用 install_template_workspace 工具安装到本任务项目。\n"
        )
        if resume_project is not None:
            prompt += _resume_prompt(resume_project)
        if job.style in ("company", "company_free"):
            cover_rule = (
                "- 本任务已启用 AI 配图，封面顶部横幅图是必生项："
                f"images/image_prompts.json 必须包含 filename={COMPANY_COVER_IMAGE_NAME}、"
                "page_role=hero_page、text_policy=none、aspect_ratio=3.4:1、status=Pending 的一行，"
                "并在其他 AI 图片之前一起执行 --render-md 和 --manifest。"
                "提示词需贴合本 PPT 主题，使用公司蓝绿 vector-illustration 画风，"
                "不生成文字、数字、Logo 或水印；主体与关键细节必须集中在图像中央约 44% 高度的横向安全带，"
                "为系统填入 3.404:1 超宽占位框时的居中 cover 裁切留出余量。"
                f"native_fill.json 必须写 cover_image: \"{COMPANY_COVER_IMAGE_RELATIVE}\"。"
                "该图未达 Generated 时不得导出或改用空白封面。\n"
                if job.ai_images else
                "- 本任务未启用 AI 配图，封面顶部图片占位区保持原稿空白；"
                "native_fill.json 不得写 cover_image。\n"
            )
            body_rule = (
                "- 正文页(第 3 页起)必须使用 03x 正文原型,保留固定页眉、品牌字标、页码与保密页脚。\n"
                "- 槽位不是表单:carrier 占位文本必须整体替换为完整的排版内容。大内容槽位"
                "(正文区、面板、卡片)至少包含 3 个元素——多行要点逐行排版、配官方图标、"
                "数据用图表/数据卡呈现;禁止用单行文字占据整个大面板,此类页面视为未完成,"
                "导出前必须逐页自查并返工。\n"
                if job.style == "company" else
                "- 正文页(第 3 页起)不套 03x 原型,但每页必须完整复刻 design_spec §I-b 页眉契约"
                "(蓝渐变横条、白色页标题、品牌字标、分隔线、页码)与保密页脚,内容区自由构图。\n"
                "- 自由版是品牌风格引用,不是结构化原型复刻:spec_lock.md 必须写 "
                "pptx_structure.mode: flat 与 template_reuse_scope: style,不得写 structured/page_layouts;"
                "SVG 中不得保留 data-pptx-master/layout/layer 等结构元数据;"
                "导出必须使用 --pptx-structure flat。\n"
            )
            cover_resource_rule = (
                "你仍须把它作为 placed 资源写入 design_spec §VIII 与 spec_lock.md images，"
                "Layout pattern 使用 `封面顶部横幅图片框`、Crop Policy 使用 adaptive，"
                if job.ai_images else
                "未启用 AI 配图时不要为该透明图片槽创建 §VIII/spec_lock 资源行，"
            )
            prompt += (
                "\n【公司模板附加约定】\n"
                "- 成品第 1、2 页最终由系统自动替换为公司模板原稿的原生封面/目录页；"
                "你照常用 01_cover/02_toc 原型完成这两页作为流程内预览,但不要在它们上面反复打磨。"
                "封面横幅会在原生合并前由系统自动同步到 svg_output 的 01_cover 图片框；"
                + cover_resource_rule
                + "但无需手工改写 01_cover.svg。\n"
                + body_rule
                + cover_rule
                + "- 页码一律填不补零的数字(3、4、5…),与公司原稿页码风格一致,不要写成 03、04。\n"
                "- 导出 PPTX 之前,必须用 write_text_file 在项目根目录写 native_fill.json,内容为一个 JSON 对象:"
                '{"title": "封面主标题", "subtitle": "封面副标题(单位/项目,一行)", "date": "封面日期,一行", '
                '"toc_title": "目录页标题", "toc": ["每个正文页对应一个目录条目"]'
                + (f', "cover_image": "{COMPANY_COVER_IMAGE_RELATIVE}"' if job.ai_images else "")
                + '}。'
                "全部使用中文,目录条目顺序与正文页一一对应。\n"
            )
        final_text = agent.run(prompt)
        if final_text:
            log(job.id, f"Agent 最终摘要:{final_text[:1000]}")
        if job.style in ("company", "company_free"):
            _company_postprocess(job, log, toolbox)
        outputs, preview = runner.collect_outputs(
            job,
            started - 1,
            project_prefix=toolbox.project_prefix,
        )
        if job.style in ("company", "company_free"):
            merged_only = [name for name in outputs if name.endswith("_company.pptx")]
            if not merged_only:
                raise AgentError("公司模板任务缺少合并后的 *_company.pptx 成品")
            out_dir = config.OUTPUTS_DIR / job.id
            for name in outputs:
                if name not in merged_only:
                    (out_dir / name).unlink(missing_ok=True)
            outputs = merged_only
        if not outputs:
            raise AgentError("Agent 已结束，但本任务项目 exports/ 中没有生成新的 PPTX")
        jobs.update(
            job,
            status="done",
            outputs=outputs,
            preview=preview,
            cost_usd=_cost_usd(agent),
            finished_at=time.time(),
            resume_existing=False,
            resume_project="",
        )
        log(job.id, f"完成:已收集 {len(outputs)} 个 PPTX")
    except Exception as exc:  # noqa: BLE001 - convert to persisted job failure
        _fail(job, log, exc)


def recommend(job: jobs.Job, log: LogFn) -> None:
    """Recommend four styles without creating a PPT project."""
    agent, _ = _new_agent(job, log)
    upload_dir = config.UPLOADS_DIR / (job.upload_id or job.id)
    prompt = prompts.build_recommend_prompt(
        upload_dir=upload_dir,
        files=job.files,
        topic=job.topic,
    )
    prompt += "\n这是轻量分析任务；可读取或转换上传材料，但不得初始化项目或写入 ppt-master/projects。"
    try:
        text = agent.run(prompt)
        recommendations = _parse_recommendations(text)
        jobs.update(
            job,
            status="done",
            recommendations=recommendations,
            cost_usd=_cost_usd(agent),
            finished_at=time.time(),
        )
        log(job.id, "完成:已生成 4 个材料定制风格")
    except Exception as exc:  # noqa: BLE001
        _fail(job, log, exc)


def plan(job: jobs.Job, log: LogFn) -> None:
    """Plan styles plus a per-page content outline without creating a PPT project."""
    agent, _ = _new_agent(job, log)
    upload_dir = config.UPLOADS_DIR / (job.upload_id or job.id)
    feedback = job.plan_feedback[-1] if job.plan_feedback else ""
    prompt = prompts.build_plan_prompt(
        upload_dir=upload_dir,
        files=job.files,
        topic=job.topic,
        pages=job.pages,
        note=job.note,
        current_plan=job.plan or None,
        feedback=feedback,
    )
    if len(job.plan_feedback) > 1:
        history = "\n".join(f"- {item}" for item in job.plan_feedback[:-1])
        prompt += f"\n【此前几轮已采纳的调整意见(保持其效果,不要回退)】\n{history}\n"
    prompt += "\n这是轻量规划任务；可读取或转换上传材料，但不得初始化项目或写入 projects。"
    try:
        text = agent.run(prompt)
        result = _parse_plan(text, fallback_styles=job.plan.get("styles"))
        jobs.update(
            job,
            status="done",
            plan=result,
            cost_usd=_cost_usd(agent),
            finished_at=time.time(),
        )
        log(job.id, f"完成:已规划 {result['pages']} 页内容分布,等待确认")
    except Exception as exc:  # noqa: BLE001
        _fail(job, log, exc)
