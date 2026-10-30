"""国内工具：读取人工种子清单，做官网存活校验。

国内 SaaS/AI 产品没有可枚举的公开 API，所以清单是人工维护的。
但「还在不在」可以机器校验——这一步能自动淘汰已经关站的产品。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from common import http_get, load_config, log, today_iso


def _probe(url: str, timeout: int) -> tuple[str, int]:
    """探活。只看能否拿到 2xx/3xx，不深挖内容。"""
    status, _, _ = http_get(url, timeout=timeout, retries=0)
    return url, status


def fetch(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config("sources.json")
    ccfg = cfg.get("cn", {})
    if not ccfg.get("enabled", True):
        log("国内源已禁用，跳过")
        return []

    seed = load_config("cn_tools.json").get("tools", [])
    log(f"国内清单载入 {len(seed)} 条")

    alive: dict[str, bool] = {}
    if ccfg.get("check_alive", True):
        timeout = ccfg.get("check_timeout_sec", 12)
        try:
            with ThreadPoolExecutor(max_workers=16) as ex:
                futs = {ex.submit(_probe, t["url"], timeout): t["url"] for t in seed}
                for f in as_completed(futs):
                    try:
                        url, st = f.result()
                        alive[url] = 200 <= st < 400
                    except Exception:  # noqa: BLE001
                        alive[futs[f]] = False
        except Exception as e:  # noqa: BLE001
            log(f"存活校验异常，跳过校验: {e}", "WARN")
            alive = {}
        ok = sum(1 for v in alive.values() if v)
        log(f"存活校验完成：{ok}/{len(alive)} 可访问")

    out: list[dict] = []
    checked = today_iso()
    for t in seed:
        url = t["url"]
        is_alive = alive.get(url, True)
        out.append({
            "origin": "cn",
            "id": f"cn:{url}",
            "name": t["name"],
            "url": url,
            "desc": t.get("desc", ""),
            "tags_hint": t.get("tags", []),
            "topics": [],
            "lang": None,
            "stars": 0,
            "pushed_at": "",
            "category": t.get("cat", "国内工具"),
            "why": "国内工具清单",
            "alive": is_alive,
            "checked": checked,
        })

    down = [r["name"] for r in out if not r["alive"]]
    if down:
        log(f"以下站点本次不可访问（保留条目，网页上会标注）：{'、'.join(down[:12])}", "WARN")
    log(f"国内源处理完成，共 {len(out)} 条")
    return out


if __name__ == "__main__":
    rows = fetch()
    print(len(rows))
    for r in rows[:5]:
        print(r["name"], r["alive"])
