"""热重载验收：改代码/改配置，进程不重启，看输出是否跟着变。

为什么要这个脚本：MCP 是常驻 stdio 进程，「改完要重启客户端」这件事最烦的地方在于
改完立刻验证的时候 —— 而这正是最需要连续改的时候。mcp_server 现在每次请求前比对
src/*.py 与 config/*.json 的 mtime，变了就地重载。

热重载这种东西不能靠「看起来对了」：它有三条静默失败路径，都实测踩过 ——
  1. reload(__main__) 抛 spec not found（reload 内部会用 _find_spec 覆盖 __spec__）
  2. 用 exec 重进模块后，模块全局被重置，监控基线丢失，第二次改动被吃成新基线
  3. 没有启动闸，exec 会重进 main() 抛 SystemExit
所以这里用两个探针各测一遍「改 → 变 → 改回 → 变回」，任何一步不动就 FAIL。

跑法：python tools/test_mcp_hotreload.py
注意：脚本会临时改写 config/concepts.json 和 src/mcp_server.py 再还原，
      中途被强杀可能留下探针字符串 —— 用 finally 兜底，正常退出一定还原。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "mcp_server.py"
CFG = ROOT / "config" / "concepts.json"
PY = sys.executable


def start():
    return subprocess.Popen([PY, str(SRC)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", bufsize=1)


def send(p, obj):
    p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
    p.stdin.flush()


def recv(p):
    while True:
        line = p.stdout.readline()
        if not line:
            return None
        if line.strip():
            return json.loads(line)


def call(p, mid, name, args=None):
    send(p, {"jsonrpc": "2.0", "id": mid, "method": "tools/call",
             "params": {"name": name, "arguments": args or {}}})
    return recv(p)["result"]["content"][0]["text"]


def sections(text: str) -> int:
    return sum(1 for ln in text.split("\n") if "【" in ln)


def probe_config(p, fails):
    """探针一：config 改动。concepts 是模块级缓存，只有重载才会变。"""
    slots = ["美术设计", "桌面应用开发"]
    n1 = sections(call(p, 2, "compose_stack", {"slots": slots, "per_slot": 2}))

    orig = CFG.read_text(encoding="utf-8")
    try:
        d = json.loads(orig)
        d["concepts"]["美术设计"] = ["AI 绘图", "图标素材"]
        CFG.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                       encoding="utf-8", newline="\n")
        time.sleep(1.2)
        n2 = sections(call(p, 3, "compose_stack", {"slots": slots, "per_slot": 2}))
    finally:
        CFG.write_text(orig, encoding="utf-8", newline="\n")
    time.sleep(1.2)
    n3 = sections(call(p, 4, "compose_stack", {"slots": slots, "per_slot": 2}))

    ok = n1 == 7 and n2 == 3 and n3 == 7
    print(f"  配置探针：{n1} 格 → 改后 {n2} 格 → 改回 {n3} 格  "
          f"{'PASS' if ok else 'FAIL（应为 7→3→7）'}")
    if not ok:
        fails.append("配置热重载")
    return 4


def probe_source(p, fails, mid):
    """探针二：改 mcp_server.py 自身。这条最难 —— 直接跑的模块是 __main__。"""
    orig = SRC.read_text(encoding="utf-8")
    old = 'f"工具总数: {len(tools)}"'
    new = 'f"工具总数【热重载生效】: {len(tools)}"'
    if old not in orig:
        print("  源码探针：探针字符串没找到，跳过（改过输出格式就要同步这里）")
        return mid
    try:
        a = call(p, mid, "get_stats").split("\n")[0]
        mid += 1
        SRC.write_text(orig.replace(old, new), encoding="utf-8", newline="\n")
        time.sleep(1.2)
        b = call(p, mid, "get_stats").split("\n")[0]
        mid += 1
    finally:
        SRC.write_text(orig, encoding="utf-8", newline="\n")
    time.sleep(1.2)
    c = call(p, mid, "get_stats").split("\n")[0]
    mid += 1

    ok = ("热重载生效" in b) and ("热重载生效" not in a) and ("热重载生效" not in c)
    print(f"  源码探针：改后 {'变了' if '热重载生效' in b else '没变'} / "
          f"改回 {'还原了' if '热重载生效' not in c else '没还原'}  "
          f"{'PASS' if ok else 'FAIL'}")
    if not ok:
        fails.append("源码热重载")
    return mid


def main() -> int:
    fails: list[str] = []
    p = start()
    try:
        send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                            "clientInfo": {"name": "hotreload", "version": "1"}}})
        recv(p)
        mid = probe_config(p, fails)
        probe_source(p, fails, mid)
    finally:
        # 无论成败都把两个探针文件还原，别把仓库留在半改状态
        p.stdin.close()
        err = p.stderr.read() if p.stderr else ""
        p.wait(timeout=5)

    bad = [ln for ln in err.split("\n") if "失败" in ln]
    if bad:
        print("  重载失败日志：")
        for ln in bad[:4]:
            print("   ", ln.strip()[:120])

    print("\n结论：" + ("PASS —— 改代码/改配置都不用重启" if not fails
                      else f"FAIL —— {fails}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
