"""全局配置:读取 .env,决定 mock/真实 API 模式。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _env_path(key: str, default: Path) -> Path:
    raw = os.environ.get(key, "").strip()
    if not raw:
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _normalize_base_path(raw: str) -> str:
    value = raw.strip()
    if not value or value == "/":
        return ""
    return "/" + value.strip("/")


BUNDLED_DATA_DIR = BASE_DIR / "data"
DATA_DIR = _env_path("PPT_DATA_DIR", BUNDLED_DATA_DIR)
UPLOADS_DIR = DATA_DIR / "uploads"      # 每个任务的材料快照(硬链接自材料库)
OUTPUTS_DIR = DATA_DIR / "outputs"
LOGS_DIR = DATA_DIR / "logs"
SAMPLE_DIR = BUNDLED_DATA_DIR / "sample"
LIBRARY_DIR = DATA_DIR / "library"      # 每账号一个材料库目录:library/<user_id>/
DB_FILE = DATA_DIR / "app.db"           # SQLite:users/sessions/materials/jobs
JOBS_FILE = DATA_DIR / "jobs.json"      # 旧版任务存档,仅迁移脚本读取
BASE_PATH = _normalize_base_path(os.environ.get("PPT_BASE_PATH", ""))
COOKIE_PATH = f"{BASE_PATH}/" if BASE_PATH else "/"


def web_path(path: str = "/") -> str:
    """Return a browser-visible path, including the reverse-proxy prefix."""
    suffix = "/" + path.lstrip("/")
    return f"{BASE_PATH}{suffix}" if BASE_PATH else suffix

PPT_MASTER_REPO = Path(os.environ.get("PPT_MASTER_REPO") or (BASE_DIR / "engine"))
COMPANY_TEMPLATE = PPT_MASTER_REPO / "skills/ppt-master/templates/decks/mt_corporate_blue"
_COMPANY_SOURCE_ENV = os.environ.get("COMPANY_TEMPLATE_SOURCE", "").strip()


def company_template_source() -> Path | None:
    """公司模板原稿 PPTX(原生母版来源):env 显式路径优先,退回仓库内预览稿。
    env 值可写相对路径(按项目根解析)或绝对路径。"""
    candidates = [BASE_DIR / Path(_COMPANY_SOURCE_ENV).expanduser()] if _COMPANY_SOURCE_ENV else []
    candidates.append(COMPANY_TEMPLATE / "exports/mt_corporate_blue_template_preview.pptx")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
PPT_MODEL = os.environ.get("PPT_MODEL", "").strip()
PPT_API_BASE_URL = (
    os.environ.get("PPT_API_BASE_URL")
    or os.environ.get("OPENAI_BASE_URL")
    or os.environ.get("ANTHROPIC_BASE_URL")
    or ""
).strip()
PPT_API_KEY = (
    os.environ.get("PPT_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or os.environ.get("ANTHROPIC_API_KEY")
    or ""
).strip()
PPT_WIRE_API = os.environ.get("PPT_WIRE_API", "responses").strip().lower()

# ── 多模型注册表:默认模型(上面的 PPT_MODEL/PPT_API_*)+ .env 里 PPT_MODEL2..9 追加 ──
MODELS: dict[str, dict] = {}


def _env_flag(key: str, default: bool) -> bool:
    value = os.environ.get(key, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _register_models() -> None:
    if PPT_MODEL:
        MODELS[PPT_MODEL] = {
            "model": PPT_MODEL,
            "label": os.environ.get("PPT_MODEL_LABEL", "").strip() or f"{PPT_MODEL}(默认)",
            "base_url": PPT_API_BASE_URL,
            "api_key": PPT_API_KEY,
            "wire": PPT_WIRE_API,
            # 图片输入(view_image 看图)能力:responses 协议默认开;可用 PPT_VISION 覆盖。
            "vision": _env_flag("PPT_VISION", PPT_WIRE_API == "responses"),
            "input_usd_per_m": _env_float("PPT_INPUT_USD_PER_M"),
            "output_usd_per_m": _env_float("PPT_OUTPUT_USD_PER_M"),
            "cached_usd_per_m": _env_float("PPT_CACHED_USD_PER_M"),
        }
    for i in range(2, 10):
        name = os.environ.get(f"PPT_MODEL{i}", "").strip()
        if not name:
            continue
        wire = (os.environ.get(f"PPT_MODEL{i}_WIRE", "").strip() or "responses").lower()
        MODELS[name] = {
            "model": name,
            "label": os.environ.get(f"PPT_MODEL{i}_LABEL", "").strip() or name,
            "base_url": os.environ.get(f"PPT_MODEL{i}_BASE_URL", "").strip() or PPT_API_BASE_URL,
            "api_key": os.environ.get(f"PPT_MODEL{i}_API_KEY", "").strip() or PPT_API_KEY,
            "wire": wire,
            # chat 协议默认关(DeepSeek 官方已实测拒收图片);支持视觉的端点用 _VISION=1 打开。
            "vision": _env_flag(f"PPT_MODEL{i}_VISION", wire == "responses"),
            "input_usd_per_m": _env_float(f"PPT_MODEL{i}_INPUT_USD_PER_M"),
            "output_usd_per_m": _env_float(f"PPT_MODEL{i}_OUTPUT_USD_PER_M"),
            "cached_usd_per_m": _env_float(f"PPT_MODEL{i}_CACHED_USD_PER_M"),
        }


def _env_float(key: str) -> float | None:
    try:
        return float(os.environ[key])
    except (KeyError, ValueError):
        return None


def model_conf(name: str = "") -> dict:
    """按名取模型配置;未注册时退回默认模型。"""
    conf = MODELS.get((name or "").strip())
    if conf:
        return conf
    if PPT_MODEL and PPT_MODEL in MODELS:
        return MODELS[PPT_MODEL]
    raise RuntimeError("没有可用的模型配置(检查 .env 的 PPT_MODEL)")


def wire_url(conf: dict) -> str:
    """按模型的协议把 base_url 规范化为最终 endpoint。"""
    base = str(conf.get("base_url", "")).rstrip("/")
    if conf.get("wire") == "chat":
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"
    if base.endswith("/responses"):
        return base
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"
PPT_API_REQUEST_TIMEOUT_SECONDS = int(os.environ.get("PPT_API_REQUEST_TIMEOUT_SECONDS", "300"))
PPT_API_RETRIES = int(os.environ.get("PPT_API_RETRIES", "5"))
PPT_AGENT_MAX_TURNS = int(os.environ.get("PPT_AGENT_MAX_TURNS", "240"))
PPT_MAX_OUTPUT_TOKENS = int(os.environ.get("PPT_MAX_OUTPUT_TOKENS", "32768"))
PPT_COMPACT_THRESHOLD = int(os.environ.get("PPT_COMPACT_THRESHOLD", "180000"))
PORT = int(os.environ.get("PORT", "8080"))
# 任务并发数:1=串行(默认);2-4=并行跑多个任务(费用与 API 并发同时叠加)
JOB_WORKERS = max(1, min(int(os.environ.get("JOB_WORKERS", "1") or 1), 4))
JOB_TIMEOUT_SECONDS = int(float(os.environ.get("JOB_TIMEOUT_MINUTES", "90")) * 60)

_MOCK_SETTING = os.environ.get("MOCK_MODE", "auto").strip().lower()


def mock_enabled() -> bool:
    if _MOCK_SETTING == "force":
        return True
    if _MOCK_SETTING == "off":
        return False
    ready, _ = agent_ready()
    return not ready


def responses_url() -> str:
    """把 provider 根地址规范化为 Responses API endpoint。"""
    base = PPT_API_BASE_URL.rstrip("/")
    if base.endswith("/responses"):
        return base
    if base.endswith("/v1"):
        return f"{base}/responses"
    return f"{base}/v1/responses"


def agent_ready() -> tuple[bool, str]:
    """返回真实 API 执行器是否可用及用户可见原因。"""
    checks = (
        (bool(PPT_API_KEY), "未配置 PPT_API_KEY/OPENAI_API_KEY/API key"),
        (bool(PPT_API_BASE_URL), "未配置 PPT_API_BASE_URL/OPENAI_BASE_URL/API 地址"),
        (bool(PPT_MODEL), "未配置 PPT_MODEL"),
        (PPT_WIRE_API in ("responses", "chat"), "PPT_WIRE_API 需为 responses 或 chat"),
        (PPT_MASTER_REPO.is_dir(), f"PPT Master 仓库不存在:{PPT_MASTER_REPO}"),
        ((PPT_MASTER_REPO / "skills/ppt-master/SKILL.md").is_file(), "PPT Master 技能文件缺失"),
        (COMPANY_TEMPLATE.is_dir(), f"公司模板工作区不存在:{COMPANY_TEMPLATE}"),
        ((BASE_DIR / "runner_agent.py").is_file(), "真实 API 执行器 runner_agent.py 缺失"),
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, "Responses API 执行器就绪"


# ── AI 配图(文生图)后端:PPT_IMAGE_MODEL 非空即启用前端开关 ──────────
PPT_IMAGE_MODEL = os.environ.get("PPT_IMAGE_MODEL", "").strip()
PPT_IMAGE_BASE_URL = (os.environ.get("PPT_IMAGE_BASE_URL", "").strip() or PPT_API_BASE_URL)
PPT_IMAGE_API_KEY = (os.environ.get("PPT_IMAGE_API_KEY", "").strip() or PPT_API_KEY)
PPT_IMAGE_QUALITY = os.environ.get("PPT_IMAGE_QUALITY", "").strip()
# Keep generated files' extension and bytes aligned. Override with jpeg/webp if needed.
PPT_IMAGE_FORMAT = os.environ.get("PPT_IMAGE_FORMAT", "png").strip().lower() or "png"


def image_gen_ready() -> tuple[bool, str]:
    """AI 配图链路是否可用:模型 + 凭证 + 引擎脚本齐备。"""
    checks = (
        (bool(PPT_IMAGE_MODEL), "未配置 PPT_IMAGE_MODEL(如 gpt-image-2)"),
        (bool(PPT_IMAGE_API_KEY), "图像后端缺少 API key"),
        (bool(PPT_IMAGE_BASE_URL), "图像后端缺少 API 地址"),
        ((PPT_MASTER_REPO / "skills/ppt-master/scripts/image_gen.py").is_file(),
         "引擎缺少 image_gen.py 脚本"),
    )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, f"图像后端就绪:{PPT_IMAGE_MODEL}"


def image_gen_env() -> dict[str, str]:
    """image_gen.py 子进程所需的环境变量(runner 定向注入,不进普通脚本)。"""
    env = {
        "IMAGE_BACKEND": "openai",
        "OPENAI_API_KEY": PPT_IMAGE_API_KEY,
        "OPENAI_BASE_URL": PPT_IMAGE_BASE_URL,
        "OPENAI_MODEL": PPT_IMAGE_MODEL,
    }
    if PPT_IMAGE_QUALITY:
        env["OPENAI_QUALITY"] = PPT_IMAGE_QUALITY
    if PPT_IMAGE_FORMAT:
        env["OPENAI_OUTPUT_FORMAT"] = PPT_IMAGE_FORMAT
    return env


SESSION_TTL_DAYS = int(os.environ.get("SESSION_TTL_DAYS", "30"))

for d in (UPLOADS_DIR, OUTPUTS_DIR, LOGS_DIR, LIBRARY_DIR):
    d.mkdir(parents=True, exist_ok=True)

_register_models()
