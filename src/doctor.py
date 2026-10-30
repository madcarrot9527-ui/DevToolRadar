# -*- coding: utf-8 -*-
"""DevToolRadar 自检。

这套东西的目标是「配置一次以后都不用管」，那就必须能自己发现坏掉：
数据没更新、索引写坏了、MCP 起不来、某个客户端的配置被覆盖、网页没跟着重建。

用法：
    tool doctor          # 或 python src/doctor.py
全通过返回 0，有问题返回 1。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = ROOT / "src"
PY = sys.executable

# 判定阈值
MAX_AGE_DAYS = 3          # 定时任务每天跑，超过 3 天没更新说明断链了
MIN_TOOLS = 1000
MIN_DESC_RATIO = 0.90

results: list[tuple[str, str, str]] = []   # (状态, 项目, 说明)


def rec(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗", "SKIP": "-"}[status]
    line = f"  {mark} {name}"
    if detail:
        line += f"  — {detail}"
    print(line, flush=True)


def _load(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400


# ---------- 1. 数据文件 ----------
def check_data() -> dict:
    print("\n[1] 数据文件")
    tools = _load(DATA / "tools.json")
    if not isinstance(tools, list) or len(tools) < MIN_TOOLS:
        rec("FAIL", "tools.json", f"读不出或条目太少（{len(tools) if isinstance(tools, list) else 'None'}）")
        return {}
    rec("PASS", "tools.json", f"{len(tools)} 条")

    # 新鲜度：3 天内算正常，超过说明定时任务断了
    age = _age_days(DATA / "tools.json")
    if age > MAX_AGE_DAYS:
        rec("WARN", "数据新鲜度",
            f"{age:.1f} 天没刷新，检查每天 03:00 的自动化 / 手动跑 refresh.cmd")
    else:
        rec("PASS", "数据新鲜度",
            f"{age*24:.1f} 小时前刷新（{datetime.fromtimestamp((DATA/'tools.json').stat().st_mtime):%m-%d %H:%M}）")

    for name in ("stats.json", "raw_github.json", "raw_awesome.json", "raw_cn.json"):
        p = DATA / name
        if p.exists():
            rec("PASS", name, f"{_age_days(p):.1f} 天前")
        else:
            rec("WARN", name, "缺失")
    return {"tools": tools}


# ---------- 2. 索引完整性 ----------
def check_index(tools: list[dict]) -> None:
    print("\n[2] 索引完整性")
    if not tools:
        rec("FAIL", "跳过", "没有数据")
        return

    bad_url = [t for t in tools if not str(t.get("url") or "").startswith("http")]
    if bad_url:
        rec("FAIL", "URL 格式", f"{len(bad_url)} 条不是 http(s) 开头")
    else:
        rec("PASS", "URL 格式", f"{len(tools)} 条全部有效")

    seen: dict[tuple, int] = {}
    for t in tools:
        k = ((t.get("name") or "").lower(), (t.get("url") or "").lower())
        seen[k] = seen.get(k, 0) + 1
    dup = {k: v for k, v in seen.items() if v > 1}
    if dup:
        rec("WARN", "重复条目", f"{len(dup)} 组同名同址")
    else:
        rec("PASS", "重复条目", "无同名同址")

    # 仓库搬家：归一化按 gh_moved.json 把旧地址并进新地址。这里反过来验一遍 ——
    # 记录里成对的地址如果又同时出现在索引里，说明合并没生效（或记录被绕开），
    # 用户会看到两个「同一个工具」。
    mv = (_load(ROOT / "data" / "gh_moved.json", {}) or {}).get("moved") or {}
    urls = {(t.get("url") or "").lower().rstrip("/") for t in tools}
    left = [o for o, n in mv.items() if o.lower() in urls and n.lower() in urls]
    if left:
        rec("FAIL", "仓库搬家", f"{len(left)} 组旧地址没并进新地址：{left[:3]}")
    else:
        rec("PASS", "仓库搬家", f"{len(mv)} 条记录，旧地址都已并走")

    # 标签必须在配置里存在，否则网页点进去是空列表
    cfg = _load(ROOT / "config" / "scenarios.json", {}) or {}
    known = {t["label"] for t in cfg.get("tags", []) if "label" in t}
    unknown: dict[str, int] = {}
    for t in tools:
        for g in t.get("tags") or []:
            if g not in known:
                unknown[g] = unknown.get(g, 0) + 1
    if unknown:
        rec("FAIL", "标签一致性", f"索引里有配置中没有的标签：{list(unknown)[:5]}")
    else:
        rec("PASS", "标签一致性", f"{len(known)} 个标签全部有定义")

    # 说明完整性。原来只看「有没有」——只要 desc 非空就算过，于是一条
    # 写着 "ORM"、一条写着 "NOW MANAGED ON CODEBERG" 都算满分。
    # 现在分开看：空说明是硬伤，重复说明说明抓取把同一段文本套在了多个条目上。
    empty_desc = [t for t in tools if not (t.get("desc") or "").strip()]
    ratio = (len(tools) - len(empty_desc)) / len(tools)
    if empty_desc:
        rec("WARN" if ratio >= MIN_DESC_RATIO else "FAIL", "说明覆盖率",
            f"{ratio*100:.1f}%（{len(empty_desc)} 条没有说明）")
    else:
        rec("PASS", "说明覆盖率", f"{ratio*100:.1f}%，全部有说明")

    seen: dict[str, list[str]] = {}
    for t in tools:
        d = (t.get("desc") or "").strip()
        if len(d) >= 20:
            seen.setdefault(d, []).append(t.get("name", ""))
    dups = [v for v in seen.values() if len(v) > 1]
    if dups:
        sample = "、".join("+".join(v[:3]) for v in dups[:2])
        rec("WARN", "说明重复", f"{len(dups)} 组共用同一段说明：{sample}")
    else:
        rec("PASS", "说明重复", "没有多个条目共用同一段说明")

    # 人工表腐化检查：表里写了 URL，索引里却没有这条，这条人工结论就是死代码。
    # 多半是条目改名、换仓库、或者下架表重复收录了它，下次改数据时会被它误导。
    table = _load(ROOT / "config" / "manual_desc.json", {}) or {}
    by_url = table.get("by_url") or {}
    if by_url:
        have = {(t.get("url") or "").rstrip("/").lower() for t in tools}
        dead = [u for u in by_url if u.rstrip("/").lower() not in have]
        if dead:
            rec("WARN", "人工说明表",
                f"{len(dead)} 条对不上索引里的条目，例：{dead[0][:60]}")
        else:
            rec("PASS", "人工说明表", f"{len(by_url)} 条全部命中")


# ---------- 3. MCP 服务 ----------
def check_mcp() -> None:
    print("\n[3] MCP 服务")
    server = SRC / "mcp_server.py"
    if not server.exists():
        rec("FAIL", "mcp_server.py", "文件不存在")
        return
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "doctor", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "find_tool", "arguments": {"query": "抓包"}}},
    ]
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in reqs) + "\n"
    try:
        p = subprocess.run([PY, str(server)], input=payload.encode("utf-8"),
                           capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        rec("FAIL", "握手", "超时（180s）")
        return

    lines = [l for l in p.stdout.decode("utf-8", "replace").split("\n") if l.strip()]
    illegal = [l for l in lines if not l.lstrip().startswith("{")]
    if illegal:
        rec("FAIL", "协议纯净度", f"{len(illegal)} 行不是 JSON（stdout 被污染）")
    else:
        rec("PASS", "协议纯净度", f"{len(lines)} 行响应全部合法")

    rep = {}
    for l in lines:
        try:
            rep[json.loads(l).get("id")] = json.loads(l)
        except Exception:
            continue
    if 2 not in rep:
        rec("FAIL", "tools/list", "没有响应")
        return
    tools = rep[2]["result"].get("tools", [])
    names = [t["name"] for t in tools]
    expect = {"find_tool", "compare_tools", "list_scenarios", "get_stats"}
    if not expect.issubset(set(names)):
        rec("FAIL", "工具清单", f"缺 {expect - set(names)}；实际 {names}")
    else:
        rec("PASS", "工具清单", "、".join(names))

    if 3 in rep:
        txt = rep[3]["result"]["content"][0]["text"]
        if "mitmproxy" in txt or "工具" in txt:
            rec("PASS", "冒烟查询", "find_tool(抓包) 有结果")
        else:
            rec("WARN", "冒烟查询", f"结果异常：{txt[:60]}")
    else:
        rec("FAIL", "冒烟查询", "find_tool 没有响应")


# ---------- 4. 客户端配置 ----------
CLIENTS = [
    ("Codex", Path.home() / ".codex" / "config.toml", "toml"),
    ("WorkBuddy", Path.home() / ".workbuddy" / "mcp.json", "json"),
    ("VSCode", Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "mcp.json", "json"),
]

# 配置里出现的绝对路径：Windows 盘符形式，或类 Unix 绝对路径
_PATH_RE = re.compile(r"[A-Za-z]:/[^'\"\s,\]}]+|/[^\s'\"\[\],}]+/?[^\s'\"\[\],}]*")
_PY_RE = re.compile(r"pythonw?(?:\.exe)?$", re.IGNORECASE)


def _norm(s: str) -> str:
    """路径比较用：统一正斜杠、不分大小写（Windows 语义）。"""
    return s.replace("\\", "/").lower()


def _paths_in(text: str) -> list[str]:
    """把配置文本里的绝对路径抽出来。

    toml 用单引号、json 用双引号，还有 args 数组里的逗号，所以先统一按正斜杠看，
    再把尾巴上的标点削掉。不同客户端的引号习惯不一样，不按格式解析，只取路径。
    """
    out: list[str] = []
    for m in _PATH_RE.finditer(text.replace("\\", "/")):
        p = m.group(0).rstrip(";,)\"'").strip()
        if len(p) > 3:
            out.append(p)
    return out


def check_clients() -> None:
    print("\n[4] 客户端配置")
    for label, path, _kind in CLIENTS:
        if not path.exists():
            # 客户端根本没装。这在别人 clone 下来的机器上是常态，不是故障——
            # 报 WARN 会让自检看起来一直在报警，久了就没人看它了。
            rec("SKIP", label, "未安装（不走 MCP 也能用 CLI 和网页）")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as e:
            rec("FAIL", label, f"读不了：{e}")
            continue
        if "devtoolradar" not in text:
            rec("FAIL", label, "配置里没有 devtoolradar 条目（可能被覆盖了）")
            continue

        # 判据是「配置指向的文件真的存在」，不是「字符串跟我这边一样」。
        # 配置里存正斜杠、sys.executable 在 Windows 上是反斜杠，直接比必假报警；
        # 而且真正会让客户端起不来的是路径指向的文件被删了（换解释器、挪目录），
        # 那种情况字符串照样能对上，只有落盘检查才发现。
        paths = _paths_in(text)
        pys = [p for p in paths if _PY_RE.search(p)]
        srvs = [p for p in paths if p.lower().endswith("mcp_server.py")]
        bad: list[str] = []
        if not pys:
            bad.append("没写 python 路径")
        elif not any(Path(p).exists() for p in pys):
            bad.append(f"python 不存在：{pys[0]}")
        if not srvs:
            bad.append("没写 mcp_server.py 路径")
        elif not any(Path(p).exists() for p in srvs):
            bad.append(f"服务脚本不存在：{srvs[0]}")

        if bad:
            rec("FAIL", label, "；".join(bad))
        elif _norm(str(PY)) not in _norm(text):
            # 路径存在但不是当前用于自检的解释器——不一定错，只是要知道
            rec("PASS", label, f"路径有效（解释器非当前：{Path(pys[0]).name if pys else '?'}）")
        else:
            rec("PASS", label, path.name)


# ---------- 5. 网页 ----------
def check_site() -> None:
    print("\n[5] 网页")
    html = ROOT / "site" / "index.html"
    if not html.exists():
        rec("WARN", "index.html", "还没生成，跑 tool open 会失败")
        return
    size = html.stat().st_size / 1024
    rec("PASS", "index.html", f"{size:.0f} KB")
    t_tools = (DATA / "tools.json").stat().st_mtime
    if html.stat().st_mtime + 5 < t_tools:
        rec("WARN", "站点同步", "网页比数据旧，需要重新生成")
    else:
        rec("PASS", "站点同步", "网页与数据一致")


# ---------- 6. 检索冒烟 ----------
SMOKE = [
    ("内网穿透", "frp"),
    ("接口 mock", "json-server"),
    ("抓包", "mitmproxy"),
    ("压测", "wrk"),
    ("抠图", "rembg"),
    ("本地大模型", "ollama"),
]


def check_search() -> None:
    print("\n[6] 检索冒烟")
    sys.path.insert(0, str(SRC))
    try:
        import search
    except Exception as e:
        rec("FAIL", "导入检索模块", str(e))
        return
    bad = []
    for q, expect in SMOKE:
        try:
            hits = search.search(q, limit=5)
        except Exception as e:
            bad.append(f"{q}（异常 {e}）")
            continue
        names = [(t.get("name") or "").lower() for t in hits]
        if expect not in names:
            bad.append(f"{q}→期望 {expect}，实际 {names[:3]}")
    if bad:
        rec("FAIL", f"冒烟 {len(SMOKE)} 条", "；".join(bad[:2]))
    else:
        rec("PASS", f"冒烟 {len(SMOKE)} 条", "期望工具都在前 5")


def _write_health(fails: list, warns: list) -> None:
    """把结论落成 health.json。

    自检如果只往日志里写，就没人看得见——每天跑一次、日志烂在 logs/ 下，
    等于没检。落一份快照，让 `tool find` 和 MCP 的 get_stats 能顺口提一句，
    这才是「配置一次不用管」的闭环：坏了会在你正常用的时候自己露出来。
    """
    try:
        payload = {
            "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": not fails,
            "checked": len(results),
            "fails": [{"item": n, "detail": d} for _s, n, d in fails],
            "warns": [{"item": n, "detail": d} for _s, n, d in warns],
        }
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "health.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass   # 自检本身不该因为写不了快照就崩


def main() -> int:
    print("DevToolRadar 自检")
    print("=" * 52)
    d = check_data()
    check_index(d.get("tools") or [])
    check_mcp()
    check_clients()
    check_site()
    check_search()

    fails = [r for r in results if r[0] == "FAIL"]
    warns = [r for r in results if r[0] == "WARN"]
    skips = [r for r in results if r[0] == "SKIP"]
    passed = len(results) - len(fails) - len(warns) - len(skips)
    _write_health(fails, warns)

    # 跳过项不算通过，也不算问题：它代表「这台机器上没有这个客户端」。
    # 所以「全部通过」这个词只在真的没有跳过项时才用。
    detail = f"{passed} 项通过"
    if skips:
        detail += f"，{len(skips)} 项跳过"
    if warns:
        detail += f"，{len(warns)} 项警告"

    print("\n" + "=" * 52)
    if fails:
        print(f"结论：FAIL —— {len(fails)} 项失败（{detail}）")
        for _s, name, _d in fails:
            print(f"   失败：{name} {_d}")
        return 1
    if warns or skips:
        print(f"结论：PASS —— {detail}")
        for _s, name, d in warns:
            print(f"   警告：{name} {d}")
        return 0
    print(f"结论：PASS —— {len(results)} 项全部通过")
    return 0


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
