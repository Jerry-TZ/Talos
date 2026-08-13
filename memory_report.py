"""把记忆网络画成一张单文件 HTML —— 只读,不联网,不起服务器。

    python memory_report.py        →  .talos/memory_report.html

**不画节点关系图。** 78 个节点 810 条边,画出来是一团毛线:好看,但看完还是不知道
该动哪里。真正能指导行动的是三件事,所以只画这三件:

  1. 哪几条技能老被捞到,哪几条从没被想起 —— FINDINGS 第四节那条「泛用技能是负资产」
     当初是手工对照发现的,这张表就是把那次手工活变成每次都能看一眼。
  2. 分数的分布 —— 那几个阈值(THRESH / BODY_LEAD / BODY_FLOOR)全是手调的,
     看见真实分数落在哪儿,比看常数有用。
  3. 正文注入了几次 —— 每次至多 1200 字,是这套机制唯一真花钱的地方。

数据只来自本地 .talos/,里面有 memory.md 的片段(前 80 字)。**这份 HTML 别往外发。**
"""
import collections
import html
import json
import os
import statistics
import sys

HOME = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HOME, ".talos")
OUT = os.path.join(D, "memory_report.html")


def _rows(name):
    p = os.path.join(D, name)
    if not os.path.exists(p):
        return []
    out = []
    for ln in open(p, encoding="utf-8"):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue                      # 坏行跳过 —— 报告不该被一行脏数据弄崩
    return out


def _bar(frac, w=180):
    n = max(1, round(frac * w))
    return f'<div class=bar><i style="width:{n}px"></i></div>'


