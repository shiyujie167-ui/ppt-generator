"""PPT 生成 Web 服务(单机版,账号登录)。启动:python3 app.py

多账号就绪、单账号在用:所有接口按登录用户隔离(材料库/任务/成品),
当前库里只有 admin 一个账号;开放同事使用 = migrate_v2.py adduser。
首次部署先运行 python3 migrate_v2.py 完成建库与历史数据迁移。
"""
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import uvicorn
from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, RedirectResponse)
from fastapi.staticfiles import StaticFiles

import config
import db
import jobs
import prompts
import qa
import runner
import runner_agent

app = FastAPI(title="PPT Master Web", root_path=config.BASE_PATH)

# 模板 deck 静态资源:公司模板封面 SVG 直接从内置引擎读
app.mount("/repo/decks", StaticFiles(directory=str(config.PPT_MASTER_REPO / "skills/ppt-master/templates/decks")), name="decks")

# 公司蓝卡片封面:公司模板原稿第 1 页的预渲染图,比引擎 SVG 骨架更贴近实际成品
# (SVG 里是 {{TITLE}} 占位符,且 <img> 方式不加载其外链品牌图)。缺图时回退 SVG。
# 重建:soffice --headless --convert-to pdf 原稿后 qlmanage -t -s 1920 渲染第 1 页。
_COMPANY_COVER_PNG = config.BUNDLED_DATA_DIR / "company" / "cover_preview.png"
_COMPANY_COVER_URL = ("company/cover.png" if _COMPANY_COVER_PNG.is_file()
                      else "repo/decks/mt_corporate_blue/templates/01_cover.svg")

COMPANY_STYLE_CARD = {
    "example_id": "__company__",
    "styleName": "公司蓝模板",
    "title": "上午同款 · mt_corporate_blue",
    "description": "公司蓝白主调,原生封面与目录,正式汇报默认选择",
    "cover": _COMPANY_COVER_URL,
    "viewer": "",
    "group": "builtin",
}


# 内置卡:公司蓝(固定骨架)+ 公司蓝自由版(品牌壳固定、版面自由);
# 瑞士/深色/自定义已从 UI 移除(prompts.STYLES 仍保留定义,供旧任务与接口层兼容)。
STYLE_CARDS = [
    COMPANY_STYLE_CARD,
    {
        "example_id": "__company_free__",
        "styleName": "公司蓝 · 自由版",
        "title": "同款封面页眉 · 版面自由设计",
        "description": "品牌壳与公司蓝完全一致(原生封面/目录、蓝渐变页眉、保密页脚、公司九色板),正文版面不套固定骨架、每页自由构图,更有设计感",
        "cover": _COMPANY_COVER_URL,
        "viewer": "",
        "group": "builtin",
    },
]
_CARD_TO_STYLE = {"__company__": "company", "__company_free__": "company_free",
                  "__style_swiss__": "swiss", "__style_dark__": "dark", "__style_custom__": "custom"}

# 示例成品画廊(可选):data/examples/ 裁剪自上游 ppt-master 示例(预渲染 SVG,
# 详见 data/examples/README.md);目录缺失时自动降级为只有内置两张风格卡。
EXAMPLES_DIR = config.BUNDLED_DATA_DIR / "examples"
EXAMPLES: list[dict] = []
if (EXAMPLES_DIR / "examples.json").is_file():
    EXAMPLES = json.loads((EXAMPLES_DIR / "examples.json").read_text(encoding="utf-8")).get("examples", [])
    app.mount("/examples", StaticFiles(directory=str(EXAMPLES_DIR)), name="examples")

EXAMPLE_CARDS = [
    {
        "example_id": f"ex:{e['id']}",
        "styleName": e["styleName"],
        "title": f"{e['title']} · {e['pages']} 页",
        "description": e["description"],
        "cover": f"examples/{e['id']}/{e['cover']}",
        "viewer": f"viewer?id={e['id']}",
        "group": "example",
    }
    for e in EXAMPLES
]

ALLOWED_EXT = {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".xls", ".md", ".txt", ".csv", ".png", ".jpg", ".jpeg", ".html"}
MAX_FILE_MB = 50


def _safe_name(name: str) -> str:
    name = os.path.basename(name or "file")
    return re.sub(r"[^\w.一-鿿-]", "_", name)[:120]


