"""检索核心（CLI 与 skill 共用）。

两级召回：
  1. 字面匹配：工具名 / 标签 / 描述里直接出现关键词
  2. 场景翻译：用户口语（"造数据"）先映射到场景标签（"接口 Mock"），
     再召回该场景下的工具

第 2 级是必须的。用户不会说"接口 Mock"，他会说"造测试数据"。
打分公式与网页版保持一致，避免命令行和网页给出互相矛盾的排序。
"""
from __future__ import annotations

import math
import re

from common import DATA_DIR, load_config, load_json

CJK = re.compile(r"[\u4e00-\u9fff]")

# 口语化前缀/停用词：「我要找个能 mock 接口的」等价于「mock 接口」
STOP_PREFIX = re.compile(
    r"^(我要|我想找|我想|帮我找|帮我|找个|找一个|找|有没有|什么|怎么|如何|可以|能|"
    r"用来|用于|一个|一款|工具|软件|库|平台|方案|推荐|的|了|吗|呢|啊|请|想)+"
)

# 中文查询展开出的英文词命中「名字」时给的分。
# 一档是 100 分，所以这个数决定它能不能跨档；超过 100 就等于当成「点名」，
# 把名字里碰巧带该词的实现顶到真正的头部工具前面 —— 数值是测出来的，见 score()。
CJK_EXPAND_NAME_HIT = 62
# 文本分档宽度：同档内按星数排。档位就是「噪声容差」——档太窄，几十个
# 巧合分就能让 ★3 的工具排到 ★57k 前面；档太宽，真正对口的工具又被大牌挤掉。
SCORE_BAND = 100

_TAG_CACHE: dict[str, set[str]] | None = None
_TERMS: dict[str, list[str]] | None = None


def load_terms() -> dict[str, list[str]]:
    global _TERMS
    if _TERMS is None:
        _TERMS = load_config("terms.json").get("terms", {}) or {}
    return _TERMS


def expand_en(qn: str) -> list[tuple[str, list[str]]]:
    """把问法里的中文概念展开成英文词，按源概念分组返回 [(中文词, [英文词])]。

    不加这层，中文查询和英文描述永远没有交集，场景标签内部的排序
    就完全由 star 决定——搜「压缩图片」会给出 OCR 工具。

    分组是必要的：「截图」会展开成 6 个英文同义词，如果一个工具的描述里
    凑巧出现其中 3 个就加三份分，会盖过真正的截图工具。

    只取最长匹配，和 query_tags 一个道理：查「数据库客户端」时，
    如果同时展开「数据库」→ database/sql，sqlmap（SQL 注入工具）就会
    靠名字里的 sql 挤进前三。只展开最长的那条，词义才不会串。
    """
    terms = load_terms()
    best = 0
    for zh in terms:
        if zh and zh in qn and len(zh) > best:
            best = len(zh)
    if not best:
        return []
    return [(zh, ens) for zh, ens in terms.items() if zh and zh in qn and len(zh) == best]


def load_index() -> list[dict]:
    return load_json(DATA_DIR / "tools.json", []) or []


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    # 反复剥前缀，否则「我要找个能压测的工具」只剥掉了「我要」
    prev = None
    while prev != s:
        prev = s
        s = STOP_PREFIX.sub("", s).strip(" 　,，、的")
    return re.sub(r"\s+", " ", s).strip()


def _tag_terms() -> dict[str, set[str]]:
    global _TAG_CACHE
    if _TAG_CACHE is None:
        rules = load_config("scenarios.json").get("tags", [])
        m: dict[str, set[str]] = {}
        for t in rules:
            terms: set[str] = set()
            for a in (t.get("alias") or []):
                a = str(a).lower().strip()
                if a:
                    terms.add(a)
            # 中文 kw 也能当别名用；英文 kw 太泛，不参与 query 翻译
            for k in (t.get("kw") or []):
                k = str(k).lower().strip()
                if k and CJK.search(k) and len(k) >= 2:
                    terms.add(k)
            m[t["label"]] = terms
        _TAG_CACHE = m
    return _TAG_CACHE


def query_tags(query: str) -> list[str]:
    """把用户问法翻译成场景标签。

    只取「最长匹配」那批标签。否则「本地大模型」会同时命中
    「模型服务」的别名和「大模型应用」的关键词，两个都召回，
    结果被星标多但不对口的语音合成项目挤满。
    """
    qn = norm(query)
    if not qn:
        return []
    hits: list[tuple[int, str]] = []
    for label, terms in _tag_terms().items():
        best = 0
        for t in terms:
            if not t:
                continue
            if t in qn:
                best = max(best, len(t))
            elif len(qn) >= 2 and len(t) >= 2 and qn in t and len(qn) >= len(t) * 0.6:
                best = max(best, len(qn))
        if best:
            hits.append((best, label))
    if not hits:
        return []
    mx = max(h[0] for h in hits)
    return [label for ln, label in hits if ln >= mx]


