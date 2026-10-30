"""数据完整性体检。

为什么单独做成脚本：判断「做完没有」不能靠临时敲命令，也不能靠印象。
这个脚本每次给同一套口径，改完数据跑一遍就知道哪里还缺、缺多少。

用法：
    python tools/audit_data.py            # 汇总
    python tools/audit_data.py -d desc    # 只看说明缺口明细
    python tools/audit_data.py -n 40      # 明细显示前 40 条

退出码：有阻塞级缺口返回 1，否则 0。可以接进 CI 或 doctor。
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DATA = ROOT / "data" / "tools.json"


def is_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


def desc_tier(d: str) -> str:
    """说明分档。

    分档标准不能只看字符数：中文 8 个字（"AI 音乐生成器"）信息量已经够了，
    英文 8 个字符（"Ebean ORM"）却几乎是空的。所以中文按 8 字、英文按 24 字符。
    """
    d = (d or "").strip()
    if not d:
        return "EMPTY"
    if is_cjk(d):
        return "OK" if len(d) >= 8 else "THIN"
    return "OK" if len(d) >= 24 else "THIN"


PLACEHOLDER = {
    "todo", "tbd", "n/a", "na", "none", "null", "-", "—", "...", "…",
    "no description", "暂无", "暂无描述", "待补充", "无", "描述",
}


def is_placeholder(d: str) -> bool:
    return (d or "").strip().lower() in PLACEHOLDER


def main() -> int:
    ap = argparse.ArgumentParser(description="数据完整性体检")
    ap.add_argument("-d", "--detail", default="", help="明细维度：desc/lang/category/pushed/tags")
    ap.add_argument("-n", "--num", type=int, default=12, help="明细条数")
    args = ap.parse_args()

    items = json.loads(DATA.read_text(encoding="utf-8"))
    n = len(items)
    print(f"总数 {n}")

    # ---- 说明完整度 ----
    tiers = collections.Counter(desc_tier(t.get("desc")) for t in items)
    empty = [t for t in items if desc_tier(t.get("desc")) == "EMPTY"]
    thin = [t for t in items if desc_tier(t.get("desc")) == "THIN"]
    print("\n【说明】")
    for k in ("OK", "THIN", "EMPTY"):
        print(f"  {k:<6} {tiers.get(k,0):>5}  {tiers.get(k,0)/n*100:5.1f}%")
    if empty:
        print("  空说明来源:", dict(collections.Counter(t.get("origin") for t in empty)))

    ph = [t for t in items if is_placeholder(t.get("desc"))]
    if ph:
        print(f"  占位符说明 {len(ph)} 条")

    dup = collections.Counter((t.get("desc") or "").strip()
                              for t in items if len((t.get("desc") or "").strip()) >= 20)
    dups = [(d, c) for d, c in dup.items() if c > 1]
    if dups:
        print(f"  重复说明 {len(dups)} 组（同一条说明挂在多个条目上）")

    # ---- 关键字段缺失 ----
    # 这里要按「性质」分组，不能只报一个百分比。同一个 44% 里混着两种东西：
    #   - URL 指向 GitHub 仓库却没抓元数据 → 我们的工程欠账，能补
    #   - URL 是官网/商店页 → 那页面根本没有「用什么语言写的」这个概念，无解
    # 还有一个 category，缺 53% 但根本不影响用户 —— 它只在算分时用，从不展示。
    # 混在一起报，会让人误以为「一半数据是坏的」。
    print("\n【字段完整度】")
    fields = ("lang", "category", "pushed_at", "tags")
    miss = {}
    for f in fields:
        m = [t for t in items if not t.get(f)]
        miss[f] = m
        print(f"  {f:<10} 缺失 {len(m):>5}  {len(m)/n*100:5.1f}%")

    # 能不能补：URL 指向 GitHub 仓库根路径的才有元数据可取
    def is_gh_repo(t: dict) -> bool:
        u = t.get("url") or ""
        return bool(re.match(r"^https://github\.com/[^/]+/[^/]+$", u))

    for f in ("lang", "pushed_at"):
        rows = miss[f]
        if not rows:
            continue
        fixable = [t for t in rows if is_gh_repo(t)]
        print(f"    └ {f} 里可补 {len(fixable)} 条（指向 GitHub 仓库），"
              f"其余 {len(rows)-len(fixable)} 条是官网/商店页，取不到")

    if miss["category"]:
        print("    └ category 只在打标算分时用到，网页不展示、CLI 和 MCP 不读，"
              "缺失不影响使用")

    # star 只有 github 源才有，单独看
    gh = [t for t in items if t.get("origin") == "github"]
    no_star = [t for t in gh if not t.get("stars")]
    print(f"  github 源 {len(gh)} 条，其中 star=0 {len(no_star)} 条")

    # 补抓之后还剩的两种缺口，必须分开说 —— 「没抓到」和「拿不到」是两件事。
    repo = [t for t in items if is_gh_repo(t)]
    bare = [t for t in repo if not t.get("stars")]
    if bare:
        print(f"  指向 GitHub 仓库的 {len(repo)} 条里，星数仍缺 {len(bare)} 条："
              f"data/gh_meta.json 里查不到，仓库可能已删、已改名或已转私有")
    else:
        print(f"  指向 GitHub 仓库的 {len(repo)} 条，星数全部到位")
    no_lang = [t for t in repo if not t.get("lang")]
    if no_lang:
        print(f"  └ 语言仍缺 {len(no_lang)} 条：ungh 代理的返回里没有 language 字段，"
              f"只能走 GitHub API（无 token 60 次/小时）。已接进每日刷新按星数降序补，"
              f"一天 60 条要约 {len(no_lang) // 60} 天")

    # ---- 场景覆盖 ----
    no_tag = [t for t in items if not t.get("tags")]
    print("\n【场景】")
    print(f"  无场景 {len(no_tag)} 条  {len(no_tag)/n*100:.1f}%")
    print("  来源:", dict(collections.Counter(t.get("origin") for t in no_tag)))
    # 无场景不等于漏标：人工表里写 null 的是「读过、确认不归任何场景」的结论
    if no_tag:
        try:
            mt = json.loads((ROOT / "config" / "manual_tags.json")
                            .read_text(encoding="utf-8")).get("by_url") or {}
            known = {(k or "").rstrip("/").lower() for k in mt}
            judged = [t for t in no_tag
                      if (t.get("url") or "").rstrip("/").lower() in known]
            print(f"  └ 其中 {len(judged)} 条是人工判读过、明确记为「不属于任何场景」的结论，"
                  f"不是漏标")
        except OSError:
            pass

    # ---- 明细 ----
    buckets = {
        "desc": empty + thin,
        **{f: miss[f] for f in fields},
    }
    if args.detail in buckets:
        rows = sorted(buckets[args.detail], key=lambda x: -(x.get("stars") or 0))
        print(f"\n【{args.detail} 明细 前 {args.num}】")
        for t in rows[:args.num]:
            d = (t.get("desc") or "(空)")[:40]
            print(f"  {(t.get('stars') or 0):>6} {t.get('origin',''):<8} "
                  f"{t['name'][:36]:<38}| {d}")

    blocked = bool(empty)
    print(f"\n结论：{'说明仍有缺口' if blocked else '说明已补齐'}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