async def _save_uploads(user_id: int, files: list[UploadFile]) -> list[str]:
    """按扩展名白名单与大小上限原子落盘,返回实际保存的文件名。"""
    upload_dir = config.LIBRARY_DIR / str(user_id)
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        name = _safe_name(f.filename or "")
        if Path(name).suffix.lower() not in ALLOWED_EXT:
            continue
        data = await f.read()
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            continue
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{name}.", suffix=".upload", dir=upload_dir, delete=False
            ) as temp_file:
                temp_file.write(data)
                temp_path = Path(temp_file.name)
            # Existing task snapshots may hard-link target. Replacing the directory
            # entry preserves their old inode; opening target for write would mutate it.
            qa.store_uploaded_source(user_id, name, temp_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        saved.append(name)
    return saved


# ── 登录 ─────────────────────────────────────────────────────────


def current_user(ppt_session: str = Cookie("")) -> dict:
    user = db.user_for_token(ppt_session)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return dict(user)


@app.get("/login", response_class=HTMLResponse)
def login_page(ppt_session: str = Cookie("")):
    if db.user_for_token(ppt_session) is not None:
        return RedirectResponse(config.web_path("/"), status_code=302)
    return (config.BASE_DIR / "templates" / "login.html").read_text(encoding="utf-8")


@app.post("/api/login")
def login(username: str = Form(...), password: str = Form(...)) -> JSONResponse:
    user = db.verify_login(username.strip(), password)
    if user is None:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    token = db.create_session(user["id"])
    response = JSONResponse({"ok": True, "user": user["username"]})
    response.set_cookie("ppt_session", token, max_age=config.SESSION_TTL_DAYS * 86400,
                        httponly=True, samesite="lax", path=config.COOKIE_PATH)
    return response


@app.post("/api/logout")
def logout(ppt_session: str = Cookie("")) -> JSONResponse:
    db.delete_session(ppt_session)
    response = JSONResponse({"ok": True})
    response.delete_cookie("ppt_session", path=config.COOKIE_PATH)
    return response


# ── 材料库(归属账号,常驻;上传即后台解析)────────────────────────


@app.get("/api/library")
def library_status(user: dict = Depends(current_user)) -> JSONResponse:
    return JSONResponse(qa.status(user["id"]))


@app.post("/api/library/files")
async def library_upload(files: list[UploadFile] = File(...),
                         user: dict = Depends(current_user)) -> JSONResponse:
    saved = await _save_uploads(user["id"], files)
    if not saved:
        return JSONResponse({"error": "没有可用文件(检查格式与大小)"}, status_code=400)
    return JSONResponse(qa.status(user["id"]))


@app.delete("/api/library/files/{name}")
def library_delete(name: str, user: dict = Depends(current_user)) -> JSONResponse:
    if not qa.remove_file(user["id"], _safe_name(name)):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return JSONResponse(qa.status(user["id"]))


def _parse_name_list(raw: str) -> set[str] | None:
    """解析前端的勾选文件名 JSON 数组;缺省/不合法返回 None(=不过滤)。"""
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data}
    except json.JSONDecodeError:
        pass
    return None


