"""DevToolRadar 公共模块：配置加载、HTTP 请求、日志。"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
LOG_DIR = ROOT / "logs"

for _d in (DATA_DIR, SITE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Windows 控制台默认 GBK，中文输出会炸，强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_LOG_FILE = LOG_DIR / f"update-{datetime.now():%Y-%m}.log"


def log(msg: str, level: str = "INFO") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config(name: str) -> dict:
    p = CONFIG_DIR / name
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        log(f"读取 {path.name} 失败: {e}", "WARN")
        return default


def _ua() -> str:
    try:
        return load_config("sources.json").get("http", {}).get(
            "user_agent", "DevToolRadar/1.0"
        )
    except Exception:
        return "DevToolRadar/1.0"


def http_get(url: str, *, headers: dict | None = None, timeout: int = 25,
             retries: int = 2, want_json: bool = False):
    """带重试的 GET。返回 (status, body_text_or_obj, resp_headers)。"""
    hdrs = {"User-Agent": _ua(), "Accept": "application/json, text/plain, */*"}
    if headers:
        hdrs.update(headers)

    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace")
                rh = {k.lower(): v for k, v in resp.headers.items()}
                if want_json:
                    try:
                        return resp.status, json.loads(text), rh
                    except json.JSONDecodeError:
                        return resp.status, None, rh
                return resp.status, text, rh
        except urllib.error.HTTPError as e:
            last_err = e
            # 403/429 是限流，值得等；404 直接放弃
            if e.code in (403, 429) and attempt < retries:
                wait = 20 * (attempt + 1)
                log(f"限流 {e.code}，等待 {wait}s 后重试 {url[:80]}", "WARN")
                time.sleep(wait)
                continue
            if e.code == 404:
                return 404, None, {}
            if attempt < retries:
                time.sleep(2)
                continue
            return e.code, None, {}
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    log(f"请求失败 {url[:90]}: {last_err}", "WARN")
    return 0, None, {}


def github_headers() -> dict:
    """有 token 就用，能显著提高限流额度（搜索 API 10/min -> 30/min）。"""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok.strip()}"
    return h


def already_logged_in_github() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))


def clip(s: str | None, n: int = 300) -> str:
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def today_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d")
