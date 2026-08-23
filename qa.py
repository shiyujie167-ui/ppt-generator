"""用户材料库 + 基于材料的轻量问答。

材料归属账号(materials 表),文件本体在 data/library/<user_id>/:
原件 + 解析出的 <原名>.md(引擎 source_to_md 后台转换,写在库目录内)。
问答是一次同步模型调用(无工具循环、不进任务队列),秒级返回。
生成任务开跑前把勾选材料硬链接快照进任务目录,复用同一份解析结果。
"""
import hashlib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import certifi

import config
import db

_LOCK = threading.Lock()
_CONVERTING: dict[tuple[int, str], str] = {}  # (user_id, name) -> 正在转换的源文件 sha256

CONVERT_TIMEOUT = 180
TEXT_EXTS = {".md", ".txt", ".csv"}          # 直接可读,无需转换
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}       # 不参与文字问答(生成阶段由引擎分析)
MAX_FILE_CHARS = 12_000                       # 单份材料进入问答上下文的上限
MAX_TOTAL_CHARS = 48_000                      # 全部材料合计上限
MIN_BLOCK_CHARS = 500                         # 剩余预算低于此值就整份不纳入(避免只塞进几个字误导模型)
MAX_HISTORY = 8                               # 携带的最近问答轮数

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _dir(user_id: int) -> Path:
    return config.LIBRARY_DIR / str(user_id)


def _md_path(user_id: int, name: str) -> Path:
    # 追加 .md 而非替换后缀:report.pdf → report.pdf.md。若沿用替换后缀,
    # 同时上传 report.md 和 report.pdf 时会同路径——pdf 被误判已转换(问答
    # 读到的是那份 .md 的内容),删除 pdf 时还会把用户的 report.md 一并删掉。
    return _dir(user_id) / (name + ".md")


def _artifact_paths(directory: Path, name: str) -> tuple[Path, Path, Path]:
    return (
        directory / (name + ".md"),
        directory / (name + ".conversion_profile.json"),
        directory / (name + "_files"),
    )


def _remove_artifacts(directory: Path, name: str) -> None:
    markdown, profile, assets = _artifact_paths(directory, name)
    markdown.unlink(missing_ok=True)
    profile.unlink(missing_ok=True)
    shutil.rmtree(assets, ignore_errors=True)


def _publish_artifacts(work_dir: Path, directory: Path, name: str) -> None:
    source_paths = _artifact_paths(work_dir, name)
    target_paths = _artifact_paths(directory, name)
    if not source_paths[0].is_file():
        raise FileNotFoundError(f"转换结果缺少 Markdown:{source_paths[0]}")

    os.replace(source_paths[0], target_paths[0])
    target_paths[1].unlink(missing_ok=True)
    if source_paths[1].is_file():
        os.replace(source_paths[1], target_paths[1])
    shutil.rmtree(target_paths[2], ignore_errors=True)
    if source_paths[2].is_dir():
        os.replace(source_paths[2], target_paths[2])


def _upsert_material(user_id: int, name: str, size: int, status: str,
                     source_sha: str, now: float) -> None:
    db.run(
        "INSERT INTO materials (user_id, name, size, status, sha256, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, name) DO UPDATE SET size = excluded.size, "
        "status = excluded.status, sha256 = excluded.sha256",
        (user_id, name, size, status, source_sha, now),
    )