@app.post("/api/library/chat")
def library_chat(question: str = Form(...), history: str = Form("[]"),
                 model: str = Form(""), files: str = Form(""),
                 user: dict = Depends(current_user)) -> JSONResponse:
    question = question.strip()[:2000]
    if not question:
        return JSONResponse({"error": "请输入问题"}, status_code=400)
    model = model.strip()
    if model and model not in config.MODELS:
        return JSONResponse({"error": f"未注册的模型:{model}"}, status_code=400)
    try:
        turns = json.loads(history)
        assert isinstance(turns, list)
    except (json.JSONDecodeError, AssertionError):
        turns = []
    try:
        result = qa.chat(user["id"], question, turns, model, _parse_name_list(files))
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    if result.get("error"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


# ── 页面 ─────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
def index(ppt_session: str = Cookie("")):
    if db.user_for_token(ppt_session) is None:
        return RedirectResponse(config.web_path("/login"), status_code=302)
    return (config.BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/viewer", response_class=HTMLResponse)
def viewer(ppt_session: str = Cookie("")):
    """示例成品的翻页预览器(前端从 /api/examples 取页面清单)。"""
    if db.user_for_token(ppt_session) is None:
        return RedirectResponse(config.web_path("/login"), status_code=302)
    return (config.BASE_DIR / "templates" / "viewer.html").read_text(encoding="utf-8")


@app.get("/company/cover.png")
def company_cover() -> FileResponse:
    """公司模板真实封面预览图(风格卡用)。"""
    return FileResponse(_COMPANY_COVER_PNG, media_type="image/png")


@app.get("/api/examples")
def api_examples(user: dict = Depends(current_user)) -> list[dict]:
    return EXAMPLES


@app.get("/api/health")
def health() -> JSONResponse:
    checks: list[tuple[bool, str]] = []
    try:
        db.fetchone("SELECT 1")
        checks.append((db.count_users() > 0, "数据库尚无账号"))
    except Exception:
        checks.append((False, "数据库不可用"))
    checks.append((jobs.workers_healthy(), "任务线程未就绪"))
    for directory in (config.DATA_DIR, config.UPLOADS_DIR, config.OUTPUTS_DIR,
                      config.LOGS_DIR, config.LIBRARY_DIR,
                      config.PPT_MASTER_REPO / "projects"):
        checks.append((directory.is_dir() and os.access(directory, os.W_OK),
                       f"目录不可写:{directory}"))
    python_bin = os.environ.get("PPT_PYTHON_BIN", "").strip()
    checks.append((bool(python_bin) and os.path.isfile(python_bin)
                   and os.access(python_bin, os.X_OK), "PPT_PYTHON_BIN 不可执行"))
    if not config.mock_enabled():
        ready, reason = config.agent_ready()
        checks.append((ready, reason))
    failure = next((reason for passed, reason in checks if not passed), "")
    if failure:
        return JSONResponse({"status": "error", "reason": failure}, status_code=503)
    return JSONResponse({"status": "ok"})


@app.get("/api/config")
def api_config(user: dict = Depends(current_user)) -> dict:
    ready, reason = config.agent_ready()
    img_ready, img_reason = config.image_gen_ready()
    return {
        "mock": config.mock_enabled(),
        "ready": ready,
        "status": reason,
        "backend": "Responses API",
        "model": config.PPT_MODEL or "(未设置)",
        "image_gen": {"ready": img_ready, "status": img_reason,
                      "model": config.PPT_IMAGE_MODEL},
        "models": [{"value": k, "label": v["label"], "wire": v["wire"]} for k, v in config.MODELS.items()],
        "styles": [{"value": k, "label": v["label"]} for k, v in prompts.STYLES.items()],
        "repo": str(config.PPT_MASTER_REPO),
        "user": user["username"],
    }


@app.get("/api/styles")
def api_styles(user: dict = Depends(current_user)) -> list[dict]:
    """风格卡片:公司模板 + 内置自由设计风格 + 示例成品同款风格。"""
    return STYLE_CARDS + EXAMPLE_CARDS


def _compose_style_brief(example_id: str) -> tuple[str, str]:
    """把风格卡片选择映射为内置风格;示例卡映射为 custom + 该示例的视觉风格规范。"""
    if example_id.startswith("ex:"):
        ex = next((e for e in EXAMPLES if e["id"] == example_id[3:]), None)
        if ex:
            brief = (
                f"{ex['styleName']}(示例成品「{ex['title']}」同款):{ex['description']}。"
                f"visual_style 锁定 {ex['style_id']},设计规范见 "
                f"references/visual-styles/{ex['style_id']}.md,严格遵循其形状语言、"
                f"字体气质、留白节奏与色彩纪律。"
            )
            return "custom", brief
    return _CARD_TO_STYLE.get(example_id, ""), ""


def _live_files(upload_id: str, names: list[str]) -> tuple[list[str], list[str]]:
    """按磁盘现状过滤任务的材料快照。快照是硬链接,正常不会缺;防御性保留。"""
    upload_dir = config.UPLOADS_DIR / upload_id
    live = [n for n in names if (upload_dir / n).is_file()]
    return live, [n for n in names if n not in live]


def _snapshot_materials(user_id: int, names: list[str], dest: Path) -> list[str]:
    """把勾选的材料库文件硬链接快照进任务目录(同盘零空间开销),连同解析
    衍生物(<名>.md / <名>.conversion_profile.json / <名>.* 目录)一起带走;
    之后用户删改材料库不影响本任务,历史任务永远对得上当时的材料。"""
    src_dir = config.LIBRARY_DIR / str(user_id)
    linked: list[str] = []

    def link(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)

    for name in names:
        original = src_dir / name
        if not original.is_file():
            continue
        # <名>.* 覆盖 .md/.conversion_profile.json;<名>_files 是 PDF 图片提取目录
        candidates = [original] + sorted(src_dir.glob(f"{name}.*")) + [src_dir / (name + "_files")]
        for item in candidates:
            if item.is_file():
                link(item, dest / item.name)
            elif item.is_dir():
                for inner in sorted(item.rglob("*")):
                    if inner.is_file():
                        link(inner, dest / item.name / inner.relative_to(item))
        linked.append(name)
    return linked


MAX_OUTLINE_PAGES = 40


def _sanitize_outline(raw: str) -> list[dict]:
    """解析并清洗前端提交的大纲 JSON;不合法时抛 ValueError。"""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"大纲 JSON 无法解析:{exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("大纲必须是非空数组")
    if len(data) > MAX_OUTLINE_PAGES:
        raise ValueError(f"大纲页数超过 {MAX_OUTLINE_PAGES} 页上限")
    outline = []
    for i, page in enumerate(data, 1):
        if not isinstance(page, dict):
            raise ValueError(f"第 {i} 页格式错误")
        title = str(page.get("title", "")).strip()
        if not title:
            raise ValueError(f"第 {i} 页缺少标题")
        points = [str(p).strip()[:300] for p in (page.get("points") or []) if str(p).strip()]
        outline.append({"title": title[:60], "points": points[:8]})
    return outline


@app.post("/api/jobs")
def create_job(
    mode: str = Form("plan"),  # plan=先出规划确认(默认);direct=跳过确认直接生成;recommend=旧版风格推荐
    style: str = Form("company"),
    example_id: str = Form(""),  # 风格卡片:__company__ / __style_* 内置 / ex:示例
    pages: str = Form("auto"),
    topic: str = Form(""),
    note: str = Form(""),
    model: str = Form(""),  # 驱动模型;空=默认 PPT_MODEL,其余须在 config.MODELS 注册表内
    upload_files: str = Form(""),  # 材料库中勾选参与生成的文件名 JSON 数组;缺省=全部
    ai_images: str = Form("0"),  # AI 文生图配图开关(需 PPT_IMAGE_MODEL 后端就绪)
    user: dict = Depends(current_user),
) -> JSONResponse:
    if not config.mock_enabled():
        ready, reason = config.agent_ready()
        if not ready:
            return JSONResponse({"error": f"真实 API 执行器未就绪:{reason}"}, status_code=503)
    if style not in prompts.STYLES:
        style = "company"
    model = model.strip()
    if model and model not in config.MODELS:
        return JSONResponse({"error": f"未注册的模型:{model}"}, status_code=400)

    ai_on = ai_images.strip().lower() in ("1", "true", "on", "yes")
    if ai_on:
        img_ready, img_reason = config.image_gen_ready()
        if not img_ready:
            return JSONResponse({"error": f"AI 配图后端未就绪:{img_reason}"}, status_code=400)

    library = [f["name"] for f in qa.status(user["id"])["files"]]
    picked = _parse_name_list(upload_files)
    names = [n for n in library if picked is None or n in picked]
    if not names and not topic.strip():
        return JSONResponse({"error": "请在左侧勾选材料,或填写主题"}, status_code=400)

    style_brief = ""
    if mode != "recommend" and example_id:
        picked_style, style_brief = _compose_style_brief(example_id)
        if picked_style:
            style = picked_style

    kind = {"recommend": "recommend", "plan": "plan"}.get(mode, "generate")
    job = jobs.create(kind=kind, user_id=user["id"], style=style, style_brief=style_brief,
                      pages=pages.strip() or "auto", topic=topic.strip()[:2000],
                      note=note.strip()[:2000], mock=config.mock_enabled(),
                      model=model or config.PPT_MODEL, ai_images=ai_on,
                      enqueue=False)
    linked = _snapshot_materials(user["id"], names, config.UPLOADS_DIR / job.id)
    jobs.update(job, upload_id=job.id, files=linked)
    jobs.enqueue(job)
    return JSONResponse({"id": job.id})


@app.post("/api/jobs/{job_id}/replan")
def replan(job_id: str, feedback: str = Form(...), outline: str = Form(""),
           user: dict = Depends(current_user)) -> JSONResponse:
    """规划确认环节:携带用户调整意见(和当前编辑到一半的大纲)让 AI 重排,同一任务重新入队。"""
    job = jobs.get(job_id)
    if job is None or job.user_id != user["id"] or job.kind != "plan" or job.status != "done":
        return JSONResponse({"error": "规划任务不存在或未完成,无法调整"}, status_code=400)
    feedback = feedback.strip()[:500]
    if not feedback:
        return JSONResponse({"error": "请填写调整意见"}, status_code=400)
    plan = dict(job.plan)
    if outline.strip():
        try:
            plan["outline"] = _sanitize_outline(outline)
            plan["pages"] = len(plan["outline"])
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
    files, _ = _live_files(job.upload_id or job.id, job.files)
    jobs.update(job, files=files, plan=plan, plan_feedback=[*job.plan_feedback, feedback],
                status="queued", error="", finished_at=None)
    jobs.enqueue(job)
    return JSONResponse({"id": job.id})


@app.post("/api/jobs/{job_id}/confirm-plan")
def confirm_plan(job_id: str, style: str = Form("company"), outline: str = Form(...),
                 model: str = Form(""), user: dict = Depends(current_user)) -> JSONResponse:
    """规划确认环节:用户确认(可能已手动编辑的)大纲与风格,派生正式生成任务。

    model 可与规划任务不同——同一份大纲可分别用不同模型各生成一份,便于对比。
    """
    parent = jobs.get(job_id)
    if parent is None or parent.user_id != user["id"] or parent.kind != "plan" or parent.status != "done":
        return JSONResponse({"error": "规划任务不存在或未完成"}, status_code=400)
    model = model.strip()
    if model and model not in config.MODELS:
        return JSONResponse({"error": f"未注册的模型:{model}"}, status_code=400)
    try:
        confirmed = _sanitize_outline(outline)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    style_brief = ""
    if style.startswith("ai:"):
        try:
            index = int(style[3:])
            if index < 0:
                raise ValueError(style)  # 负数会命中 Python 负索引,静默选中最后一项
            rec = (parent.plan.get("styles") or [])[index]
        except (ValueError, IndexError):
            return JSONResponse({"error": "无效的风格选择"}, status_code=400)
        if "公司蓝" in str(rec.get("name", "")):
            style = "company"  # AI 建议的公司蓝走内置流程,保证原生封面/目录合并
        else:
            style, style_brief = "custom", f"{rec.get('name', '')}:{rec.get('description', '')}"
    elif style in ("", "parent"):
        # 跟随规划任务的风格(即提交时选的模板卡),保留示例卡携带的 style_brief
        style, style_brief = parent.style, parent.style_brief
    elif style not in prompts.STYLES:
        style = "company"

    upload_id = parent.upload_id or parent.id
    files, dropped = _live_files(upload_id, parent.files)
    job = jobs.create(kind="generate", user_id=user["id"], style=style, style_brief=style_brief,
                      pages=str(len(confirmed)), topic=parent.topic, note=parent.note,
                      files=files, upload_id=upload_id,
                      outline=confirmed, mock=config.mock_enabled(),
                      model=model or parent.model or config.PPT_MODEL,
                      ai_images=parent.ai_images)
    return JSONResponse({"id": job.id, "dropped_files": dropped})


@app.post("/api/jobs/{job_id}/generate")
def generate_from_recommendation(job_id: str, choice: int = Form(...),
                                 user: dict = Depends(current_user)) -> JSONResponse:
    parent = jobs.get(job_id)
    if parent is None or parent.user_id != user["id"] or parent.kind != "recommend" or parent.status != "done":
        return JSONResponse({"error": "推荐任务不存在或未完成"}, status_code=400)
    if not (0 <= choice < len(parent.recommendations)):
        return JSONResponse({"error": "无效的风格选择"}, status_code=400)
    rec = parent.recommendations[choice]
    brief = f"{rec.get('name', '')}:{rec.get('description', '')}"
    upload_id = parent.upload_id or parent.id
    files, dropped = _live_files(upload_id, parent.files)
    job = jobs.create(kind="generate", user_id=user["id"], style="custom", style_brief=brief,
                      pages=parent.pages, topic=parent.topic, note=parent.note,
                      files=files, upload_id=upload_id,
                      mock=config.mock_enabled(), model=parent.model or config.PPT_MODEL,
                      ai_images=parent.ai_images)
    return JSONResponse({"id": job.id, "dropped_files": dropped})


@app.get("/api/jobs")
def list_jobs(user: dict = Depends(current_user)) -> list[dict]:
    return [_public_job(j) for j in jobs.all_jobs(user_id=user["id"])]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict = Depends(current_user)):
    job = jobs.get(job_id)
    if job is None or job.user_id != user["id"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    return _public_job(job)


def _resume_checkpoint(job: jobs.Job) -> Path | None:
    if (
        job.kind != "generate"
        or job.status != "failed"
        or not runner_agent.is_transient_api_failure(job.error)
    ):
        return None
    return runner_agent.find_resume_checkpoint(job)


def _can_resume(job: jobs.Job) -> bool:
    return _resume_checkpoint(job) is not None


def _public_job(job: jobs.Job) -> dict:
    payload = job.public()
    payload["can_resume"] = _can_resume(job)
    return payload


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, user: dict = Depends(current_user)) -> JSONResponse:
    """Requeue a failed generation from its persisted project checkpoint."""
    job = jobs.get(job_id)
    if job is None or job.user_id != user["id"]:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    project = _resume_checkpoint(job)
    if project is None:
        return JSONResponse(
            {"error": "该任务不可续跑，或缺少完整检查点"},
            status_code=409,
        )
    resumed = jobs.resume_failed(job.id, project.name)
    if resumed is None:
        return JSONResponse({"error": "任务状态已变化，暂时无法续跑"}, status_code=409)
    try:
        runner._log(job.id, f"续跑已入队:使用检查点 {project.name}")
    except OSError:
        pass
    return JSONResponse({"id": resumed.id, "project": project.name})


def _remove_scoped_tree(root: Path, name: str) -> None:
    """Remove one direct child directory without allowing a path to escape root."""
    root = root.resolve()
    target = (root / name).resolve()
    if root in target.parents:
        shutil.rmtree(target, ignore_errors=True)


def _cleanup_job_artifacts(job: jobs.Job) -> None:
    _remove_scoped_tree(config.OUTPUTS_DIR, job.id)

    log_root = config.LOGS_DIR.resolve()
    log_path = (log_root / f"{job.id}.log").resolve()
    if log_root in log_path.parents:
        log_path.unlink(missing_ok=True)

    projects_root = (config.PPT_MASTER_REPO / "projects").resolve()
    if projects_root.is_dir():
        prefix = f"web_{job.id}"
        for candidate in projects_root.iterdir():
            if candidate.name == prefix or candidate.name.startswith(prefix + "_"):
                _remove_scoped_tree(projects_root, candidate.name)

    snapshot_id = job.upload_id or job.id
    snapshot_in_use = any(
        (other.upload_id or other.id) == snapshot_id
        for other in jobs.all_jobs()
    )
    if not snapshot_in_use:
        _remove_scoped_tree(config.UPLOADS_DIR, snapshot_id)


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, user: dict = Depends(current_user)) -> JSONResponse:
    job = jobs.get(job_id)
    if job is None or job.user_id != user["id"]:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    if job.status not in ("done", "failed"):
        return JSONResponse({"error": "运行中或排队中的任务不能删除"}, status_code=409)
    try:
        removed = jobs.remove(job_id)
    except ValueError:
        return JSONResponse({"error": "任务状态已变化,暂时不能删除"}, status_code=409)
    if removed is None:
        return JSONResponse({"error": "任务不存在"}, status_code=404)
    _cleanup_job_artifacts(removed)
    return JSONResponse({"ok": True})


