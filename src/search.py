"""检索核心（CLI 与 skill 共用）。

两级召回：
  1. 字面匹配：工具名 / 标签 / 描述里直接出现关键词
  2. 场景翻译：用户口语（"造数据"）先映射到场景标签（"接口 Mock"），
     再召回该场景下的工具

第 2 级是必须的。用户不会说"接口 Mock"，他会说"造测试数据"。
打分公式与网页版保持一致，避免命令行和网页给出互相矛盾的排序。

第 3 级（`rescue`）只在 1、2 两级一条都没命中时启用：按「查询里的字有多稀罕」
做字面兜底。因为用户在搜索框里写的是句子，「我要做一个后台管理系统」整句一个
词表概念都碰不上时，给一片空白比给几条沾边的更糟 —— 后者至少能顺着点下去。
"""
from __future__ import annotations

import math
import re

from common import DATA_DIR, load_config, load_json

CJK = re.compile(r"[\u4e00-\u9fff]")

# 口语化前缀/停用词：「我要找个能 mock 接口的」等价于「mock 接口」
STOP_PREFIX = re.compile(
    r"^(我要|我想找|我想|帮我找|帮我|找个|找一个|找|有没有|什么|怎么|如何|可以|能|"
    r"用来|用于|做一个|做个|弄一个|弄个|搞一个|搞个|写一个|写个|搭一个|搭个|"
    r"建一个|建个|整一个|整个|一个|一款|工具|软件|库|平台|方案|推荐|的|了|吗|呢|啊|请|想|做)+"
)
# 尾部的通用名词。中文里「找 X 工具」和「找 X」是同一件事，标签名却只写 X。
# 不含「应用」「产品」：剥掉它们会把「开发手机应用」变成「开发手机」，
# 比不剥更差 —— 剥完必须还是个说法，不能剥成一句没意义的话。
STOP_SUFFIX = re.compile(r"(工具集|工具|软件|平台|方案|系统|服务)$")

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


_MOVED: dict[str, str] | None = None


def _moved_map() -> dict[str, str]:
    """GitHub 仓库搬家的「旧地址 → 新地址」，来自 data/gh_moved.json 的 moved 段。

    为什么别名表要过这一层：also 白名单是按 URL 精确匹配的，而仓库会搬家。
    facebook/react-native 已经迁到 react/react-native，写死旧地址的表会**静默失效**
    —— 不报错、不提示，★126,851 的项目就这么掉了标签，要等人工比对才发现。
    同理也覆盖另一头：条目自己还挂着旧地址时，映射成新地址一样能对上。
    """
    global _MOVED
    if _MOVED is None:
        raw = (load_json(DATA_DIR / "gh_moved.json", {}) or {}).get("moved") or {}
        _MOVED = {str(k).rstrip("/").lower(): str(v).rstrip("/").lower()
                  for k, v in raw.items()}
    return _MOVED


def apply_also(rows: list[dict]) -> None:
    """把场景标签里声明的「跨格成员」补进条目的 tags。

    打标签天生是单归属的：manual_tags.json 一个 url 只能存一个标签，规则也只算出
    最像的那一个。但有些工具本身就是两个能力位的东西——electron-builder 既是
    「桌面应用开发」的零件，也是「构建打包」的手段。少了这条，问「打包成 exe」
    时它进不了打包那一格，只能给前端打包器。

    写在 scenarios.json 的 tag.also 里，而不是给工具加第二张表：归属是场景的
    属性，不是工具的属性，改场景时能一眼看到它借了谁。
    """
    rules = load_config("scenarios.json").get("tags", []) or []
    moved = _moved_map()
    url2tags: dict[str, list[str]] = {}
    for rule in rules:
        for u in (rule.get("also") or []):
            k = str(u).rstrip("/").lower()
            url2tags.setdefault(moved.get(k, k), []).append(rule["label"])
    if not url2tags:
        return
    for t in rows:
        u = (t.get("url") or "").rstrip("/").lower()
        for g in url2tags.get(moved.get(u, u), ()):
            tags = t.setdefault("tags", [])
            if g not in tags:
                tags.append(g)


