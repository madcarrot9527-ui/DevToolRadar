"""把 site/index.html 发布成公开网页（GitHub Pages），并验证线上与本地一致。

为什么需要这一步：refresh.py 跑完只更新本地 site/index.html，线上那份是发布时
冻住的快照。2026-11-19 发布一次之后，线上停在 6831 条，而本地每天在长——四天
就差 25 条，页面上却看不出来。doctor 的「站点同步」只比本地两个文件，给的是
「本地网页与本地数据一致」，它根本没管线上。

为什么走 Git Data API 而不是 git push：
1. `git push` 需要凭据落盘或把 token 拼进 URL，后者会漏进日志和 shell 历史。
2. 我们要的是「单提交分支」——每次发布用 parent 为空的新提交 + 强制移动 ref，
   分支永远只有 1 个提交，不会像逐日提交那样让仓库历史线性膨胀（11-19 那次
   瘦身就是为了这个）。
3. 纯标准库，和项目其他部分一致，不需要装 gitpython。

退出码：0 = 线上与本地一致；1 = 发布或验证失败。refresh.py 把它当警告处理，
不改整轮退出码——数据已经在盘上，线上没跟上不该让计划任务报错重试。
"""
from __future__ import annotations

import base64
import email.utils
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_HTML = ROOT / "site" / "index.html"
BRANCH = "gh-pages"
WAIT_SECONDS = 180          # Pages 构建实测 30-60s，留足余量
POLL_INTERVAL = 6


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        raise SystemExit("缺少 GITHUB_TOKEN / GH_TOKEN，无法发布")
    return tok


def _slug() -> tuple[str, str]:
    """从 git remote 推导 owner/repo，避免在这里再写死一份身份。"""
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout.strip()
    except Exception:
        url = ""
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", url)
    if not m:
        raise SystemExit(f"无法从 origin 推导仓库：{url!r}")
    return m.group(1), m.group(2)


def _api(method: str, path: str, payload=None, token: str = ""):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "devtoolradar-publish",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def page_url() -> str:
    """公开地址。doctor 也要用它校验线上，所以单独暴露出来，别两处各拼一遍。"""
    owner, repo = _slug()
    return f"https://{owner}.github.io/{repo}/"


def _online_head(url: str, bust: bool = False) -> tuple[int, float]:
    """HEAD 取线上页面的 (Content-Length, Last-Modified 时间戳)，失败返回 (-1, 0)。

    为什么用 HEAD 而不是 GET：整页 1.8 MB，GET 实测要 69s，HEAD 只要 3s，
    而我们要判的只是「线上是不是本次这份」。GitHub Pages 的 HEAD 返回的是
    未压缩原文长度，和本地文件字节数可以直接比。
    Last-Modified 是构建时间，用它来证「这个长度来自本次构建」，而不是
    长度恰好相同的上一版。
    """
    target = url
    if bust:
        target = f"{url}{'&' if '?' in url else '?'}_={int(time.time())}"
    try:
        req = urllib.request.Request(
            target, method="HEAD",
            headers={"Cache-Control": "no-cache", "User-Agent": "devtoolradar-publish"})
        with urllib.request.urlopen(req, timeout=45) as r:
            size = int(r.headers.get("Content-Length") or -1)
            lm = r.headers.get("Last-Modified")
        ts = 0.0
        if lm:
            try:
                ts = email.utils.parsedate_to_datetime(lm).timestamp()
            except Exception:
                ts = 0.0
        return size, ts
    except Exception:
        return -1, 0.0


def main() -> int:
    if not SRC_HTML.exists():
        print("site/index.html 不存在，先跑刷新")
        return 1
    html = SRC_HTML.read_bytes()
    local_items = html.count(b'"n":')
    print(f"准备发布 {len(html):,} 字节 / {local_items} 条")

    token = _token()
    owner, repo = _slug()
    url = page_url()

    code, blob = _api("POST", f"/repos/{owner}/{repo}/git/blobs",
                      {"content": base64.b64encode(html).decode(), "encoding": "base64"}, token)
    if code not in (200, 201):
        print(f"建 blob 失败 {code}: {blob.get('error', '')}")
        return 1

    code, empty = _api("POST", f"/repos/{owner}/{repo}/git/blobs",
                       {"content": "", "encoding": "base64"}, token)
    code, tree = _api("POST", f"/repos/{owner}/{repo}/git/trees", {
        "tree": [
            {"path": "index.html", "mode": "100644", "type": "blob", "sha": blob["sha"]},
            {"path": ".nojekyll", "mode": "100644", "type": "blob", "sha": empty["sha"]},
        ]}, token)
    if code not in (200, 201):
        print(f"建 tree 失败 {code}: {tree.get('error', '')}")
        return 1

    code, cm = _api("POST", f"/repos/{owner}/{repo}/git/commits", {
        "message": f"publish: 开发工具雷达静态快照（{local_items} 条，单提交分支）",
        "tree": tree["sha"], "parents": [],
    }, token)
    if code not in (200, 201):
        print(f"建 commit 失败 {code}: {cm.get('error', '')}")
        return 1
    commit_ts = time.time()

    code, _ = _api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{BRANCH}", token=token)
    if code == 200:
        code, out = _api("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{BRANCH}",
                         {"sha": cm["sha"], "force": True}, token)
    else:
        code, out = _api("POST", f"/repos/{owner}/{repo}/git/refs",
                         {"ref": f"refs/heads/{BRANCH}", "sha": cm["sha"]}, token)
    if code not in (200, 201):
        print(f"更新 ref 失败 {code}: {out.get('error', '')}")
        return 1
    print(f"gh-pages 已指向新提交 {cm['sha'][:10]}")

    code, pages = _api("POST", f"/repos/{owner}/{repo}/pages",
                       {"source": {"branch": BRANCH, "path": "/"}}, token)
    if code == 409:
        code, pages = _api("PUT", f"/repos/{owner}/{repo}/pages",
                           {"source": {"branch": BRANCH, "path": "/"}}, token)
    if code not in (200, 201, 204):
        print(f"配置 Pages 失败 {code}: {pages.get('error', '')}")
        return 1

    # 光看 API 说 built 不够——构建完成不等于本次内容已经生效（可能还在 CDN 后面）。
    # 直接拿线上字节数比：一致才算发布成功，这是唯一有意义的判据。
    deadline = time.time() + WAIT_SECONDS
    last = (-1, -1)
    while time.time() < deadline:
        size, lm = _online_head(url)
        if size != len(html) or lm < commit_ts - 120:
            # CDN 可能还挂着旧页，加时间戳再确认一次；两次都不对才算没生效。
            size, lm = _online_head(url, bust=True)
        if size == len(html) and lm >= commit_ts - 120:
            when = time.strftime("%H:%M:%S", time.localtime(lm)) if lm else "未知"
            print(f"线上已生效：{size:,} 字节 / {local_items} 条，构建于 {when}，与本地一致")
            print(f"公开地址：{url}")
            return 0
        print(f"  等待 Pages 生效… 线上 {size:,} 字节（目标 {len(html):,}）")
        time.sleep(POLL_INTERVAL)

    print(f"等待 {WAIT_SECONDS}s 后线上仍不一致：线上 {last[0]:,} 字节，本地 {len(html):,} 字节")
    print(f"公开地址：{url}")
    return 1


if __name__ == "__main__":
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(main())
