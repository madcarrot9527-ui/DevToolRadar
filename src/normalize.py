"""归一化 + 场景打标。

这是整个系统的心脏。原始数据是三个互不相干的列表，这里做四件事：
  1. URL 规范化去重（同一工具在多个源出现时合并）
  2. 按「你想干什么」打场景标签（而不是按技术栈）
  3. 计算质量分（star / 活跃度 / 人工精选 / 可用性）
  4. 丢弃低质噪声（尤其是没有场景归属的 awesome 条目）
"""
from __future__ import annotations

import math
import re
from datetime import date
from urllib.parse import urlparse

from common import DATA_DIR, load_config, load_json, log

CJK = re.compile(r"[\u4e00-\u9fff]")
GH_RE = re.compile(r"https?://(?:www\.)?github\.com/([^/?#]+)/([^/?#]+)", re.I)
HOST_RE = re.compile(r"https?://(?:www\.)?([^/?#]+)", re.I)
# 「我是别人的客户端/封装」的自述。用于识别同名附属品，见 _mark_wrappers。
WRAP_RE = re.compile(
    r"\b(?:python|node|nodejs|js|go|rust|java|ruby|php|dotnet|\.net|c#|cli|ts"
    r"|typescript|swift|kotlin|dart|flutter|wasm)\s+client\s+for\b"
    r"|\b(?:wrapper|binding|bindings|sdk|client|cli)\s+for\b"
    r"|\bunofficial\b|\bthird[- ]party\b|第三方|非官方|客户端封装|非官方客户端",
    re.IGNORECASE,
)


def norm_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    u = re.sub(r"(?<=\?)[^#]*#.*$", "", u)
    u = re.split(r"[?#]", u)[0].rstrip("/")
    m = GH_RE.match(u)
    if m:
        repo = m.group(2)
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        return f"https://github.com/{m.group(1)}/{repo}"
    return u


def _build_matchers(tag_rules: list[dict]):
    """把关键词编译成匹配器。英文走边界断言，中文走子串。

    用 lookaround 而不是 \\b：关键词里常有 '.'、'-'、'+'（如 d3.js、ci-cd、c++），
    \\b 在这些位置不成立，会漏掉大量真实命中。

    连字符挡在边界外：复合词里的词根不算独立命中，否则 "best-benchmarked"
    会把一个 AI 记忆系统算成压测工具。

    试过让 name 字段改用宽松边界（'-' 也算边界），能捞回 v2ray-core、
    mikro-orm 等 23 条，但同时把 8 条已有记录打降级——"pandas" 在 pandas-ai
    里命中后数据处理涨到 6.7，阈值跟着抬到 3.685，把 3.35 的「大模型应用」
    挤掉了；react-awesome-slider 则因为中段那个 awesome 被认成清单。
    净收益不抵这个代价，已撤回。这类「产品名带后缀」的条目改走 topic_map
    的显式兜底，那条路径按构造零回归。
    """
    out = []
    for t in tag_rules:
        kws = []
        for kw in t.get("kw", []):
            k = kw.lower().strip()
            if not k:
                continue
            if CJK.search(k):
                kws.append(("in", k, k))
            else:
                # 允许常见词形后缀：关键词写 "load test"，要能命中 "load testing"；
                # 写 "container" 要能命中 "containers"。否则大量真实命中会漏掉。
                pat = re.compile(
                    r"(?<![a-z0-9\-])" + re.escape(k)
                    + r"(?:s|es|ed|ing|er|ers|ion|ions)?(?![a-z0-9\-])")
                kws.append(("re", pat, pat))
        out.append((t["label"], kws))
    return out


# 字段权重：名字是自报家门（最可信），category 是精选清单的人工分类，
# desc 是作者自己写的定位，topics 权重压得最低——因为项目会为了被搜到
# 而滥打热门 topic（一个仪表盘也标 docker/kubernetes）。
FIELD_W = {"name": 3.0, "category": 1.5, "topics": 1.35, "desc": 1.0}

# 有些场景不该拿 category 当证据。category 是精选清单的章节标题
# （"Machine Learning"、"Command Line Learning"、"React Tutorials"），说的是
# 「这条被归在清单的哪一节」，不是「这个工具是干什么的」。对绝大多数场景这个
# 区分无所谓，但「学习资源」的关键词是 learn / tutorial / guide 这类泛词，
# 让章节标题参与投票就等于把整节机器学习库判成教程 —— pytorch（★10 万）当过一次，
# Microsoft Mage 当过两次。
#
# 试过的替代方案，都不如这一条：
#   A. 关键词 learn 改整词匹配 → 少 30 条，pytorch 自己掉标签（它靠的是 category）。
#   B. A + 「高星无场景条目保留」→ 5426 条、学习资源 138，但产出 102 条无场景条目，
#      得为它们另写一套显示逻辑，代价大于收益。
#   C. 本方案 → 5425 条（只丢 1 条）、其余条目标签零改动。
#
# 剩下的一半仍是人工活：learn 这个词本身删不掉（实测删掉丢 30 条），
# 教程仓库名/描述里恰好有 learning 的，还得靠 config/manual_tags.json 纠。
NO_CATEGORY_LABELS = {"学习资源"}

# 弱标签：几乎每个项目都说自己「支持 docker 部署」「可自托管」「有 pipeline」。
# 这类标签要求分数接近最强标签才算数，否则按场景浏览会被灌满不相干的东西。
WEAK_RATIO = 0.8
NORMAL_RATIO = 0.55

