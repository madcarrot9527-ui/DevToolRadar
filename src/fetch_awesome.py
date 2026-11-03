"""awesome-* 仓库 README 解析。

这些列表是人工精选，条目质量普遍高于纯 star 排序，很多还带中文描述。
阮一峰周刊的条目带中文说明，价值尤其高，单独走 weekly 模式。

取文策略（重要）：本机 raw.githubusercontent.com 被 SSL 干扰，全线不通，
所以主路径走 api.github.com（已证实可达），按天缓存到 data/.cache 节省配额。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlparse

from common import (DATA_DIR, clip, github_headers, http_get, load_config,
                    load_json, log)

CACHE_DIR = DATA_DIR / ".cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 条目形态多样，必须容忍装饰：
#   - [Name](url) - Desc                    （标准）
#   - **[Name](url)**：Desc                  （标题加粗）
#   1. **[Name](url)**：Desc                 （有序列表，半角句点）
#   1、[Name](url)                           （阮一峰周刊，中文顿号编号）
#   * :white_check_mark: [Name](url)：Desc   （1c7，带 emoji 状态）
#
# 关键约束：每一段都必须禁止跨行（用 [^...\n] 而不是 \s 或 .）。
# 否则「没有写描述的条目」会把下一行条目的描述偷过来——
# 曾导致 ngrok 的描述变成 ytunnel 的描述。
ITEM_RE = re.compile(
    r"^[ \t]*(?:[-*+]|\d{1,3}[、.．])[ \t]*"
    r"[^\[\n]*?"
    r"\[([^\]\n]{1,120})\]"
    r"\([ \t]*(https?://[^)\s]+)[ \t]*\)"
    r"[ \t]*(?:[-–—:：|][ \t]*)?([^\n]{0,300})",
    re.MULTILINE,
)
# 周刊只有这两节是真工具，其余（文章/图片/文摘/言论/往年回顾）不是
WEEKLY_KEEP_CAT = {"工具", "资源"}
HEAD_RE = re.compile(r"^(#{2,4})\s+(.{1,80}?)\s*$", re.MULTILINE)
WEEKLY_RE = re.compile(r"issue[-/]?(\d+)", re.IGNORECASE)

SKIP_CAT = re.compile(
    r"book|article|blog|course|newsletter|podcast|talk|video|"
    r"interview|job|hiring|community|related|contribut|license|footnote|"
    r"书籍|文章|课程|播客|社区|相关|贡献|许可|目录|table of contents|^contents",
    re.IGNORECASE,
)
# 精确标题黑名单。不能拿 SKIP_CAT 那样裸正则去扫，否则会误伤真分类：
#   Machine Learning（含 learning）、Resource Planning（HRM 软件）、Link Preview（功能）
# 这些是工具分类，不是「资源/链接」章节。
# Links 章节尤其脏——那是一堆产品页的「Web / X / LinkedIn / GitHub」社交链接，
# 条目名就是 Web、X、LinkedIn，留着只会污染检索。
SKIP_CAT_EXACT = {
    "links", "link", "tutorials", "tutorial", "resources", "resource",
    "cheatsheet", "cheat sheet", "cheat sheets", "cheatsheets",
    "reading list", "books", "articles", "videos", "podcasts", "courses",
    "official resources", "external resources", "general resources",
    "other resources", "more resources", "related resources",
    "useful resources", "additional resources", "learning resources",
    "external links", "useful links", "related links", "official links",
    "awesome lists", "related lists", "credits", "acknowledgements",
    "acknowledgments", "changelog", "news",
}
SKIP_CAT_TAIL = re.compile(
    r"^(?:official|external|general|other|related|useful|additional|more|learning)"
    r"\s+(?:resources?|links?|tutorials?|guides?)$",
    re.IGNORECASE,
)


def _cat_key(title: str) -> str:
    """章节标题归一化：去编号、emoji、标点，小写。"""
    t = re.sub(r"^[\d.)\s、]+", "", (title or "").strip())
    t = re.sub(r"[^\w\u4e00-\u9fff]+", " ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _skip_cat(title: str) -> bool:
    if not title or SKIP_CAT.search(title):
        return True
    key = _cat_key(title)
    return key in SKIP_CAT_EXACT or bool(SKIP_CAT_TAIL.match(key))
SKIP_TITLE = re.compile(
    r"^(awesome|table of contents|contents|contributing|license|footnotes|"
    r"back to top|↑|toc)",
    re.IGNORECASE,
)
BAD_HOSTS = {
    "img.shields.io", "badgen.net", "travis-ci.org", "travis-ci.com",
    "github.com", "twitter.com", "x.com", "discord.gg", "reddit.com",
    "youtube.com", "opencollective.com", "patreon.com", "buymeacoffee.com",
    "githubusercontent.com", "raw.githubusercontent.com",
}


def _norm_url(u: str) -> str:
    u = re.sub(r"[?&]utm_[^&]+", "", u.strip().rstrip("/"))
    return u.rstrip("?&")


def _cache_path(key: str):
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{h}.txt"


def fetch_text(repo: str, branch: str, path: str = "README.md",
               use_cache: bool = True) -> str | None:
    """取仓库内某个文本文件。api.github.com 优先，raw 与 jsDelivr 兜底。"""
    key = f"{repo}|{branch}|{path}"
    cp = _cache_path(key)
    if use_cache and cp.exists() and date.fromtimestamp(cp.stat().st_mtime) == date.today():
        return cp.read_text(encoding="utf-8", errors="replace")

    candidates = []
    if path.lower().endswith("readme.md") or path.lower() == "readme":
        candidates.append(
            (f"https://api.github.com/repos/{repo}/readme",
             {**github_headers(), "Accept": "application/vnd.github.raw"}))
    candidates.append(
        (f"https://api.github.com/repos/{repo}/contents/{path}?ref={branch}",
         {**github_headers(), "Accept": "application/vnd.github.raw"}))
    candidates.append(
        (f"https://raw.githubusercontent.com/{repo}/{branch}/{path}", {}))
    candidates.append(
        (f"https://cdn.jsdelivr.net/gh/{repo}@{branch}/{path}", {}))

    for url, hdrs in candidates:
        try:
            status, body, _ = http_get(url, headers=hdrs, timeout=30, retries=1)
            if status != 200 or not body:
                continue
            text = body
            # 若返回的是 JSON（raw accept 被中间层忽略），尝试 base64 解码
            stripped = text.lstrip()
            if stripped.startswith("{"):
                try:
                    obj = json.loads(text)
                    b64 = obj.get("content")
                    if b64:
                        text = base64.b64decode(b64).decode("utf-8", errors="replace")
                    elif obj.get("download_url"):
                        st2, t2, _ = http_get(obj["download_url"], timeout=30, retries=1)
                        if st2 == 200 and t2:
                            text = t2
                        else:
                            continue
                    else:
                        continue
                except Exception:  # noqa: BLE001
                    continue
            if len(text) < 50:
                continue
            try:
                cp.write_text(text, encoding="utf-8")
            except Exception:
                pass
            log(f"取文成功 {repo}/{path} via {urlparse(url).hostname} ({len(text)}B)")
            return text
        except Exception as e:  # noqa: BLE001
            log(f"取文异常 {url[:70]}: {e}", "WARN")
    return None


def _cat_blocks(text: str) -> list[tuple[str, str]]:
    marks = [(m.start(), m.group(2).strip()) for m in HEAD_RE.finditer(text)]
    if not marks:
        return [("", text)]
    out: list[tuple[str, str]] = []
    for i, (pos, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        out.append((title, text[pos:end]))
    return out


def _clean_desc(d: str) -> str:
    """清掉描述里的 markdown 残留，尤其是 awesome 列表常见的内联链接污染。"""
    d = d or ""
    d = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", d)   # 图片
    d = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", d)    # 内联链接 → 文本
    d = re.sub(r"\(\s*https?://[^)]*\)", "", d)       # 裸 URL 括号
    d = re.sub(r"https?://\S+", "", d)                # 裸 URL
    # GitHub emoji 短代码 :telescope: 之类。必须在 strip(":") 之前剥，
    # 否则会退化成 "telescope: 描述" 这种以单词开头、看着像人名的噪声。
    # 首字符强制为小写字母，避免误伤 "10:30:" 这种时间串。
    d = re.sub(r":([a-z][a-z0-9_+-]{1,29}):", " ", d)
    d = re.sub(r"[*_`<>|]", "", d)                    # 装饰符号
    # 去掉「（@xxx 投稿）」这类署名尾巴
    d = re.sub(r"[（(]\s*@[\w\-. ]+\s*(?:投稿|分享)?\s*[)）]\s*$", "", d)
    d = d.strip(" -–—:：")
    d = re.sub(r"\s+", " ", d).strip()
    return d if len(d) >= 6 else ""


def _looks_like_sentence(name: str) -> bool:
    """链接文字是一整句介绍，而不是工具名。

    典型来源：awesome 列表里作者懒得写名字，直接把介绍当链接文字。
      - [Run dlint security linter on your Python code](https://github.com/xen0l/dlint-check)
    这种条目留着会把一整句话当工具名，检索和展示都没法看。
    """
    n = (name or "").strip()
    if not n:
        return False
    words = n.split()
    return len(words) >= 7 or len(n) > 55


# 这些路径段太泛，拿来当工具名毫无意义
_GENERIC_SEG = {
    "learn", "docs", "doc", "blog", "wiki", "guide", "tutorial", "index",
    "home", "en", "zh", "readme", "releases", "latest", "about", "product",
    "products", "features", "pricing", "download", "page", "html",
}


def _derive_name(url: str) -> str | None:
    """从 URL 反推一个像样的工具名，推不出来就返回 None（宁可不改）。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    segs = [s for s in p.path.split("/") if s]
    if host in ("github.com", "gitlab.com", "bitbucket.org") and len(segs) >= 2:
        cand = segs[1]
    elif segs:
        cand = segs[-1]
    else:
        return None
    cand = re.sub(r"\.(html?|php|aspx?|md|htm)$", "", cand, flags=re.IGNORECASE)
    cand = cand.strip("-_.")
    if not (2 <= len(cand) <= 40) or cand.lower() in _GENERIC_SEG:
        return None
    return cand


def _is_self_ref(url: str, repo: str) -> bool:
    u = url.lower().rstrip("/")
    r = f"github.com/{repo.lower()}".rstrip("/")
    if not u.startswith(r):
        return False
    # 只有指向仓库根或目录才算自引用；指向 issue/PR 的也算
    rest = u[len(r):].strip("/")
    return rest == "" or rest.startswith(("blob", "tree", "issues", "pull", "#"))


def _parse_readme(text: str, repo: str, label: str, max_items: int,
                  fixed_cat: str | None = None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for cat, body in _cat_blocks(text):
        if cat and _skip_cat(cat):
            continue
        for m in ITEM_RE.finditer(body):
            name, url = m.group(1).strip(), m.group(2).strip()
            if SKIP_TITLE.match(name):
                continue
            url = _norm_url(url)
            host = (urlparse(url).hostname or "").lower()
            if host in BAD_HOSTS:
                if not (host == "github.com" and not _is_self_ref(url, repo)):
                    continue
            if _is_self_ref(url, repo):
                continue
            if url in seen:
                continue
            seen.add(url)
            desc = _clean_desc(m.group(3))
            # 链接文字是整句介绍、且没有单独的描述时，把句子挪到 desc，
            # 名字改用 URL 里的仓库名。/ 推不出名字就原样保留，不猜。
            if not desc and _looks_like_sentence(name):
                derived = _derive_name(url)
                if derived:
                    desc = _clean_desc(name)
                    name = derived
            out.append({
                "origin": "awesome",
                "id": f"aw:{url}",
                "name": clip(re.sub(r"[*_`]", "", name), 80),
                "url": url,
                "desc": clip(desc, 300),
                "topics": [],
                "lang": None,
                "stars": 0,
                "pushed_at": "",
                "category": fixed_cat or cat,
                "why": label,
                "repo": repo,
            })
            if len(out) >= max_items:
                return out
    return out


def _latest_issue(text: str) -> int:
    nums = [int(n) for n in WEEKLY_RE.findall(text)]
    return max(nums) if nums else 0


def _fetch_weekly(repo: str, branch: str, label: str, max_issues: int = 8) -> list[dict]:
    readme = fetch_text(repo, branch, "README.md")
    if not readme:
        log(f"awesome {repo}: README 不可用", "WARN")
        return []
    top = _latest_issue(readme)
    if not top:
        log(f"awesome {repo}: 未解析到期号", "WARN")
        return []

    out: list[dict] = []
    seen: set[str] = set()
    for n in range(top, max(top - max_issues, 0), -1):
        body = fetch_text(repo, branch, f"docs/issue-{n}.md")
        if not body:
            continue
        lines = body.splitlines()
        cur = ""
        for i, line in enumerate(lines):
            hm = HEAD_RE.match(line)
            if hm:
                cur = hm.group(2).strip()
                continue
            # 只在「工具」「资源」两节里取条目
            if not any(k in cur for k in WEEKLY_KEEP_CAT):
                continue
            m = ITEM_RE.match(line)
            if not m:
                continue
            name, url = m.group(1).strip(), m.group(2).strip()
            if SKIP_TITLE.match(name):
                continue
            url = _norm_url(url)
            if url in seen:
                continue
            desc = _clean_desc(m.group(3))
            # 周刊版式：标题 → 空行 → 图片 → 空行 → 描述。
            # 图片要跳过而不是终止，否则整期的描述都会丢。
            if not desc:
                for nxt in lines[i + 1: i + 9]:
                    s = nxt.strip()
                    if not s:
                        continue
                    if s.startswith("#") or ITEM_RE.match(s):
                        break
                    if s.startswith("!") or s.startswith("[!["):
                        continue
                    desc = _clean_desc(s)
                    if desc:
                        break
            seen.add(url)
            out.append({
                "origin": "awesome",
                "id": f"aw:{url}",
                "name": clip(re.sub(r"[*_`]", "", name), 80),
                "url": url,
                "desc": clip(desc, 300),
                "topics": [],
                "lang": None,
                "stars": 0,
                "pushed_at": "",
                "category": cur or "周刊",
                "why": f"{label} 第{n}期",
                "repo": repo,
            })
    log(f"awesome {repo}: 周刊 {top}~{max(top-max_issues+1,1)} 期共 {len(out)} 条")
    return out


def fetch(cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config("sources.json")
    acfg = cfg.get("awesome", {})
    if not acfg.get("enabled", True):
        log("awesome 源已禁用，跳过")
        return []
    max_items = acfg.get("max_items_per_repo", 400)

    all_rows: list[dict] = []
    failed: list[str] = []
    for entry in acfg.get("repos", []):
        repo = entry["repo"]
        branch = entry.get("branch", "master")
        label = entry.get("label", repo)
        try:
            if entry.get("mode") == "weekly":
                rows = _fetch_weekly(repo, branch, label)
            else:
                text = fetch_text(repo, branch, "README.md")
                if not text:
                    log(f"awesome {repo}: README 不可用", "WARN")
                    failed.append(repo)
                    continue
                rows = _parse_readme(text, repo, label, max_items,
                                     entry.get("fixed_category"))
                log(f"awesome {repo} ({label}): {len(rows)} 条")
            all_rows.extend(rows)
        except Exception as e:  # noqa: BLE001
            log(f"awesome {repo} 解析异常: {e}", "WARN")
            failed.append(repo)

    # 单个源抓不到，不能连累整个索引。update.py 的回退是「整个 awesome 源 0 条才用旧
    # 文件」，而这里失败的是 12 个源里的 2、3 个——总数照样非零，回退不会触发，
    # 于是那两三个列表的条目就被静默删掉了。2026-11-03 真发生过一次：
    # viatsko/awesome-vscode（SSL 全断）、k4m4/terminals-are-sexy（403）、
    # agarrharr/awesome-cli-apps 三个源失败，索引从 5417 掉到 5152，而自检全绿。
    # 所以按「源」粒度回退：失败的那个源用它上次落盘的条目顶上。
    if failed:
        prev = load_json(DATA_DIR / "raw_awesome.json", []) or []
        want = set(failed)
        carried = [r for r in prev if (r.get("repo") or "") in want]
        if carried:
            log(f"awesome: {len(failed)} 个源本次失败（{', '.join(failed)}），"
                f"沿用上次缓存 {len(carried)} 条", "WARN")
            all_rows.extend(carried)
        else:
            log(f"awesome: {len(failed)} 个源本次失败且没有缓存可沿（{', '.join(failed)}）",
                "WARN")

    dedup: dict[str, dict] = {}
    for r in all_rows:
        dedup.setdefault(r["id"], r)
    log(f"awesome 抓取完成，共 {len(dedup)} 条唯一条目")
    return list(dedup.values())


if __name__ == "__main__":
    rows = fetch()
    print(f"\n总数: {len(rows)}")
    for r in rows[:15]:
        print(f"- {r['name']} | {r['url'][:60]} | {r['desc'][:50]} | [{r['category']}]")