def load_index() -> list[dict]:
    rows = load_json(DATA_DIR / "tools.json", []) or []
    apply_also(rows)
    return rows


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    # 反复剥前缀，否则「我要找个能压测的工具」只剥掉了「我要」
    prev = None
    while prev != s:
        prev = s
        s = STOP_PREFIX.sub("", s).strip(" 　,，、的")
    s = re.sub(r"\s+", " ", s).strip()
    # 尾部的通用名词同样要剥。「地图工具」整串对不上任何标签（标签叫「地图LBS」），
    # 于是 score 从 483 掉到 62，排序全交给巧合 —— 实测 ★0 的 NextGIS Web 和
    # ★0 的 EasyMap Desktop 双双排到 ★45,819 的 Leaflet 前面。
    # 剥完不足两个字就不剥（「工具」本身剥成空串没有意义）。
    m = STOP_SUFFIX.search(s)
    if m:
        cut = s[:m.start()].strip(" 　,，、的")
        if len(cut) >= 2:
            s = cut
    return s


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
        # 一格都没命中，再看是不是「美术设计」这种横跨几格的上位说法。
        # 和 compose 同一条顺序：精确的格子优先，展开只做兜底 ——
        # 用户说「美术设计」时不该只给矢量化工具，但说「矢量绘图」时也不该把
        # 六个格子的东西都倒出来。
        hit = resolve_concept(query)
        if hit:
            known = set(_tag_terms())
            return [x for x in hit[1] if x in known]
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


_AMBIGUOUS: set[str] | None = None


def _ambiguous_names() -> set[str]:
    """名字标识不了「是谁」的那些词，不该拿「点名」这一档最高证据。

    两个条件同时成立才算：
      1. 库里不止一条同名——prisma/orm 与 cycle/orm 都叫 orm，光看名字分不出
      2. 这个词在其他条目的描述里也频繁出现（出现在 ≥8 条 desc 里）

    只满足第 1 条不能降权。goose 有 3 个仓库（AI agent / 数据库迁移 / 压测），
    但 "goose" 在别人描述里出现 **0 次**——它是专名。用户打「goose 数据库迁移」
    就是在点名 pressly/goose，降权会把正确答案打掉（这是实测踩过的坑）。

    第 2 条才构成「通用技术词」的证据：专名不会在几十条别人的描述里被反复提及。
    orm 是 42 条，cli 是 105 条，goose 是 0 条。
    """
    global _AMBIGUOUS
    if _AMBIGUOUS is None:
        import re as _re
        from collections import Counter
        rows = load_index()
        own = Counter((t.get("name") or "").lower() for t in rows)
        cand = {k for k, v in own.items() if k and v >= 2}
        # 一次遍历统计「每个词出现在多少条 desc 里」，比按候选逐个正则扫全库快得多
        df: Counter = Counter()
        for t in rows:
            for w in set(_re.findall(r"[a-z0-9]{2,}", (t.get("desc") or "").lower())):
                df[w] += 1
        _AMBIGUOUS = {n for n in cand if df.get(n, 0) >= 8}
    return _AMBIGUOUS


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
    if n == ql and n not in _ambiguous_names():
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
        if tk == n and n not in _ambiguous_names():
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


# 兜底检索用的虚词。整句里这些字几乎不含信息，留着只会让「我要做一个」这种前缀
# 拖高覆盖率、把不相干的条目捞上来。
_STOP_CHARS = set(
    "我你他她它想要能可么吗呢吧啊的了是在有和与或就都也还太很把给对从到"
    "做个弄找用请帮写啥咋什哪这那些呀哈上下里中"
)
_EN_WORD = re.compile(r"[a-z0-9][a-z0-9+.#_-]*")


def _rescue_seq(query: str) -> list[str]:
    """句子里「剩下的内容」：英文按词，中文按单字，虚词丢掉，保持原顺序。"""
    ql = norm(query)
    seq: list[str] = []
    seen: set[str] = set()
    for m in _EN_WORD.finditer(ql):
        w = m.group(0)
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            seq.append(w)
    for ch in ql:
        if CJK.match(ch) and ch not in _STOP_CHARS and ch not in seen:
            seen.add(ch)
            seq.append(ch)
    return seq


def _rescue_bigrams(seq: list[str]) -> list[str]:
    """相邻两格拼成的二字词：「后台管理系统」→ 后台 / 管理 / 系统。"""
    return [seq[i] + seq[i + 1] for i in range(len(seq) - 1)]


def _tag_texts(rows: list[dict]) -> list[str]:
    """每条工具的可匹配文本。rescue 与 _infer_from_weights 共用同一份拼法，避免两处口径漂移。"""
    return [" ".join([
        (t.get("name") or "").lower(),
        " ".join(t.get("tags") or []).lower(),
        (t.get("category") or "").lower(),
        (t.get("desc") or "").lower(),
    ]) for t in rows]