# 长描述里，命中词出现在第几个字符以内才算「作者的定位句」。
# 90 是量出来的拐点：再放宽到 120 会多收 ~33 条，但代价是按 Flask-AppBuilder、
# Keeper.sh 这种「描述里顺带提了一句安全/日历」的误判也进来了；
# 收紧到 60 会连 plane（项目管理）、AFFiNE（笔记知识库）一起丢掉。
# 见 _score_one 里第 5 项的说明。
DESC_POS_MAX = 90

_ENUM_SEP = re.compile(r"[,，、;；]")
_PAREN = re.compile(r"\(([^()]{20,})\)")


def _is_enum_desc(desc: str) -> bool:
    """描述里有没有一段「括号包起来的长枚举」。

    留作参考，当前**未启用**。试过两版都被测回：
      1. 全句逗号/顿号密度 ≥ 6 —— 中文描述天然用顿号罗列功能
         （「支持笔记、待办、日程、密码管理…」），一刀切剥掉 108 条中文工具的标签，
         typeorm、pac4j、lanproxy、koreader 全部误伤。
      2. 只认括号内枚举（本函数）—— 1 对 3 错：正确剥掉 3x-ui 的「内网穿透」，
         但同时让 electerm 丢了「文件同步/终端增强」、platform 丢了「项目管理」，
         dvinyl 甚至整条掉出索引。
    结论：为一条误标引入全库规则，性价比为负。3x-ui 那一条按「可接受噪声」处理。
    再要动它，得先有能区分「协议清单」和「功能清单」的证据，而不是标点密度。
    """
    for g in _PAREN.findall(desc):
        if len(_ENUM_SEP.findall(g)) >= 4:
            return True
    return False


_TOPIC_MAP: dict[str, str] | None = None


_MANUAL_DESC: dict[str, str] | None = None
_DROP_URLS: set[str] | None = None
_GH_META: dict[str, dict] | None = None
_MOVED: dict[str, str] | None = None


def _gh_moved() -> dict[str, str]:
    """GitHub 上确认过的「仓库搬家」记录：旧地址 → 新地址（都转小写）。

    来源是 `fetch_gh_meta.py --moved`：它对「同名但 owner 不同」的组逐个
    试探，看旧地址是不是 301 到组里的另一个地址。除此之外没有可靠判据 ——
    星数接近不行（express-typescript 那两条只差 12 星，是两个不同项目），
    名字相同也不行（bun、tabby、goose 都是同名不同物）。

    记录只填空、不删旧项：归一化吃的是 raw_*.json，旧地址哪天被重新抓回来，
    这条记录还得在。
    """
    global _MOVED
    if _MOVED is None:
        raw = load_json(DATA_DIR / "gh_moved.json", {}) or {}
        m = raw.get("moved") if isinstance(raw, dict) else None
        _MOVED = {str(k).lower(): str(v).lower() for k, v in (m or {}).items()}
    return _MOVED


def _gh_meta() -> dict[str, dict]:
    """事后补抓的 GitHub 元数据，按 owner/repo 索引。

    为什么单独一个缓存文件：`fetch_github` 是按 topic 搜的，`fetch_awesome`
    只解析 README 根本不调 API。于是精选清单里那批 star 不够高、没被 topic
    覆盖的仓库，进来时只有「名字 + 一句话」—— 星数、语言、更新时间全空，
    卡片上光秃秃的，用户没法判断这工具还能不能用（`Qwen` 两万星、
    `LLaMA-Factory` 七万星，在库里都挂着 0 星）。

    补的数据不写回 raw_*.json：那是「这次抓到了什么」的存档，混进事后补的
    会让它失去可信度。这里读独立的 data/gh_meta.json，只填空、不覆盖。
    """
    global _GH_META
    if _GH_META is None:
        raw = load_json(DATA_DIR / "gh_meta.json", {}) or {}
        _GH_META = {str(k).lower(): v for k, v in raw.items() if isinstance(v, dict)}
    return _GH_META


def _manual_desc() -> dict[str, str]:
    """人工补写的一句话说明，按 URL 索引。

    存在的理由：有一批条目的作者压根没写 description —— GitHub 上就是 null，
    抓一万次也抓不回来（Unity 的 EntityComponentSystemSamples 8212 星，无说明）。
    还有一批是 awesome 列表里「名字本身就是说明」的条目，sdras/awesome-actions
    整个列表都是这个形态，名字叫 "Deploy to Netlify"，说明就到此为止。
    规则补不出这些，只能人读一眼再写一句话。
    """
    global _MANUAL_DESC
    if _MANUAL_DESC is None:
        raw = (load_config("manual_desc.json").get("by_url") or {})
        _MANUAL_DESC = {norm_url(k).lower(): str(v).strip()
                        for k, v in raw.items() if str(v).strip()}
    return _MANUAL_DESC


def _drop_urls() -> set[str]:
    """确认「不是工具」的条目，直接丢弃。

    awesome 列表和周刊是按「文章/资源」组织的，解析器只认 markdown 列表项，
    于是教程、面试题、产品需求投票板都会被抓进来。这些放进工具雷达里
    让检索结果变脏 —— 查 React 冒出一篇官方教程，点进去发现不是工具。
    """
    global _DROP_URLS
    if _DROP_URLS is None:
        raw = (load_config("drop.json").get("by_url") or {})
        _DROP_URLS = {norm_url(k).lower() for k in raw}
    return _DROP_URLS


