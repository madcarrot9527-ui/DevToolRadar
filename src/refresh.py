"""每日刷新入口（可执行、可验证）。

为什么要有这个文件：原来只有 refresh.cmd，而 .bat 只能在 cmd.exe 里跑。
本机的 cmd.exe 从 Bash 和 PowerShell 两个方向都被安全策略挡着，导致这段逻辑
从来没被执行过一次——`logs/refresh.log` 一直不存在。一个没人跑过的入口
等于没有入口，「配置一次以后都不用管」这句话就无从验证。

现在逻辑放在这里，用同一个解释器直接跑：

    python src/refresh.py            # 完整刷新：抓取 → 打标 → 生成网页 → 自检
    python src/refresh.py --daily    # 日常轻量：只抓 GitHub，awesome/cn 沿用缓存
    python src/refresh.py --fast     # 只重建，不联网（改配置后验证用）

为什么分 --daily 和完整刷新（2026-11-19 加）：
    各源的数据变化速度差两个数量级，每天全量重抓等于把周级、月级的数据按日级
    成本付费。74 个 topic 查询占整轮 60% 的时间，但它产出的「新工具」本来就是
    个位数；awesome 12 个清单和国内 167 个站点的存活状态更是周/月级变化。
    所以：每天 --daily（只 GitHub，约 15 分钟），每周日跑一次完整刷新把
    awesome/cn 补上。跳过不等于丢数据——update.py 里 skip 的源会读
    data/raw_*.json 沿用上次结果，索引条数不会因为 --daily 而变少。

计划任务直接指向 `python.exe src/refresh.py` 即可，不经过 cmd.exe。
refresh.cmd 保留，改成一行调用本文件，兼容已经配好的旧任务。

退出码约定：取 update 的退出码。doctor 失败只写警告，不改退出码——
自检红不代表数据坏，数据已经在盘上了，不该因此让计划任务报错重试。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run(name: str, script: Path, extra: list[str], logf) -> int:
    """跑一个子脚本，把它的输出**实时**转发进日志。返回退出码。

    为什么不用 subprocess.run(capture_output=True)：那样要等子进程结束才拿到输出，
    跑一次完整抓取十几分钟，这期间日志是空的——真卡死了也不知道卡在哪一步。
    改成 Popen + 逐行读，日志跟着进度走，事后还能对时间戳定位慢在哪。
    """
    cmd = [sys.executable, str(script)] + extra
    logf.write(f"[{name}] $ {' '.join(cmd)}\n")
    logf.flush()
    t0 = time.time()
    env = dict(os.environ)
    # 子进程输出要落进文件，必须钉死 UTF-8，否则中文日志变成一串问号；
    # 再关掉缓冲，否则子进程攒满一个 block 才吐一次，实时转发就失效了。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    try:
        p = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert p.stdout is not None
        for line in p.stdout:
            logf.write(line if line.endswith("\n") else line + "\n")
            logf.flush()
        rc = p.wait()
    except Exception as e:  # noqa: BLE001  子进程没起来也要留痕，不能静默
        logf.write(f"!! 启动失败：{e}\n")
        rc = 127
    logf.write(f"[{name}] 退出码 {rc}，用时 {time.time() - t0:.1f}s\n\n")
    logf.flush()
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="DevToolRadar 每日刷新")
    ap.add_argument("--fast", action="store_true",
                    help="不联网，只用缓存重建（改配置后验证用）")
    ap.add_argument("--daily", action="store_true",
                    help="日常轻量：只抓 GitHub，awesome/cn 沿用缓存（每周跑一次完整刷新）")
    args = ap.parse_args()

    if args.fast and args.daily:
        print("--fast 与 --daily 不能同时用：前者完全不联网，后者要抓 GitHub。")
        return 2

    LOGS.mkdir(parents=True, exist_ok=True)
    logf = open(LOGS / "refresh.log", "a", encoding="utf-8")

    logf.write(f"========== {_now()} start ==========\n")
    logf.write(f"python: {sys.executable}\n")
    logf.write(f"cwd   : {ROOT}\n")
    # 模式写进日志头部：事后翻日志时，「awesome 是 skipped」到底是本次轻量模式的
    # 设计，还是抓取失败，只看 update 段是分不出来的。
    mode = "fast（不联网，仅重建）" if args.fast else (
        "daily（仅 GitHub，awesome/cn 沿用缓存）" if args.daily else "full（三源全抓）")
    logf.write(f"mode  : {mode}\n")
    logf.flush()

    # --fast 靠 data/raw_*.json 重建，而那些文件不入库（可重新抓，且每天刷新
    # 会产生几 MB 的 diff）。刚 clone 下来的仓库没有它们，这时候跑 --fast
    # 等于拿空数据重建索引，所以先拦住，别让它跑到写盘那一步。
    if args.fast:
        missing = [n for n in ("raw_github.json", "raw_awesome.json", "raw_cn.json")
                   if not (ROOT / "data" / n).exists()]
        if missing:
            msg = ("--fast 需要 data/ 下的缓存，缺：" + "、".join(missing) +
                   "。先跑一次完整刷新：python src/refresh.py")
            print(msg)
            logf.write("[中止] " + msg + "\n")
            logf.close()
            return 2

    # --daily 跳过 awesome/cn，改成沿用它们的落盘缓存。缓存缺失时索引会从
    # 6800 多掉到 github 单源的 3900 多，update.py 的 10% 骤降闸会拒绝写盘
    # ——那是正确的保护，但报错位置离现场太远，所以在开头就把话说清楚。
    if args.daily:
        missing = [n for n in ("raw_awesome.json", "raw_cn.json")
                   if not (ROOT / "data" / n).exists()]
        if missing:
            msg = ("--daily 需要 awesome/cn 的落盘缓存来沿用，缺：" + "、".join(missing) +
                   "。先跑一次完整刷新：python src/refresh.py")
            print(msg)
            logf.write("[中止] " + msg + "\n")
            logf.close()
            return 2

    extra: list[str] = []
    if args.fast:
        extra = ["--no-github", "--no-awesome", "--no-cn"]
    elif args.daily:
        # 只抓 GitHub。awesome（12 个清单）和 cn（167 个站点存活校验）是周/月级
        # 数据，每天重抓纯属白费请求；跳过时 update.py 会读 raw_*.json 沿用上次
        # 结果，索引条数不变，只是 stats 里该源状态记成 skipped。
        extra = ["--no-awesome", "--no-cn"]
    # 补 GitHub 元数据放在 update 之前：update 会读 gh_meta.json 把星数/语言/更新时间
    # 填进索引，所以缓存必须先更新好。代价是「这次新抓进来的条目」要等明天才补上，
    # 换来的是流程简单、不会出现「归一化两遍」的绕法。
    #
    # --moved 一起带上：星数补齐后，「旧组织 / 新组织」的重复条目会同时浮上来，
    # 而合并它们唯一的硬证据是 GitHub 自己的 301。只探同名不同 owner 的组，
    # 查过的地址 30 天内不再探，日常增量基本是零。
    #
    # --reserve-core 40：gh_meta 补 language 用的是和必收录抓取同一个 core 配额池
    # （未认证 60 次/小时）。之前不留额度，gh_meta 一口气把 60 次全吃在 language 上，
    # 必收录就只能整批吃 403（11-18 必收录 0/6、11-19 22/25 都卡在这），而
    # 「工具在不在库里」比「语言标签好不好看」重要得多。留 40 次：30 天 TTL 下
    # 必收录平均每天只需 4~5 个，40 足够覆盖一周积压。
    if not args.fast:
        _run("gh_meta", ROOT / "src" / "fetch_gh_meta.py",
             ["--api", "--moved", "--reserve-core", "40"], logf)
    rc_u = _run("update", ROOT / "src" / "update.py", extra, logf)
    rc_d = _run("doctor", ROOT / "src" / "doctor.py", [], logf)

    logf.write(f"========== {_now()} done (update={rc_u} doctor={rc_d}) ==========\n")
    if rc_d != 0:
        logf.write("[警告] 自检未通过，详见上文 doctor 段\n")
    if rc_u != 0:
        logf.write("[警告] 刷新异常，索引可能仍是上一版\n")
    logf.close()

    print(f"update={rc_u} doctor={rc_d}，日志：{LOGS / 'refresh.log'}")
    return rc_u


if __name__ == "__main__":
    raise SystemExit(main())