def _infer_from_weights(wc: dict[str, float], tc: float, seq: list[str],
                        rows: list[dict]) -> str | None:
    """按「查询里的字覆盖了多少场景名」推断场景，输入是算好的字权重。

    口语不会说出场景名本身，但会说出组成它的字：「抓手机上的包」拆开是抓/手/机/包，
    没有「抓包」这个连词，而「抓包代理」这个名字同时含「抓」和「包」。

    阈值 0.4 是量出来的：该查询对「抓包代理」的加权覆盖率 0.48，第二名候选只有 0.33，
    中间有明显空档；再放宽，「我要做一个后台管理系统」会被判成「桌面与系统工具」（0.33）。
    0.1 的领先差是防止两个场景贴在一起时硬选一个。
    """
    if len([c for c in seq if CJK.match(c)]) < 2 or tc <= 0:
        return None
    labels: dict[str, set[str]] = {}
    for t in rows:
        for g in t.get("tags") or []:
            labels.setdefault(g, set(g))
    best = sorted(
        ((sum(wc[c] for c in seq if wc[c] and c in lc) / tc, lab)
         for lab, lc in labels.items()),
        reverse=True)
    if best and best[0][0] >= 0.4 and (len(best) == 1 or best[0][0] - best[1][0] >= 0.1):
        return best[0][1]
    return None


def rescue(query: str, rows: list[dict], limit: int = 12) -> list[dict]:
    """一条都没命中时的字面兜底，见模块开头第 3 级召回。

    两档证据，二字词优先于单字：
      - 二字词：用户原话里连着的两个词。命中它说明命中的是「词组」而不是散落的字，
        「后台管理系统」靠的是「后台」「管理」，跟一个恰好含「管」字的项目不是一回事。
      - 单字：只在整句几乎被覆盖（≥80%）时才认，用来兜拆不开的情况。

    每个词都按「越稀罕越值钱」加权（log(N/df)），所以查询里的「机」「包」这种常见字
    权重低，靠它们蹭不进结果。

    只看字面、不看语义，所以它给的是「最接近」而不是「对」——网页上会明写这句话。
    只在 0 命中时调用，不会改动任何已有查询的排序。
    """
    seq = _rescue_seq(query)
    if not seq:
        return []
    grams = _rescue_bigrams(seq)
    texts = _tag_texts(rows)
    n = len(texts)

    def _w(term: str) -> float:
        df = sum(1 for s in texts if term in s)
        return math.log((n + 1) / (df + 1)) if df else 0.0

    wg = {g: _w(g) for g in grams}
    wc = {c: _w(c) for c in seq}
    tg, tc = sum(wg.values()), sum(wc.values())
    if tg <= 0 and tc <= 0:
        return []

    # 标签层证据：命中的条目直接提到最前面。判据与 resolve_tag 共用
    # （见 _infer_from_weights）——两处各写一份的话，同一句话在两条路径上
    # 会得到不同答案，之前就是这样：find_tool 能出 mitmproxy，compose 报缺口。
    _lab = _infer_from_weights(wc, tc, seq, rows)
    inferred: set[str] = {_lab} if _lab else set()

    out: list[tuple[int, int, float, float, dict]] = []
    for t, s in zip(rows, texts):
        g_hit = [g for g in grams if wg[g] and g in s]
        c_hit = [c for c in seq if wc[c] and c in s]
        bg = sum(wg[g] for g in g_hit) / tg if tg else 0.0
        cg = sum(wc[c] for c in c_hit) / tc if tc else 0.0
        bias = 0 if (inferred & set(t.get("tags") or [])) else 1
        # 场景名推断命中的条目，字面上一个字都对不上也要留下——「抓包代理」里的工具
        # 描述是英文的，跟「我想抓手机上的包」一个汉字都不重合。
        if bias and not g_hit and cg < 0.8:
            continue
        # 清单/教材沉底：它们只是「提到过这个词」，不是做这件事的工具。
        out.append((bias, 2 if is_list(t) else 0, len(g_hit), bg, cg, t))
    out.sort(key=lambda x: (x[0], x[1], -x[2], -x[3], -x[4], -(x[5].get("stars") or 0)))
    return [t for *_, t in out[:limit]]



def tag_desc() -> dict[str, str]:
    """场景标签 → 一句话说明。给模型挑能力位用。"""
    return {t["label"]: (t.get("desc") or "")
            for t in load_config("scenarios.json").get("tags", [])}


def tag_count() -> int:
    """能力位个数。给提示语用——写死数字的那版，加第 69 个场景之后所有
    「68 个场景里没有」的报错就变成假话了。"""
    return len(load_config("scenarios.json").get("tags", []))