def _topic_map() -> dict[str, str]:
    global _TOPIC_MAP
    if _TOPIC_MAP is None:
        raw = (load_config("topic_map.json").get("map") or {})
        _TOPIC_MAP = {str(k).lower(): str(v) for k, v in raw.items()}
    return _TOPIC_MAP


def _topic_map_tags(r: dict, known: set[str], limit: int = 1) -> list[str]:
    """兜底：用人工维护的「任务型 topic」映射补一个场景。

    只在自动判定完全为空时调用。topics 绝大多数是技术栈（python/docker/cli），
    但也有一批词说的就是功能本身（image-processing、note-taking），
    这些词和场景一一对应，做成显式映射比让打分去猜安全。

    约束两条，都是为了不动已有的东西：
      1. 只补空：已有标签的条目一律不碰，所以对现有索引是纯增量。
      2. 最多补一个：补错了也只错一条场景，不会把一条工具塞进三个场景。
    """
    m = _topic_map()
    out: list[str] = []
    for tp in (r.get("topics") or []):
        lab = m.get(str(tp).lower())
        if lab and lab in known and lab not in out:
            out.append(lab)
            if len(out) >= limit:
                break
    return out


_MANUAL_TAGS: dict[str, str | None] | None = None


def _manual_tags() -> dict[str, str | None]:
    global _MANUAL_TAGS
    if _MANUAL_TAGS is None:
        raw = (load_config("manual_tags.json").get("by_url") or {})
        _MANUAL_TAGS = {norm_url(k).lower(): v for k, v in raw.items()}
    return _MANUAL_TAGS


def _manual_tag(r: dict) -> tuple[bool, str | None]:
    """人工判读表：人读完作者自述后给出的归属，可信度高于任何规则。

    为什么非要有它：场景词表只能做字面匹配，而作者的自述用的是另一套说法——
    jq 写「Command-line JSON processor」、pandoc 写「Universal markup converter」、
    bubbletea 写「TUI framework」，一个词都不在表里；52 个场景里也没有「编辑器本体」
    「语言运行时」这种类目。规则再怎么调都补不出这些，只能人读一眼。这张表就是
    人读的结果，也是唯一能给出「它确实没有场景」这个结论的地方。

    返回值是二元组，因为要能区分三种情况：
      (True,  "终端增强") —— 读过了，归这个场景
      (True,  None)       —— 读过了，确认不属于任何场景，到此为止别再猜
      (False, None)       —— 表里没有，交给后面的兜底
    中间那种必须能表达出来，否则 vscode（194k）、node（123k）、deno（108k）这类
    编辑器本体和语言运行时会被后面的规则硬塞一个标签，那比不标糟得多。

    只在自动判定为空时调用，已有标签的一律不碰 —— 对现有索引是纯增量、零改判。
    """
    m = _manual_tags()
    url = norm_url(r.get("url")).lower()
    if url not in m:
        return False, None
    lab = m[url]
    return True, (str(lab) if lab else None)


def _score_one(fields: dict[str, str],
               matchers) -> list[tuple[str, tuple[float, float, int, bool, int]]]:
    """返回 [(标签, (总分, 最强字段权重, 命中词数, 是否在正文命中, 描述里最早命中位置))]。

    后三项里，第 4 项用来识别「只有 topic 撑腰」的标签：GitHub topic 是作者随手打的，
    一个翻墙代理也会标 tunnel，Xray-core、v2ray-core 就这么进了「内网穿透」。

    第 5 项是给长描述规则用的。原来那条规则按「描述长度 > 150」判定「这是技术栈清单、
    不是定位」，长度只是代理指标：作者的定位句永远在最前面，要列的支持项永远在后面。
    fzf 的 "A general purpose command-line fuzzy finder, can be used with any list: …"
    定位句就在第 30 个字，却因为后面拖了一串用途被整条判死，于是 83k star 的 fzf
    连「搜索」都没有。改成看命中位置：越靠前越是定位。

    曾在这条链上加过「多词短语命中长描述可豁免」，实测 80 条收益带出 15 条改判
    （crawlee 丢了「爬虫抓取」、freeplane 丢了「笔记知识库」），已整体撤除。
    """
    scores: dict[str, tuple[float, float, int, bool, int]] = {}
    for label, kws in matchers:
        s = 0.0
        best = 0.0
        nhit = 0
        body = False
        dpos = 10 ** 6
        skip_cat = label in NO_CATEGORY_LABELS
        for kind, k, k_name in kws:
            hit_any = False
            for fname, text in fields.items():
                if not text:
                    continue
                # 见 NO_CATEGORY_LABELS：这类场景的章节标题不算用途证据。
                if skip_cat and fname == "category":
                    continue
                mat = k_name if fname == "name" else k
                if kind == "in":
                    ok = mat in text
                    pos = text.find(mat) if ok else -1
                else:
                    m = mat.search(text)
                    ok = bool(m)
                    pos = m.start() if m else -1
                if ok:
                    s += FIELD_W[fname]
                    best = max(best, FIELD_W[fname])
                    hit_any = True
                    if fname != "topics":
                        body = True
                    if fname == "desc" and pos >= 0:
                        dpos = min(dpos, pos)
            if hit_any:
                nhit += 1
        if s:
            scores[label] = (round(s, 2), best, nhit, body, dpos)
    return sorted(scores.items(), key=lambda kv: (-kv[1][0], kv[0]))


