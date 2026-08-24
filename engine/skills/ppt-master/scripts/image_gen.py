#!/usr/bin/env python3
"""AI image generation CLI (Path A backend for image-generator.md).

Implements the manifest contract from references/image-generator.md against an
OpenAI-compatible ``/v1/images/generations`` endpoint (``IMAGE_BACKEND=openai``,
covering official OpenAI and relay gateways that speak the same protocol).

Modes
  image_gen.py --manifest project/images/image_prompts.json [-o DIR]
  image_gen.py --render-md project/images/image_prompts.json
  image_gen.py --list-backends
  image_gen.py "prompt" --filename hero.png --aspect_ratio 16:9 -o DIR

Environment (process env wins; missing values filled from the first .env found
in: cwd, skill dir, clone repo root, ~/.ppt-master/.env):
  IMAGE_BACKEND            required; this build supports "openai" only
  OPENAI_API_KEY           required for the openai backend
  OPENAI_BASE_URL          optional; default https://api.openai.com
  OPENAI_MODEL             optional default model (per-item model wins)
  OPENAI_SIZE_PRESET       auto | gpt-image | gpt-image-2 | legacy | dall-e-2
  OPENAI_RESPONSE_FORMAT   auto | b64_json | url | omit
  OPENAI_OUTPUT_FORMAT     auto | png | jpeg | webp
  OPENAI_QUALITY           auto | omit | low | medium | high | standard | hd
  IMAGE_CONCURRENCY        manifest-mode default concurrency (CLI wins)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
REPO_ROOT = SKILL_DIR.parent.parent

REQUEST_TIMEOUT = 300
ITEM_MAX_ATTEMPTS = 3
SIZE_NAMES = {"512px", "1K", "2K", "4K"}
OUTPUT_FORMATS = {"png", "jpeg", "webp"}
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8", b"RIFF", b"GIF8")
PAGE_ROLES = {"local", "hero_page"}
TEXT_POLICIES = {"none", "embedded"}
STATUSES = {"Pending", "Generated", "Failed", "Needs-Manual"}
LEGACY_TYPES = {
    "background": ("hero_page", None),
    "hero": ("hero_page", None),
    "portrait": ("local", None),
    "typography": ("hero_page", "embedded"),
}

_manifest_lock = threading.Lock()
_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


# ── env / config ─────────────────────────────────────────────────


def _load_env_files() -> None:
    """Fill os.environ gaps from the first readable .env (process env wins)."""
    candidates = [
        Path.cwd() / ".env",
        SKILL_DIR / ".env",
        REPO_ROOT / ".env",
        Path.home() / ".ppt-master" / ".env",
    ]
    for candidate in candidates:
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
        return


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi  # noqa: PLC0415 - optional dependency

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


class Backend:
    """OpenAI-compatible images endpoint."""

    def __init__(self, model_override: str = ""):
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        base = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.openai.com"
        base = base.rstrip("/")
        for endpoint in ("/chat/completions", "/responses"):
            if base.endswith(endpoint):
                base = base[:-len(endpoint)].rstrip("/")
                break
        if base.endswith("/images/generations"):
            self.url = base
        elif base.endswith("/v1"):
            self.url = f"{base}/images/generations"
        else:
            self.url = f"{base}/v1/images/generations"
        self.model = model_override or os.environ.get("OPENAI_MODEL", "").strip()
        self.size_preset = os.environ.get("OPENAI_SIZE_PRESET", "auto").strip().lower() or "auto"
        self.response_format = os.environ.get("OPENAI_RESPONSE_FORMAT", "auto").strip().lower() or "auto"
        self.output_format = os.environ.get("OPENAI_OUTPUT_FORMAT", "auto").strip().lower() or "auto"
        self.quality = os.environ.get("OPENAI_QUALITY", "auto").strip().lower() or "auto"
        self.ssl_context = _ssl_context()

    def validate(self) -> str:
        if not self.api_key:
            return "OPENAI_API_KEY 未配置"
        if not self.model:
            return "OPENAI_MODEL 未配置且未传 --model"
        return ""

    # -- size mapping -------------------------------------------------
    def resolve_size(self, aspect_ratio: str, image_size: str) -> str:
        """Map aspect ratio to a concrete WxH for the active preset.

        Unknown gateways may reject any fixed vocabulary, so callers fall back
        through resolve_size -> "auto" -> omit on size-related 400s.
        """
        wide, tall, square = "1536x1024", "1024x1536", "1024x1024"
        if self.size_preset == "legacy":  # dall-e-3 vocabulary
            wide, tall = "1792x1024", "1024x1792"
        elif self.size_preset == "dall-e-2":
            side = "512x512" if image_size == "512px" else "1024x1024"
            return side
        try:
            w_str, h_str = (aspect_ratio or "1:1").split(":", 1)
            ratio = float(w_str) / float(h_str)
        except (ValueError, ZeroDivisionError):
            ratio = 1.0
        if ratio > 1.15:
            return wide
        if ratio < 0.87:
            return tall
        return square

    # -- one request --------------------------------------------------
    def generate(self, prompt: str, aspect_ratio: str, image_size: str, model: str = "") -> bytes:
        """Generate one image; returns raw image bytes. Raises GenError."""
        payload: dict = {
            "model": model or self.model,
            "prompt": prompt,
            "n": 1,
            "size": self.resolve_size(aspect_ratio, image_size),
        }
        if self.quality not in ("auto", "omit", ""):
            payload["quality"] = self.quality
        if self.response_format in ("b64_json", "url"):
            payload["response_format"] = self.response_format
        if self.output_format in OUTPUT_FORMATS:
            payload["output_format"] = self.output_format

        # Compatibility ladder: size -> "auto" -> omit; then drop optional fields.
        attempts = [dict(payload)]
        step = dict(payload)
        step["size"] = "auto"
        attempts.append(step)
        step = {k: v for k, v in payload.items() if k != "size"}
        attempts.append(step)

        last_error: GenError | None = None
        for index, body in enumerate(attempts):
            try:
                return self._post(body)
            except GenError as exc:
                last_error = exc
                lowered = exc.message.lower()
                if exc.retryable:
                    raise  # 429/5xx/network: caller owns backoff, not the ladder
                if index == 0 and "size" in lowered:
                    continue
                if index == 1 and ("size" in lowered or "auto" in lowered):
                    continue
                if "response_format" in lowered and "response_format" in body:
                    trimmed = {k: v for k, v in body.items() if k != "response_format"}
                    return self._post(trimmed)
                if "output_format" in lowered and "output_format" in body:
                    trimmed = {k: v for k, v in body.items() if k != "output_format"}
                    return self._post(trimmed)
                if "quality" in lowered and "quality" in body:
                    trimmed = {k: v for k, v in body.items() if k != "quality"}
                    return self._post(trimmed)
                raise
        raise last_error  # pragma: no cover - ladder always raises or returns

    def _post(self, body: dict) -> bytes:
        raw = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=raw,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "ppt-web/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT, context=self.ssl_context) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:600]
            except OSError:
                pass
            retryable = exc.code == 429 or exc.code >= 500
            raise GenError(f"HTTP {exc.code}: {detail}", retryable=retryable, rate_limited=exc.code == 429) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GenError(f"网络错误: {exc}", retryable=True) from exc

        items = data.get("data") or []
        if not items:
            raise GenError(f"响应缺少 data: {str(data)[:300]}")
        entry = items[0]
        if entry.get("b64_json"):
            blob = base64.b64decode(entry["b64_json"])
        elif entry.get("url"):
            blob = self._download(entry["url"])
        else:
            raise GenError(f"响应缺少 b64_json/url: {str(entry)[:300]}")
        if not blob:
            raise GenError("返回图片为空")
        if not blob.startswith(IMAGE_MAGIC):
            raise GenError(f"返回内容不是已知图片格式(前 16 字节 {blob[:16]!r})")
        return blob

    def _download(self, url: str) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT, context=self.ssl_context) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise GenError(f"下载图片失败: {exc}", retryable=True) from exc


class GenError(Exception):
    def __init__(self, message: str, retryable: bool = False, rate_limited: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.rate_limited = rate_limited


# ── manifest handling ────────────────────────────────────────────


def _read_manifest(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"无法读取清单 {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"清单 JSON 无法解析 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"清单根节点必须是 JSON 对象: {path}")
    if not isinstance(data.get("items"), list):
        raise SystemExit(f"清单缺少 items 数组: {path}")
    _validate_manifest_items(data["items"], path)
    return data


def _validate_manifest_items(items: list, path: Path) -> None:
    """Validate execution-critical fields and normalize declared legacy forms."""
    seen: set[str] = set()
    legacy_count = 0
    for index, item in enumerate(items, start=1):
        label = f"items[{index - 1}]"
        if not isinstance(item, dict):
            raise SystemExit(f"{label} 必须是 JSON 对象: {path}")

        raw_name = str(item.get("filename", "")).strip()
        name = _safe_filename(raw_name)
        if raw_name != name:
            raise SystemExit(f"{label}.filename 必须是安全的单层文件名: {raw_name!r}")
        if name in seen:
            raise SystemExit(f"清单包含重复 filename: {name}")
        seen.add(name)

        legacy_type = str(item.get("type", "")).strip().lower()
        if legacy_type in LEGACY_TYPES:
            page_role, text_policy = LEGACY_TYPES[legacy_type]
            item.pop("type", None)
            item["page_role"] = page_role
            if text_policy:
                item["text_policy"] = text_policy
            legacy_count += 1

        page_role = str(item.get("page_role", "")).strip()
        if page_role == "full_page":
            page_role = "hero_page"
            legacy_count += 1
        if not page_role:
            page_role = "local"
            legacy_count += 1
        if page_role not in PAGE_ROLES:
            raise SystemExit(f"{label}.page_role 无效: {page_role!r}")
        item["page_role"] = page_role

        text_policy = str(item.get("text_policy", "")).strip()
        if not text_policy:
            text_policy = "none"
            legacy_count += 1
        if text_policy not in TEXT_POLICIES:
            raise SystemExit(f"{label}.text_policy 无效: {text_policy!r}")
        item["text_policy"] = text_policy

        if not str(item.get("aspect_ratio", "")).strip():
            raise SystemExit(f"{label}.aspect_ratio 不能为空")
        image_size = str(item.get("image_size", "")).strip()
        if image_size and image_size not in SIZE_NAMES:
            raise SystemExit(f"{label}.image_size 无效: {image_size!r}")
        status = str(item.get("status", "")).strip()
        if not status:
            status = "Pending"
        if status not in STATUSES:
            raise SystemExit(f"{label}.status 无效: {status!r}")
        item["status"] = status

        _validate_slice_metadata(item, label)

    if legacy_count:
        log(f"[兼容] 清单中 {legacy_count} 处旧字段已按声明规则归一化")


def _validate_slice_metadata(item: dict, label: str) -> None:
    grid = str(item.get("slice_grid", "")).strip()
    names_text = str(item.get("slice_names", "")).strip()
    if bool(grid) != bool(names_text):
        raise SystemExit(f"{label}.slice_grid 与 slice_names 必须同时提供")
    if not grid:
        return
    match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", grid, flags=re.IGNORECASE)
    if not match:
        raise SystemExit(f"{label}.slice_grid 必须为 RxC: {grid!r}")
    rows, cols = (int(value) for value in match.groups())
    names = [name.strip() for name in names_text.split(",") if name.strip()]
    if len(names) != rows * cols:
        raise SystemExit(f"{label}.slice_names 数量应为 {rows * cols},实际 {len(names)}")
    if len(set(names)) != len(names):
        raise SystemExit(f"{label}.slice_names 不得重复")
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
            raise SystemExit(f"{label}.slice_names 含非法 basename: {name!r}")


def _write_manifest(path: Path, data: dict) -> None:
    with _manifest_lock:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def _valid_image_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    return bool(head) and head.startswith(IMAGE_MAGIC)


def _safe_filename(name: str) -> str:
    cleaned = Path(name.strip()).name
    if not cleaned or cleaned.startswith("."):
        raise SystemExit(f"非法输出文件名: {name!r}")
    return cleaned


def run_manifest(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).expanduser().resolve()
    if not manifest_path.is_file():
        raise SystemExit(f"清单不存在: {manifest_path}")
    data = _read_manifest(manifest_path)
    output_dir = Path(args.output).expanduser().resolve() if args.output else manifest_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    backend = Backend(model_override=args.model)
    problem = backend.validate()
    if problem:
        raise SystemExit(f"后端未就绪: {problem}")

    todo: list[dict] = []
    for item in data["items"]:
        name = _safe_filename(str(item.get("filename", "")))
        target = output_dir / name
        if item.get("status") == "Needs-Manual":
            # 获取终态(image-base.md §6):等待人工供图/流程 reconcile,CLI 不自动重试
            log(f"[跳过] {name} 为 Needs-Manual 终态,不自动重试")
            continue
        if item.get("status") == "Generated":
            if _valid_image_file(target):
                log(f"[跳过] {name} 已生成且文件有效")
                continue
            item["status"] = "Failed"
            log(f"[降级] {name} 标记 Generated 但文件缺失/损坏,重新生成")
        if not str(item.get("prompt", "")).strip():
            item["status"] = "Failed"
            item["last_error"] = "prompt 为空"
            log(f"[失败] {name} prompt 为空")
            continue
        todo.append(item)
    _write_manifest(manifest_path, data)

    concurrency = args.concurrency or int(os.environ.get("IMAGE_CONCURRENCY", "3") or 3)
    concurrency = max(1, min(concurrency, 8))
    attempts: dict[str, int] = {}
    wave = 0
    while todo:
        wave += 1
        rate_limited = False
        failures: list[dict] = []

        def _work(item: dict) -> None:
            nonlocal rate_limited
            name = _safe_filename(str(item["filename"]))
            started = time.monotonic()
            try:
                blob = backend.generate(
                    prompt=str(item["prompt"]),
                    aspect_ratio=str(item.get("aspect_ratio", "1:1")),
                    image_size=str(item.get("image_size", args.image_size)),
                    model=str(item.get("model", "") or ""),
                )
                (output_dir / name).write_bytes(blob)
                item["status"] = "Generated"
                item.pop("last_error", None)
                log(f"[生成] {name} ({len(blob) // 1024} KB, {time.monotonic() - started:.1f}s)")
            except GenError as exc:
                item["status"] = "Failed"
                item["last_error"] = exc.message[:500]
                if exc.rate_limited:
                    rate_limited = True
                if exc.retryable:
                    failures.append(item)
                log(f"[失败] {name}: {exc.message[:300]}")
            _write_manifest(manifest_path, data)

        log(f"[第 {wave} 轮] 待生成 {len(todo)} 张,并发 {concurrency}")
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            list(pool.map(_work, todo))

        todo = []
        for item in failures:
            key = str(item["filename"])
            attempts[key] = attempts.get(key, 0) + 1
            if attempts[key] < ITEM_MAX_ATTEMPTS:
                todo.append(item)
            else:
                log(f"[放弃] {key} 连续 {ITEM_MAX_ATTEMPTS} 次可重试失败,保持 Failed(可重跑本命令续做)")
        if todo and rate_limited:
            concurrency = max(1, concurrency // 2)
            pause = min(20 * wave, 60)
            log(f"[限流] 并发降为 {concurrency},暂停 {pause}s 后重试")
            time.sleep(pause)

    done = sum(1 for i in data["items"] if i.get("status") == "Generated")
    manual = [str(i.get("filename")) for i in data["items"] if i.get("status") == "Needs-Manual"]
    failed = [str(i.get("filename")) for i in data["items"]
              if i.get("status") not in ("Generated", "Needs-Manual")]
    log(f"[完成] Generated {done}/{len(data['items'])}"
        + (f";Failed(可重跑本命令重试): {', '.join(failed)}" if failed else "")
        + (f";Needs-Manual(人工处理,不重试): {', '.join(manual)}" if manual else ""))
    render_md(str(manifest_path))
    return 0 if not failed else 1


# ── markdown sidecar ─────────────────────────────────────────────


def render_md(manifest_arg: str) -> int:
    manifest_path = Path(manifest_arg).expanduser().resolve()
    data = _read_manifest(manifest_path)
    lines = ["# Image Prompts", ""]
    if data.get("deck_rendering"):
        lines.append(f"- **Deck rendering**: {data['deck_rendering']}")
    scheme = data.get("color_scheme")
    if scheme:
        text = json.dumps(scheme, ensure_ascii=False) if isinstance(scheme, (dict, list)) else str(scheme)
        lines.append(f"- **Color scheme**: {text}")
    lines.append(f"- **Items**: {len(data['items'])}")
    lines.append("")
    for index, item in enumerate(data["items"], start=1):
        lines.append(f"## {index}. {item.get('filename', '(unnamed)')} — {item.get('status', 'Pending')}")
        meta = []
        for key in ("purpose", "type", "page_role", "text_policy", "aspect_ratio", "image_size", "model",
                    "slice_grid", "slice_names"):
            if item.get(key):
                meta.append(f"{key}: {item[key]}")
        if meta:
            lines.append("- " + " · ".join(meta))
        if item.get("alt_text"):
            lines.append(f"- alt: {item['alt_text']}")
        if item.get("last_error"):
            lines.append(f"- last_error: {item['last_error']}")
        lines.append("")
        lines.append("> " + str(item.get("prompt", "")).replace("\n", "\n> "))
        lines.append("")
    sidecar = manifest_path.with_suffix(".md")
    sidecar.write_text("\n".join(lines), encoding="utf-8")
    log(f"[写出] {sidecar}")
    return 0


# ── single-image form ────────────────────────────────────────────


def run_single(args: argparse.Namespace) -> int:
    backend = Backend(model_override=args.model)
    problem = backend.validate()
    if problem:
        raise SystemExit(f"后端未就绪: {problem}")
    output_dir = Path(args.output).expanduser().resolve() if args.output else Path.cwd()
    output_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_filename(args.filename or "generated_image.png")
    try:
        blob = backend.generate(args.prompt, args.aspect_ratio, args.image_size)
    except GenError as exc:
        log(f"[失败] {exc.message[:500]}")
        return 1
    (output_dir / name).write_bytes(blob)
    log(f"[生成] {output_dir / name} ({len(blob) // 1024} KB)")
    return 0


def list_backends() -> int:
    print("openai  (Core) — OpenAI 兼容 /v1/images/generations;env: OPENAI_API_KEY / "
          "OPENAI_BASE_URL / OPENAI_MODEL / OPENAI_SIZE_PRESET / OPENAI_RESPONSE_FORMAT / "
          "OPENAI_OUTPUT_FORMAT / OPENAI_QUALITY")
    print("其余 provider 后端(gemini/zhipu/…)本构建未随附;OpenAI 兼容中转一律走 openai。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?", help="单图模式:一段完整的图片提示词")
    parser.add_argument("--manifest", help="清单模式:image_prompts.json 路径")
    parser.add_argument("--render-md", dest="render_md", help="仅渲染清单的 Markdown sidecar")
    parser.add_argument("--list-backends", action="store_true", help="列出支持的后端")
    parser.add_argument("--concurrency", type=int, default=0, help="清单模式并发数(默认 IMAGE_CONCURRENCY 或 3)")
    parser.add_argument("--image_size", "--image-size", dest="image_size", default="1K",
                        choices=sorted(SIZE_NAMES), help="默认尺寸档;清单行 image_size 优先")
    parser.add_argument("--aspect_ratio", "--aspect-ratio", dest="aspect_ratio", default="1:1",
                        help="单图模式宽高比,如 16:9")
    parser.add_argument("--filename", default="", help="单图模式输出文件名")
    parser.add_argument("-o", "--output", default="", help="输出目录(清单模式默认清单所在目录)")
    parser.add_argument("-b", "--backend", default="", help="覆盖 IMAGE_BACKEND")
    parser.add_argument("-m", "--model", default="", help="默认模型;清单行 model 优先")
    args = parser.parse_args()

    if args.list_backends:
        return list_backends()

    _load_env_files()
    if args.render_md:
        return render_md(args.render_md)

    backend_name = (args.backend or os.environ.get("IMAGE_BACKEND", "")).strip().lower()
    if not backend_name:
        raise SystemExit("IMAGE_BACKEND 未配置(本构建支持 openai;OpenAI 兼容中转也走 openai)")
    if backend_name != "openai":
        raise SystemExit(f"后端 {backend_name!r} 本构建未随附,仅支持 openai(兼容中转同样用 openai)")

    if args.manifest:
        return run_manifest(args)
    if args.prompt:
        return run_single(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
