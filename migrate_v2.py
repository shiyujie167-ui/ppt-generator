"""一次性迁移 + 账号管理命令(可重复执行,幂等)。

迁移:建 SQLite(data/app.db)→ 创建 admin 账号 → 导入 data/jobs.json 的
历史任务(归属 admin)→ 扫描 data/uploads/ 各会话目录,把原始材料按内容
哈希去重并入 data/library/<admin_id>/(硬链接,解析好的 .md 一并带走,
兼容旧 stem 命名),登记进 materials 表。旧 jobs.json 与 uploads/ 目录
原样保留作备份(历史任务的快照目录仍被引用)。

用法:
  .venv/bin/python migrate_v2.py                     # 迁移(默认建 admin/admin666)
  .venv/bin/python migrate_v2.py adduser 名 密码 [admin]   # 建账号(开放同事用)
  .venv/bin/python migrate_v2.py disable 名 / enable 名    # 停用/恢复账号
  .venv/bin/python migrate_v2.py passwd 名 新密码           # 改密并踢下线
  .venv/bin/python migrate_v2.py schema                     # 只确保表结构,不导入历史数据

生产环境首次迁移可用 PPT_ADMIN_USERNAME/PPT_ADMIN_PASSWORD 覆盖默认管理员。
"""
import json
import os
import shutil
import sys
from pathlib import Path

import config
import db
import jobs
import qa

ADMIN_USER = "admin"
ADMIN_PASS = "admin666"


def _ensure_admin() -> int:
    username = os.environ.get("PPT_ADMIN_USERNAME", "").strip() or ADMIN_USER
    password = os.environ.get("PPT_ADMIN_PASSWORD", "") or ADMIN_PASS
    user = db.get_user(username)
    if user is not None:
        return int(user["id"])
    uid = db.create_user(username, password, role="admin")
    print(f"+ 已创建管理员账号 {username}(id={uid})")
    return uid


def _import_jobs(admin_id: int) -> None:
    if not config.JOBS_FILE.exists():
        print("- 无 jobs.json,跳过任务导入")
        return
    try:
        data = json.loads(config.JOBS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"! jobs.json 无法解析,跳过任务导入:{exc}")
        return
    existing = {row["id"] for row in db.fetchall("SELECT id FROM jobs")} if _jobs_table_exists() else set()
    imported = 0
    for jid, jd in data.items():
        if jid in existing:
            continue
        fields = {k: v for k, v in jd.items() if k in jobs.Job.__dataclass_fields__}
        fields.pop("user_id", None)
        job = jobs.Job(user_id=admin_id, **fields)
        if job.status in ("queued", "running"):  # 存档里的中断任务不复活
            job.status = "failed"
            job.error = job.error or "迁移时处于中断状态,未自动重跑"
        jobs._save(job)  # noqa: SLF001 — 迁移脚本走内部通道
        imported += 1
    print(f"+ 任务导入:新增 {imported} 条(已存在 {len(existing)} 条跳过);jobs.json 保留作备份")


def _jobs_table_exists() -> bool:
    return db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'") is not None


def _originals_in(directory: Path) -> list[str]:
    """目录里的「原始材料」清单:优先会话 manifest,其次任务记录的 files。"""
    manifest = directory / "_session.json"
    if manifest.is_file():
        try:
            return [n for n in json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
                    if (directory / n).is_file()]
        except (json.JSONDecodeError, OSError):
            return []
    job = jobs.get(directory.name)
    if job is not None:
        return [n for n in job.files if (directory / n).is_file()]
    return []


def _link_or_copy(source: Path, target: Path) -> None:
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


def _artifact(directory: Path, name: str, suffix: str, originals: set[str]) -> Path | None:
    """找该材料的解析衍生物:新式 <name><suffix>,旧式 <stem><suffix>(不能撞原始材料)。"""
    new_style = directory / (name + suffix)
    if new_style.is_file():
        return new_style
    old_style = directory / (Path(name).stem + suffix)
    if old_style.is_file() and old_style.name not in originals:
        return old_style
    return None


