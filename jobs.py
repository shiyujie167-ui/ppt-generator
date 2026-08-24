"""任务模型 + 线程池队列 + SQLite 持久化(任务归属 user_id,多账号就绪)。

对外接口与旧版(jobs.json)完全一致:create/get/all_jobs/update/enqueue/
load/start_worker,并提供终态任务删除;内部改为每任务一行落 SQLite,行级原子写。内存中的
Job dataclass 仍是工作对象,其余模块无感知。旧 data/jobs.json 由
migrate_v2.py 一次性导入后仅作备份。
"""
import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, fields, asdict

import config
import db

_LOCK = threading.Lock()
_QUEUE: "queue.Queue[str]" = queue.Queue()
_JOBS: dict[str, "Job"] = {}
_WORKERS: list[threading.Thread] = []


@dataclass
class Job:
    id: str
    created_at: float
    user_id: int = 0        # 归属账号(users.id);0 = 未迁移的历史遗留
    status: str = "queued"  # queued | running | done | failed
    kind: str = "generate"  # generate | recommend | plan
    # 唯一产品模板的内部兼容 ID。历史任务可能仍带 company 等旧值，
    # 真实执行入口会再次归一化；新建 Job 默认直接落到最终模板。
    style: str = "company_free"
    style_brief: str = ""   # 历史兼容字段；唯一模板任务始终留空
    pages: str = "auto"
    topic: str = ""
    note: str = ""
    files: list[str] = field(default_factory=list)
    upload_id: str = ""     # 材料快照目录 id(通常等于任务 id;派生任务复用父任务的)
    recommendations: list[dict] = field(default_factory=list)  # recommend 任务的输出
    plan: dict = field(default_factory=dict)  # plan 任务的输出:{styles, pages, outline}
    plan_feedback: list[str] = field(default_factory=list)  # 规划确认环节用户的历轮调整意见
    outline: list[dict] = field(default_factory=list)  # generate 任务:用户确认的每页内容分布
    mock: bool = False
    model: str = ""
    ai_images: bool = False  # 本任务启用 AI 文生图配图(image_gen.py 链路)
    started_at: float | None = None
    finished_at: float | None = None
    outputs: list[str] = field(default_factory=list)  # 相对 outputs/<id>/ 的文件名
    preview: str = ""  # 预览图文件名(可选)
    cost_usd: float | None = None
    error: str = ""
    resume_existing: bool = False  # 显式从失败任务的磁盘检查点续跑
    resume_project: str = ""       # 服务端绑定的唯一检查点目录名

    @property
    def duration(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.time()
        return int(end - self.started_at)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("resume_existing", None)
        d.pop("resume_project", None)
        d["duration"] = self.duration
        return d


_FIELDS = [f.name for f in fields(Job)]
_JSON_FIELDS = {"files", "recommendations", "plan", "plan_feedback", "outline", "outputs"}
_AFFINITY = {"created_at": "REAL", "started_at": "REAL", "finished_at": "REAL",
             "cost_usd": "REAL", "user_id": "INTEGER", "mock": "INTEGER",
             "ai_images": "INTEGER", "resume_existing": "INTEGER"}
_ready = False


def _ensure() -> None:
    """按 dataclass 字段自建 jobs 表(惰性,便于测试改路径后再首连)。"""
    global _ready
    if _ready:
        return
    cols = ", ".join(
        f"{name} {_AFFINITY.get(name, 'TEXT')}" + (" PRIMARY KEY" if name == "id" else "")
        for name in _FIELDS
    )
    db.run(f"CREATE TABLE IF NOT EXISTS jobs ({cols})")
    # dataclass 新增字段时,老库的既有表用 ALTER 补列(旧行取 NULL,读取时归一化)
    existing = {row["name"] for row in db.fetchall("PRAGMA table_info(jobs)")}
    for name in _FIELDS:
        if name not in existing:
            db.run(f"ALTER TABLE jobs ADD COLUMN {name} {_AFFINITY.get(name, 'TEXT')}")
    db.run("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
    _ready = True


def _save(job: Job) -> None:
    _ensure()
    d = asdict(job)
    row = [json.dumps(d[n], ensure_ascii=False) if n in _JSON_FIELDS else d[n] for n in _FIELDS]
    placeholders = ", ".join("?" for _ in _FIELDS)
    db.run(f"INSERT OR REPLACE INTO jobs ({', '.join(_FIELDS)}) VALUES ({placeholders})", row)


def _from_row(row) -> Job:
    kwargs = {}
    for name in _FIELDS:
        value = row[name]
        if name in _JSON_FIELDS:
            value = json.loads(value) if value else Job.__dataclass_fields__[name].default_factory()
        kwargs[name] = value
    kwargs["mock"] = bool(kwargs["mock"])
    kwargs["ai_images"] = bool(kwargs.get("ai_images") or 0)
    kwargs["resume_existing"] = bool(kwargs.get("resume_existing") or 0)
    kwargs["resume_project"] = str(kwargs.get("resume_project") or "")
    kwargs["user_id"] = int(kwargs["user_id"] or 0)
    return Job(**kwargs)


def load() -> None:
    _ensure()
    for row in db.fetchall("SELECT * FROM jobs"):
        job = _from_row(row)
        if job.status in ("queued", "running"):  # 重启打断的任务自动重新排队从头跑
            job.status = "queued"
            job.started_at = None
            job.finished_at = None
            job.error = ""
            _save(job)
            _QUEUE.put(job.id)
        _JOBS[job.id] = job


def create(*, enqueue: bool = True, **kwargs) -> Job:
    job = Job(id=uuid.uuid4().hex[:12], created_at=time.time(), **kwargs)
    with _LOCK:
        _JOBS[job.id] = job
        _save(job)
    if enqueue:
        _QUEUE.put(job.id)
    return job


def enqueue(job: Job) -> None:
    """Queue a fully prepared job after uploads and metadata are persisted."""
    if job.status != "queued":
        raise ValueError(f"cannot enqueue job in status {job.status}")
    _QUEUE.put(job.id)


def get(job_id: str) -> Job | None:
    return _JOBS.get(job_id)


def all_jobs(user_id: int | None = None) -> list[Job]:
    items = _JOBS.values()
    if user_id is not None:
        items = [j for j in items if j.user_id == user_id]
    return sorted(items, key=lambda j: j.created_at, reverse=True)


def update(job: Job, **kwargs) -> None:
    with _LOCK:
        for k, v in kwargs.items():
            setattr(job, k, v)
        _save(job)


def resume_failed(job_id: str, project_name: str) -> Job | None:
    """Atomically mark and enqueue a failed generation for checkpoint resumption."""
    with _LOCK:
        job = _JOBS.get(job_id)
        expected = rf"web_{re.escape(job_id)}_ppt169_\d{{8}}"
        if not re.fullmatch(expected, project_name):
            return None
        if job is None or job.kind != "generate" or job.status != "failed":
            return None
        changed = db.run(
            "UPDATE jobs SET status = ?, started_at = ?, finished_at = ?, "
            "outputs = ?, preview = ?, cost_usd = ?, error = ?, resume_existing = ?, "
            "resume_project = ? "
            "WHERE id = ? AND kind = ? AND status = ?",
            (
                "queued", None, None, "[]", "", None, "", 1, project_name,
                job.id, "generate", "failed",
            ),
        )
        if changed.rowcount != 1:
            return None
        job.status = "queued"
        job.started_at = None
        job.finished_at = None
        job.outputs = []
        job.preview = ""
        job.cost_usd = None
        job.error = ""
        job.resume_existing = True
        job.resume_project = project_name
        _QUEUE.put(job.id)
        return job


def _claim_queued(job_id: str) -> Job | None:
    """Claim one queue token exactly once, even if duplicate tokens exist."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None or job.status != "queued":
            return None
        job.status = "running"
        job.started_at = time.time()
        _save(job)
        return job


def remove(job_id: str) -> Job | None:
    """Atomically remove a completed/failed job from memory and SQLite."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        if job.status not in ("done", "failed"):
            raise ValueError(f"cannot remove job in status {job.status}")
        db.run("DELETE FROM jobs WHERE id = ?", (job_id,))
        return _JOBS.pop(job_id)


def start_worker(run_fn) -> None:
    """后台线程池:消费队列。JOB_WORKERS=1 即串行;>1 时并行跑多个任务
    (每个任务的工作区/日志/输出彼此隔离,并行安全;代价是 API 并发与费用同时叠加)。"""

    def loop() -> None:
        while True:
            job_id = _QUEUE.get()
            job = _claim_queued(job_id)
            if job is None:
                continue
            try:
                run_fn(job)
            except Exception as exc:  # noqa: BLE001 — worker 不能死
                update(job, status="failed", error=f"内部错误:{exc}", finished_at=time.time())

    for i in range(config.JOB_WORKERS):
        worker = threading.Thread(target=loop, daemon=True, name=f"ppt-worker-{i + 1}")
        worker.start()
        _WORKERS.append(worker)


def workers_healthy() -> bool:
    return len(_WORKERS) == config.JOB_WORKERS and all(worker.is_alive() for worker in _WORKERS)