def _topic_only_tag(hit_all, weak_labels: set[str]) -> list[str]:
    """兜底：正文里没有证据、但有 >=2 个 topic 同时指向同一场景时，补一个。

    只在自动判定和 topic_map 都为空的条目上调用，已有标签的一律不碰，
    所以按构造零改判 —— 试过在主判定里放行同类证据，改判 167 条、四成是错的。

    为什么门槛是 2：单个 topic 是噪声（django 的 tags 里有 orm、curl 有 mqtt），
    两个以上同时指向同一场景，才是作者在反复自报身份。

    为什么泛场景要单独抬高门槛：数据库 / 认证鉴权 / 监控告警 / 部署托管 / 自托管 /
    性能压测的关键词是 database、auth、monitoring、docker 这类人人能沾的词，几乎每个
    后端框架的 topics 里都有。实测全用 2 时收回 121 条、准确率只有七成——「数据库」
    18 条里 6 条是 C++ 框架（poco、userver、apalis、lithium、treefrog），只因 topics
    里有 database。但一刀切整个场景都不收也不对：被挡掉的是 nocodb(65k)、directus(38k)、
    etcd(52k)、rocksdb(32k)，换来的只是 poco、userver 这些小星误判，账不划算。
    所以给它们单独要 3 个 topic 共识，两者兼顾。
    """
    for label, (_sc, _best, nhit, body, _dpos) in hit_all:
        need = 3 if label in _GENERIC_TAGS else 2
        if body or nhit < need:
            continue
        if label in weak_labels:
            continue
        return [label]
    return []


# 这些场景的关键词是「谁都能沾一点」的基础设施词，不适合拿 topics 共现来推断
_GENERIC_TAGS = {"数据库", "认证鉴权", "监控告警", "部署托管", "自托管", "性能压测"}


def _quality(r: dict) -> float:
    q = 0.0
    stars = r.get("stars") or 0
    if stars:
        q += min(55.0, math.log10(stars + 1) * 13.0)
    p = r.get("pushed_at") or ""
    if p:
        try:
            d = (date.today() - date.fromisoformat(p[:10])).days
            if d <= 90:
                q += 22
            elif d <= 365:
                q += 14
            elif d <= 730:
                q += 6
        except ValueError:
            pass
    origin = r.get("origin")
    if origin == "cn":
        q += 26           # 人工精选，本身就是一个信号
        if r.get("alive"):
            q += 6
    elif origin == "awesome":
        q += 5
    q += min(12, len(r.get("tags") or []) * 3)
    return round(q, 1)


def _merge_homepages(rows: list[dict]) -> list[dict]:
    """把「官网条目」并进同名的 GitHub 仓库条目。

    ollama.com 和 github.com/ollama/ollama 是同一个产品的两个入口，但在索引里
    是两条独立记录，星数一个 18 万一个 0。模型看到会当成两个工具，
    甚至把 0 星的官网当成「另一个项目」。

    判据故意很窄，必须同时满足：同名、存在 GitHub 仓库、仓库星数够、官网域名
    的主干正好等于产品名（ollama.com ↔ ollama）。像 fabfile.org（fabric 部署工具）
    和 danielmiessler/Fabric（AI 工具）这种同名不同物的，域名对不上，不会被合并。
    """
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault((r.get("name") or "").strip().lower(), []).append(r)
    drop: set[int] = set()
    merged = 0
    for name, group in by.items():
        if len(group) < 2 or not name:
            continue
        gh = [r for r in group if "github.com/" in (r.get("url") or "")]
        if not gh:
            continue
        keep = max(gh, key=lambda x: x.get("stars") or 0)
        if (keep.get("stars") or 0) < 500:
            continue
        for r in group:
            if r is keep or "github.com/" in (r.get("url") or ""):
                continue
            try:
                host = (urlparse(r.get("url") or "").hostname or "").lower()
            except ValueError:
                continue
            if host.startswith("www."):
                host = host[4:]
            # 域名主体和工具名的对应关系不像想象中那么整齐：
            # joplin 的官网是 joplinapp.org、micro 是 micro-editor.github.io、
            # jenkins 是 jenkins-ci.org，要求两者完全相等会全漏掉。
            #
            # 试过放开成「域名主体以工具名开头」，多合 13 条，其中 4 条是错的：
            # sentinelguard.io（阿里限流组件）并进了 cartalyst/sentinel（PHP 权限库）、
            # coralproject.net（Vox Media 评论系统）并进了 Human-Agent-Society/CORAL。
            # 又试过加一道「两条说明要有共同词」的门槛，还是不行 ——
            # 技术套话让这个判据失效：两个完全无关的项目都写 code / source。
            #
            # 结论是这件事用规则做不可靠。宁可留两条重复入口（现在有 slug 能分清），
            # 也不能把两个真产品并成一个 —— 后者是数据错误，前者只是不够整洁。
            # 名字自带 TLD 的先剥掉（changedetection.io 对 changedetection.io）。
            nm = name.replace(" ", "")
            nm_core = re.sub(r"\.(io|sh|app|dev|ai|com|org|net|so|cc|to)$", "", nm)
            body = host.split(".")[0]
            if not host or not (body == nm or (bool(nm_core) and body == nm_core)):
                continue
            # 国内精选清单是人工写的：中文描述更好读，精选身份也是质量信号。
            # 并进仓库条目时不能被更长的英文描述顶掉，否则 ECharts、Nacos 这些
            # 反而退回成一条普通英文记录。
            if r.get("origin") == "cn":
                keep["origin"] = "cn"
                keep["why"] = r.get("why") or keep.get("why")
                if r.get("desc") and CJK.search(r["desc"]):
                    keep["desc"] = r["desc"]
                if r.get("tags"):
                    keep["tags"] = r["tags"]
                if r.get("name"):
                    keep["name"] = r["name"]
            elif len(r.get("desc") or "") > len(keep.get("desc") or ""):
                keep["desc"] = r["desc"]
            for f in ("stars", "forks"):
                if (r.get(f) or 0) > (keep.get(f) or 0):
                    keep[f] = r[f]
            keep.setdefault("home", r.get("url"))
            drop.add(id(r))
            merged += 1
    if merged:
        log(f"官网并入仓库：{merged} 条重复入口已合并")
    return [r for r in rows if id(r) not in drop]


