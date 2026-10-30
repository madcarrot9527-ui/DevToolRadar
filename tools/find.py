"""按名字或 URL 找条目，看它的全部字段。

为什么要单独做：调试「为什么这个词搜不到」「这条的标签凭什么这么打」时，
每次敲一段内联 python 查单条很啰嗦，而且每次字段打印格式都不一样。
这个脚本固定输出，和 audit_data.py 配套用 —— 一个看全局缺什么，一个看单条怎么了。

用法：
    python tools/find.py bun                 # 名字包含 bun
    python tools/find.py -e bun              # 名字完全等于 bun
    python tools/find.py -u oven-sh          # URL 包含
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "tools.json"

# 打印顺序照抄 normalize 的 keep 元组，这样和 tools.json 的字段顺序一致，
# 扫一眼就知道有没有字段被吃掉。
ORDER = ("name", "slug", "url", "home", "desc", "origin", "tags", "lang",
         "stars", "pushed_at", "category", "why", "alive", "checked",
         "quality", "zh", "wrap", "id")


def show(t: dict) -> None:
    print("-" * 66)
    for k in ORDER:
        if k not in t:
            continue
        v = t[k]
        if k == "desc" and len(str(v)) > 120:
            v = f"{v[:120]}…（共 {len(str(t[k]))} 字）"
        print(f"  {k:<10} {v}")


def main() -> int:
    ap = argparse.ArgumentParser(description="按名字或 URL 查条目")
    ap.add_argument("query", help="要查的名字或 URL 片段")
    ap.add_argument("-e", "--exact", action="store_true", help="名字必须完全相等")
    ap.add_argument("-u", "--url", action="store_true", help="按 URL 查")
    ap.add_argument("-n", "--num", type=int, default=20, help="最多显示几条")
    args = ap.parse_args()

    items = json.loads(DATA.read_text(encoding="utf-8"))
    q = args.query.strip()
    if args.url:
        hits = [t for t in items if q.lower() in (t.get("url") or "").lower()]
    elif args.exact:
        hits = [t for t in items if (t.get("name") or "").strip().lower() == q.lower()]
    else:
        # 名字大小写不敏感；连写和分词都算命中（bun 命中 bun、bun.sh）
        ql = q.lower()
        hits = [t for t in items if ql in (t.get("name") or "").lower()]

    if not hits:
        print(f"没有匹配「{q}」的条目。")
        return 1

    hits.sort(key=lambda x: -(x.get("stars") or 0))
    print(f"命中 {len(hits)} 条，按星数排序：")
    for t in hits[:args.num]:
        show(t)
    if len(hits) > args.num:
        print(f"\n还有 {len(hits) - args.num} 条未显示，用 -n 调整。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