_ALNUM = re.compile(r"[a-z0-9]")


def _hit_strict(needle: str, text: str) -> bool:
    """严格按词边界匹配。用于「中文概念翻译出来的英文词」。

    这些词是从 terms.json 里查出来的标准词（对比→diff），不是用户手打的，
    所以不该做子串模糊匹配：`diff` 命中 `different`、`log` 命中 `catalog`
    都会让整库一半条目沾上分，最后按 star 一排，第一名就成了体量最大的那个
    不相干项目。查「文本对比」时 deer-flow 排第一就是这么来的（它的描述里有
    different levels of task）。
    """
    if not needle or not text:
        return False
    if CJK.search(needle):
        return needle in text
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                     text, re.IGNORECASE) is not None


def _hit(needle: str, text: str) -> bool:
    """needle 是否出现在 text 里。

    **只对 2 字符的缩写强制词边界**，更长的词仍按子串判。原因：
    `ip` 会命中 `VoIP`、`Recipes`（rec-ip-es），`os` 命中 `postgres`，
    `ui` 命中 `build`——这类 2 字符误命中会把不相干的高星项目顶到第一。
    但 3 字符以上不能一刀切：`log` 要靠子串命中 `spdlog`/`logrus`，
    `k8s` 要命中 `k8sgpt`，`json` 要命中 `jsonhero`，`docker` 要命中
    `lazydocker`。全上词边界会让日志场景第一名变成 `swift-log` 这种。
    """
    if not needle or not text:
        return False
    if CJK.search(needle):
        return needle in text
    if len(needle) <= 2 and _ALNUM.search(needle):
        return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                         text, re.IGNORECASE) is not None
    return needle in text