def _unique_name(lib: Path, name: str, taken: dict[str, str], sha: str) -> str | None:
    """同名同内容→None(跳过);同名异内容→加 (2)/(3) 后缀;新名→原名。"""
    candidate = name
    serial = 2
    while candidate in taken:
        if taken[candidate] == sha:
            return None
        base_stem, base_ext = Path(name).stem, Path(name).suffix
        candidate = f"{base_stem}({serial}){base_ext}"
        serial += 1
    return candidate


def _import_materials(admin_id: int) -> None:
    lib = config.LIBRARY_DIR / str(admin_id)
    lib.mkdir(parents=True, exist_ok=True)
    taken: dict[str, str] = {  # 库中已有:name -> sha256
        row["name"]: row["sha256"]
        for row in db.fetchall("SELECT name, sha256 FROM materials WHERE user_id = ?", (admin_id,))
    }
    seen_hashes = set(taken.values())
    added = dup = renamed = 0
    dirs = sorted((d for d in config.UPLOADS_DIR.iterdir() if d.is_dir()),
                  key=lambda d: d.stat().st_mtime)
    registered: list[str] = []
    for directory in dirs:
        originals = set(_originals_in(directory))
        for name in sorted(originals):
            source = directory / name
            sha = qa.sha256_file(source)
            if sha in seen_hashes:
                dup += 1
                continue
            final = _unique_name(lib, name, taken, sha)
            if final is None:
                dup += 1
                continue
            if final != name:
                renamed += 1
            _link_or_copy(source, lib / final)
            for suffix in (".md", ".conversion_profile.json"):
                artifact = _artifact(directory, name, suffix, originals)
                if artifact is not None:
                    _link_or_copy(artifact, lib / (final + suffix))
            taken[final] = sha
            seen_hashes.add(sha)
            registered.append(final)
            added += 1
    qa.register(admin_id, registered)  # 计状态并登记;缺 .md 的标 pending,服务启动后自动补转
    print(f"+ 材料入库:新增 {added} 份(内容重复跳过 {dup},同名异内容改名 {renamed});"
          f"库目录 {lib}")


def migrate() -> None:
    db.init()
    jobs._ensure()  # noqa: SLF001
    admin_id = _ensure_admin()
    jobs.load()  # 供 _originals_in 查任务记录;首轮为空表也安全
    _import_jobs(admin_id)
    # 重新载入(导入的任务进内存,后续材料扫描能查到 files)
    jobs._JOBS.clear()  # noqa: SLF001
    jobs.load()
    _import_materials(admin_id)
    users = db.count_users()
    mats = db.fetchone("SELECT COUNT(*) AS n FROM materials")["n"]
    total_jobs = db.fetchone("SELECT COUNT(*) AS n FROM jobs")["n"]
    print(f"= 迁移完成:账号 {users} 个,材料 {mats} 份,任务 {total_jobs} 条")


def main() -> int:
    args = sys.argv[1:]
    if not args:
        migrate()
        return 0
    cmd = args[0]
    if cmd == "schema" and len(args) == 1:
        db.init()
        jobs._ensure()  # noqa: SLF001 - deployment-safe schema initialization
        print("+ 数据库表结构已就绪")
        return 0
    if cmd == "adduser" and len(args) >= 3:
        role = "admin" if (len(args) > 3 and args[3] == "admin") else "user"
        if db.get_user(args[1]) is not None:
            print(f"! 账号已存在:{args[1]}")
            return 1
        db.init()
        uid = db.create_user(args[1], args[2], role=role)
        print(f"+ 已创建账号 {args[1]}(id={uid},role={role})")
        return 0
    if cmd in ("disable", "enable") and len(args) >= 2:
        db.init()
        ok = db.set_disabled(args[1], cmd == "disable")
        print(("+ 已停用 " if cmd == "disable" else "+ 已恢复 ") + args[1] if ok else f"! 账号不存在:{args[1]}")
        return 0 if ok else 1
    if cmd == "passwd" and len(args) >= 3:
        db.init()
        ok = db.set_password(args[1], args[2])
        print(f"+ 已修改密码并注销现有会话:{args[1]}" if ok else f"! 账号不存在:{args[1]}")
        return 0 if ok else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
