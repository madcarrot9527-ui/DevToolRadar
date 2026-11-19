"""GitHub 抓取：按 topic 搜索高星且活跃的仓库。"""
from __future__ import annotations

import time
from urllib.parse import quote

from common import (DATA_DIR, already_logged_in_github, clip, days_ago_iso,
                    github_headers, http_get, load_config, load_json, log,
                    save_json)

API = "https://api.github.com/search/repositories"
REPO_API = "https://api.github.com/repos/"
PINNED_CACHE = DATA_DIR / ".cache" / "pinned.json"
PINNED_CACHE.parent.mkdir(parents=True, exist_ok=True)


def _to_row(r: dict, label: str) -> dict:
    full = r.get("full_name") or ""
    return {
        "origin": "github",
        "id": f"gh:{full}",
        "name": r.get("name") or full.split("/")[-1],
        "full_name": full,
        "url": r.get("html_url"),
        "desc": clip(r.get("description"), 320),
        "topics": r.get("topics") or [],
        "lang": r.get("language"),
        "stars": r.get("stargazers_count", 0),
        "forks": r.get("forks_count", 0),
        "pushed_at": (r.get("pushed_at") or "")[:10],
        "updated_at": (r.get("updated_at") or "")[:10],
        "license": (r.get("license") or {}).get("spdx_id"),
        "homepage": r.get("homepage") or "",
        "why": label,
    }


def _fetch_pinned(cfg: dict, delay: float, timeout: int, retries: int) -> dict[str, dict]:
    """按仓库全名逐个抓。用 core API（额度比 search 宽松）。

    注意：core API 未认证只有 60 次/小时，而必收录清单（124 个）本身就超过
    一个小时的额度。所以结果按仓库缓存，只抓“没缓存或过期”的。

    TTL 定 30 天而不是 7 天（2026-11-19 改）：这几天的日志显示，7 天 TTL 会让
    缓存集中到期——124 个按 7 天过期，平均每天要重抓约 18 个，一旦某天配额被
    gh_meta 抢走（例如 11-19 开跑时 core 只剩 9 次），当天到期的就补不回来，
    第二天又叠上新的到期量，形成「越欠越多」的尖峰（11-18/11-19 连续两轮
    必收录失败 22~54 个都是这个形状）。30 天把尖峰从每周一次压到每月一次。

    TTL 拉长的代价只有星数/语言晚三周更新：星数在检索里只是
    `25×log10(stars)` 的加分项（上限 120 分），三周的星数变化折算不到 1 分，
    对排序没有可观测影响。
    """
    out: dict[str, dict] = {}
    repos = (cfg.get("pinned") or {}).get("repos") or []
    if not repos:
        return out

    cache = load_json(PINNED_CACHE, {}) or {}
    ttl = 30 * 86400
    now = time.time()
    fresh = {k: v for k, v in cache.items()
             if isinstance(v, dict) and (now - (v.get("_cached_at") or 0)) < ttl}

    need = [r for r in repos if r not in fresh]
    for full in repos:
        if full in fresh:
            row = dict(fresh[full])
            row.pop("_cached_at", None)
            out[full] = row

    if need:
        log(f"必收录仓库：{len(repos)} 个中 {len(need)} 个需要抓取"
            f"（core API 剩余额度有限，其余走缓存）")
    for i, full in enumerate(need, 1):
        status, data, _ = http_get(REPO_API + full, headers=github_headers(),
                                   timeout=timeout, retries=1, want_json=True)
        if status != 200 or not isinstance(data, dict):
            log(f"必收录仓库抓取失败 {full} (status={status})", "WARN")
            continue
        if data.get("archived"):
            continue
        row = _to_row(data, "必收录")
        out[row["full_name"]] = row
        fresh[row["full_name"]] = {**row, "_cached_at": now}
        time.sleep(0.4)
        if i % 10 == 0:
            save_json(PINNED_CACHE, fresh)   # 中途落盘，限流中断也不白跑

    if need:
        save_json(PINNED_CACHE, fresh)
    log(f"必收录仓库：成功 {len(out)}/{len(repos)}")
    return out


def fetch(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config("sources.json")
    gcfg = cfg.get("github", {})
    if not gcfg.get("enabled", True):
        log("GitHub 源已禁用，跳过")
        return []

    hcfg = cfg.get("http", {})
    min_stars = gcfg.get("min_stars", 800)
    per_page = gcfg.get("per_page", 100)
    max_pages = gcfg.get("max_pages_per_query", 1)
    cutoff = days_ago_iso(gcfg.get("pushed_within_days", 540))
    delay = hcfg.get("delay_with_token_sec" if already_logged_in_github()
                     else "delay_no_token_sec", 7.5)
    timeout = hcfg.get("timeout_sec", 25)
    retries = hcfg.get("retries", 2)

    if already_logged_in_github():
        log("GitHub: 检测到 token，使用高配额模式")
    else:
        log("GitHub: 未配置 token，走未认证限流（10 请求/分钟，速度慢但可用）")

    out: dict[str, dict] = {}
    queries = gcfg.get("queries", [])
    total_q = len(queries)

    for qi, item in enumerate(queries, 1):
        base_q = item["q"] if isinstance(item, dict) else str(item)
        label = item.get("label", base_q) if isinstance(item, dict) else base_q
        # 已经带 stars/pushed 限定的查询不再追加，避免语法冲突
        q = base_q
        if "stars:" not in q:
            q += f" stars:>={min_stars}"
        if "pushed:" not in q:
            q += f" pushed:>{cutoff}"

        for page in range(1, max_pages + 1):
            url = (f"{API}?q={quote(q)}&sort={gcfg.get('sort','stars')}"
                   f"&order=desc&per_page={per_page}&page={page}")
            status, data, _ = http_get(url, headers=github_headers(),
                                       timeout=timeout, retries=retries, want_json=True)
            if status != 200 or not isinstance(data, dict):
                log(f"GitHub 查询失败 [{label}] status={status}", "WARN")
                break

            items = data.get("items", [])
            for r in items:
                if r.get("archived") or r.get("fork"):
                    continue
                full = r.get("full_name")
                if not full or full in out:
                    continue
                out[full] = _to_row(r, label)

            log(f"GitHub [{qi}/{total_q}] {label}: 累计 {len(out)} 条")
            if len(items) < per_page:
                break
            if page < max_pages:
                time.sleep(delay)

        if qi < total_q:
            time.sleep(delay)

    # 必收录白名单：补上 topic 搜索抓不到的核心工具
    pinned = _fetch_pinned(cfg, delay, timeout, retries)
    added = 0
    for k, v in pinned.items():
        if k not in out:
            out[k] = v
            added += 1
    if added:
        log(f"必收录补充了 {added} 个此前漏掉的仓库")

    log(f"GitHub 抓取完成，共 {len(out)} 条唯一仓库")
    return list(out.values())


if __name__ == "__main__":
    from common import DATA_DIR, save_json
    rows = fetch()
    if rows:
        save_json(DATA_DIR / "raw_github.json", rows)
        print(f"saved {len(rows)} -> raw_github.json")
    else:
        print("0 rows")
