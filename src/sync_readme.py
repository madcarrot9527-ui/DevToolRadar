"""把 README 里的实时数字对齐到 data/stats.json。

为什么要有这个文件：
    README 首屏写着「现在是 5498 个工具，69 个场景标签」，而索引每天刷新一次。
    这句话从写下的第二天起就是错的——2026-11-21 那天实际是 6575 条、82 个标签，
    差了一千多条。项目的第一卖点是「每天都在刷新」，首屏却摆着一个 20 天前的数字，
    对第一次点进来的人是直接减分。

    手改不解决问题：改完当天是对的，第二天又错。所以改成从数据生成——
    每轮刷新的最后一步跑一次本脚本，把 README 里的计数改成本次刷新后的真实值。

为什么用「带上下文的整句匹配」而不是裸数字替换：
    README 里还有大量历史叙述性的数字（「32 条固定查询」「197 条带上了 slug」
    「9 个换了组织的仓库」），那些是某一次实验的记录，不该跟着刷新变。
    所以每条规则都要求数字后面跟着固定措辞（`N 个工具`、`N 条随你搜`、
    `N 个场景名`……），只改活口径，不碰叙事口径。

    如果哪天有人改了那些句子的措辞，规则会匹配不上。这时候不能静默通过——
    脚本会把「0 次命中的规则」列出来并返回 1，让日志里能看见，而不是等到
    下次发现首屏数字又过时了才回头查。

用法：
    python src/sync_readme.py          # 对齐并打印改了哪几处
    python src/sync_readme.py --check  # 只检查不改，有偏差返回 1

退出码：
    0 = 已是最新（或已成功改写）
    1 = stats.json 缺失，或有规则一条都没匹配上（README 措辞变了）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
STATS = ROOT / "data" / "stats.json"


def _rules(total: int, tags: int) -> list[tuple[str, str, str]]:
    """(说明, 正则, 替换文本)。说明只用于日志，方便对着看改了哪一处。"""
    return [
        ("首屏工具总数", r"\*\*\d+ 个工具\*\*", f"**{total} 个工具**"),
        ("首屏场景标签数", r"\d+ 个场景标签", f"{tags} 个场景标签"),
        ("双击即搜的条数", r"\d+ 条随你搜", f"{total} 条随你搜"),
        ("说明覆盖的条数", r"\d+ 条全都有", f"{total} 条全都有"),
        ("目录树里的条数", r"（当前 \d+ 条）", f"（当前 {total} 条）"),
        ("标签不外露的条数", r"\d+ 个标签全部是", f"{tags} 个标签全部是"),
        ("兜底检索的场景数", r"\d+ 个场景名", f"{tags} 个场景名"),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="把 README 的实时数字对齐到 stats.json")
    ap.add_argument("--check", action="store_true", help="只检查，不写文件")
    args = ap.parse_args()

    if not STATS.exists():
        print(f"缺 {STATS}，先跑一次刷新（python src/refresh.py --fast）。")
        return 1

    stats = json.loads(STATS.read_text(encoding="utf-8"))
    total = int(stats["total"])
    tags = len(stats.get("by_tag") or {})
    if not total or not tags:
        print(f"stats.json 内容异常：total={total} tags={tags}，不动 README。")
        return 1

    text = README.read_text(encoding="utf-8")
    before = text
    missed: list[str] = []

    for label, pattern, repl in _rules(total, tags):
        text, n = re.subn(pattern, repl, text)
        if n == 0:
            missed.append(label)
        else:
            print(f"  {label}: {n} 处")

    changed = text != before
    print(f"目标值：{total} 个工具 / {tags} 个场景标签")

    if changed:
        if args.check:
            print("README 与数据不一致（--check 模式，未写盘）")
            return 1
        README.write_text(text, encoding="utf-8")
        print("README 已更新")
    else:
        print("README 已是最新，无需改动")

    if missed:
        # 措辞被改过 → 规则失效 → 数字会悄悄重新变旧。必须让日志里看得见。
        print("警告：以下规则一条都没匹配上，README 措辞可能改过了，请核对："
              + "、".join(missed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
