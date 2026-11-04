"""生成单文件检索页。

关键决策：数据直接内联进 HTML，而不是 fetch 外部 JSON。
因为双击打开是 file:// 协议，浏览器会用 CORS 挡掉本地 fetch，
内联是唯一能做到「无服务器、双击即用、离线可用」的方式。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime

from common import DATA_DIR, SITE_DIR, load_config, load_json, log

# 数据字段压缩，减小内联体积
SHORT = {"name": "n", "url": "u", "desc": "d", "origin": "s", "tags": "g",
         "lang": "l", "stars": "st", "pushed_at": "p", "category": "c",
         "quality": "q", "alive": "a", "zh": "z", "wrap": "w", "slug": "o"}

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DevToolRadar · 开发工具雷达</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f7f8fa; --panel:#fff; --ink:#111827; --muted:#6b7280; --line:#e5e7eb;
  --accent:#2563eb; --accent-soft:#eff6ff; --warn:#b45309; --ok:#059669;
  --shadow:0 1px 2px rgba(16,24,40,.06),0 1px 3px rgba(16,24,40,.1);
}
@media (prefers-color-scheme:dark){
  :root{--bg:#0f1115;--panel:#171a21;--ink:#e5e7eb;--muted:#9ca3af;--line:#272b35;
        --accent:#60a5fa;--accent-soft:#1e293b;--warn:#fbbf24;--ok:#34d399;
        --shadow:0 1px 2px rgba(0,0,0,.4)}
}
body{background:var(--bg);color:var(--ink);font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
     -webkit-font-smoothing:antialiased;padding:0 20px 80px}
.wrap{max-width:1080px;margin:0 auto}
header{padding:32px 0 18px}
.hd{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h1{font-size:21px;font-weight:700;letter-spacing:-.2px}
.hd .sub{color:var(--muted);font-size:13px}
.stats{color:var(--muted);font-size:12.5px;margin-top:6px}
.stats b{color:var(--ink);font-weight:600}
.search{position:relative;margin:16px 0 14px}
.search input{width:100%;padding:14px 16px 14px 44px;font-size:16px;color:var(--ink);
  background:var(--panel);border:1px solid var(--line);border-radius:11px;outline:none;
  box-shadow:var(--shadow);transition:border-color .15s,box-shadow .15s}
.search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.search .ico{position:absolute;left:15px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:15px}
.search kbd{position:absolute;right:12px;top:50%;transform:translateY(-50%);font:11px/1 ui-monospace,monospace;
  color:var(--muted);background:var(--bg);border:1px solid var(--line);border-radius:5px;padding:4px 6px}
.chips{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}
.chip{font-size:12.5px;padding:5px 11px;border-radius:999px;background:var(--panel);
  border:1px solid var(--line);color:var(--muted);cursor:pointer;user-select:none;transition:.12s;white-space:nowrap}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.chip .cnt{opacity:.6;margin-left:3px;font-size:11px}
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;padding:9px 0 14px;
  border-bottom:1px solid var(--line);margin-bottom:16px;font-size:13px;color:var(--muted)}
.bar select{font-size:13px;padding:5px 8px;border-radius:7px;border:1px solid var(--line);
  background:var(--panel);color:var(--ink);outline:none}
.bar label{display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none}
.bar .reset{margin-left:auto;cursor:pointer;color:var(--accent);font-size:12.5px;display:none}
.bar .reset.show{display:block}
.card{background:var(--panel);border:1px solid var(--line);border-radius:11px;padding:13px 16px;
  margin-bottom:9px;box-shadow:var(--shadow);transition:.13s;cursor:pointer;position:relative}
.card:hover{border-color:var(--accent);transform:translateY(-1px)}
.card.sel{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.t1{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.nm{font-weight:600;font-size:15.5px;letter-spacing:-.1px}
.ow{font-size:11px;color:var(--muted);font-family:ui-monospace,monospace;
    background:var(--bg);border:1px solid var(--line);border-radius:4px;
    padding:1px 5px;letter-spacing:0}
.sr{font-size:11px;padding:1.5px 7px;border-radius:5px;font-weight:500;letter-spacing:.2px}
.sr.github{background:#eef2ff;color:#4338ca}
.sr.awesome{background:#f0fdf4;color:#166534}
.sr.cn{background:#fef2f2;color:#b91c1c}
@media (prefers-color-scheme:dark){
  .sr.github{background:#1e1b4b;color:#a5b4fc}
  .sr.awesome{background:#052e16;color:#86efac}
  .sr.cn{background:#3f1212;color:#fca5a5}
}
.badge{font-size:11px;color:var(--muted);display:inline-flex;align-items:center;gap:3px}
.star{color:#d97706;font-weight:600}
.ds{color:var(--muted);font-size:13.2px;margin-top:4px;display:-webkit-box;-webkit-line-clamp:2;
  -webkit-box-orient:vertical;overflow:hidden}
.tg{display:flex;gap:5px;flex-wrap:wrap;margin-top:8px}
.tag{font-size:11px;padding:1.5px 7px;border-radius:5px;background:var(--accent-soft);
  color:var(--accent);cursor:pointer}
.tag:hover{text-decoration:underline}
.dead{color:var(--warn);font-size:11px}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty .big{font-size:15px;margin-bottom:8px;color:var(--ink)}
.empty .hint{font-size:13px;max-width:520px;margin:0 auto;line-height:1.9}
.empty .hint span{color:var(--accent);cursor:pointer;border-bottom:1px dashed currentColor}
.more{text-align:center;padding:22px;color:var(--muted);font-size:13px}
footer{margin-top:40px;padding-top:16px;border-top:1px solid var(--line);
  color:var(--muted);font-size:12px;line-height:1.9}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);
  border:1px solid var(--line);padding:1px 5px;border-radius:4px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="hd"><h1>DevToolRadar</h1><span class="sub">开发工具雷达</span></div>
    <div class="stats">__STATS__</div>
    <div class="search">
      <span class="ico">⌕</span>
      <input id="q" placeholder="我要做什么？试试「mock 接口」「造测试数据」「解析 PDF」「内网穿透」" autocomplete="off" spellcheck="false">
      <kbd>/</kbd>
    </div>
    <div class="chips" id="chips"></div>
    <div class="bar">
      <label>来源
        <select id="src">
          <option value="">全部</option>
          <option value="github">GitHub</option>
          <option value="awesome">精选清单</option>
          <option value="cn">国内工具</option>
        </select>
      </label>
      <label><input type="checkbox" id="zh"> 仅中文</label>
      <label><input type="checkbox" id="hot"> 近期活跃</label>
      <select id="sort">
        <option value="q">按推荐度</option>
        <option value="st">按 Star</option>
        <option value="p">按更新时间</option>
        <option value="n">按名称</option>
      </select>
      <span id="cnt"></span>
      <span class="reset" id="reset">清空筛选</span>
    </div>
  </header>
  <div id="results"></div>
  <footer>
    数据自动刷新 · <code>python src/update.py</code> 手动更新<br>
    快捷键：<code>/</code> 搜索 · <code>j</code>/<code>k</code> 上下 · <code>Enter</code> 打开 · <code>Esc</code> 清空
  </footer>
</div>
<script>
const D = __DATA__;
const TAGS = __TAGS__;
const TAGMAP = __TAGMAP__;
// 上位说法 → 一组能力位（「美术设计」= AI 绘图 + 像素画 + 矢量化 + 图标 + 修图 + 原型）。
// 与 Python 端 resolve_concept 同一张表、同一条顺序：格子优先，展开只做兜底。
const CONCEPTS = __CONCEPTS__;
const TERMS = __TERMS__;
const q = document.getElementById('q');
const res = document.getElementById('results');
const cntEl = document.getElementById('cnt');
const srcEl = document.getElementById('src');
const zhEl = document.getElementById('zh');
const hotEl = document.getElementById('hot');
const sortEl = document.getElementById('sort');
const chipsEl = document.getElementById('chips');
const resetEl = document.getElementById('reset');

let activeTag = '', selIdx = -1, view = [], PAGE = 60, shown = PAGE, chipsAll = false, rescued = false;

// 去掉口语化停用词，让「我要找个能 mock 接口的」也能命中
const STOP = /^(我要|我想|我想找|找一?个|找个|有没有|什么|怎么|如何|帮我|可以|能|用来|用于|一个|的|了|吗|呢|啊|工具|软件|库|平台|方案|推荐)+/g;

function norm(s){
  s = (s||'').toLowerCase().trim();
  // 反复剥，否则「我要找个能压测的」只剥掉「我要」
  let prev = null;
  while(prev !== s){ prev = s; s = s.replace(STOP,'').trim(); }
  return s.replace(/\s+/g,' ').trim();
}

// 口语 → 场景标签。只取最长匹配，避免「本地大模型」同时召回「模型服务」和「大模型应用」
function queryTags(qn){
  if(!qn) return [];
  const hits = [];
  for(const label in TAGMAP){
    let best = 0;
    for(const t of TAGMAP[label]){
      if(!t) continue;
      if(qn.includes(t)) best = Math.max(best, t.length);
      else if(qn.length>=2 && t.length>=2 && t.includes(qn) && qn.length>=t.length*0.6) best = Math.max(best, qn.length);
    }
    if(best) hits.push([best, label]);
  }
  if(!hits.length){
    // 一格都没命中，再看是不是「美术设计」这种横跨几格的上位说法。
    let bestK = '', bestLn = 0;
    for(const k in CONCEPTS){
      const kn = norm(k);
      if(!kn) continue;
      if(kn === qn) return CONCEPTS[k].slice();
      if((qn.includes(kn) || kn.includes(qn)) && Math.min(kn.length, qn.length) > bestLn){
        bestK = k; bestLn = Math.min(kn.length, qn.length);
      }
    }
    return bestLn >= 2 ? CONCEPTS[bestK].slice() : [];
  }
  const mx = Math.max(...hits.map(h=>h[0]));
  return hits.filter(h=>h[0]>=mx).map(h=>h[1]);
}

// 中文概念 → 英文词。返回 [ [中文概念, [英文词...]] ]，
// 分组是为了让同一个概念只计一次分，否则描述里堆同义词的工具会虚高。
function expandEn(qn){
  // 只取最长匹配，和 Python 端一致：查「数据库客户端」时如果同时展开
  // 「数据库」→ database/sql，sqlmap（SQL 注入工具）会靠名字里的 sql 挤进前三。
  let best = 0;
  for(const zh in TERMS){ if(zh && qn.includes(zh) && zh.length > best) best = zh.length; }
  const out = [];
  if(!best) return out;
  for(const zh in TERMS){
    if(zh && qn.includes(zh) && zh.length === best) out.push([zh, TERMS[zh]]);
  }
  return out;
}

// 2 字符缩写必须按词边界判：查「查我的 IP」时 ip 会命中 VoIP、Recipes（rec-ip-es），
// 结果是一个 SIP3 监控工具和一个菜谱项目排第一。3 字符以上仍用子串——
// log 要命中 spdlog、k8s 要命中 k8sgpt、json 要命中 jsonhero，
// 全上词边界会让「日志」的第一名变成 swift-log。与 Python 端 _hit() 保持一致。
// 用 (^|[^a-z0-9]) 而不是 lookbehind：Safari 16.4 以前不支持后者。
function hit(tk, text){
  if(!tk || !text) return false;
  if(/[\u4e00-\u9fff]/.test(tk)) return text.includes(tk);
  if(tk.length <= 2 && /[a-z0-9]/.test(tk)){
    const esc = tk.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    return new RegExp('(^|[^a-z0-9])' + esc + '([^a-z0-9]|$)', 'i').test(text);
  }
  return text.includes(tk);
}

// 严格词边界。用于「中文概念翻译出来的英文词」在描述里匹配：
// diff 不该命中 different、log 不该命中 catalog，否则整库一半条目都沾分，
// 最后按 star 一排，第一名成了体量最大的不相干项目。与 Python 端 _hit_strict 一致。
function hitStrict(tk, text){
  if(!tk || !text) return false;
  if(/[\u4e00-\u9fff]/.test(tk)) return text.includes(tk);
  const esc = tk.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp('(^|[^a-z0-9])' + esc + '([^a-z0-9]|$)', 'i').test(text);
}

// ---- 兜底检索：只有在主判定一条都没命中时才走 ----
// 用户在搜索框里写的是句子（「我要做一个后台管理系统」），而 score() 只认词表里的
// 概念。整句一个概念都碰不上时给一片空白，比给几条沾边的更糟。
// 与 src/search.py 的 rescue() 同一套规则，两边必须一致，否则网页和命令行两种结果。
const STOP_CHARS = new Set('我你他她它想要能可么吗呢吧啊的了是在有和与或就都也还太很把给对从到做个弄找用请帮写啥咋什哪这那些呀哈上下里中'.split(''));
function rescueSeq(raw){
  const ql = norm(raw), seq = [], seen = new Set();
  (ql.match(/[a-z0-9][a-z0-9+.#_-]*/g) || []).forEach(w=>{
    if(w.length >= 2 && !seen.has(w)){ seen.add(w); seq.push(w); }
  });
  for(const ch of ql){
    if(/[\u4e00-\u9fff]/.test(ch) && !STOP_CHARS.has(ch) && !seen.has(ch)){ seen.add(ch); seq.push(ch); }
  }
  return seq;
}
function rescue(raw, rows, limit){
  const seq = rescueSeq(raw);
  if(!seq.length) return [];
  const grams = [];
  for(let i=0;i+1<seq.length;i++) grams.push(seq[i]+seq[i+1]);
  const texts = rows.map(t=>((t.n||'')+' '+(t.g||[]).join(' ')+' '+(t.c||'')+' '+(t.d||'')).toLowerCase());
  const N = texts.length, memo = {};
  const wOf = tk=>{
    if(memo[tk] !== undefined) return memo[tk];
    let df = 0;
    for(const s of texts) if(s.includes(tk)) df++;
    return memo[tk] = df ? Math.log((N+1)/(df+1)) : 0;
  };
  const wg = grams.map(wOf), wc = seq.map(wOf);
  const tg = wg.reduce((a,b)=>a+b,0), tc = wc.reduce((a,b)=>a+b,0);
  if(tg <= 0 && tc <= 0) return [];
  // 标签层证据：查询里的字和场景名比。「我想抓手机上的包」拆开是 抓/手/机/包，
  // 没有「抓包」这个连词，但场景名「抓包代理」同时含「抓」和「包」。这类命中
  // 比逐字匹配可信，命中的条目直接提到最前面。阈值 0.4 与 Python 端同一套实测值。
  const labels = {};
  rows.forEach(t=>(t.g||[]).forEach(g=>{ if(!labels[g]) labels[g] = new Set(g); }));
  let inferred = '';
  if(seq.filter(c=>/[\u4e00-\u9fff]/.test(c)).length >= 2){
    const rated = Object.keys(labels).map(lab=>{
      let got = 0;
      seq.forEach((c,j)=>{ if(wc[j] && labels[lab].has(c)) got += wc[j]; });
      return [tc ? got/tc : 0, lab];
    }).sort((a,b)=> b[0]-a[0] || (a[1]<b[1]?-1:1));
    if(rated.length && rated[0][0] >= 0.4 && (rated.length === 1 || rated[0][0] - rated[1][0] >= 0.1)){
      inferred = rated[0][1];
    }
  }
  const out = [];
  rows.forEach((t,i)=>{
    let gh = 0, bg = 0, cg = 0;
    grams.forEach((g,j)=>{ if(wg[j] && texts[i].includes(g)){ gh++; bg += wg[j]; } });
    seq.forEach((c,j)=>{ if(wc[j] && texts[i].includes(c)) cg += wc[j]; });
    bg = tg ? bg/tg : 0;
    cg = tc ? cg/tc : 0;
    const bias = inferred && (t.g||[]).includes(inferred) ? 0 : 1;
    if(bias && !gh && cg < 0.8) return;
    out.push([bias, isRef(t)?2:0, -gh, -bg, -cg, -(t.st||0), t]);
  });
  out.sort((a,b)=> a[0]-b[0] || a[1]-b[1] || a[2]-b[2] || a[3]-b[3] || a[4]-b[4] || a[5]-b[5]);
  return out.slice(0, limit).map(x=>x[6]);
}

function score(t, raw){
  if(!raw) return 0;
  const ql = norm(raw);
  if(!ql) return 0;
  const n = (t.n||'').toLowerCase();
  const d = (t.d||'').toLowerCase();
  const g = (t.g||[]).join(' ').toLowerCase();
  const c = (t.c||'').toLowerCase();
  // 描述命中单独累计、最后封顶（与 Python 端一致）。
  // 不封顶会被关键词堆砌刷分：一个工具的说明把「内网穿透 反向代理 端口转发
  // 内网计算机」全写一遍，每个概念各加一次，硬生生把 110k star 的项目挤下去。
  const DESC_CAP = 70;
  let s = 0, ds = 0;
  const isCjk = /[\u4e00-\u9fff]/.test(ql);
  if(n === ql) s += 900;
  // 中文查询里，「名字含这个词」和「标签命中」是同一件事（都在说「这条属于这个
  // 品类」），不叠加。叠加的后果：★3 的「草灰笔记」靠名字拿 180、靠标签再拿 305，
  // 搜「笔记」排第一。与 Python 端 score() 同一条规则。
  const nameHit = hit(ql,n);
  if(hit(ql,n) && !(isCjk && hit(ql,g))) s += isCjk ? 180 : 260;
  if(hit(ql,g)) s += 210;
  if(hit(ql,c)) s += 90;
  // 中文整串命中描述不给满：中文描述里「对比」「限流」这种词太容易蹭到，
  // 而英文正牌工具只能靠 terms 翻译拿六成分。与 Python 端一致。
  if(hit(ql,d)) ds += isCjk ? 0 : 70;
  const toks = ql.split(/[\s,，、]+/).filter(x=>x.length>0);
  for(const tk of toks){
    if(tk.length<1) continue;
    // 用户「点名 + 说任务」时（goose 数据库迁移），名字精确等于某个词按近似精确命中算
    if(tk === n) s += 300;
    else if(hit(tk,n) && !(isCjk && nameHit)) s += 110;
    if(hit(tk,g)) s += 95;
    if(hit(tk,c)) s += 40;
    if(hit(tk,d)) ds += 26;
  }
  // 中文概念 → 英文词。同一概念只计一次最高分；
  // 多词短语用词袋，否则 "remove background" 匹配不到 "remove images background"
  // 中文查询展开出的英文词命中名字，只算描述分 —— 用户打「笔记」不会是想点 vnote，
  // 名字带 note 的恰恰都是小工具，不带 note 的 joplin/logseq/AppFlowy 才是正主。
  // 与 Python 端 score() 同一条规则，两边必须一致，否则网页和命令行给两种排序。
  for(const pair of expandEn(ql)){
    let best = 0, bestD = 0;
    for(const en of pair[1]){
      const ws = en.split(' ');
      // 名字是标识符，复合词正常（ImgCompress、jsonhero），用子串；
      // 描述是自然语言，用词边界，否则 diff 会命中 different。
      const hn = ws.every(w=>hit(w,n));
      const hd = ws.every(w=>hitStrict(w,d));
      if(hn) best = Math.max(best, isCjk ? 62 : 90);
      else if(hd) bestD = Math.max(bestD, 62);
    }
    s += best;
    ds += bestD;
  }
  // 影响力微调，最多 120 分：只用来打破「信息重复计数」造成的错位（0 星的小工具站
  // 靠名字里带查询词压过 40k 星的正牌），不足以让高星盖过真正对口的工具。
  // 只给名字或标签命中的条目，否则纯靠描述词袋蹭到分的会连 star 一起蹭。
  // 这段 Python 端早就有，网页端一直是缺的 —— 缺的后果是网页上 ★3 的工具能排第一。
  let starBonus = 0;
  if(nameHit || hit(ql,g)) starBonus = Math.min(120, Math.floor(25 * Math.log10(((t.st||0)) + 1)));
  return s + Math.min(ds, DESC_CAP) + starBonus;
}

// 参考读物（清单仓库、书、课程）不是工具，排序时整体沉底。
// 用户问「容器」要的是 podman，不是一本 26k star 的 Docker 教材。
const REF_RE = /^\s*(?:awesome[-_ ]|the\s+awesome|curated\s+list|list\s+of\b)|(?:a\s+)?curated\s+list\s+of|list\s+of\s+\w+\s+(?:for|to)\b|^\s*(?:the\s+)?(?:book|course|guide|handbook|cookbook|tutorial)\b|\b(?:e-?book|cheat\s?sheet|interview\s+questions?|roadmap|study\s+notes?|learning\s+notes?|awesome\s+list)\b|\budemy\b|\bcoursera\b|\bpluralsight\b|\bvideo\s+course\b|\blearn\s+and\s+understand\b|从真实案例中学习|电子书|教程|课程|面试题|学习笔记|读书笔记|路线图/i;

// 只在名字上判的读物特征（教材标题句式），避免误伤描述里正常提到的情况
const NAME_REF_RE = /best[\s_-]?practices?|\d+\s*[-_ ]?days?[\s_-]+of[\s_-]|\bfrom\s+scratch\b|\bfor\s+beginners\b|入门|从零开始/i;

function isRef(t){
  const nm = t.n||'';
  return REF_RE.test(nm) || REF_RE.test((t.d||'').slice(0,80)) || NAME_REF_RE.test(nm);
}

function fmtStars(n){
  if(!n) return '';
  return n >= 1000 ? (n/1000).toFixed(n>=10000?0:1)+'k' : String(n);
}

function ago(p){
  if(!p) return '';
  const d = Math.floor((Date.now() - new Date(p+'T00:00:00').getTime())/86400000);
  if(isNaN(d)) return '';
  if(d<0) return '';
  if(d<30) return d+' 天前';
  if(d<365) return Math.floor(d/30)+' 个月前';
  return Math.floor(d/365)+' 年前';
}

function recent(p){
  if(!p) return false;
  const d = (Date.now() - new Date(p+'T00:00:00').getTime())/86400000;
  return d <= 365;
}

function renderChips(){
  chipsEl.innerHTML = '';
  const all = document.createElement('div');
  all.className = 'chip' + (activeTag===''?' on':'');
  all.textContent = '全部';
  all.onclick = ()=>{ activeTag=''; renderChips(); apply(); };
  chipsEl.appendChild(all);
  (chipsAll ? TAGS : TAGS.slice(0,22)).forEach(([name,n])=>{
    const c = document.createElement('div');
    c.className = 'chip' + (activeTag===name?' on':'');
    c.innerHTML = name.replace(/&/g,'&amp;') + '<span class="cnt">'+n+'</span>';
    c.onclick = ()=>{ activeTag = (activeTag===name?'':name); renderChips(); apply(); };
    chipsEl.appendChild(c);
  });
  if(TAGS.length > 22){
    const more = document.createElement('div');
    more.className = 'chip';
    more.textContent = chipsAll ? '收起' : '更多 ' + (TAGS.length - 22);
    more.onclick = ()=>{ chipsAll = !chipsAll; renderChips(); };
    chipsEl.appendChild(more);
  }
}

function apply(){
  const raw = q.value;
  rescued = false;
  const src = srcEl.value;
  const zh = zhEl.checked;
  const hot = hotEl.checked;
  let rows = D;

  if(activeTag) rows = rows.filter(t => (t.g||[]).includes(activeTag));
  if(src) rows = rows.filter(t => t.s === src);
  if(zh) rows = rows.filter(t => t.z === 1);
  if(hot) rows = rows.filter(t => recent(t.p));

  if(raw.trim()){
    const qn = norm(raw);
    const want = new Set(queryTags(qn));
    // 查询词正好等于某个场景标签（「接口 Mock」「内网穿透」）时，这个标签下的
    // 条目全都相关，按影响力排；名字里碰巧含关键词不再重复加分。
    const exact = new Set([...want].filter(x=>norm(x)===qn));
    const scored = [];
    for(const t of rows){
      let sc = score(t, raw);
      // 场景命中给足权重，否则「造测试数据」这种说法永远打不中
      if(want.size && (t.g||[]).some(g=>want.has(g))) sc += 180;
      if(sc > 0) scored.push([sc, t]);
    }
    // 先按「是不是读物 / 是不是同名封装件」分层，再按 100 分一档，同档按 star。
    scored.sort((a,b)=>{
      const ta = (isRef(a[1])?2:0) + (a[1].w?1:0);
      const tb = (isRef(b[1])?2:0) + (b[1].w?1:0);
      if(ta !== tb) return ta - tb;
      const ea = exact.size && (a[1].g||[]).some(g=>exact.has(g)) ? 0 : 1;
      const eb = exact.size && (b[1].g||[]).some(g=>exact.has(g)) ? 0 : 1;
      if(ea !== eb) return ea - eb;
      const ba = ea===0 ? 0 : Math.floor(a[0]/100);
      const bb = eb===0 ? 0 : Math.floor(b[0]/100);
      return bb - ba || (b[1].st||0)-(a[1].st||0) || (b[1].q||0)-(a[1].q||0);
    });
    view = scored.map(x=>x[1]);
    if(!view.length){
      const got = rescue(raw, rows, 300);
      if(got.length){ view = got; rescued = true; }
    }
  } else {
    view = rows.slice();
    const s = sortEl.value;
    if(s==='q') view.sort((a,b)=>(b.q||0)-(a.q||0));
    else if(s==='st') view.sort((a,b)=>(b.st||0)-(a.st||0));
    else if(s==='p') view.sort((a,b)=>String(b.p||'').localeCompare(String(a.p||'')));
    else if(s==='n') view.sort((a,b)=>String(a.n||'').localeCompare(String(b.n||''),'zh'));
  }

  shown = PAGE;
  selIdx = -1;
  cntEl.textContent = view.length + ' 个结果';
  resetEl.classList.toggle('show', !!(raw.trim()||activeTag||src||zh||hot));
  draw();
}

function draw(){
  if(!view.length){
    const qv = (q.value||'').trim();
    res.innerHTML = '<div class="empty"><div class="big">没有匹配的工具</div>' +
      '<div class="hint">' + (qv
        ? '换个说法试试。DevToolRadar 按「你想干什么」归类，不是按技术栈。<br>比如把「造数据」换成 <span data-s="接口 Mock">接口 Mock</span>，把「穿透」换成 <span data-s="内网穿透">内网穿透</span>。'
        : '当前筛选条件下没有结果，试试清空筛选。') + '</div></div>';
    res.querySelectorAll('[data-s]').forEach(el=>{
      el.onclick = ()=>{ q.value=''; activeTag = el.dataset.s; renderChips(); apply(); };
    });
    return;
  }

  const frag = document.createDocumentFragment();
  if(rescued){
    const note = document.createElement('div');
    note.className = 'more';
    note.textContent = '没有完全匹配的，下面按字面最接近的排。换个说法更准，比如只说关键词：抓包 / mock 接口';
    frag.appendChild(note);
  }
  const limit = Math.min(shown, view.length);
  for(let i=0;i<limit;i++){
    const t = view[i];
    const el = document.createElement('div');
    el.className = 'card' + (i===selIdx?' sel':'');
    el.dataset.i = i;
    const srcName = t.s==='github'?'GitHub':(t.s==='cn'?'国内':'精选');
    const bits = [];
    bits.push('<span class="sr '+(t.s||'')+'">'+srcName+'</span>');
    if(t.st) bits.push('<span class="badge star">★ '+fmtStars(t.st)+'</span>');
    if(t.l) bits.push('<span class="badge">'+t.l+'</span>');
    if(t.p) bits.push('<span class="badge">更新 '+ago(t.p)+'</span>');
    if(t.a === false) bits.push('<span class="dead">站点不可达</span>');
    const tags = (t.g||[]).map(g=>'<span class="tag" data-tag="'+g.replace(/"/g,'&quot;')+'">'+g+'</span>').join('');
    // 同名条目（bun / tabby / goose）带上 slug，否则两行长得一模一样分不清点哪个
    const slugBit = t.o ? '<span class="ow">'+(t.o)+'</span>' : '';
    el.innerHTML = '<div class="t1"><span class="nm">'+(t.n||'')+'</span>'+slugBit+bits.join('')+'</div>' +
      (t.d?'<div class="ds">'+t.d.replace(/</g,'&lt;')+'</div>':'') +
      (tags?'<div class="tg">'+tags+'</div>':'');
    el.onclick = (e)=>{
      if(e.target.dataset.tag){ e.stopPropagation(); activeTag = e.target.dataset.tag; renderChips(); apply(); return; }
      window.open(t.u,'_blank','noopener');
    };
    frag.appendChild(el);
  }
  res.innerHTML = '';
  res.appendChild(frag);
  if(view.length > shown){
    const m = document.createElement('div');
    m.className = 'more';
    m.textContent = '还有 ' + (view.length - shown) + ' 个，继续滚动加载';
    res.appendChild(m);
  }
}

let tmr = null;
q.addEventListener('input', ()=>{ clearTimeout(tmr); tmr = setTimeout(apply, 110); });
[srcEl, sortEl].forEach(e=>e.addEventListener('change', apply));
[zhEl, hotEl].forEach(e=>e.addEventListener('change', apply));
resetEl.onclick = ()=>{ q.value=''; activeTag=''; srcEl.value=''; zhEl.checked=false; hotEl.checked=false; renderChips(); apply(); q.focus(); };

document.addEventListener('keydown', e=>{
  if(e.key === '/' && document.activeElement !== q){ e.preventDefault(); q.focus(); q.select(); return; }
  if(e.key === 'Escape'){ q.value=''; activeTag=''; renderChips(); apply(); q.blur(); return; }
  if(document.activeElement === q && e.key !== 'Enter') return;
  if(e.key === 'j' || e.key === 'ArrowDown'){ e.preventDefault(); selIdx = Math.min(selIdx+1, Math.min(shown,view.length)-1); draw(); scrollSel(); }
  else if(e.key === 'k' || e.key === 'ArrowUp'){ e.preventDefault(); selIdx = Math.max(selIdx-1, 0); draw(); scrollSel(); }
  else if(e.key === 'Enter' && selIdx >= 0 && view[selIdx]){ window.open(view[selIdx].u,'_blank','noopener'); }
});

function scrollSel(){
  const el = res.querySelector('.card.sel');
  if(el) el.scrollIntoView({block:'nearest'});
}

window.addEventListener('scroll', ()=>{
  if(shown >= view.length) return;
  if(window.innerHeight + window.scrollY > document.body.offsetHeight - 500){
    shown += PAGE; draw();
  }
});

renderChips();
// 支持 ?q=抓包&tag=代理客户端 直接打开某个查询，链接可以存书签、发给人。
// 只看 query 不看 hash —— hash 已经被 #focus 占了。
if(location.search){
  const p = new URLSearchParams(location.search);
  if(p.get('q')) q.value = p.get('q');
  if(p.get('tag')) activeTag = p.get('tag');
  if(p.get('src')) srcEl.value = p.get('src');
  if(p.get('q') || p.get('tag')) renderChips();
}
apply();
if(location.hash === '#focus') q.focus();
</script>
</body>
</html>
"""