def _merge_repo_duplicates(rows: list[dict]) -> list[dict]:
    """同一个产品在 GitHub 上有多个组织入口时，只留一条。

    典型是项目迁移：v2ray/v2ray-core 迁到 v2fly/v2ray-core，gost 从 ginuerzh
    迁到 go-gost。两条记录的 name 和 desc 一字不差，但 url 不同，_merge_homepages
    只管「官网并进仓库」，管不到这种情况——于是索引里出现两个同名同描述、
    星数不同的条目，用户会当成两个工具。

    判据故意收紧到「name 和 desc 都完全相同」，因为同名不同物很常见
    （tabby 是终端也是 AI 编码器，bun 是运行时也是 ORM），只靠名字会误合。

    保留规则：留 pushed_at 最新的那条（活跃仓库才是用户该点的），
    但星数取组内最大值——星数是产品整体的热度，不该因为换了组织就掉一半。
    """
    by: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        nm = (r.get("name") or "").strip().lower()
        ds = (r.get("desc") or "").strip().lower()
        if not nm or not ds:
            continue
        if "github.com/" not in (r.get("url") or ""):
            continue
        by.setdefault((nm, ds), []).append(r)

    drop: set[int] = set()
    merged = 0
    for (nm, _ds), group in by.items():
        if len(group) < 2:
            continue
        keep = max(group, key=lambda x: (x.get("pushed_at") or "", x.get("stars") or 0))
        for r in group:
            if r is keep:
                continue
            for f in ("stars", "forks"):
                if (r.get(f) or 0) > (keep.get(f) or 0):
                    keep[f] = r[f]
            if not keep.get("tags") and r.get("tags"):
                keep["tags"] = r["tags"]
            drop.add(id(r))
            merged += 1
            log(f"同产品多仓库合并：{r.get('url')} → {keep.get('url')}")
    return [r for r in rows if id(r) not in drop]


def _merge_moved_repos(rows: list[dict]) -> list[dict]:
    """同一个仓库在旧地址、新地址各留一条时，只留新地址那条。

    这批重复是补元数据的副作用：星数补回来之前，旧地址那半边的 star 是 0，
    被 _quality 压在几千名，看不见；补上真实星数后两边并排浮上来 ——
    搜「终端」时 jwilm/alacritty 和 alacritty/alacritty 占着第 2、3 名，
    说明文字还是两种措辞，用户会当成两个工具。

    判据用 GitHub 自己的 301，不用「名字像 + 星数差不多」这种猜测
    （理由见 _gh_moved）。留新地址那条，因为那是 GitHub 认定的当前位置。

    合并时保住精选身份：old 那条来自精选清单的话，origin 提上来（awesome
    有 5 分质量分，CLI 上还显示成 [精选]），名字、分类、中文说明也优先留
    人工写的那份 —— 一个 `Alacritty` 一个 `alacritty`，前者才是用户认得的写法。
    """
    moved = _gh_moved()
    if not moved:
        return rows
    by_url = {norm_url(r.get("url")).lower(): r for r in rows}
    drop: set[int] = set()
    merged = 0
    for old, new in moved.items():
        if old == new:
            continue
        gone, keep = by_url.get(old), by_url.get(new)
        if not gone or not keep or gone is keep:
            continue
        for f in ("stars", "forks"):
            if (gone.get(f) or 0) > (keep.get(f) or 0):
                keep[f] = gone[f]
        for f in ("lang", "category"):
            if not keep.get(f) and gone.get(f):
                keep[f] = gone[f]
        if len(gone.get("desc") or "") > len(keep.get("desc") or ""):
            keep["desc"] = gone["desc"]
        if gone.get("tags"):
            keep["tags"] = list(dict.fromkeys(
                (keep.get("tags") or []) + list(gone["tags"])))
        if gone.get("origin") in ("awesome", "cn"):
            keep["origin"] = gone["origin"]
            keep["why"] = gone.get("why") or keep.get("why")
            gn, kn = (gone.get("name") or ""), (keep.get("name") or "")
            if gn and (not kn or (gn[:1].isupper() and kn[:1].islower())):
                keep["name"] = gn
        keep["moved_from"] = gone.get("url")
        # 星数和精选身份都可能变了，质量分要重算 —— 上面那些合并发生在
        # 打分之后，不重算等于白合，这条仍按旧分卡在原来的位置。
        keep["quality"] = _quality(keep)
        drop.add(id(gone))
        merged += 1
        log(f"仓库搬家合并：{gone.get('url')} → {keep.get('url')}")
    if merged:
        log(f"搬家合并：{merged} 条旧地址并入新地址")
    return [r for r in rows if id(r) not in drop]