def build() -> str:
    rows = _rows("recall_trace.jsonl")
    # 同一个文件里两种行:检索那一刻写的(带 picked),和这一轮跑完回填的(带 out)。
    # 混在一起数,「轮检索」会凭空翻倍。
    trace = [r for r in rows if "out" not in r]
    outs = [r["out"] for r in rows if isinstance(r.get("out"), dict)]
    cache = _rows("cache_trace.jsonl")
    hits = {}
    hp = os.path.join(D, "recall_hits.json")
    if os.path.exists(hp):
        try:
            hits = json.load(open(hp, encoding="utf-8"))
        except ValueError:
            pass

    picked = collections.Counter()        # 被捞到几次
    bodied = collections.Counter()        # 其中给了正文几次
    scores = []
    for r in trace:
        for p in r.get("picked", []):
            k = p.get("key", "")[:70]
            picked[k] += 1
            if p.get("body"):
                bodied[k] += 1
            if isinstance(p.get("score"), (int, float)):
                scores.append(p["score"])

    parts = ["<h1>记忆网络</h1>",
             f"<p class=dim>{len(trace)} 轮检索 · {len(hits)} 条记忆有命中记录 · "
             f"{sum(bodied.values())} 次注入过正文</p>"]

    # ① 谁老被捞到,谁从没被想起 —— 前者可能是噪声,后者是死重
    parts.append("<h2>① 被捞到的次数(和其中给了正文的次数)</h2>"
                 "<p class=dim>经常进前五、却从没当过第一名的,大概率是噪声技能 —— "
                 "它靠“什么都沾边”挤掉真正对题的那条。</p><table>")
    for k, n in picked.most_common(18):
        b = bodied[k]
        parts.append(f"<tr><td class=k>{html.escape(k)}</td><td>{_bar(n / max(picked.values()))}</td>"
                     f"<td class=n>{n}</td><td class=n>{'正文 ' + str(b) if b else ''}</td></tr>")
    parts.append("</table>")

    dead = [k for k, v in hits.items()
            if isinstance(v, list) and len(v) >= 2 and v[0] >= 8 and v[1] == 0]
    if dead:
        parts.append("<h2>② 见过很多次、一次都没被想起</h2>"
                     "<p class=dim>存了个寂寞。/forget 会提议删这些(只提议 Talos 自己写的)。</p><ul>")
        for k in dead[:12]:
            parts.append(f"<li>{html.escape(k[:90])} <span class=dim>(出现 {hits[k][0]} 次)</span></li>")
        parts.append("</ul>")

    # ③ 分数落在哪儿 —— 阈值是手调的,看真实分布比看常数有用
    if scores:
        buckets = collections.Counter(min(9, int(s * 10)) for s in scores)
        parts.append("<h2>③ 激活分数的分布</h2>"
                     f"<p class=dim>中位数 {statistics.median(scores):.2f} · "
                     f"范围 {min(scores):.2f}~{max(scores):.2f} · 共 {len(scores)} 次命中。"
                     "阈值 THRESH/BODY_FLOOR 都是手调的,这张图是它们该不该动的唯一依据。</p><table>")
        top = max(buckets.values())
        for b in range(10):
            parts.append(f"<tr><td class=k>{b/10:.1f} – {(b+1)/10:.1f}</td>"
                         f"<td>{_bar(buckets.get(b, 0) / top)}</td>"
                         f"<td class=n>{buckets.get(b, 0)}</td></tr>")
        parts.append("</table>")

    # ④ 注入正文到底有没有让这一轮更省 —— 复盘写完技能就结束,从来不知道哪条真管用。
    # **按结果行自己带的 bodies 分组,不按 q 去跟检索行对。** 复盘用同一个 query 再检索
    # 一遍、同一个问题问两次,按 q 分组就把没拿到正文的那些轮也算进「注入过」那一组。
    groups = {True: [], False: []}
    for out in outs:
        if isinstance(out.get("calls"), int) and not out.get("capped"):
            groups[bool(out.get("bodies"))].append(out["calls"])
    parts.append("<h2>④ 注入了技能正文的轮,是不是更省</h2>")
    if not any(groups.values()):
        parts.append("<p class=dim>还没有数据 —— 结果是这一轮跑完才回填的,正常用几轮再来看。</p>")
    else:
        for label, want in (("注入过技能正文", True), ("只给了描述", False)):
            g = groups[want]
            if g:
                parts.append(f"<p><b>{label}</b> n={len(g)} · 工具调用中位数 "
                             f"{statistics.median(g):.1f} · 范围 {min(g)}~{max(g)}</p>")
        capped_n = sum(1 for o in outs if o.get("capped"))
        parts.append(f"<p class=dim>差值小于 2 次、或任一组 n&lt;8 时,别下结论 —— "
                     f"任务难度本身的方差比这大。撞了步数上限的 {capped_n} 轮不计入"
                     f"(那是「卡住了」,不是「花得多」)。这是燃尽表,每跑一个真任务加一条。</p>")

    # ⑤ 缓存 —— system 变没变 × 命中多少
    parts.append("<h2>⑤ KV 缓存命中</h2>")
    if not cache:
        parts.append("<p class=dim>还没有数据。正常用几轮,复盘写过技能之后再来看 —— "
                     "要比的是「system 块变了的那些轮」和「没变的那些轮」。</p>")
    else:
        for label, want in (("system 没变", False), ("system 变了(复盘写过技能)", True)):
            g = [r["hit"] for r in cache if r.get("sys_changed") is want and r.get("hit") is not None]
            if g:
                parts.append(f"<p><b>{label}</b> n={len(g)} · 中位数 "
                             f"{statistics.median(g):.0%} · 范围 {min(g):.0%}~{max(g):.0%}</p>")
        parts.append("<p class=dim>差值小于 10 个百分点、或任一组 n&lt;8 时,别下结论。</p>")

    return ("<meta charset=utf-8><title>Talos 记忆网络</title><style>"
            "body{font:15px/1.7 system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 20px}"
            "h1{font-size:22px}h2{font-size:17px;margin-top:32px}"
            ".dim{color:#777;font-size:13px}table{width:100%;border-collapse:collapse}"
            "td{padding:3px 6px;vertical-align:middle}.k{font-size:12px;color:#333}"
            ".n{text-align:right;font-variant-numeric:tabular-nums;color:#666;width:70px}"
            ".bar{background:#eee;height:9px;border-radius:5px;width:180px}"
            ".bar i{display:block;height:9px;background:#7a5af8;border-radius:5px}"
            "@media(prefers-color-scheme:dark){body{background:#111;color:#ddd}"
            ".k{color:#bbb}.bar{background:#333}}</style>" + "\n".join(parts))


if __name__ == "__main__":
    if not os.path.isdir(D):
        sys.exit("没有 .talos/ —— 先正常用几轮。")
    os.makedirs(D, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build())
    print("写好了:", OUT)
    print("(里面有 memory.md 的片段,别往外发)")