def build(tools: list[dict] | None = None) -> str:
    tools = tools or load_json(DATA_DIR / "tools.json", [])
    if not tools:
        raise SystemExit("data/tools.json 为空，先跑一次 update.py")

    packed = []
    for t in tools:
        row = {}
        for k, short in SHORT.items():
            v = t.get(k)
            if v in (None, "", 0, [], False):
                continue
            if k == "url":
                v = str(v).replace("https://", "").replace("http://", "")
            row[short] = v
        packed.append(row)

    # 标签热度，供顶部快捷入口用。全量下发、页面上先露 22 个、剩下的点「更多」展开：
    # 只下发 24 个热门标签时，「爬虫抓取」「缓存」「消息队列」这些场景在网页上根本
    # 没有入口，用户只能靠搜索框撞词。
    counter = Counter()
    for t in tools:
        for g in t.get("tags") or []:
            counter[g] += 1
    tags = [[k, v] for k, v in counter.most_common()]

    by_src = Counter(t.get("origin") for t in tools)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    stats = (f"共 <b>{len(tools)}</b> 个工具 · "
             f"GitHub <b>{by_src.get('github', 0)}</b> · "
             f"精选清单 <b>{by_src.get('awesome', 0)}</b> · "
             f"国内 <b>{by_src.get('cn', 0)}</b> · "
             f"最后刷新 {stamp}")

    # 口语 → 场景标签映射表。网页必须和命令行用同一套，否则同一个查询
    # 在网页和终端会给出不同答案。只收中文词：英文词太泛，容易乱命中。
    cjk = re.compile(r"[\u4e00-\u9fff]")
    tagmap: dict[str, list[str]] = {}
    for rule in load_config("scenarios.json").get("tags", []):
        terms = [a for a in (rule.get("alias") or []) if a]
        terms += [k for k in (rule.get("kw") or []) if k and cjk.search(k) and len(k) >= 2]
        if terms:
            tagmap[rule["label"]] = list(dict.fromkeys(terms))

    terms_map = load_config("terms.json").get("terms", {})
    concepts = load_config("concepts.json").get("concepts", {})

    html = (TEMPLATE
            .replace("__DATA__", json.dumps(packed, ensure_ascii=False, separators=(",", ":")))
            .replace("__TAGS__", json.dumps(tags, ensure_ascii=False, separators=(",", ":")))
            .replace("__TAGMAP__", json.dumps(tagmap, ensure_ascii=False, separators=(",", ":")))
            .replace("__CONCEPTS__", json.dumps(concepts, ensure_ascii=False, separators=(",", ":")))
            .replace("__TERMS__", json.dumps(terms_map, ensure_ascii=False, separators=(",", ":")))
            .replace("__STATS__", stats))

    out = SITE_DIR / "index.html"
    # newline="\n" 不能省：Windows 上 write_text 默认把 \n 翻成 \r\n，于是每次
    # 重建都整文件变成 CRLF，git 每次都提示「换行会被替换」，diff 里也混进
    # 只有换行不同的噪声。仓库里 .gitattributes 定了 eol=lf，这里就得跟着写 LF。
    out.write_text(html, encoding="utf-8", newline="\n")
    size_kb = out.stat().st_size / 1024
    log(f"检索页已生成：{out}（{size_kb:.0f} KB，{len(tools)} 条，{len(tags)} 个场景入口）")
    return str(out)


if __name__ == "__main__":
    build()
