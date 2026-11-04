"""DevToolRadar MCP Server —— 零依赖实现。

为什么手写而不用官方 SDK：
  1. 只读工具，协议子集极小（initialize / tools.list / tools.call）
  2. 不引入第三方包 = 换任何 Python 环境都能直接跑，不会因为 pip 装不上而失效
  3. 你换哪个客户端、哪个大模型都无所谓，这是开放协议（MCP），不是某家私有机制

传输：stdio，每行一条 JSON-RPC 2.0 消息（MCP stdio transport 规范）。

关键约束：stdout 只能输出协议消息。任何调试信息必须走 stderr，
否则客户端解析到非 JSON 内容会直接断开连接。下面第 1 步就是防这个。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# 1. 抢占真正的 stdout 用于协议通信，把 sys.stdout 重定向到 stderr
#    这样即使某个模块内部 print 了东西，也不会污染协议流
# ---------------------------------------------------------------------------
_PROTO = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="\n",
                          line_buffering=False)
sys.stdout = sys.stderr
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import search as S  # noqa: E402
from common import DATA_DIR, ROOT, load_json  # noqa: E402

SERVER_NAME = "devtoolradar"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTO = "2024-11-05"


def _log(msg: str) -> None:
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
SRC_CN = {"github": "GitHub", "awesome": "精选", "cn": "国内"}

# 营销堆词：有些项目把描述写成 SEO 关键词墙（「增强版 全修 新版 二开」），
# 对模型理解毫无帮助，只保留第一段有信息量的话。
_JUNK = ("二开", "全修", "新版", "增强版", "破解", "免费版", "全网最", "史上最")


def _clean_desc(d: str) -> str:
    if not d:
        return ""
    # 竖线分隔的堆词，取信息量最大的第一段
    if "|" in d:
        parts = [p.strip() for p in d.split("|") if p.strip()]
        if parts:
            d = max(parts[:3], key=lambda p: len(p) - 12 * sum(j in p for j in _JUNK))
    d = " ".join(d.split())
    return d


def _freshness(p: str) -> str:
    """把更新日期翻译成维护状态，模型不必自己去算天数。"""
    if not p:
        return ""
    from datetime import date
    try:
        days = (date.today() - date.fromisoformat(p[:10])).days
    except ValueError:
        return ""
    if days <= 90:
        return f"{p}（活跃）"
    if days <= 365:
        return f"{p}（1 年内更新）"
    if days <= 730:
        return f"{p}（2 年内更新，可能放缓）"
    return f"{p}（超过 2 年未更新，谨慎）"


def _signals(t: dict) -> str:
    """把元数据翻译成选型依据，而不是让模型自己从数字里猜。"""
    bits = []
    st = t.get("stars") or 0
    if st >= 20000:
        bits.append("star 很高，生态成熟")
    elif st >= 5000:
        bits.append("使用量可观")
    elif st >= 1000:
        bits.append("小规模但有人在用")
    elif st:
        bits.append("star 少，需自行评估")
    origin = t.get("origin")
    if origin == "cn":
        bits.append("国内产品，访问快、通常有中文文档")
    elif origin == "awesome":
        bits.append("来自人工精选清单")
    if t.get("alive") is False:
        bits.append("官网近期探活失败")
    # 同名条目常常不是本体：搜 ngrok 可能命中它的 Node 包装库。
    # 不标注的话模型会拿封装库当官方产品推荐。
    low = (t.get("desc") or "").lower()
    if any(k in low for k in ("wrapper for", "client for", "sdk for", "binding for",
                              "unofficial", "第三方", "非官方")):
        bits.append("⚠ 这是某个工具的封装/客户端，不一定本体")
    return "；".join(bits)


def _fmt_full(i: int, t: dict) -> str:
    src = SRC_CN.get(t.get("origin", ""), t.get("origin", ""))
    # 同名条目（bun / tabby / goose）带上 slug，否则两行长得一样分不清
    nm = t.get('name', '') + S.slug_suffix(t)
    head = [f"{i}. {nm}  [{src}]"]
    if t.get("stars"):
        st = t["stars"]
        head.append(f"★{st/1000:.1f}k" if st >= 1000 else f"★{st}")
    if t.get("lang"):
        head.append(t["lang"])
    fr = _freshness(t.get("pushed_at") or "")
    if fr:
        head.append(fr)

    lines = [" · ".join(head)]
    d = _clean_desc(t.get("desc") or "")
    if d:
        lines.append(f"   {d[:240]}{'…' if len(d) > 240 else ''}")
    sig = _signals(t)
    if sig:
        lines.append(f"   信号：{sig}")
    if t.get("tags"):
        lines.append(f"   场景：{' / '.join(t['tags'])}")
    if t.get("url"):
        lines.append(f"   {t['url']}")
    # 官网单独一行。索引里「仓库 + 官网」已经合并成一条，官网地址不显示就丢了。
    if t.get("home") and t["home"] != t.get("url"):
        lines.append(f"   官网：{t['home']}")
    return "\n".join(lines)


def _fmt_brief(t: dict) -> str:
    src = SRC_CN.get(t.get("origin", ""), t.get("origin", ""))
    d = _clean_desc(t.get("desc") or "")
    st = t.get("stars") or 0
    star = f"★{st/1000:.1f}k" if st >= 1000 else (f"★{st}" if st else "")
    tail = f" — {d[:70]}{'…' if len(d) > 70 else ''}" if d else ""
    nm = t.get('name', '') + S.slug_suffix(t)
    return f"  · {nm} [{src}] {star}{tail}".rstrip()


def tool_find_tool(args: dict) -> str:
    query = (args.get("query") or "").strip()
    tag = (args.get("tag") or "").strip()
    source = (args.get("source") or "").strip()
    chinese_only = bool(args.get("chinese_only"))
    try:
        limit = int(args.get("limit") or 8)
    except (TypeError, ValueError):
        limit = 8
    limit = max(1, min(limit, 30))

    if not query and not tag:
        return ("需要提供 query（你想做什么，例如「造测试数据」「内网穿透」）"
                "或 tag（场景标签，可先用 list_scenarios 查看）。")

    meta: dict = {}
    rows = S.search(query, tag=tag, src=source, zh=chinese_only, limit=limit, meta=meta)
    if not rows:
        hint = S.query_tags(query) if query else []
        msg = f"索引里没有匹配「{query or tag}」的工具。"
        if hint:
            msg += f"\n已识别到的场景标签: {', '.join(hint)}，但该场景下暂无条目。"
        else:
            msg += ("\n可能是问法没被识别。可以先用 list_scenarios 看看有哪些场景，"
                    "或者换个更通用的说法（例如把「做个接口假数据服务」换成「mock 接口」）。")
        return msg

    # 分层呈现：前 3 条给全貌，其余只给一行。
    # 平铺 8 条同样详略时，模型的注意力会被摊薄，反而不容易挑出最佳答案。
    head = []
    if meta.get("rescued"):
        # 不对症时凑出来的结果，必须说清楚，否则「共 N 条」会被当成答案。
        head.append("⚠ 没有对症的工具。下面是按字面最接近的，仅供参考；"
                    "换个更短的说法（「抠图」「抓包」这种关键词）命中率更高。")
    if query:
        tags_hit = S.query_tags(query)
        if tags_hit:
            head.append(f"「{query}」→ 场景：{' / '.join(tags_hit)}")

    primary = rows[:3]
    rest = rows[3:]
    blocks = ["\n\n".join(_fmt_full(i, t) for i, t in enumerate(primary, 1))]
    if rest:
        blocks.append("其余备选（同样命中该场景，按推荐度排序）：\n"
                      + "\n".join(_fmt_brief(t) for t in rest))
    tail = (f"共 {len(rows)} 条。以上只含索引里的元数据（star / 维护状态 / 场景标签），"
            f"不包含安装方式、费用、与同类工具的取舍细节——这些需要看项目文档确认。")
    return "\n".join(head + [b for b in blocks if b] + [tail]).strip()


def tool_list_scenarios(args: dict) -> str:
    tags = S.all_tags()
    if not tags:
        return f"索引为空或尚未生成。请在 {ROOT} 下运行 python src/update.py"
    descs = S.tag_desc()
    by: dict[str, list[dict]] = {}
    for t in S.load_index():
        if S.is_list(t):
            continue
        for g in t.get("tags") or []:
            by.setdefault(g, []).append(t)
    lines = [f"共 {len(tags)} 个能力位（场景标签），括号内是该场景的工具数。",
             "「例」是这一格目前最硬的几条，先看有没有中意的再决定要不要细看：", ""]
    for name, n in tags:
        row = f"- {name} ({n})"
        d = descs.get(name) or ""
        if d:
            row += f"：{d}"
        ex = sorted(by.get(name, []), key=lambda t: -(t.get("stars") or 0))[:2]
        if ex:
            row += " —— 例：" + "、".join(t.get("name", "") for t in ex)
        lines.append(row)
    lines.append("")
    lines.append("要一次看几个能力位，用 compose_stack(slots=[...])；"
                 "只看一个用 find_tool(tag=...)。")
    return "\n".join(lines)


def tool_compose_stack(args: dict) -> str:
    raw = args.get("slots") or []
    if isinstance(raw, str):
        raw = [x for x in re.split(r"[,，、;；\s]+", raw) if x]
    slots = [str(x).strip() for x in raw if str(x).strip()][:6]
    if len(slots) < 2:
        return ("compose_stack 至少给两个能力位，例如 slots=[\"低代码\",\"认证鉴权\",\"ORM\",\"部署\"]。"
                "只有一格就够的话用 find_tool。")
    try:
        per = int(args.get("per_slot") or 2)
    except (TypeError, ValueError):
        per = 2
    per = max(1, min(per, 4))

    items = S.compose(slots, per_slot=per)
    gaps = [x["slot"] for x in items if not x["label"]]
    expanded: dict[str, int] = {}
    blocks = [f"按 {len(items)} 个能力位各取 {per} 条"
              f"（每格按对症程度排、同分看 star，清单/教程已排除，跨能力位不重复）："]
    for item in items:
        if not item["label"]:
            continue
        via = item.get("via")
        if via:
            expanded[via] = expanded.get(via, 0) + 1
        said = f"由「{via}」展开" if via else f"说「{item['slot']}」"
        blocks.append(f"\n【{item['label']}】{said}，"
                      f"该场景共 {item['total']} 条，取 {len(item['tools'])}：")
        for i, t in enumerate(item["tools"], 1):
            bits = []
            if t.get("stars"):
                bits.append(f"★{t['stars']}")
            if t.get("origin") == "cn":
                bits.append("国内")
            if t.get("lang"):
                bits.append(t["lang"])
            if t.get("pushed_at"):
                bits.append(t["pushed_at"])
            nm = t.get("name", "") + S.slug_suffix(t)
            blocks.append(f"{i}. {nm}  {' · '.join(bits)}")
            if t.get("desc"):
                d = t["desc"]
                blocks.append(f"   {d[:90]}{'…' if len(d) > 90 else ''}")
            if t.get("url"):
                blocks.append(f"   {t['url']}")
        if not item["tools"]:
            blocks.append("   （这一格筛完没有剩余，都已在别的能力位里出现过）")
    if gaps:
        blocks.append("\n缺口：" + "、".join(f"「{g}」" for g in gaps)
                      + f" 在 {S.tag_count()} 个能力位里没有对应场景。这块要么自己写，"
                        "要么换个说法再 find_tool 试一次。")
    for via, cnt in expanded.items():
        blocks.append(f"\n「{via}」不是一个能力位，是横跨 {cnt} 格的上位说法，已按每格一条展开"
                      f"（每格只给 1 条，因为没点名要哪一格）。要更多就在 slots 里直接点"
                      f"具体格子名。")
    blocks.append("\n拼装注意：以上只有元数据（star / 语言 / 维护状态 / 场景标签），"
                  "不含许可协议、部署方式、性能数据，装之前先看项目文档。"
                  "组合时要判断三件事：同一格只能选一个（两个 ORM 不能一起上）、"
                  "生态要对得上（Vue 脚手架配 React 组件库等于白干）、"
                  "先后顺序（先定数据库和框架，再定 UI 库）。")
    return "\n".join(blocks)


def tool_get_stats(args: dict) -> str:
    st = load_json(DATA_DIR / "stats.json", {}) or {}
    tools = load_json(DATA_DIR / "tools.json", []) or []
    if not tools:
        return f"索引为空。请在 {ROOT} 下运行 python src/update.py"

    note = {"fresh": "本次已更新", "cached": "沿用缓存", "skipped": "本次跳过",
            "failed": "抓取失败"}
    srcs = st.get("sources", {})
    lines = [
        f"工具总数: {len(tools)}",
        f"最后刷新: {st.get('updated_at', '未知')}",
    ]
    if srcs:
        # 别只报「跳过」——那是本次运行的动作，不是数据的新旧。
        # 模型看到 skipped 会以为数据过期了。带上每个源的实际抓取时间才说得清。
        asof = st.get("as_of") or {}
        parts = []
        for k, v in srcs.items():
            note2 = note.get(v, v)
            if v in ("skipped", "cached") and asof.get(k):
                note2 += f"，数据截至 {asof[k]}"
            parts.append(f"{k}={note2}")
        lines.append("数据源: " + "，".join(parts))
    bo = st.get("by_origin", {})
    if bo:
        lines.append("来源分布: " + "，".join(
            f"{SRC_CN.get(k, k)} {v}" for k, v in bo.items()))

    # 数据新鲜度提醒，让模型能主动建议刷新
    ts = st.get("updated_at")
    if ts:
        from datetime import datetime
        try:
            age = (datetime.now() - datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")).days
            if age >= 3:
                lines.append(f"注意: 数据已 {age} 天未刷新，建议运行 python src/update.py")
        except ValueError:
            pass

    # 自检结论也报出来，让模型能主动建议修，而不是等彻底不灵了才发现。
    # 太久远的结论不报——那说明自检本身也停了，数据新鲜度那条已经在报了。
    hp = DATA_DIR / "health.json"
    if hp.exists():
        h = load_json(hp, {}) or {}
        try:
            h_age = time.time() - hp.stat().st_mtime
        except OSError:
            h_age = 0
        if h.get("ok") is False and h_age < 7 * 86400:
            fails = h.get("fails") or []
            names = "、".join(str(f.get("item")) for f in fails[:3])
            lines.append(f"注意: 自检（{h.get('at', '时间未知')}）{len(fails)} 项失败——"
                         f"{names}，请运行 python src/doctor.py 查看详情")
    return "\n".join(lines)


def tool_compare_tools(args: dict) -> str:
    names = args.get("names") or []
    if isinstance(names, str):
        names = [x.strip() for x in names.replace("，", ",").split(",") if x.strip()]
    names = [str(n).strip() for n in names if str(n).strip()][:6]
    if len(names) < 2:
        return "至少给两个工具名才能对比，例如 names=['frp','ngrok']。"

    pool = S.load_index()
    # 同名仓库不止一个（如 ngrok 有本体也有 Node 包装库），取 star 最高的那个，
    # 否则用户想对比 ngrok，拿到的可能是某个 2k star 的 wrapper。
    by_name: dict[str, dict] = {}
    for t in pool:
        k = (t.get("name") or "").lower()
        prev = by_name.get(k)
        if prev is None or (t.get("stars") or 0) > (prev.get("stars") or 0):
            by_name[k] = t

    found: list[dict] = []
    missing: list[str] = []
    for nm in names:
        key = nm.lower()
        if key in by_name:
            found.append(by_name[key])
            continue
        # 精确名没命中就用检索兜底（用户可能记的是别名）
        hit = S.search(nm, limit=1)
        if hit:
            found.append(hit[0])
        else:
            missing.append(nm)

    if not found:
        return f"索引里找不到这些工具：{', '.join(missing)}。先用 find_tool 确认名字。"

    lines = [f"对比 {len(found)} 个工具（数据来自本地索引，不含安装方式与费用信息）：", ""]
    for t in found:
        st = t.get("stars") or 0
        star = f"★{st/1000:.1f}k" if st >= 1000 else (f"★{st}" if st else "star 未知")
        lines.append(f"▸ {t.get('name','')}")
        lines.append(f"   {star} · 语言 {t.get('lang') or '未知'} · "
                     f"{_freshness(t.get('pushed_at') or '') or '更新时间未知'} · "
                     f"来源 {SRC_CN.get(t.get('origin',''), t.get('origin',''))}")
        d = _clean_desc(t.get("desc") or "")
        if d:
            lines.append(f"   说明：{d[:200]}")
        if t.get("tags"):
            lines.append(f"   场景：{' / '.join(t['tags'])}")
        if t.get("url"):
            lines.append(f"   {t['url']}")
        lines.append("")

    # 差异提示：把「谁更值得选」从数字里直接算出来，省得模型自己推
    tips = []
    with_star = [t for t in found if t.get("stars")]
    if len(with_star) >= 2:
        top = max(with_star, key=lambda t: t["stars"])
        tips.append(f"star 最高：{top['name']}（{top['stars']}），通常意味着生态和文档更成熟")
    cn = [t for t in found if t.get("origin") == "cn"]
    if cn:
        tips.append(f"国内产品：{'、'.join(t['name'] for t in cn)}——访问快、一般有中文文档，但可能闭源")
    stale = []
    from datetime import date
    for t in found:
        p = t.get("pushed_at") or ""
        try:
            if p and (date.today() - date.fromisoformat(p[:10])).days > 730:
                stale.append(t["name"])
        except ValueError:
            pass
    if stale:
        tips.append(f"超过 2 年未更新：{'、'.join(stale)}——选前先确认是否还有维护")
    if tips:
        lines.append("差异提示：")
        lines.extend(f"- {x}" for x in tips)
    if missing:
        lines.append(f"\n（未找到：{', '.join(missing)}）")
    lines.append("\n注意：这些是索引里的客观信号，不等于适用性判断。"
                 "具体选型还要看是否需要自建服务器、团队技术栈、许可协议——索引里没有这些信息。")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "find_tool",
        "description": (
            "在本地开发工具索引中查找工具。索引含 5426 条，覆盖 GitHub 高星项目、"
            "awesome 人工精选清单、国内 SaaS/AI 产品，按「你想干什么」而非技术栈归类。\n\n"
            "何时使用（漏得最多的是第一种）：\n"
            "1. 用户描述了一个开发任务，而不是「要个工具」——「我想做一个后台管理系统」"
            "「我要抓手机上的包」「帮我做个官网」「怎么把 PDF 里的文字提出来」。"
            "这些句子里没有「工具」二字，但每一句后面都有一堆现成的高星项目。"
            "在准备动手写代码、选技术方案、或回答「我可以帮你写」之前，先调用一次。\n"
            "2. 用户问「有没有能做 X 的工具」「推荐个 X」「用什么工具能 Y」"
            "「找个替代 X 的开源方案」「国内有什么好用的 X」。\n"
            "3. 用户要在几个方案里选，但还没点名具体工具。\n\n"
            "什么样的不需要查：纯代码问题（报错、语法、重构）、用户已经指定了用哪个库、"
            "和「选什么工具」无关的任务。不要为了用工具而用工具。\n\n"
            "为什么值得查：索引带 star 数、语言、最后更新时间、是否国内产品，比记忆可靠；"
            "查一次约 1 秒，比推荐错工具便宜。\n\n"
            "查法：query 直接用中文自然语言即可，系统会自动把口语翻译成场景"
            "（「造测试数据」→「接口 Mock」，「定时任务」→「工作流自动化」，"
            "「让外面访问我的本地服务」→「内网穿透」）。\n\n"
            "如何解读结果：\n"
            "- star 反映流行度，但不等于是最适合你的；小众工具可能更贴合特定需求\n"
            "- 更新时间决定是否还该用：超过 2 年未更新的，要提醒用户风险\n"
            "- 结果是元数据（名称/star/语言/维护状态/场景/链接），"
            "**不含安装方式、费用、许可协议、与同类工具的具体取舍**\n\n"
            "重要边界：拿到的信息不足以回答「怎么装」「要钱吗」「生产能不能用」时，"
            "直接说明索引里没有这些信息、建议看项目文档，不要替我编造。"
            "用户问「A 和 B 该选哪个」时，改用 compare_tools。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "你想做什么，用自然语言。例如：造测试数据、内网穿透、压缩图片、本地跑大模型",
                },
                "tag": {
                    "type": "string",
                    "description": "按场景标签精确过滤，可先用 list_scenarios 查看有哪些。与 query 可同时使用",
                },
                "source": {
                    "type": "string",
                    "enum": ["github", "awesome", "cn"],
                    "description": "限定来源：github=GitHub 项目，awesome=人工精选清单，cn=国内产品",
                },
                "chinese_only": {
                    "type": "boolean",
                    "description": "只看有中文名称或中文描述的工具",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 8，最多 30",
                    "minimum": 1,
                    "maximum": 30,
                },
            },
        },
    },
    {
        "name": "compose_stack",
        "description": (
            "一次查多个能力位，用来「把一件事拆成几样东西配合完成」。\n\n"
            "何时使用：用户要完成的是一个需要多种组件配合的目标，而不是「找一个工具」。"
            "例如「做个后台管理系统」= 脚手架 + 认证鉴权 + ORM/数据库 + 部署；"
            "「做个 AI 客服」= 模型服务 + 大模型应用 + 即时通讯；"
            "「搭个知识库」= 笔记知识库 + 搜索 + 文档协作。\n\n"
            "怎么用：你自己把需求拆成 2-6 个能力位（中文词即可，会自动解析成场景标签），"
            "一次传进来，它按位返回候选 + 报出哪一格在索引里没有（缺口）。"
            "然后你在结果之上做组合取舍，不要只挑一个工具交差。\n\n"
            "拆解要由你判断，这份索引只提供「每个能力位上有什么、有多强」。"
            "不知道有哪些能力位时先调 list_scenarios。\n\n"
            "返回内容是元数据（名称/star/语言/维护状态/场景/链接），"
            "不含许可、费用、部署方式、性能数据——这些要看项目文档，不要编。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "slots": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "能力位，2-6 个。用中文说即可，如 [\"低代码\",\"登录鉴权\",\"数据库\",\"部署\"]",
                },
                "per_slot": {
                    "type": "integer",
                    "description": "每格取几条，默认 2，最多 4",
                },
            },
            "required": ["slots"],
        },
    },
    {
        "name": "compare_tools",
        "description": (
            "并排对比 2-6 个具体工具，给出各自的 star、语言、维护状态、场景归属，"
            "并自动算出差异提示（谁最流行、有没有国内替代、哪些已经停更）。\n\n"
            "何时使用：用户问「A 和 B 选哪个」「X 和 Y 有什么区别」「该用 frp 还是 ngrok」时。"
            "先用 find_tool 确认工具名，再调这个做对比。\n\n"
            "边界：只能对比索引里有的客观元数据，不能替代实际的适用性判断"
            "（是否要自建服务器、许可协议、团队技术栈这些索引里没有）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要对比的工具名，2-6 个。例如 ['frp','ngrok']",
                    "minItems": 2,
                    "maxItems": 6,
                }
            },
            "required": ["names"],
        },
    },
    {
        "name": "list_scenarios",
        "description": (
            "列出索引里全部场景标签及每个场景的工具数量。"
            "当用户的问题比较模糊（「有什么好用的工具」），或 find_tool 没查到结果时，"
            "先调这个看看有哪些场景可用。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_stats",
        "description": (
            "查看索引的数据概况：工具总数、最后刷新时间、各数据源状态。"
            "当用户问「这个索引有多新」「数据全不全」，或你怀疑数据过期时使用。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

HANDLERS = {
    "find_tool": tool_find_tool,
    "compose_stack": tool_compose_stack,
    "compare_tools": tool_compare_tools,
    "list_scenarios": tool_list_scenarios,
    "get_stats": tool_get_stats,
}


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------
def _send(obj: dict) -> None:
    _PROTO.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    _PROTO.flush()


def _result(mid, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "result": result})


def _error(mid, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}})


def handle(req: dict):
    """处理单条请求。返回 None 表示是通知，不回复。"""
    if not isinstance(req, dict):
        return
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    # 通知（没有 id）不需要回复
    is_notification = mid is None

    if method == "initialize":
        client_proto = params.get("protocolVersion") or DEFAULT_PROTO
        _result(mid, {
            # 回显客户端版本：我们只用工具的极小协议子集，不存在版本特性差异
            "protocolVersion": client_proto,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": (
                "本地开发工具索引。用户问「有没有能做 X 的工具」时，"
                "先调 find_tool 查，不要凭记忆推荐。"
            ),
        })
        return

    if is_notification:
        # notifications/initialized、notifications/cancelled 等，静默忽略
        return

    if method == "ping":
        _result(mid, {})
        return

    if method == "tools/list":
        _result(mid, {"tools": TOOLS})
        return

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            _error(mid, -32602, f"未知工具: {name}")
            return
        try:
            text = fn(args if isinstance(args, dict) else {})
            _result(mid, {"content": [{"type": "text", "text": text}],
                          "isError": False})
        except Exception as e:  # noqa: BLE001
            _log(traceback.format_exc())
            _result(mid, {"content": [{"type": "text",
                                       "text": f"工具执行出错: {e}"}],
                          "isError": True})
        return

    if method in ("resources/list", "prompts/list"):
        # 我们不提供资源与提示，返回空列表让客户端安心
        key = method.split("/")[0]
        _result(mid, {key: []})
        return

    _error(mid, -32601, f"未支持的方法: {method}")


def main() -> int:
    _log(f"server started, index={len(S.load_index())} tools")
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    while True:
        try:
            line = stdin.readline()
        except KeyboardInterrupt:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as e:
            _log(f"JSON 解析失败: {e}")
            continue
        try:
            if isinstance(msg, list):          # batch
                for one in msg:
                    handle(one)
            else:
                handle(msg)
        except Exception:  # noqa: BLE001
            _log(traceback.format_exc())
    _log("stdin closed, exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