def store_uploaded_source(user_id: int, name: str, temp_path: Path) -> None:
    """Commit an upload and its cache state as one versioned filesystem operation."""
    directory = _dir(user_id)
    target = directory / name
    with _LOCK:
        source_sha = sha256_file(temp_path)
        previous = db.fetchone(
            "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        changed = previous is not None and previous["sha256"] != source_sha
        if changed:
            # Remove cache names before publishing the new source. Historical task
            # snapshots retain their linked inodes, while concurrent snapshots can
            # only observe old-source/no-cache or new-source/no-cache.
            _remove_artifacts(directory, name)
        os.replace(temp_path, target)
        status = _initial_status(user_id, name)
        if previous is not None and previous["sha256"] == source_sha and previous["status"] == "pending":
            status = "pending"
        _upsert_material(user_id, name, target.stat().st_size, status, source_sha, time.time())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _initial_status(user_id: int, name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXTS:
        return "skip"
    if ext in TEXT_EXTS or _md_path(user_id, name).is_file():
        return "done"
    return "pending"


def _convert_worker(user_id: int, name: str, expected_sha: str) -> None:
    src = _dir(user_id) / name
    script = config.PPT_MASTER_REPO / "skills" / "ppt-master" / "scripts" / "source_to_md.py"
    env = dict(os.environ)
    for key in list(env):  # 与执行器同款:子进程不带任何密钥
        upper = key.upper()
        if any(marker in upper for marker in
               ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET", "PASSWORD")):
            env.pop(key, None)
    # 不传 --images,走各格式默认行为(PDF=filtered 提取内嵌图片):这份转换会被
    # 正式生成任务复用,砍掉图片会让 Agent 拿不到 PDF 里的图片素材。
    ok = False
    work_path: Path | None = None
    try:
        work_path = Path(tempfile.mkdtemp(prefix=".convert-", dir=_dir(user_id)))
        snapshot = work_path / name
        try:
            os.link(src, snapshot)
        except OSError:
            shutil.copy2(src, snapshot)
        if sha256_file(snapshot) != expected_sha:
            return
        out = work_path / (name + ".md")
        cmd = [os.environ.get("PPT_PYTHON_BIN", "python3"), str(script), str(snapshot), "-o", str(out)]
        completed = subprocess.run(
            cmd, cwd=str(config.PPT_MASTER_REPO), env=env,
            capture_output=True, timeout=CONVERT_TIMEOUT,
        )
        ok = completed.returncode == 0 and out.is_file()
    except Exception:
        ok = False
    finally:
        restart_latest = False
        with _LOCK:
            row = db.fetchone(
                "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            try:
                source_matches = src.is_file() and sha256_file(src) == expected_sha
            except OSError:
                source_matches = False
            current = bool(
                row is not None
                and row["sha256"] == expected_sha
                and row["status"] == "pending"
                and source_matches
            )
            if current:
                status = "failed"
                if ok and work_path is not None:
                    try:
                        _publish_artifacts(work_path, _dir(user_id), name)
                        status = "done"
                    except OSError:
                        _remove_artifacts(_dir(user_id), name)
                db.run(
                    "UPDATE materials SET status = ? "
                    "WHERE user_id = ? AND name = ? AND sha256 = ? AND status = 'pending'",
                    (status, user_id, name, expected_sha),
                )
            if _CONVERTING.get((user_id, name)) == expected_sha:
                _CONVERTING.pop((user_id, name), None)
            latest = db.fetchone(
                "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            restart_latest = bool(
                latest is not None
                and latest["status"] == "pending"
                and latest["sha256"] != expected_sha
            )
        if work_path is not None:
            shutil.rmtree(work_path, ignore_errors=True)
        if restart_latest:
            _kick_pending(user_id)


def _kick_pending(user_id: int) -> None:
    """为 pending 材料起转换线程;_CONVERTING 去重,重复触发不会重复转换。"""
    rows = db.fetchall(
        "SELECT name, sha256 FROM materials WHERE user_id = ? AND status = 'pending'",
        (user_id,),
    )
    with _LOCK:
        todo = []
        for row in rows:
            key = (user_id, row["name"])
            if key in _CONVERTING:
                continue
            latest = db.fetchone(
                "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
                key,
            )
            if latest is None or latest["status"] != "pending" or latest["sha256"] != row["sha256"]:
                continue
            _CONVERTING[key] = row["sha256"]
            todo.append((row["name"], row["sha256"]))
    for name, expected_sha in todo:
        threading.Thread(
            target=_convert_worker, args=(user_id, name, expected_sha), daemon=True
        ).start()


def register(user_id: int, names: list[str]) -> None:
    """把(可能新上传的)文件登记进材料库并转换缺失的。同名重传覆盖原条目。"""
    directory = _dir(user_id)
    now = time.time()
    for name in names:
        path = directory / name
        if not path.is_file():
            continue
        with _LOCK:
            source_sha = sha256_file(path)
            previous = db.fetchone(
                "SELECT sha256, status FROM materials WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            changed = previous is not None and previous["sha256"] != source_sha
            if changed:
                _remove_artifacts(directory, name)
            status = _initial_status(user_id, name)
            if previous is not None and previous["sha256"] == source_sha and previous["status"] == "pending":
                status = "pending"
            _upsert_material(user_id, name, path.stat().st_size, status, source_sha, now)
    _kick_pending(user_id)


def remove_file(user_id: int, name: str) -> bool:
    with _LOCK:
        cur = db.run("DELETE FROM materials WHERE user_id = ? AND name = ?", (user_id, name))
        if cur.rowcount == 0:
            return False
        (_dir(user_id) / name).unlink(missing_ok=True)
        _remove_artifacts(_dir(user_id), name)
    return True


def _rows(user_id: int) -> list:
    return db.fetchall(
        "SELECT name, size, status FROM materials WHERE user_id = ? ORDER BY created_at, id",
        (user_id,))


def status(user_id: int) -> dict:
    """材料库清单(前端左栏数据源)。有 pending 时顺手补转(重启后自愈)。"""
    _kick_pending(user_id)
    directory = _dir(user_id)
    out = []
    for row in _rows(user_id):
        try:
            size = (directory / row["name"]).stat().st_size
        except OSError:
            size = row["size"]
        out.append({"name": row["name"], "size": size, "status": row["status"]})
    return {"files": out}


# ── 问答 ─────────────────────────────────────────────────────────


def _gather_context(user_id: int, only: set[str] | None = None) -> tuple[list[tuple[str, str]], int, list[str]]:
    """收集已就绪材料文本;only 非空时只取勾选的那几份。
    返回 (材料列表, 转换中数量, 因合计超限整份未纳入的文件名)。"""
    blocks: list[tuple[str, str]] = []
    omitted: list[str] = []
    pending = 0
    total = 0
    for row in _rows(user_id):
        name, st = row["name"], row["status"]
        if only is not None and name not in only:
            continue
        if st == "pending":
            pending += 1
            continue
        if st != "done":
            continue
        path = _dir(user_id) / name
        if Path(name).suffix.lower() not in TEXT_EXTS:
            path = _md_path(user_id, name)
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + "\n…[该材料过长,已截断]"
        remaining = MAX_TOTAL_CHARS - total
        if remaining < MIN_BLOCK_CHARS:
            # 预算耗尽:整份不纳入并如实上报,不再塞只剩截断标记的空块误导模型
            omitted.append(name)
            continue
        if len(text) > remaining:
            text = text[:remaining] + "\n…[材料合计过长,已截断]"
        blocks.append((name, text))
        total += len(text)
    return blocks, pending, omitted


def _build_prompt(blocks: list[tuple[str, str]], history: list[dict], question: str,
                  omitted: list[str] | None = None) -> str:
    parts = [
        "你是「PPT 生成台」的材料问答助手。用户上传了做 PPT 的原始材料,正在生成前"
        "与你确认材料内容、梳理讲述思路。要求:只依据下方材料回答,材料没有的信息"
        "明确说不知道,不要编造;回答用中文,直接、简洁,适合快速阅读;涉及数字、"
        "报价、日期等关键信息时逐条列出。",
        "",
    ]
    for name, text in blocks:
        parts.append(f"【材料:{name}】\n{text}\n")
    if omitted:
        parts.append(
            "【注意】以下材料因合计长度限制本次完全未提供,你没有看过它们,"
            "不要臆测其内容;用户问及时请说明未纳入并建议单独勾选后再问:"
            + "、".join(omitted) + "\n"
        )
    if history:
        parts.append("【此前对话】")
        for turn in history[-MAX_HISTORY:]:
            role = "用户" if turn.get("role") == "user" else "助手"
            parts.append(f"{role}:{str(turn.get('text', ''))[:2000]}")
        parts.append("")
    parts.append(f"【用户问题】\n{question}")
    return "\n".join(parts)


def _post(url: str, api_key: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "ppt-web/1.0",
        },
        method="POST",
    )
    timeout = min(config.PPT_API_REQUEST_TIMEOUT_SECONDS, 180)
    last: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CTX) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:400]
            if (exc.code == 429 or exc.code >= 500) and attempt == 0:
                last = exc
                time.sleep(2)
                continue
            raise RuntimeError(f"API {exc.code}:{body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt == 0:
                continue
            raise RuntimeError(f"API 连接失败:{exc}") from exc
    raise RuntimeError(f"API 连接失败:{last}")


def _extract_responses_text(data: dict) -> str:
    parts = []
    for item in data.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    parts.append(c.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def chat(user_id: int, question: str, history: list[dict], model: str = "",
         only: set[str] | None = None) -> dict[str, Any]:
    if config.mock_enabled():
        return {"answer": "(演示模式)真实部署后,这里会基于你上传的材料回答问题。", "model": "mock"}
    blocks, pending, omitted = _gather_context(user_id, only)
    if not blocks:
        if pending:
            return {"error": f"还有 {pending} 份材料在解析中,请稍等几秒再问。"}
        return {"error": "勾选的材料里没有可问答的文字内容(图片不参与问答)。"}

    conf = config.model_conf(model)
    prompt = _build_prompt(blocks, history, question, omitted)
    if conf.get("wire") == "chat":
        payload = {"model": conf["model"], "messages": [{"role": "user", "content": prompt}],
                   "max_tokens": 2048}
        data = _post(config.wire_url(conf), conf.get("api_key") or config.PPT_API_KEY, payload)
        try:
            answer = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return {"error": "模型返回格式异常"}
        usage = data.get("usage") or {}
        tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
    else:
        payload = {"model": conf["model"], "input": prompt, "max_output_tokens": 2048, "store": False}
        data = _post(config.wire_url(conf), conf.get("api_key") or config.PPT_API_KEY, payload)
        answer = _extract_responses_text(data)
        usage = data.get("usage") or {}
        tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if not answer:
        return {"error": "模型没有返回内容,请重试"}
    notes = []
    if pending:
        notes.append(f"还有 {pending} 份材料在解析中,本回答未包含它们")
    if omitted:
        notes.append(f"材料合计过长,本回答未包含:{'、'.join(omitted)}(可只勾选它们单独提问)")
    note = f"({';'.join(notes)})" if notes else ""
    return {"answer": answer + ("\n\n" + note if note else ""),
            "model": conf["model"], "tokens_in": tokens_in, "tokens_out": tokens_out}
