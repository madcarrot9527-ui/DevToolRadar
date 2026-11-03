"""更新编排入口。

设计原则：任一数据源失败都不能摧毁整个索引。
每个源抓完先落盘成 raw_*.json；某个源这次抓挂了，就沿用上次的落盘结果，
并在 stats 里标记该源用的是旧数据。
"""
from __future__ import annotations

import argparse
import time
import traceback
from datetime import datetime

from common import DATA_DIR, load_json, log, save_json

SRC_FILES = {
    "github": DATA_DIR / "raw_github.json",
    "awesome": DATA_DIR / "raw_awesome.json",
    "cn": DATA_DIR / "raw_cn.json",
}


def _run_source(name: str, enabled: bool) -> tuple[list[dict], str]:
    """跑单个源。返回 (数据, 状态)。状态：fresh / cached / skipped / failed。"""
    if not enabled:
        cached = load_json(SRC_FILES[name], [])
        log(f"[{name}] 本次跳过，沿用缓存 {len(cached)} 条")
        return cached, "skipped"

    t0 = time.time()
    try:
        if name == "github":
            import fetch_github as mod
            rows = mod.fetch()
        elif name == "awesome":
            import fetch_awesome as mod
            rows = mod.fetch()
        elif name == "cn":
            import fetch_cn as mod
            rows = mod.fetch()
        else:
            raise ValueError(name)

        if rows:
            save_json(SRC_FILES[name], rows)
            log(f"[{name}] 完成 {len(rows)} 条，用时 {time.time()-t0:.1f}s")
            return rows, "fresh"

        cached = load_json(SRC_FILES[name], [])
        log(f"[{name}] 本次 0 条，回退到缓存 {len(cached)} 条", "WARN")
        return cached, "cached"
    except Exception as e:  # noqa: BLE001
        log(f"[{name}] 抓取异常: {e}", "ERROR")
        log(traceback.format_exc(limit=3), "ERROR")
        cached = load_json(SRC_FILES[name], [])
        log(f"[{name}] 回退到缓存 {len(cached)} 条", "WARN")
        return cached, ("cached" if cached else "failed")


def main() -> int:
    ap = argparse.ArgumentParser(description="DevToolRadar 数据刷新")
    ap.add_argument("--no-github", action="store_true", help="跳过 GitHub 抓取")
    ap.add_argument("--no-awesome", action="store_true", help="跳过 awesome 列表抓取")
    ap.add_argument("--no-cn", action="store_true", help="跳过国内工具处理")
    ap.add_argument("--no-site", action="store_true", help="只更新数据，不重新生成网页")
    ap.add_argument("--only-site", action="store_true", help="用现有数据重新生成网页")
    args = ap.parse_args()

    t_start = time.time()
    log("=" * 62)
    log("DevToolRadar 刷新开始")

    if args.only_site:
        import build_site
        build_site.build()
        log(f"完成，用时 {time.time()-t_start:.1f}s")
        return 0

    status: dict[str, str] = {}
    rows: list[dict] = []

    for name, skip in (("github", args.no_github), ("awesome", args.no_awesome),
                       ("cn", args.no_cn)):
        got, st = _run_source(name, not skip)
        status[name] = st
        rows.extend(got)

    log(f"原始数据合计 {len(rows)} 条")

    # 安全闸：原始数据一条都没有，但磁盘上已经有索引——这不是「世界清空了」，
    # 而是缓存缺失（刚 clone 就跑 --fast，而 raw_*.json 不入库）或者三个源
    # 同时被跳过。这时候写盘会把几千条换成空数组，且不可逆。宁可报错退出。
    if not rows:
        old = load_json(DATA_DIR / "tools.json", []) or []
        if old:
            log(f"原始数据 0 条，但磁盘上已有索引 {len(old)} 条 —— 拒绝覆盖。", "ERROR")
            log("原因通常是 data/raw_*.json 不存在（这些文件不入库），"
                "而这次三个源都被跳过了。", "ERROR")
            log("先跑一次完整刷新：python src/refresh.py", "ERROR")
            return 2

    import normalize
    tools = normalize.normalize(rows)

    # 数量骤降等于静默丢数据。正常的日间波动在 1% 以内（新收录、去重搬家互相抵消），
    # 超过 10% 只可能是某个源大面积抓取失败。这时候写盘就是把故障固化成「新数据」，
    # 而且下一次刷新会拿这个更小的基数当基准，一路滑下去都没人拦。
    # 2026-11-03 真发生过：3 个 awesome 源失败（SSL 断 + 403），5417 掉到 5152。
    old_tools = load_json(DATA_DIR / "tools.json", []) or []
    if old_tools and len(tools) < len(old_tools) * 0.9:
        log(f"索引骤降 {len(old_tools)} → {len(tools)}（跌了 "
            f"{(1 - len(tools) / len(old_tools)) * 100:.1f}%，超过 10%），"
            f"判定为抓取故障，拒绝写盘，保留上一版索引。", "ERROR")
        log("去 logs/refresh.log 查哪个源出问题；确认是真实下架再手动放行。", "ERROR")
        return 3

    save_json(DATA_DIR / "tools.json", tools)

    stats = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total": len(tools),
        "sources": status,
        "by_origin": {},
        "by_tag": {},
        "elapsed_sec": round(time.time() - t_start, 1),
    }
    # 每个源的落地时间。本次跳过的源（--no-github 之类）状态虽然还是 skipped，
    # 但数据其实是上次抓的，模型看到「跳过」会误判成数据过期。
    as_of: dict[str, str] = {}
    for name, path in SRC_FILES.items():
        try:
            as_of[name] = datetime.fromtimestamp(
                path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            continue
    stats["as_of"] = as_of
    from collections import Counter
    stats["by_origin"] = dict(Counter(t.get("origin") for t in tools))
    tc: Counter = Counter()
    for t in tools:
        for g in t.get("tags") or []:
            tc[g] += 1
    stats["by_tag"] = dict(tc.most_common())
    save_json(DATA_DIR / "stats.json", stats)

    if not args.no_site:
        import build_site
        build_site.build(tools)

    log(f"刷新完成：{len(tools)} 个工具，用时 {time.time()-t_start:.1f}s")
    log(f"数据源状态：{status}")
    log("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