def _mark_wrappers(rows: list[dict]) -> None:
    """标出「同名但是别人客户端/封装」的条目。

    ollama/ollama-python 与 ollama/ollama 同名，前者只是后者的 Python 客户端。
    但它的说明里反复出现 Ollama，文本相关度反而更高，于是搜「ollama」时
    封装件排在本体前面 —— 这正是用户最容易被误导的地方。

    判定条件全部满足才算：同名、星数差一个量级以上、描述自认是封装/客户端。
    """
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault((r.get("name") or "").strip().lower(), []).append(r)
    for _name, group in by.items():
        if len(group) < 2:
            continue
        top = max((r.get("stars") or 0) for r in group)
        if top < 1000:
            # 都还是小项目，谁是本体说不清，不标
            continue
        for r in group:
            if (r.get("stars") or 0) * 5 > top:
                continue      # 量级相当，不是附属品
            if WRAP_RE.search(r.get("desc") or ""):
                r["wrap"] = 1


def _slug_of(url: str) -> str:
    """取一个能区分同名条目的标识：代码托管站用 owner/repo，其它站用域名。"""
    p = urlparse(url or "")
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segs = [s for s in p.path.split("/") if s]
    if host in ("github.com", "gitlab.com", "bitbucket.org") and len(segs) >= 2:
        repo = segs[1]
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        return f"{segs[0]}/{repo}"
    return host


def _mark_name_clashes(rows: list[dict]) -> None:
    """同名条目补一个 slug，让用户能分清点哪个。

    全库有 95 组同名、201 条：bun 既是 JS 运行时又是 Go ORM，tabby 既是终端
    又是 AI 编码助手，goose 有三条完全不同的东西（AI agent / 数据库迁移 / 压测）。
    名字一样时，列表里两行长得一模一样，用户没法判断该点哪个。

    只在「slug 真的不同」时才标 —— 同一域名下的不同路径（官网 + 仓库）区分不了，
    标了也没用，那类交给 _merge_homepages 去合并。
    """
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault((r.get("name") or "").strip().lower(), []).append(r)
    marked = 0
    groups = 0
    for _name, group in by.items():
        if len(group) < 2:
            continue
        groups += 1
        slugs = {_slug_of(r.get("url") or "") for r in group}
        slugs.discard("")
        if len(slugs) < 2:
            continue
        for r in group:
            s = _slug_of(r.get("url") or "")
            if s:
                r["slug"] = s
                marked += 1
    if marked:
        log(f"同名消歧：{groups} 组重名里 {marked} 条补上 slug")


