"""DevToolRadar 命令行入口。

用法：
    tool find "mock 接口"          搜工具
    tool find --tag "内网穿透"      按场景列
    tool tags                       看有哪些场景
    tool stats                      看数据概况
    tool open                       打开检索网页
    tool doctor                     自检（数据/索引/MCP/客户端/网页）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from common import DATA_DIR, ROOT, SITE_DIR, load_json
import search as s

SRC_LABEL = {"github": "GitHub", "awesome": "精选", "cn": "国内"}
STALE_DAYS = 3          # 数据超过这个天数就认为过期
LOCK_TTL_SEC = 1800     # 刷新锁的有效期，防止并发重复启动


def _maybe_refresh(no_color: bool) -> None:
    """数据过期就悄悄在后台起一次刷新，不阻塞当前查询。

    为什么放在 CLI 而不是只靠计划任务：用户经常改完配置就忘了刷新，
    与其让他看到过期数据，不如自己修好。
    """
    st = load_json(DATA_DIR / "stats.json", {}) or {}
    ts = st.get("updated_at")
    stale = True
    age_txt = ""
    if ts:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            age = (datetime.now() - dt).days
            stale = age >= STALE_DAYS
            age_txt = f"{age} 天前"
        except ValueError:
            pass

    if not stale:
        return

    lock = DATA_DIR / ".refreshing"
    if lock.exists() and (time.time() - lock.stat().st_mtime) < LOCK_TTL_SEC:
        return

    try:
        lock.write_text(str(os.getpid()), encoding="utf-8")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # 走 refresh.py 而不是 update.py：这条兜底路径以前只抓数据、不跑自检，
        # 出了问题没有任何痕迹。refresh.py 会把全过程写进 logs/refresh.log，
        # 顺便跑一次 doctor，把结论落到 health.json。
        # 输出仍然丢给 DEVNULL——用户正在查询，不该被后台日志打断。
        subprocess.Popen(
            [sys.executable, str(ROOT / "src" / "refresh.py")],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        msg = (f"数据是 {age_txt}，已在后台开始刷新（首次约需几分钟）。"
               if age_txt else "还没有数据，已在后台开始首次抓取。")
        print(_c("33", msg, not no_color))
        if age_txt:
            print(_c("90", "本次结果来自旧数据，刷新完成后重新查询即可。", not no_color))
    except Exception as e:  # noqa: BLE001
        print(f"（后台刷新启动失败：{e}）")


def _c(code: str, text: str, on: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if on else text


def fmt_stars(n: int) -> str:
    if not n:
        return ""
    return f"★{n/1000:.0f}k" if n >= 10000 else (f"★{n/1000:.1f}k" if n >= 1000 else f"★{n}")


def _health_note(no_color: bool) -> None:
    """上次自检没过的话，在正常查询时提一句。

    自检结论如果只活在日志里就没人看。放在最常用的 find 前面，
    用户（和模型）在正常用的时候就会撞见，而不是等哪天彻底不灵了才回头查。
    """
    p = DATA_DIR / "health.json"
    if not p.exists():
        return
    h = load_json(p, {}) or {}
    if h.get("ok") is not False:
        return
    # 自检本身也可能早就没跑了，太久远的结论别拿来吓人，数据新鲜度那关会单独报
    try:
        age = (time.time() - p.stat().st_mtime) / 86400
    except OSError:
        return
    if age > 7:
        return
    fails = h.get("fails") or []
    head = "、".join(str(f.get("item")) for f in fails[:2])
    more = f" 等 {len(fails)} 项" if len(fails) > 2 else ""
    print(_c("31", f"! 上次自检（{h.get('at', '时间未知')}）没通过：{head}{more}", not no_color))
    print(_c("90", "  跑 tool doctor 看详情。当前结果可能来自不完整的数据。", not no_color))
    print()


def cmd_compose(args) -> int:
    """一次查几个能力位，给「一件事要几样东西配合」用。"""
    _maybe_refresh(args.no_color)
    color = sys.stdout.isatty() and not args.no_color
    items = s.compose(args.slots, per_slot=args.num, intent=getattr(args, "intent", ""))
    if not items:
        print("至少要给一个能力位，例如：tool compose 低代码 认证鉴权 ORM 部署")
        return 1

    gaps = [x for x in items if not x["label"]]
    expanded: list[str] = []
    for item in items:
        if not item["label"]:
            print(f"\n[{_c('31', '缺口', color)}] 「{item['slot']}」— "
                  f"{s.tag_count()} 个能力位里没有对应场景")
            continue
        via = item.get("via")
        if via:
            for v in via.split(" / "):
                if v not in expanded:
                    expanded.append(v)
        hit_words = item.get("hit_words") or {}
        others = item.get("others") or []
        head = f"（说「{item['slot']}」，该场景共 {item['total']} 条，这里取前 {len(item['tools'])}）"
        if via:
            head = f"（由「{via}」展开，该场景共 {item['total']} 条，这里取前 {len(item['tools'])}）"
        print(f"\n[{_c('1;36', item['label'], color)}] {head}")
        if hit_words:
            print(_c("32", "  跟你的话直接对得上（可能是现成同类）：", color))
        elif item.get("no_match"):
            print(_c("90", "  你的话里没有能直接匹配这一格的词（正常，这格讲「怎么做」）；"
                           "以下是这格主力：", color))
        for t in item["tools"]:
            bits = []
            if t.get("stars"):
                bits.append(_c("33", fmt_stars(t["stars"]), color))
            if t.get("origin") == "cn":
                bits.append("国内")
            if t.get("pushed_at"):
                bits.append(t["pushed_at"])
            nm = t.get('name', '') + s.slug_suffix(t)
            hit = hit_words.get(t.get("url") or t.get("name") or "")
            mark = _c("32", f"  命中 {'、'.join(hit)}", color) if hit else ""
            print(f"  · {nm}  {' '.join(bits)}{mark}")
            if t.get("desc"):
                d = t["desc"]
                print(f"    {d[:88]}{'…' if len(d) > 88 else ''}")
            if t.get("url"):
                print(f"    {_c('4;34', t['url'], color)}")
        if others:
            print(_c("90", "  这格其他高星（不一定跟你的需求相关）：", color))
            for t in others:
                nm = t.get('name', '') + s.slug_suffix(t)
                st = fmt_stars(t["stars"]) if t.get("stars") else ""
                d = (t.get("desc") or "")[:60]
                print(_c("90", f"  · {nm} {st} — {d}{'…' if len(t.get('desc') or '') > 60 else ''}",
                         color))
    print()
    if gaps:
        print(f"有 {len(gaps)} 个能力位没有对应场景，这部分要么靠自己写，要么换个说法再 find。")
    for via in expanded:
        print(f"「{via}」不是能力位，已按上位说法展开成几格，每格只给代表作；"
              f"要细看哪一格就直接点那一格的名字。")
    print(f"以上是 {len(items)} 个能力位的候选，只含元数据（star / 维护状态 / 场景），"
          f"不含许可、部署方式、性能数据。")
    return 0


def cmd_find(args) -> int:
    _maybe_refresh(args.no_color)
    _health_note(args.no_color)
    meta: dict = {}
    rows = s.search(args.query, tag=args.tag, src=args.src, zh=args.zh,
                    limit=args.num, meta=meta)
    color = sys.stdout.isatty() and not args.no_color
    if meta.get("rescued") and rows:
        # 兜底结果不是答案。原来这里照样说「共 N 条」，读起来和真答案一个语气。
        print(_c("33", "！没有对症的。下面是按字面最接近的，仅供参考 —— "
                       "换个短一点的说法（比如只说「抠图」「抓包」）更准。", color))
        print()

    if not rows:
        if not s.load_index():
            print("索引还是空的，首次抓取正在后台进行，几分钟后再试一次。")
            return 1
        print(f"没有匹配「{args.query or args.tag}」的工具。")
        print("换个说法试试，或者用 `tool tags` 看看有哪些场景。")
        return 1

    for i, t in enumerate(rows, 1):
        name = _c("1;36", t.get("name", ""), color)
        # 同名条目（bun / tabby / goose）带上 slug，否则两行长得一样分不清
        slug = _c("90", s.slug_suffix(t), color)
        src = t.get("origin", "")
        badge = _c("35" if src == "cn" else ("34" if src == "github" else "32"),
                   f"[{SRC_LABEL.get(src, src)}]", color)
        bits = [badge]
        if t.get("stars"):
            bits.append(_c("33", fmt_stars(t["stars"]), color))
        if t.get("lang"):
            bits.append(t["lang"])
        if t.get("pushed_at"):
            bits.append(t["pushed_at"])
        if t.get("alive") is False:
            bits.append(_c("31", "站点不可达", color))

        print(f"\n{i:2d}. {name}{slug}  {' '.join(bits)}")
        if t.get("desc"):
            d = t["desc"]
            print(f"    {d[:96]}{'…' if len(d) > 96 else ''}")
        if t.get("url"):
            print(f"    {_c('4;34', t['url'], color)}")
        if t.get("tags"):
            print(f"    {_c('90', ' · '.join(t['tags']), color)}")

    print(f"\n共 {len(rows)} 条。更多见网页：tool open")
    # 和 MCP 那条建议同一个意思：一句话命中多个能力位时，上面只是混合排序的清单，
    # 用户要的是「这一套怎么拼」。命令行里也得说，否则同一个查询在终端和对话里
    # 得到的下一步不一样。
    if args.query:
        tags_hit = s.query_tags(args.query)
        if len(tags_hit) >= 2:
            cnt = dict(s.all_tags())
            print("这条需求涉及 "
                  + f"{len(tags_hit)} 个能力位："
                  + "、".join(f"{x}({cnt.get(x, 0)})" for x in tags_hit))
            print("要按位各取再拼，用：tool compose "
                  + " ".join(f"\"{x}\"" for x in tags_hit[:6]))
    return 0


def cmd_tags(args) -> int:
    tags = s.all_tags()
    if not tags:
        print("索引为空，先跑 python src/update.py")
        return 1
    print(f"{len(tags)} 个场景标签：\n")
    line = ""
    for name, n in tags:
        piece = f"{name}({n})"
        if len(line) + len(piece) > 78:
            print("  " + line.rstrip())
            line = ""
        line += piece + "  "
    if line.strip():
        print("  " + line.rstrip())
    print("\n用 tool find --tag \"标签名\" 列出某个场景下的所有工具。")
    return 0


def cmd_stats(args) -> int:
    st = load_json(DATA_DIR / "stats.json", {})
    tools = load_json(DATA_DIR / "tools.json", [])
    if not tools:
        print("索引为空，先跑 python src/update.py")
        return 1
    print(f"工具总数      {len(tools)}")
    print(f"最后刷新      {st.get('updated_at', '未知')}")
    srcs = st.get("sources", {})
    if srcs:
        # 「跳过」说的是这次运行的动作，不是数据的新旧。只报动作会让人以为数据没抓过
        # （新 clone 下来跑 --fast 重建时，三个源全是 skipped）。带上实际落盘时间才说得清。
        note = {"fresh": "本次已更新", "cached": "沿用缓存", "skipped": "已跳过", "failed": "抓取失败"}
        asof = st.get("as_of") or {}
        parts = []
        for k, v in srcs.items():
            s2 = note.get(v, v)
            if v in ("skipped", "cached") and asof.get(k):
                s2 += f"，数据截至 {asof[k]}"
            parts.append(f"{k}:{s2}")
        print("数据源        " + "，".join(parts))
    bo = st.get("by_origin", {})
    if bo:
        print("来源分布      " + "，".join(f"{SRC_LABEL.get(k, k)} {v}" for k, v in bo.items()))
    bt = st.get("by_tag", {})
    if bt:
        top = list(bt.items())[:10]
        print("覆盖最多场景  " + "，".join(f"{k} {v}" for k, v in top))
    site = SITE_DIR / "index.html"
    if site.exists():
        print(f"检索网页      {site}（{site.stat().st_size/1024:.0f} KB）")
    return 0


def cmd_open(args) -> int:
    site = SITE_DIR / "index.html"
    if not site.exists():
        print("网页还没生成，先跑 python src/update.py")
        return 1
    webbrowser.open(site.resolve().as_uri())
    print(f"已在浏览器打开 {site}")
    return 0


def cmd_doctor(args) -> int:
    """自检。逻辑在 doctor.py，这里只做入口。

    控制台默认代码页未必能显示 ✓/✗，先尽力切到 utf-8，切不动就让它原样输出，
    不能让「输出字符集」把「系统是不是好的」这件事判成假故障。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import doctor
    return doctor.main()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="tool", description="DevToolRadar — 按「你想干什么」找开发工具")
    sub = ap.add_subparsers(dest="cmd")

    f = sub.add_parser("find", help="搜索工具")
    f.add_argument("query", nargs="?", default="", help="你想做什么，例如 \"mock 接口\"")
    f.add_argument("-t", "--tag", default="", help="按场景标签过滤")
    f.add_argument("-s", "--src", default="", choices=["", "github", "awesome", "cn"],
                   help="按来源过滤")
    f.add_argument("-z", "--zh", action="store_true", help="只看中文工具")
    f.add_argument("-n", "--num", type=int, default=12, help="返回条数")
    f.add_argument("--no-color", action="store_true", help="关闭颜色")

    cp = sub.add_parser("compose", help="一次查多个能力位，拼一套方案")
    cp.add_argument("slots", nargs="+", help="能力位，可给多个：低代码 认证鉴权 ORM 部署")
    cp.add_argument("-n", "--num", type=int, default=2, help="每个能力位取几条，默认 2")
    cp.add_argument("-i", "--intent", default="",
                    help="用户原话，例如「做个吉伊桌面宠物」。给了就把每格分成"
                         "「跟你的话对得上的」和「这格其他高星」两段")
    cp.add_argument("--no-color", action="store_true", help="关闭颜色")
    cp.set_defaults(func=cmd_compose)
    f.set_defaults(func=cmd_find)

    tg = sub.add_parser("tags", help="列出所有场景标签")
    tg.set_defaults(func=cmd_tags)

    st = sub.add_parser("stats", help="数据概况")
    st.set_defaults(func=cmd_stats)

    op = sub.add_parser("open", help="打开检索网页")
    op.set_defaults(func=cmd_open)

    dr = sub.add_parser("doctor", help="自检：数据是否新鲜、MCP 是否可用、客户端配置是否完好")
    dr.set_defaults(func=cmd_doctor)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
