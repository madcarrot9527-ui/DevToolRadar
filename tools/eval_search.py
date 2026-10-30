"""检索质量评测：跑 config/eval_queries.json 里的查询，看期望工具在不在前 N。

为什么要有这个：改打分权重、补同义词、加场景，每一项都会牵动全库排名。
没有固定查询集的话，改完只能靠自己挑几个查询看着"还行"，很容易
修好一个、弄坏三个（实测：给所有英文词加词边界，30 条查询里 16 条变差）。

用法：
    python tools/eval_search.py            # 跑全部
    python tools/eval_search.py -v         # 逐条打印前 3
    python tools/eval_search.py -k 压测     # 只看某个查询
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import search as s  # noqa: E402


def load_cases() -> dict[str, list[str]]:
    p = ROOT / "config" / "eval_queries.json"
    return json.loads(p.read_text(encoding="utf-8"))["queries"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-k", "--only", default="")
    ap.add_argument("-n", "--top", type=int, default=3)
    args = ap.parse_args()

    cases = load_cases()
    names = {t["name"].lower(): t for t in s.load_index()}

    ok = fail = missing = 0
    fails: list[tuple[str, list[str], list[str]]] = []
    for q, expects in cases.items():
        if args.only and args.only not in q:
            continue
        got = [x["name"] for x in s.search(q, limit=args.top)]
        got_l = [g.lower() for g in got]
        exp_l = [e.lower() for e in expects]
        inlib = [e for e in exp_l if e in names]
        if not inlib:
            missing += 1
            if args.verbose:
                print(f"  ?? {q:12s} 期望的 {expects} 库里都没有")
            continue
        hit = [e for e in inlib if e in got_l]
        if hit:
            ok += 1
            if args.verbose:
                print(f"  OK {q:12s} -> {got}")
        else:
            fail += 1
            fails.append((q, expects, got))
            if args.verbose:
                print(f"  XX {q:12s} -> {got}   期望含 {expects}")

    total = ok + fail
    if total:
        print(f"\n前 {args.top} 命中 {ok}/{total} = {ok * 100 // total}%"
              + (f"（{missing} 条期望工具库里没有，已跳过）" if missing else ""))
    if fails:
        print("\n未命中：")
        for q, e, g in fails:
            print(f"  {q:12s} 期望 {e} / 实际 {g}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
