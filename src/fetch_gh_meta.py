"""给「URL 指向 GitHub 仓库、但没抓到元数据」的条目补星数、语言、更新时间；
顺带确认哪些仓库搬过家（旧地址 301 到新地址），供归一化阶段合并重复条目。

为什么会有这批条目：`fetch_github` 是按 topic 搜索的，`fetch_awesome` 只解析 README
根本不调 API。于是精选清单里那批 star 不够高、没被 topic 覆盖的仓库，进来时只有
「名字 + 一句话」，星数、语言、更新时间全是空的 —— 卡片上光秃秃的，用户没法判断
这工具还能不能用。

三个数据源，按性价比排序：
  1. ungh.cc —— 不限速的 GitHub 代理，能拿 star / pushedAt / description，**没有 language**
  2. GitHub core API —— 能拿全部，但无 token 只有 60 次/小时
  3. 本地缓存 data/gh_meta.json —— 拿过的就不再请求

结果写进 `data/gh_meta.json`，**不改 raw_*.json**。raw 是「这次抓到了什么」的存档，
混进事后补的数据会让它失去可信度；normalize 阶段读这份缓存作为补充证据。

用法：
    python src/fetch_gh_meta.py               # 补所有缺口（先走代理，代理拿不到的走 API）
    python src/fetch_gh_meta.py --limit 50    # 只补 50 条（试探用）
    python src/fetch_gh_meta.py --stats       # 只看缺口和缓存现状
    python src/fetch_gh_meta.py --moved       # 只查仓库搬家（旧地址是否 301 到新地址）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "gh_meta.json"
MOVED = DATA / "gh_moved.json"
# 搬家记录查过一次就够，但半年后可能又搬一次，所以留个复查周期。
RECHECK_DAYS = 30

GH_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)$")
UA = "DevToolRadar/1.0 (+data enrichment)"
UNGH = "https://ungh.cc/repos/{}"
API_REPO = "https://api.github.com/repos/{}"


def _get(url: str, timeout: int = 20) -> dict | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"_missing": True}
        if e.code in (403, 429):
            return {"_ratelimited": True}
        return None
    except Exception:
        return None


def _iso_date(s: str) -> str:
    """把 GitHub 的 2026-10-29T10:16:58Z 压成 2026-10-29。"""
    s = (s or "").strip()
    return s[:10] if len(s) >= 10 else ""


def _via_ungh(slug: str) -> dict | None:
    d = _get(UNGH.format(slug))
    if not d or d.get("_missing"):
        return {"missing": True}
    if d.get("_ratelimited"):
        return None
    r = d.get("repo") or d
    out = {
        "stars": int(r.get("stars") or 0),
        "pushed_at": _iso_date(r.get("pushedAt") or r.get("updatedAt") or ""),
        "desc": (r.get("description") or "").strip(),
        "src": "ungh",
    }
    return out if out["stars"] or out["pushed_at"] else None


def _via_api(slug: str) -> dict | None:
    d = _get(API_REPO.format(slug))
    if not d:
        return None
    if d.get("_missing"):
        return {"missing": True}
    if d.get("_ratelimited"):
        return None
    return {
        "stars": int(d.get("stargazers_count") or 0),
        "pushed_at": _iso_date(d.get("pushed_at") or ""),
        "lang": d.get("language") or "",
        "desc": (d.get("description") or "").strip(),
        "src": "api",
    }


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cache(c: dict) -> None:
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=0, sort_keys=True),
                     encoding="utf-8")


def gaps(tools: list[dict]) -> list[tuple[str, str]]:
    """返回 [(slug, url)]：URL 是 GitHub 仓库根、且星数或更新时间缺的。"""
    out, seen = [], set()
    for t in tools:
        u = (t.get("url") or "").rstrip("/")
        m = GH_RE.match(u)
        if not m:
            continue
        if t.get("stars") and t.get("pushed_at"):
            continue
        slug = f"{m.group(1)}/{m.group(2)}"
        if slug.lower() in seen:
            continue
        seen.add(slug.lower())
        out.append((slug, u))
    return out


# ---------- 仓库搬家探测 ----------
# 补元数据之后冒出来的新问题：星数补齐了，一批「旧组织 / 新组织」的重复条目
# 也从地板下面浮上来并排显示。搜「终端」时 jwilm/alacritty 和
# alacritty/alacritty 占着第 2、3 名，说明文字还不一样，用户会当成两个工具。
#
# 判据的坑：星数接近不能当证据。express-typescript 的两条星数差 12、仓库名
# 一样，但确实是两个不同项目（edwinhern vs GeekyAnts）。名字相同也不行。
#
# 唯一硬证据是 GitHub 自己说的话：仓库换组织或改名后，旧地址 301 到新地址。
# 只探「名字看起来是同一个仓库」的组（首次 60 组 132 个地址，36 秒），不必全库探；
# 探过的地址 30 天内不再探，日常增量是零。

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # noqa: D401  签名由父类决定
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _probe(url: str, tries: int = 3) -> tuple[int, str]:
    """返回 (状态码, 跳转目标)。不跟随跳转，只看 GitHub 怎么回答。

    本机对 github.com 的 TLS 握手偶发 UNEXPECTED_EOF，所以重试 3 次；
    仍然失败就返回 -1，交给调用方留旧结论（不把「没探到」当成「没搬家」）。
    """
    for i in range(tries):
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; DevToolRadar/1.0)"})
        try:
            with _OPENER.open(req, timeout=20) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            return e.code, (e.headers.get("Location") or "")
        except Exception:
            if i == tries - 1:
                return -1, ""
            time.sleep(0.6 * (i + 1))
    return -1, ""


def _same_name_groups(tools: list[dict]) -> dict[str, list[str]]:
    """按仓库名分组，只留「看起来像同一个仓库的两条地址」的组。

    两个维度都算同一组：
      - 名字完全相同（jwilm/alacritty vs alacritty/alacritty，换组织）
      - 去掉连字符/下划线/点之后相同（oh-my-zsh vs ohmyzsh，换组织顺便改名）
    第二种差点漏掉：robbyrussell/oh-my-zsh 301 到 ohmyzsh/ohmyzsh，两条并排在
    搜「终端」的前三名里，用户看到的是同一个项目的两份。

    owner 和名字一起改的还是会漏，但那种连名字都对不上，靠名字分组本身就兜不住。
    """
    by: dict[str, list[str]] = {}
    for t in tools:
        u = (t.get("url") or "").rstrip("/")
        m = GH_RE.match(u)
        if not m:
            continue
        key = re.sub(r"[-_.]", "", m.group(2).lower())
        by.setdefault(key, []).append(u)
    return {k: sorted(set(v)) for k, v in by.items() if len(set(v)) > 1}


def load_moved() -> dict:
    try:
        d = json.loads(MOVED.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    d.setdefault("moved", {})
    d.setdefault("checked", {})
    return d


def save_moved(d: dict) -> None:
    d["updated"] = date.today().isoformat()
    MOVED.write_text(json.dumps(d, ensure_ascii=False, indent=0, sort_keys=True),
                     encoding="utf-8")


def _stale(day: str) -> bool:
    try:
        return (date.today() - date.fromisoformat(day)).days >= RECHECK_DAYS
    except Exception:
        return True


def check_moved(tools: list[dict], workers: int = 8, delay: float = 0.1) -> int:
    """探「名字看起来是同一个仓库」的组，把确认的搬家记进 data/gh_moved.json。

    只记「两个地址都在索引里」的跳转 —— 那才叫重复条目。跳到索引外的
    （docker/github-actions → docker-archive/github-actions）说明这条只是
    改过名，索引里没有第二条，合并不需要它，记了也没人消费。

    记录**不随索引收缩而删除**：归一化用的是 raw_*.json，旧地址哪天被重新
    抓回来，这条记录还得在，否则重复条目会重新冒出来。
    """
    groups = _same_name_groups(tools)
    urls = sorted({u for v in groups.values() for u in v})
    state = load_moved()
    moved, checked = state["moved"], state["checked"]
    todo = [u for u in urls if _stale(checked.get(u, ""))]
    print(f"疑似同一仓库的组 {len(groups)} 个，涉及 {len(urls)} 个地址；"
          f"本次要探 {len(todo)} 个（其余 {RECHECK_DAYS} 天内探过）")
    if not todo:
        print(f"搬家记录 {len(moved)} 条，无新增")
        return 0

    known = {u.lower() for u in urls}
    today = date.today().isoformat()
    found, failed = 0, 0
    t0 = time.time()

    def work(u):
        time.sleep(delay)
        return u, _probe(u)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for u, (code, loc) in ex.map(work, todo):
            checked[u] = today
            if code == -1:
                failed += 1
                # 探不到就别改结论：把上次的探访日期退回去，下次还会重试
                checked.pop(u, None)
                continue
            if code != 301 or not loc:
                continue
            loc = loc.rstrip("/")
            if not GH_RE.match(loc) or loc.lower() not in known:
                continue
            moved[u] = loc
            found += 1
            print(f"  搬家：{u} → {loc}")

    save_moved(state)
    el = time.time() - t0
    print(f"探测完成：新确认搬家 {found} 条，探不到的 {failed} 条，"
          f"累计 {len(moved)} 条，用时 {el:.0f}s")
    print(f"记录文件：{MOVED}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="补 GitHub 元数据")
    ap.add_argument("--limit", type=int, default=0, help="最多补多少条")
    ap.add_argument("--workers", type=int, default=4, help="并发数")
    ap.add_argument("--delay", type=float, default=0.12, help="每次请求前的延迟（秒）")
    ap.add_argument("--stats", action="store_true", help="只看现状")
    ap.add_argument("--api", action="store_true",
                    help="同时走 GitHub API 补 language（会消耗 60/h 配额）")
    ap.add_argument("--moved", action="store_true",
                    help="查仓库搬家：疑似同一仓库的旧地址是否 301 到新地址")
    args = ap.parse_args()

    tools = json.loads((DATA / "tools.json").read_text(encoding="utf-8"))

    # --moved 单独用时只做搬家探测，不去补元数据 —— 两个动作的耗时差一个量级，
    # 混在一起会让人以为查个重也要跑五分钟。
    if args.moved and not args.api:
        return check_moved(tools)

    todo = gaps(tools)
    cache = load_cache()

    hit = [s for s, _ in todo if s.lower() in {k.lower() for k in cache}]
    miss = [(s, u) for s, u in todo if s.lower() not in {k.lower() for k in cache}]

    print(f"索引 {len(tools)} 条")
    print(f"缺元数据的 GitHub 仓库 {len(todo)} 个：缓存里已有 {len(hit)}，待补 {len(miss)}")
    if args.stats:
        no_lang = sum(1 for v in cache.values() if not v.get("lang"))
        print(f"缓存 {len(cache)} 条，其中缺语言 {no_lang} 条"
              f"（ungh 不给 language，要 --api 才能补）")
        return 0

    if args.limit:
        miss = miss[:args.limit]
        print(f"本次只补 {len(miss)} 条")

    done = failed = 0
    t0 = time.time()

    def work(item):
        slug, _url = item
        time.sleep(args.delay)
        r = _via_ungh(slug)
        if r is None:
            failed_here = True
            return slug, None, failed_here
        return slug, r, False

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(work, it) for it in miss]
        for i, f in enumerate(as_completed(futs), 1):
            slug, r, bad = f.result()
            if bad:
                failed += 1
            elif r is not None:
                cache[slug] = r
                done += 1
            if i % 100 == 0:
                el = time.time() - t0
                print(f"  {i}/{len(miss)}  成功 {done} 失败 {failed}  "
                      f"用时 {el:.0f}s  {i/el:.1f} 条/秒")
                save_cache(cache)

    if args.api:
        # language 只有 GitHub API 给，无 token 60 次/小时，补不完 1000+ 条。
        # 所以按星数降序补：先补用户最可能遇到的，尾巴慢慢来。
        # 试过两条歪路，都不通：ungh 代理的返回里根本没 language 字段；
        # GitHub 仓库页面的语言区块是 React 骨架屏，HTML 里没有实际值。
        no_lang = [(k, v) for k, v in cache.items() if not v.get("lang")]
        no_lang.sort(key=lambda kv: -(kv[1].get("stars") or 0))
        rl = _get("https://api.github.com/rate_limit") or {}
        left = (rl.get("resources", {}).get("core", {}) or {}).get("remaining", 0)
        print(f"\n缺 language 的缓存条目 {len(no_lang)} 条，当前 API 配额剩 {left}")
        if left and no_lang:
            todo_api = no_lang[:left]
            print(f"  本次按星数降序补前 {len(todo_api)} 条")
            ok = 0
            for i, (slug, old) in enumerate(todo_api, 1):
                time.sleep(0.5)
                r = _via_api(slug)
                if r is None:
                    print("  被限流，停在这里（配额恢复后再跑一次即可）")
                    break
                if r.get("missing"):
                    old["missing"] = True
                    continue
                merged = dict(old)
                merged.update({k: v for k, v in r.items() if v})
                cache[slug] = merged
                ok += 1
                if i % 20 == 0:
                    print(f"    {i}/{len(todo_api)}  补到 language {ok} 条")
                    save_cache(cache)
            save_cache(cache)
            print(f"  补到 language {ok} 条")

    save_cache(cache)
    el = time.time() - t0
    print(f"\n完成：补到 {done} 条，失败 {failed} 条，缓存共 {len(cache)} 条，用时 {el:.0f}s")
    print(f"缓存文件：{CACHE}")
    if args.moved:
        print()
        check_moved(tools)
    print("接下来跑 python src/update.py --no-github --no-awesome --no-cn 让改动生效")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