def slug_suffix(t: dict) -> str:
    """给同名条目补的「(owner/name)」，只在它真的能区分时返回。

    slug 是「同名条目」才标的，共 197 条。但其中 138 条的尾段和名字一模一样
    （redis 的 slug 是 redis/redis、psf/requests 的 slug 是 psf/requests），
    直接拼上去就是「redis redis/redis」——把名字写了两遍，反而更难读。
    真正有信息量的是尾段不同的那批：redis（redis-py）、ollama（ollama-python）。
    """
    s = (t.get("slug") or "").strip()
    if not s:
        return ""
    tail = s.split("/")[-1].strip().lower()
    name = (t.get("name") or "").strip().lower()
    if not tail or tail == name:
        return ""
    return f" ({s})"


def resolve_tag(name: str) -> str | None:
    """把模型给的说法解析成场景标签。

    模型不一定记得住每个标签的准确写法，会写「登录」「权限」「后台」这种。所以：
    先精确匹配标签，再走别名/中文关键词，最后退到双向子串。解析不出来就返回 None，
    让上层如实告诉模型「这个能力位索引里没有」，而不是硬套一个错的场景。
    """
    q = norm(name)
    if not q:
        return None
    labels = [t["label"] for t in load_config("scenarios.json").get("tags", [])]
    for lab in labels:
        if q == lab.lower():
            return lab
    # 别名整词相等优先：「部署」既是「部署托管」的别名、又出现在「容器与 K8s」的
    # 「部署容器」里，靠长度分不出来，靠相等就能。
    for lab, terms in _tag_terms().items():
        if q in terms:
            return lab
    best_ln, best = 0, None
    for lab, terms in _tag_terms().items():
        for t in terms:
            if not t:
                continue
            # q in t 必须卡长度比例，否则任何短词都会命中长别名：
            # 「网页」是「抓网页」的子串，于是「写个网页」被判成爬虫抓取。
            # 和 query_tags 用同一个 0.6 门槛，两条路径的宽严标准才一致。
            if t in q or (q in t and len(q) >= len(t) * 0.6):
                ln = min(len(t), len(q))
                if ln > best_ln:
                    best_ln, best = ln, lab
    if best:
        return best
    # 刻意**不**用 infer_tag 兜底。试过，退回来了。
    #
    # 加它的动机是「同一句话，find_tool 能出 mitmproxy，compose 却报缺口」——
    # 看着像不一致。但两条路径的职责本来就不同：
    #   find_tool 给「最接近的几条」并明写仅供参考，猜错了用户一眼能看出来；
    #   compose 拿场景去取整格候选，猜错了就是整格错，用户还以为那是对的。
    # 所以 compose 宁可报缺口，逼模型把「抓手机上的包」重新拆成「抓包代理」。
    #
    # 实测它错得多离谱（按字覆盖率挑场景）：
    #   做关卡   -> 网关与反向代理（「关」命中）
    #   做播客   -> 代理客户端（「客」命中）
    #   工单系统  -> 桌面与系统工具（「工」「系」「统」命中，覆盖 0.73）
    #   做推荐系统 -> 桌面与系统工具（「系」「统」命中）
    # 而正确的那一例「抓手机上的包」覆盖只有 0.48 —— 正例与反例的数值区间重叠，
    # 调阈值分不开。分辨这两个，靠的是「抓包代理」是不是个真实品类，不是字面。
    return None


_CONCEPTS: dict[str, list[str]] | None = None


def load_concepts() -> dict[str, list[str]]:
    global _CONCEPTS
    if _CONCEPTS is None:
        raw = load_config("concepts.json").get("concepts", {}) or {}
        _CONCEPTS = {str(k).strip(): [str(x) for x in v if str(x).strip()]
                     for k, v in raw.items()}
    return _CONCEPTS


def resolve_concept(name: str) -> tuple[str, list[str]] | None:
    """把上位说法（「美术设计」）展开成一组能力位，取最长匹配。

    存在的理由：scenarios.json 有一条硬纪律 —— 一个词只能归属一个格子，否则两格
    互相抢结果。但「美术设计」不属于任何单一格子，它是矢量化、像素画、图标素材、
    图像编辑、AI 出图、原型设计的合集。塞进哪一格都错，报「没有这一格」也不对
    （用户的原话：怎么会在这个美术设计上没有工具呢）。

    所以单开一层：格子的 alias 管「等于哪一格」，这里管「横跨哪几格」。
    上层先试 resolve_tag，失败才落到这里 —— 精确的格子永远优先于展开。
    """
    q = norm(name)
    if not q:
        return None
    best, best_ln = None, 0
    for k, labels in load_concepts().items():
        kn = norm(k)
        if not kn:
            continue
        if kn == q:
            return k, labels
        if (kn in q or q in kn) and min(len(kn), len(q)) > best_ln:
            best, best_ln = (k, labels), min(len(kn), len(q))
    # 至少要两个字，否则「图」这种单字会把整张表兜住
    return best if best_ln >= 2 else None