def score(t: dict, raw: str) -> int:
    ql = norm(raw)
    if not ql:
        return 0
    n = (t.get("name") or "").lower()
    d = (t.get("desc") or "").lower()
    g = " ".join(t.get("tags") or []).lower()
    c = (t.get("category") or "").lower()

    # 描述命中的分数单独累计，最后封顶。
    # 不封顶会被关键词堆砌刷分：某个内网穿透工具的说明写了
    # 「高性能跨平台的内网穿透解决方案 远程内网计算机 域名访问内网站点
    #   反向代理内网服务 端口转发 http代理」
    # 于是「内网」「穿透」「反向代理」各自成组逐个加分，硬生生把 110k star
    # 的正牌项目挤下去。封顶后，堆砌最多只能拿到一条正常描述的分数。
    DESC_CAP = 70
    ds = 0

    s = 0
    if n == ql:
        s += 900
    is_cjk = bool(CJK.search(ql))
    name_hit = _hit(ql, n)
    if name_hit:
        # 中文查询的「名字含整串」不给满 260：两个字的中文词太泛，
        # 「iOS 截图装饰」名字里有「截图」不代表它是这个品类的代表。
        # 英文名命中可信得多（用户多半在打工具名），保持 260。
        #
        # 而且它和「标签命中」是同一件事的两个说法——都在回答「这条属不属于这个
        # 品类」。同一概念只计一次，所以两者同时成立时不再叠加：
        # 「草灰笔记」名字含「笔记」、标签也是「笔记知识库」，叠加后拿到
        # 180+210+95=485，比 ★57k 的 joplin（210+95+118=423）还高，
        # 搜「笔记」第一位是个 ★3 的工具。标签已经说了它属于这个品类，
        # 名字是不是中文就不该再有加分。名字证据真正有用的场合是「标签没命中」，
        # 那时它是唯一线索，这条规则不动它。
        if not (is_cjk and _hit(ql, g)):
            s += 180 if is_cjk else 260
    if _hit(ql, g):
        s += 210
    if _hit(ql, c):
        s += 90
    # 中文整串命中描述不给满分：中文描述里出现「对比」「限流」这两个字太容易
    # （画图工具写「左右对比」、小红书运营工具写「限流自查」），而英文正牌工具
    # 只能靠 terms 翻译拿到六成分。不平权的结果是中文小工具站稳定压过正牌。
    # 中文仍走下面 token 的 26 分，不至于一点不算。
    if _hit(ql, d):
        ds += 0 if is_cjk else 70

    for tk in re.split(r"[\s,，、]+", ql):
        if not tk:
            continue
        # 用户经常「点名 + 说任务」：goose 数据库迁移、frp 内网穿透。
        # 这时名字精确等于某个词，就该按近似精确命中算，不能只给 110 分
        # —— 否则会被一堆 star 更高的泛化工具盖掉，用户点名的那个根本不出现。
        if tk == n:
            s += 300
        elif _hit(tk, n) and not (is_cjk and name_hit):
            s += 110
        if _hit(tk, g):
            s += 95
        if _hit(tk, c):
            s += 40
        if _hit(tk, d):
            ds += 26

    # 中文概念 → 英文词。同一个概念只计一次最高分，多词短语用词袋判断
    # （desc 里写的是 "remove images background"，严格短语匹配会漏）。
    for _zh, ens in expand_en(ql):
        best = 0
        best_d = 0
        for en in ens:
            if " " in en:
                ws = en.split()
                # 名字是标识符，复合词很正常（ImgCompress、jsonhero），子串判；
                # 描述是自然语言，词是分好的，用词边界判——
                # 否则 diff 会命中 different、log 会命中 catalog。
                hit_n = all(_hit(w, n) for w in ws)
                hit_d = all(_hit_strict(w, d) for w in ws)
            else:
                hit_n = _hit(en, n)
                hit_d = _hit_strict(en, d)
            if hit_n:
                # 中文查询展开出来的英文词，命中了名字也不能算「点名」。
                # 用户打「笔记」，不可能是想点 vnote —— 他不知道有这个工具；
                # 名字里带 note 恰恰是小工具的特征（vnote / note-gen / fsnotes /
                # crossnote），而这一行最该出现的大牌全都不带（joplin ★57k、
                # logseq ★45k、AppFlowy ★78k、siyuan ★47k）。
                #
                # 给多少分是实测定下来的。一档 100 分，这个数决定能不能跨档：
                #   62（= 描述命中同分，本文件用的值）：已经是「不比描述命中更强」，
                #     但 62 仍够跨档，所以 vnote ★13k 排到了 joplin ★57k 前面 —— 这是
                #     已知残留，不是没想到（见 README「已知限制」泛类词那条）。
                #   30：想让它跨不过档，实测评测从 30/32 掉到 25/32 ——
                #     压缩图片、终端、shell 美化、去广告全靠这个分才浮得上来。
                #   0：名字里带该词、描述里却没写的工具直接归零，更差。
                # 三个数都试过，62 是唯一不额外伤人的，所以留 62，把「joplin 排第 7」
                # 作为已知限制记下来，而不是继续动这个数。
                best = max(best, CJK_EXPAND_NAME_HIT if is_cjk else 90)
            elif hit_d:
                best_d = max(best_d, 62)
        # 名字命中与描述命中分别记账，是有意的：试过合并成「同一概念只取一次」，
        # 中文查询下全线塌方（32 条评测从 30 掉到 22）—— 一批英文工具正是靠
        # 「名字里带这个词 + 描述里再提一次」才浮得上来（压缩图片、向量检索、
        # 代码格式化、终端都是）。所以这里保持分记。
        s += best
        ds += best_d
    # 影响力微调，最多 120 分：只用来打破「信息重复计数」造成的错位
    # （0 star 的小工具站靠名字里带查询词，压过 40k star 的正牌），
    # 不足以让高星盖过真正对口的工具。只给名字或标签命中的条目——
    # 否则纯靠描述词袋蹭到分的条目会连 star 一起蹭。
    star_bonus = 0
    if name_hit or _hit(ql, g):
        star_bonus = min(120, int(25 * math.log10((t.get("stars") or 0) + 1)))
    return s + min(ds, DESC_CAP) + star_bonus


_LIST_RE = re.compile(
    r"^\s*(?:awesome[-_ ]|the\s+awesome|curated\s+list|list\s+of\b)"
    r"|(?:a\s+)?curated\s+list\s+of|list\s+of\s+\w+\s+(?:for|to)\b"
    # 书、课、面试题、笔记、路线图：star 很高但都不是工具，
    # 比如 docker_practice（书，26k）、udemy-docker-mastery（课程）。
    r"|^\s*(?:the\s+)?(?:book|course|guide|handbook|cookbook|tutorial)\b"
    r"|\b(?:e-?book|cheat\s?sheet|interview\s+questions?|roadmap|"
    r"study\s+notes?|learning\s+notes?|awesome\s+list)\b"
    # 课程平台名和「边做边学」句式，是教材最稳的指纹
    r"|\budemy\b|\bcoursera\b|\bpluralsight\b|\bvideo\s+course\b"
    r"|\blearn\s+and\s+understand\b|从真实案例中学习"
    r"|电子书|教程|课程|面试题|学习笔记|读书笔记|路线图",
    re.IGNORECASE,
)


