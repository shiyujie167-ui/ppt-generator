"""任务执行器:mock 演示或独立 Responses API Agent。"""
import shutil
import time
from pathlib import Path

import config
import jobs
import prompts


def log_path(job_id: str) -> Path:
    return config.LOGS_DIR / f"{job_id}.log"


def _log(job_id: str, text: str) -> None:
    with log_path(job_id).open("a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {text}\n")


def run_job(job: jobs.Job) -> None:
    # 队列里可能还有迁移前的旧任务；执行前把它们统一到唯一 MT 模板，
    # 这样 mock、规划、推荐和真实生成四条路径的任务状态都一致。
    canonical_style = prompts.normalize_style(getattr(job, "style", "")) or prompts.FINAL_STYLE
    if getattr(job, "style", "") != canonical_style or getattr(job, "style_brief", ""):
        jobs.update(job, style=canonical_style, style_brief="")
    else:
        job.style = canonical_style
        job.style_brief = ""
    kind = {"recommend": "风格推荐", "plan": "内容规划"}.get(job.kind, "生成")
    _log(job.id, f"任务开始({kind};{'mock 演示' if job.mock else '真实运行'};模型:{job.model or '默认'})")
    if job.mock:
        if job.kind == "recommend":
            _run_mock_recommend(job)
        elif job.kind == "plan":
            _run_mock_plan(job)
        else:
            _run_mock(job)
    else:
        _run_real(job)


# ── mock:没有 key 时的全流程演示 ─────────────────────────────────────────

_MOCK_STAGES = [
    ("读取并转换源材料(source_to_md)", 3),
    ("初始化项目工作区", 2),
    ("策略师阶段:三阶段设计决策(已委托,自动确认)", 5),
    ("逐页生成 SVG(10 页)", 8),
    ("SVG 质量检查", 3),
    ("导出原生 PPTX + postflight 审计", 4),
]


def _run_mock(job: jobs.Job) -> None:
    for stage, seconds in _MOCK_STAGES:
        _log(job.id, stage)
        time.sleep(seconds)
    out_dir = config.OUTPUTS_DIR / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    sample = config.SAMPLE_DIR / "sample_company_template.pptx"
    montage = config.SAMPLE_DIR / "sample_montage.png"
    outputs, preview = [], ""
    if sample.exists():
        name = "演示样例-非本次生成.pptx"
        shutil.copy2(sample, out_dir / name)
        outputs.append(name)
    if montage.exists():
        preview = "预览.png"
        shutil.copy2(montage, out_dir / preview)
    _log(job.id, "完成。注意:演示模式返回的是预置样例(上午的三菱评审 PPT),不是用你材料生成的;配好 .env 的 key 后才是真实生成")
    jobs.update(job, status="done", outputs=outputs, preview=preview, cost_usd=0.0, finished_at=time.time())


def _run_mock_recommend(job: jobs.Job) -> None:
    _log(job.id, "浏览材料,分析内容气质与受众…")
    time.sleep(4)
    _log(job.id, "确认 MT 公司最终模板(唯一模板;演示数据)")
    time.sleep(2)
    jobs.update(job, status="done",
                recommendations=[dict(prompts.MOCK_RECOMMENDATIONS[0])],
                cost_usd=0.0, finished_at=time.time())
    _log(job.id, "完成:唯一 MT 公司最终模板已就绪")


def _run_mock_plan(job: jobs.Job) -> None:
    if job.plan_feedback:
        _log(job.id, f"按调整意见修订大纲(演示数据,意见:{job.plan_feedback[-1][:60]})")
        time.sleep(3)
        plan = dict(job.plan or prompts.MOCK_PLAN)
    else:
        _log(job.id, "浏览材料,分析内容结构与受众…")
        time.sleep(4)
        _log(job.id, "规划唯一 MT 公司模板与每页内容分布(演示数据;真实模式会按材料定制)")
        time.sleep(2)
        plan = dict(prompts.MOCK_PLAN)
    # 历史规划结果可能携带旧的多风格列表；修订/回放时也必须只保留这一项。
    plan["styles"] = [dict(prompts.MOCK_RECOMMENDATIONS[0])]
    if isinstance(plan.get("outline"), list):
        plan["outline"] = [dict(page) for page in plan["outline"]]
        plan["pages"] = len(plan["outline"])
    jobs.update(job, status="done", plan=plan, cost_usd=0.0, finished_at=time.time())
    _log(job.id, "完成:请在任务卡片里确认大纲(可直接编辑或让 AI 调整),然后开始生成")


# ── 真实运行:由 runner_agent.py 提供 ────────────────────────────────────


def _run_real(job: jobs.Job) -> None:
    ready, reason = config.agent_ready()
    if not ready:
        msg = f"真实 API 执行器未就绪:{reason}"
        _log(job.id, msg)
        jobs.update(job, status="failed", error=msg, finished_at=time.time())
        return
    try:
        import runner_agent  # noqa: PLC0415
    except ImportError:
        msg = "无法加载 runner_agent.py(真实 API 执行模块)"
        _log(job.id, msg)
        jobs.update(job, status="failed", error=msg, finished_at=time.time())
        return
    if job.kind in ("recommend", "plan"):
        fn = getattr(runner_agent, job.kind, None)
        if fn is None:
            msg = f"runner_agent.py 缺少 {job.kind}(),真实 API 执行器版本不完整"
            _log(job.id, msg)
            jobs.update(job, status="failed", error=msg, finished_at=time.time())
            return
        fn(job, _log)
    else:
        runner_agent.run(job, _log)


def collect_outputs(job: jobs.Job, since: float, project_prefix: str = "") -> tuple[list[str], str]:
    """把 since 之后新生成的 projects/*/exports/*.pptx(和质检拼图)拷到 outputs/<id>/。"""
    out_dir = config.OUTPUTS_DIR / job.id
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    preview = ""
    projects_dir = config.PPT_MASTER_REPO / "projects"
    project_glob = f"{project_prefix}*/exports/*.pptx" if project_prefix else "*/exports/*.pptx"
    candidates = [
        pptx
        for pptx in projects_dir.glob(project_glob)
        if pptx.stat().st_mtime >= since
    ]
    if job.style in ("company", "company_free"):
        candidates.sort(key=lambda path: (not path.stem.endswith("_company"), str(path)))
    else:
        candidates.sort()
    for pptx in candidates:
        shutil.copy2(pptx, out_dir / pptx.name)
        outputs.append(pptx.name)
        if job.style in ("company", "company_free") and getattr(job, "ai_images", False):
            cover = next(iter(sorted((pptx.parent.parent / "svg_final").glob("*.svg"))), None)
            if cover is not None and not preview:
                preview = "封面预览.svg"
                shutil.copy2(cover, out_dir / preview)
        montage = pptx.parent.parent / "validation" / "final_montage.png"
        if montage.exists() and not preview:
            preview = "预览.png"
            shutil.copy2(montage, out_dir / preview)
        if not preview:
            cover = next(iter(sorted((pptx.parent.parent / "svg_final").glob("*.svg"))), None)
            if cover is not None:
                preview = "封面预览.svg"
                shutil.copy2(cover, out_dir / preview)
    return outputs, preview