def compose(slots: list[str], per_slot: int = 2,
            exclude_lists: bool = True) -> list[dict]:
    """一次查多个能力位。给「做一件事，需要几样东西配合」的场景用。

    为什么不自己拆需求：拆解靠的是对需求的理解，那是模型该做的；这份索引能提供的是
    「每个能力位上目前有什么、有多强」。硬写一套「后台管理系统 → 4 个场景」的模板，
    换个需求就失效，而且模板本身也没有依据。所以这里只做一件事：模型点名要哪几个位，
    就一次把每个位的头几名取回来，顺带报出取不到的位（缺口）。

    返回每项：{slot(原话), label(解析到的场景), total, tools[]}，label 为 None 表示
    这个说法在现有能力位里没有对应场景。由上位概念展开来的项多带一个 via 字段
    （原话），上层据此标明「这几格是从一个说法展开的」，不假装用户逐格点过名。
    """
    out: list[dict] = []
    used: set[str] = set()
    known = {t["label"] for t in load_config("scenarios.json").get("tags", [])}
    for raw in slots:
        label = resolve_tag(raw)
        if not label:
            # 格子解析不出来，再看是不是「美术设计」这种横跨几格的上位说法。
            # 顺序不能反：精确的格子永远优先，展开只是兜底。
            hit = resolve_concept(raw)
            if hit:
                cname, labels = hit
                # concepts.json 里写错一个字就静默少一格，所以这里按现有标签过滤一次，
                # 漏掉的由 doctor 的「概念表」那一项报出来。
                labels = [x for x in labels if x in known][:6]
                for lab in labels:
                    total = sum(1 for t in load_index() if lab in (t.get("tags") or []))
                    # 展开的格子每格只取一条：用户没点名要哪一格，给多了就是替他做决定。
                    got = search(raw, tag=lab, limit=per_slot + 4)
                    cand = [t for t in got if not (exclude_lists and is_list(t))]
                    cand = [t for t in cand if (t.get("url") or t.get("name")) not in used]
                    picked = cand[:1]
                    for t in picked:
                        used.add(t.get("url") or t.get("name"))
                    out.append({"slot": raw, "label": lab, "total": total,
                                "tools": picked, "via": cname})
                if labels:
                    continue
            out.append({"slot": raw, "label": None, "total": 0, "tools": []})
            continue
        total = sum(1 for t in load_index() if label in (t.get("tags") or []))
        # 每格用 search() 自己排，而不是在这里另写一套排序。
        # 曾经按 star 排：图像处理 148 条里能去背景的 rembg 排第 8，每格取 2 时永远
        # 取不到它，抠图那格给的是 OCR 工具。也曾按 score(t, 原话) 直排：数据库那格
        # 第一名叫作 sqlmap（注入扫描器，描述里全是「数据库」）。两种都是「只用了一个
        # 信号」。search() 本来就是「相关性 + 场景 + 热度 + 清单沉底」的合成判定，
        # 直接用它，网页、命令行、组合三处口径也就一致了。
        got = search(raw, tag=label, limit=max(per_slot * 3, per_slot + len(used) + 6))
        cand = [t for t in got if not (exclude_lists and is_list(t))]
        # 跨能力位去重：一个脚手架同时挂着「低代码」和「认证鉴权」，两格都列它
        # 只会让人以为要装两遍。已经在前面出现过的，这一格往下顺延。
        cand = [t for t in cand if (t.get("url") or t.get("name")) not in used]
        picked = cand[:max(1, per_slot)]
        for t in picked:
            used.add(t.get("url") or t.get("name"))
        out.append({"slot": raw, "label": label, "total": total, "tools": picked})
    return out


def search(query: str = "", *, tag: str = "", src: str = "", zh: bool = False,
           limit: int = 12, meta: dict | None = None) -> list[dict]:
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
    if scored:
        return [t for _, t in scored[:limit]]
    # 兜底结果不是「答案」，是不对症时按字面凑出来的。把这件事写进 meta，
    # 让 CLI 和 MCP 有能力如实标注 —— 原来这里什么都只说「共 N 条」，
    # 查「做一个桌面宠物」返回几个不相干项目，读起来和正确答案一个语气。
    got = rescue(query, rows, limit)
    if meta is not None:
        meta["rescued"] = bool(got)
        meta["rescue_query"] = query
    return got


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