def normalize(rows: list[dict], cfg: dict | None = None) -> list[dict]:
    cfg = cfg or load_config("sources.json")
    rules = load_config("scenarios.json").get("tags", [])
    matchers = _build_matchers(rules)
    weak_labels = {t["label"] for t in rules if t.get("weak")}
    log(f"场景标签规则载入 {len(rules)} 个（其中 {len(weak_labels)} 个是弱标签，判定更严）")

    # ---- 1. 按 URL 去重，合并同源信息 ----
    drops = _drop_urls()
    descs = _manual_desc()
    dropped_by_list = 0
    merged: dict[str, dict] = {}
    for r in rows:
        url = norm_url(r.get("url"))
        if not url or not url.startswith("http"):
            continue
        key = url.lower()
        if key in drops:
            dropped_by_list += 1
            continue
        prev = merged.get(key)
        if prev is None:
            nr = dict(r)
            nr["url"] = url
            nr["_tags_hint"] = list(r.get("tags_hint") or [])
            merged[key] = nr
            continue
        # 合并：star 和信息以更「实」的一方为准，描述取更长的
        for f in ("stars", "forks"):
            if (r.get(f) or 0) > (prev.get(f) or 0):
                prev[f] = r[f]
        if not prev.get("pushed_at") and r.get("pushed_at"):
            prev["pushed_at"] = r["pushed_at"]
        if len(r.get("desc") or "") > len(prev.get("desc") or ""):
            prev["desc"] = r["desc"]
        if not prev.get("lang") and r.get("lang"):
            prev["lang"] = r["lang"]
        # 分类要「谁有取谁」，不能让先出现的那行决定结果。
        # GitHub 抓来的行没有 category，awesome 列表有人工分类；
        # 如果 github 行恰好排在前，人工分类就被丢掉，打标随之失去证据
        # —— rembg 的「图像处理」就是这么丢的（它来自 awesome 的 Image Processing）。
        if not prev.get("category") and r.get("category"):
            prev["category"] = r["category"]
        # 源优先级：cn 是人工精选，优先级最高；其次是 github（有 star 信号）
        rank = {"cn": 3, "github": 2, "awesome": 1}
        if rank.get(r.get("origin"), 0) > rank.get(prev.get("origin"), 0):
            prev["origin"] = r["origin"]
            prev["id"] = r["id"]
            if r.get("why"):
                prev["why"] = r["why"]
        prev["_tags_hint"] = prev["_tags_hint"] + list(r.get("tags_hint") or [])
        if not prev.get("alive") and r.get("alive"):
            prev["alive"] = True

    log(f"去重合并：{len(rows)} -> {len(merged)}")
    if dropped_by_list:
        log(f"下架表：剔除 {dropped_by_list} 条确认非工具的条目")

    # ---- 1.4 事后补抓的 GitHub 元数据 ----
    # 只填空、不覆盖：抓取时拿到的值一律优先，这里只补「压根没有」的。
    # 放在打标和算质量分之前，补上的 star 会参与 _quality 计算 —— 这是有意的，
    # 这批条目原来 cardinality 全是 0，质量分卡在地板 8 分，补完才是它们该在的位置。
    meta = _gh_meta()
    enriched = 0
    for key, r in merged.items():
        m = GH_RE.match(r.get("url") or "")
        if not m:
            continue
        hit = meta.get(f"{m.group(1)}/{m.group(2)}".lower())
        if not hit:
            continue
        changed = False
        if not (r.get("stars") or 0) and hit.get("stars"):
            r["stars"] = int(hit["stars"])
            changed = True
        if not r.get("pushed_at") and hit.get("pushed_at"):
            r["pushed_at"] = hit["pushed_at"]
            changed = True
        if not r.get("lang") and hit.get("lang"):
            r["lang"] = hit["lang"]
            changed = True
        if not (r.get("desc") or "").strip() and hit.get("desc"):
            r["desc"] = hit["desc"]
            changed = True
        if changed:
            enriched += 1
    if enriched:
        log(f"补抓元数据：{enriched} 条拿到星数/语言/更新时间")

    # ---- 1.5 人工说明覆盖 ----
    # 放在合并之后、打标之前：打标能读到补好的说明，判定更准；
    # 而合并阶段「描述取更长的」那条规则也不会把人工说明冲掉。
    #
    # 无条件覆盖，不比长度。曾经比过，结果中文说明吃亏：「自托管的碎片笔记应用…」
    # 35 个字，被原版英文 43 个字符判成「更短」而不覆盖。中英文按字符数比长度
    # 本来就没有可比性，而这张表是人写的，可信度天然高于抓来的自动摘要。
    desc_fixed = 0
    for key, r in merged.items():
        d = descs.get(key)
        if d and d != (r.get("desc") or "").strip():
            r["desc"] = d
            desc_fixed += 1
    if desc_fixed:
        log(f"人工说明表：补写 {desc_fixed} 条说明")

    # ---- 2. 打场景标签 ----
    out: list[dict] = []
    dropped_no_tag = 0
    dropped_empty = 0
    manual_tagged = 0
    manual_null = 0
    for r in merged.values():
        name = (r.get("name") or "").lower()
        topics = [str(t).replace("-", " ") for t in (r.get("topics") or [])]
        fields = {
            "name": name,
            "topics": " ".join(topics).lower(),
            "category": (r.get("category") or "").lower(),
            "desc": (r.get("desc") or "").lower(),
        }

        hit = _score_one(fields, matchers)
        # 先剔除「只靠 topics」的标签，再算最强分。顺序很重要：
        # 否则一个 topic-only 的高分标签会当上 top_score，再按 NORMAL_RATIO
        # 把正文有据的标签挤掉 —— curl 就是这么被 topics 里的 mqtt 带走的。
        #
        # 但「只靠 topics」不等于「不可信」：单个 topic 是噪声（django 的 orm、
        # curl 的 mqtt），两个以上 topic 同时指向同一场景就是作者在反复自报身份了。
        #
        # 试过在这一行直接放行 nhit>=2（hit = ... or v[2] >= 2）：收回 131 条、
        # 降级 0，但改判 167 条且约四成是错的 —— nodebestpractices 从「学习资源」
        # 变成「单元测试」、gogs 从「自托管」变成「数据库」、dokploy 从「部署托管」
        # 变成「数据库」，都是 database/testing 这类泛 topic 把正确标签挤掉。
        # 主判定一放松就重演「补词」那类连锁改判，所以这行维持原样。
        # 这类证据改在下方「只补空」的兜底里用（见 _topic_only_tag）。
        desc_len = len((r.get("desc") or "").strip())
        hit_all = hit
        hit = [(l, v) for l, v in hit if v[3] or desc_len < 20]
        strong: list[str] = []
        weak_hit: list[str] = []
        rescued: list[str] = []
        if hit:
            top_score = hit[0][1][0]
            for label, (sc, best, nhit, body, dpos) in hit:
                if sc < max(1.0, top_score * NORMAL_RATIO):
                    continue
                # 长描述多半是「我支持这些协议/技术栈」的清单，不是定位。
                # curl 的说明里逐条列了 MQTT、IMAPS、POP3、SFTP —— 它「支持」这些，
                # 不等于它是消息队列或文件同步工具。短描述才是作者自己的定位。
                # 所以长描述必须靠名字/分类级别的强证据（> topics 权重）才算数。
                #
                # 光看长度会误伤：Chat2DB 的 "…local-first database client"、nuclei 的
                # "customizable vulnerability scanner"、fzf 的 "…fuzzy finder" 都是正经
                # 定位句，只是后面还拖着一串用途。所以再看命中位置：定位句总在最前面，
                # 支持清单总在后面（DESC_POS_MAX 之内算定位句）。
                #
                # 但「位置靠前」只是弱证据，不足以进主判定。曾让它直接进 strong，
                # 39 条已有标签被顶掉（memos 丢「笔记知识库」、quivr 丢「大模型应用」、
                # unstructured 丢「PDF 文档」）—— 救回一个标签的代价是挤掉一个更准的。
                # 现在它只进 rescued，只在主判定完全为空时补一个，按构造不可能改判。
                if best <= FIELD_W["topics"] and desc_len > 150:
                    if dpos <= DESC_POS_MAX:
                        rescued.append(label)
                    continue
                if label in weak_labels:
                    # 弱标签只是「基础设施相关」，不是业务定位。
                    # 只有在没有业务标签可依靠时才会被采用。
                    if sc >= max(1.0, top_score * WEAK_RATIO):
                        weak_hit.append(label)
                    continue
                # 除最强的业务标签外，其余必须有强信号支撑，
                # 只在描述里顺带一提的不算。
                if strong and best <= FIELD_W["desc"]:
                    continue
                strong.append(label)
            # 有业务归属就只标业务标签；纯基础设施类项目才退回弱标签，且只取一个。
            # 试过让弱标签与强标签共存（strong[:2] + weak_hit[:1]），实测改动面从 15
            # 涨到 144 条且该丢的照样丢，净负，已回退。
            auto = strong[:3] if strong else weak_hit[:1]
        else:
            auto = []
        # 人工标注优先：国内清单的 tags 是逐条写过的，比自动判定准。
        # 已经人工归过类的，只再补一个非基础设施类的自动标签。
        hint = list(r.get("_tags_hint") or [])
        if hint:
            extra = [x for x in auto if x not in weak_labels][:1]
            tags = list(dict.fromkeys(hint + extra))
        else:
            # 人工判读表是权威，优先于任何规则：
            #   有值   → 用人工结论，规则算出来的一律丢掉
            #   null   → 人工确认过「它不属于任何场景」，把规则结果清空
            #   没记录 → 走规则（新抓进来的条目都归这里）
            #
            # 原来这张表只在「规则算空」时才查，结果一开新场景就露馅了：
            # "日志与追踪"上线后，TeslaMate（车辆数据记录）、Wavelog（业余无线电通联日志）、
            # wger（健身记录）、gpslogger（GPS 轨迹）全被捞了进来——它们的自述里都有
            # logging / logger / log，规则分不清「记录数据的软件」和「日志系统」，人一眼能分。
            # 所以否决权必须真的生效，不能只在规则沉默时才有话语权。
            known, label = _manual_tag(r)
            if known:
                auto = [label] if label else []
                if label:
                    manual_tagged += 1
                else:
                    manual_null += 1
            elif not auto:
                # 兜底顺序是有讲究的：topic_map 是逐键人工核过的显式映射，
                # 可信度高于「命中词出现在描述前 120 字」这种位置推断。
                # 曾经把 rescued 排在前面，memos（笔记知识库）、quivr（大模型应用）
                # 都被一条更泛的弱标签顶掉——它们的正解本来就是 topic_map 给的。
                auto = (_topic_map_tags(r, {l for l, _ in matchers})
                        or rescued[:1])
                if not auto:
                    # 最后一档：正文没证据，但多个 topic 同时指向同一场景。
                    # 排在 topic_map 之后，因为那张表是逐条核过的，这条是规则推断。
                    auto = _topic_only_tag(hit_all, weak_labels)
            tags = list(dict.fromkeys(auto))

        r["tags"] = tags
        r["zh"] = 1 if CJK.search((r.get("name") or "") + (r.get("desc") or "")) else 0
        r.pop("_tags_hint", None)
        r.pop("tags_hint", None)

        # ---- 3. 质量过滤 ----
        if not r.get("desc") and not tags:
            dropped_empty += 1
            continue
        # awesome 条目没有 star 背书，必须能归入至少一个场景，否则就是纯噪声
        if r.get("origin") == "awesome" and not tags:
            dropped_no_tag += 1
            continue

        r["quality"] = _quality(r)
        out.append(r)

    log(f"场景打标完成：保留 {len(out)} 条"
        f"（丢弃无场景归属 awesome {dropped_no_tag} 条，空描述 {dropped_empty} 条）")
    if manual_tagged or manual_null:
        log(f"人工判读表：补标签 {manual_tagged} 条，确认无场景 {manual_null} 条")

    # ---- 3.5 合并同一产品的「官网 + 仓库」双入口 ----
    out = _merge_homepages(out)
    # 同产品换组织（v2ray → v2fly、ginuerzh/gost → go-gost）会留下两条同名同描述的记录
    out = _merge_repo_duplicates(out)
    # 换组织但改了描述措辞的那批：靠 GitHub 自己的 301 记账，见 _gh_moved
    out = _merge_moved_repos(out)
    # 标记「同名封装件」
    # ollama/ollama-python 和 ollama/ollama 都叫 ollama，前者只是后者的 Python 客户端，
    # 但它的说明反复出现 Ollama，文本分反而更高，于是搜 ollama 时排在本体前面。
    # 这里在数据里一次性标好 wrap 标记，网页/CLI/MCP 直接读，避免三处各写一套判断。
    _mark_wrappers(out)
    # 同名条目补 slug（bun / tabby / goose 这类，光看名字分不清）
    _mark_name_clashes(out)

    # ---- 4. 排序：质量分降序 ----
    out.sort(key=lambda x: (-(x.get("quality") or 0), -(x.get("stars") or 0)))

    # ---- 5. 精简为稳定 schema ----
    keep = ("id", "name", "url", "home", "desc", "origin", "tags", "lang", "stars",
            "pushed_at", "category", "why", "alive", "checked", "quality", "zh",
            "wrap", "slug", "moved_from")
    final = [{k: r.get(k) for k in keep if r.get(k) not in (None, "")} for r in out]
    return final


if __name__ == "__main__":
    from common import load_json, DATA_DIR
    raw = load_json(DATA_DIR / "raw.json", [])
    rows = normalize(raw)
    print(len(rows))