@app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse)
def get_log(job_id: str, offset: int = 0, user: dict = Depends(current_user)):
    job = jobs.get(job_id)
    if job is None or job.user_id != user["id"]:
        return PlainTextResponse("", status_code=404)
    p = runner.log_path(job_id)
    if not p.exists():
        return ""
    data = p.read_text(encoding="utf-8", errors="replace")
    return data[offset:]


@app.get("/api/jobs/{job_id}/files/{name}")
def download(job_id: str, name: str, user: dict = Depends(current_user)):
    job = jobs.get(job_id)
    if job is None or job.user_id != user["id"]:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = (config.OUTPUTS_DIR / job_id / _safe_name(name)).resolve()
    if not path.is_file() or config.OUTPUTS_DIR.resolve() not in path.parents:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, filename=path.name)


def main() -> None:
    db.init()
    if db.count_users() == 0:
        print("* 尚未初始化账号:请先运行  .venv/bin/python migrate_v2.py  完成建库与迁移")
    jobs.load()
    jobs.start_worker(runner.run_job)
    ready, reason = config.agent_ready()
    mode = "mock 演示模式" if config.mock_enabled() else "真实 API 生成模式"
    print(f"* PPT Master Web 启动:http://127.0.0.1:{config.PORT}{time.strftime('  [%H:%M:%S]')}")
    print(f"* 当前:{mode};模型:{config.PPT_MODEL or '(未设置)'};状态:{reason}")
    uvicorn.run(app, host="127.0.0.1", port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