# 只在名字上判的读物特征。"best practices" / "30 days of" 这类句式几乎只出现在
# 教材标题里（nodebestpractices、30-Days-Of-Python），但描述里偶尔会正常提到，
# 所以单独一条，只查 name。
_NAME_ONLY_REF = re.compile(
    r"best[\s_-]?practices?|\d+\s*[-_ ]?days?[\s_-]+of[\s_-]|"
    r"\bfrom\s+scratch\b|\bfor\s+beginners\b|入门|从零开始",
    re.IGNORECASE,
)


def is_list(t: dict) -> bool:
    """判断是不是「参考读物」而不是工具本身：清单仓库、书、课程、面试题集。

    这类条目（awesome-tunneling、docker_practice 等）在场景检索里很碍事：
    用户问「内网穿透」，想拿的是 frp、gost、chisel，不是一份《隧道工具大全》；
    问「容器」，要的是 podman，不是一本 Docker 教材。
    它们仍然保留在索引里（按名字能搜到），但排在真工具之后。
    """
    name = t.get("name") or ""
    return bool(_LIST_RE.search(name) or _LIST_RE.search((t.get("desc") or "")[:80])
                or _NAME_ONLY_REF.search(name))


def search(query: str = "", *, tag: str = "", src: str = "", zh: bool = False,
           limit: int = 12) -> list[dict]:
    rows = load_index()
    if tag:
        rows = [t for t in rows if tag in (t.get("tags") or [])]
    if src:
        rows = [t for t in rows if t.get("origin") == src]
    if zh:
        rows = [t for t in rows if t.get("zh")]

    if not (query or "").strip():
        rows = sorted(rows, key=lambda t: -(t.get("quality") or 0))
        return rows[:limit]

    want_tags = set(query_tags(query))
    # 查询词正好等于某个场景标签（「接口 Mock」「内网穿透」）时，这个标签下的
    # 条目已经全部相关了，再拿「名字里含没含这个词」去排就是重复计分：
    # mockoon 名字里有 mock 于是 +110 跨档，压掉了 75k star 的 json-server。
    # 这类查询改为只按影响力排；如果查询只是标签的一部分（「截图」⊂「截图录屏」），
    # 说明还有别的意思，仍按命中强度排。
    qn = norm(query)
    exact_tags = {x for x in want_tags if norm(x) == qn}
    scored: list[tuple[int, dict]] = []
    for t in rows:
        sc = score(t, query)
        if want_tags and (set(t.get("tags") or []) & want_tags):
            sc += 180          # 场景命中，权重高于描述里的零散词
        if sc > 0:
            scored.append((sc, t))

    def _key(x: tuple[int, dict]):
        sc, t = x
        # 清单/教材 排最后，其次是同名的封装件（ollama-python 之于 ollama）。
        # 两者都不是「本体」，但都要保留在结果里。
        tier = (2 if is_list(t) else 0) + (1 if t.get("wrap") else 0)
        if exact_tags and (set(t.get("tags") or []) & exact_tags):
            # 场景命中：同档，影响力说话
            return (tier, 0, 0, -(t.get("stars") or 0), -(t.get("quality") or 0))
        return (tier, 1, -(sc // SCORE_BAND), -(t.get("stars") or 0),
                -(t.get("quality") or 0))

    scored.sort(key=_key)
    return [t for _, t in scored[:limit]]


# 曾尝试过一条更粗的不变量：「每个场景前 3 条必须有一条代表作」，
# 做法是排序后把高星且命中查询词的条目提上来。实测不成立，已放弃：
# 高星和「属于这个品类」不相关——高星仓库的描述都很长，随便就能蹭到查询词。
# 151 个查询里最严的一版还改错 4 处（学习→FreeDomain、传文件→seaweedfs、
# 同步文件→seaweedfs），只有 截图→ShareX 一处是对的。
# 剩下的不理想（如 截图 里 ShareX 排第 6）属于「顺序不理想、领域正确」，
# 按「概览够用」的标准可以接受，不值得为它引入更糟的错误。


def all_tags() -> list[tuple[str, int]]:
    from collections import Counter
    c: Counter = Counter()
    for t in load_index():
        for g in t.get("tags") or []:
            c[g] += 1
    return c.most_common()


if __name__ == "__main__":
    for q in ("造测试数据", "我要找个能压测的工具", "定时任务"):
        print(f"--- {q} -> tags={query_tags(q)}")
        for t in search(q, limit=3):
            print("   ", t["name"], "|", t.get("tags"))
