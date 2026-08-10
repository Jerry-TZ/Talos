"""drawio_layout: 一份「谁连谁」的 JSON → 一张排好版的 .drawio。

    python drawio_layout.py graph.json out.drawio
    python drawio_layout.py --selfcheck

**为什么要有它。** 上一轮我让模型自己写 mxGeometry 的坐标,拿回来的是 15 个
一模一样的灰框排成两列、20 条线对穿。查了一圈 GitHub 上同类项目,结论是一致的:

  · Agents365-ai/drawio-skill —— 用 Graphviz 定位,**模型从不写坐标**;
  · simonpo/drawio-ninja —— 只管 XML 合法性,布局明确写在「不保证」里,
    原话是「LLM 缺乏内在的空间推理能力」;它留了个 drawio-prettifier 阶段,没实现。

所以分工是:**模型给语义(有哪些块、谁连谁、哪个是闸),工具给坐标。**
分层布局(Sugiyama)是六十年前解决的问题,拿试错去做是浪费调用。

这台机器上没有 dot、没有 networkx,也不为这件事装 —— 分层版够用,就几十行:
  ① DFS 找回边(指向还在栈上的节点),定秩时把回边摘掉,图就成了 DAG;
  ② 秩 = 从入口起的最长路径 —— 用最长而不是最短,才不会让一条边跨好几层;
  ③ 层内顺序 = 前驱重心(barycenter)迭代几轮,这是减少交叉最便宜的启发式;
  ④ 坐标 = 秩 × 行高、序 × 列宽,每层居中后对齐到网格。

排完的图天然满足 drawiocheck 的对齐、不重叠、边不过长 —— **断言没消失,
只是不再是模型的活。**
"""
import json
import re
import sys
from xml.sax.saxutils import escape, quoteattr

GRID = 10
VGAP, HGAP = 60, 60
COLGAP = 140              # 栏间距。要明显大于栏内的 HGAP —— 判官只能靠留白认出「这是两栏」
MAX_ASPECT = 2.0          # 和 drawiocheck 同一个数:超了就多折一栏
BARYCENTER_ROUNDS = 4

# 形状按 kind 分,尺寸只有三种(drawiocheck 允许至多三种),填充取 Wong 色盲安全色
KINDS = {
    "start":     "rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#E69F00;",
    "terminal":  "rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#E69F00;",
    "gate":      "rhombus;whiteSpace=wrap;html=1;fillColor=#56B4E9;",
    "step":      "rounded=0;whiteSpace=wrap;html=1;fillColor=none;",
}
DEFAULT_KIND = "step"

# 尺寸不再按 kind 写死,而是按文字量从这三档里挑 —— 写死是上一版文字撑出框的**根因**:
# 标签从 4 个字到 40 个字,框却全是 220×70。判官那条「尺寸种类 ≤3」逼出了「全都一样大」,
# 而全都一样大 + 标签长短差十倍 = 文字必然出框。**一条规则制造了另一条规则要治的病。**
# 档位仍然只有三种宽,所以「尺寸别乱」那个初衷没丢 —— 丢的只是「必须一样大」。
WIDTHS = (200, 280, 360)
PAD = 16                  # 文字左右各留的空白
LINE_H = 16               # 12px 字的行高


def _text_w(s):
    """12px 下的文字宽度。中日韩按全宽 12,拉丁按 6.6 —— 够粗但方向对,
    而「够粗」正是这里要的:宁可框大一点,也别让字戳出去。"""
    return sum(12.0 if ord(c) > 0x2E80 else 6.6 for c in s)


def _fit(label, kind):
    """挑一个刚好放得下这段文字的尺寸。

    菱形是关键:它在离中心 dy 处的可用宽度只有 `w·(1-2|dy|/h)`,
    两行文字的外侧那行离中心 LINE_H/2,可用宽度掉到 w 的四分之三左右。
    按外接矩形算就会出框 —— 上一版就是这么出的。"""
    tw = _text_w(label)
    # 先把行数压到最少,再加宽 —— 顺序反过来的话所有块都会停在最窄那档、
    # 靠断行硬塞进去,而 `check_permission(state, cls, name, args)` 断成两行是很难读的。
    # 图里的字大半是代码标识符,标识符断行读者要自己拼回去。**宁可宽,别断。**
    for lines in (1, 2, 3):
        for w in WIDTHS:
            h = 50 if lines == 1 else 50 + (lines - 1) * LINE_H
            if kind == "gate":
                h += 20                                   # 菱形本来就要高一点才装得下
                avail = w * (1 - (lines - 1) * LINE_H / h) - PAD
            else:
                avail = w - PAD
            if tw / lines <= avail:
                return w, _snap(h)
    return WIDTHS[-1], _snap(50 + 2 * LINE_H)


def _snap(v):
    return int(round(v / GRID) * GRID)


def _back_edges(nodes, edges):
    """DFS,指向仍在递归栈上的节点的边就是回边。定秩前必须摘掉,否则最长路径不收敛。"""
    adj = {n: [] for n in nodes}
    for i, e in enumerate(edges):
        adj[e["source"]].append((e["target"], i))
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in nodes}
    back = set()

    def dfs(u):
        color[u] = GRAY
        for v, i in adj[u]:
            if color[v] == GRAY:
                back.add(i)
            elif color[v] == WHITE:
                dfs(v)
        color[u] = BLACK

    for n in nodes:
        if color[n] == WHITE:
            dfs(n)
    return back


def _ranks(nodes, edges, back):
    """秩 = 从入口起的最长路径。最短路径会让一条边跨好几层,看起来就是长线穿过整张图。"""
    fwd = [(e["source"], e["target"]) for i, e in enumerate(edges) if i not in back]
    preds = {n: [] for n in nodes}
    for u, v in fwd:
        preds[v].append(u)
    rank = {n: 0 for n in nodes}
    for _ in range(len(nodes)):                 # 松弛到不动点;DAG 上最多 |V| 轮
        changed = False
        for v in nodes:
            if preds[v]:
                r = max(rank[u] for u in preds[v]) + 1
                if r > rank[v]:
                    rank[v], changed = r, True
        if not changed:
            break
    return rank


def _order(nodes, edges, back, rank):
    """层内顺序:按前驱/后继的重心迭代。减少交叉最便宜的启发式,不求最优。"""
    layers = {}
    for n in nodes:
        layers.setdefault(rank[n], []).append(n)
    pos = {n: i for r in layers for i, n in enumerate(layers[r])}
    fwd = [(e["source"], e["target"]) for i, e in enumerate(edges) if i not in back]
    for rnd in range(BARYCENTER_ROUNDS):
        nbr = {n: [] for n in nodes}
        for u, v in fwd:                        # 奇数轮看前驱,偶数轮看后继,来回扫
            (nbr[v] if rnd % 2 == 0 else nbr[u]).append(u if rnd % 2 == 0 else v)
        for r in sorted(layers):
            layers[r].sort(key=lambda n: (sum(pos[m] for m in nbr[n]) / len(nbr[n])
                                          if nbr[n] else pos[n]))
            for i, n in enumerate(layers[r]):
                pos[n] = i
    return layers


def _columns(ranks, row_h, col_w, rank, edges, back):
    """一柱到底太高就折成几栏 —— 论文里长流程本来就是这么排的,不是取巧。

    只切**连续的秩区间**:同一层的块永远待在同一栏,栏内仍然自上而下读。

    切在哪儿,以前只看长宽比 —— 于是切口正好落在分叉最密的地方,栏间拉出四条线,
    判官(它管着「换栏边免检长度和上行,免检不能变成藏线的地方」)当场报红。
    **判官是对的:该改的是切口,不是上限。** 所以现在在满足长宽比的前提下,
    枚举所有切法,挑**跨栏边最少**的那一个;跨了不止一栏的切法直接淘汰
    (那种边会被判官判成「跳着栏连」,而且画出来就是横穿全图)。
    栏数最多到 4,切点组合最多几百种,算它一遍比排错一次便宜得多。"""
    from itertools import combinations
    fwd = [(rank[e["source"]], rank[e["target"]])
           for i, e in enumerate(edges) if i not in back]

    def score(cuts):
        col = {r: sum(1 for c in cuts if r >= ranks[c]) for r in ranks}
        spans = [col[b] - col[a] for a, b in fwd]
        if any(d > 1 for d in spans):
            return None                                   # 跳栏,淘汰
        groups = [[r for r in ranks if col[r] == k] for k in range(len(cuts) + 1)]
        if any(not g for g in groups):
            return None
        h = max(sum(row_h[r] + VGAP for r in g) - VGAP for g in groups)
        w = len(groups) * col_w + (len(groups) - 1) * COLGAP
        if h > MAX_ASPECT * w:
            return None
        return max([spans.count(1)] + [sum(1 for a, b in fwd if col[a] == k and col[b] == k + 1)
                                       for k in range(len(cuts))]), groups

    best = None
    for k in range(0, min(3, len(ranks) - 1) + 1):        # k 个切点 = k+1 栏
        for cuts in combinations(range(1, len(ranks)), k):
            got = score(cuts)
            if got and (best is None or got[0] < best[0]):
                best = got
        if best:                                          # 栏数够用就不再往上加
            return best[1]
    return [ranks]


# XML 1.0 里非法的控制字符。`quoteattr` 只管 & < > " 和空白,这些它照原样写出去,
# 于是产出一个 **drawio 打不开、ET.parse 直接 ParseError** 的文件。而 JSON 里 ""
# 完全合法 —— 模型从别处粘一段说明进来就可能带上。判官那时只能回一句
# 「读不了 …: not well-formed」,连是哪个标签都指不出来。**在写出去之前就拦掉。**
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def build(graph):
    if not isinstance(graph, dict) or "nodes" not in graph or "edges" not in graph:
        # 整套设计的前提是「给模型一句它能照着改的话」。裸 KeyError 给的是调用栈。
        raise ValueError("JSON 要有 nodes 和 edges 两个键:"
                         '{"title": "…", "nodes": [{"id","label","kind"}], '
                         '"edges": [{"source","target","label","kind"}]}')
    for i, n in enumerate(graph["nodes"]):
        if not isinstance(n, dict) or not n.get("id"):
            raise ValueError(f"第 {i + 1} 个块没有 id —— 每个块都要有 id 和 label")
    nodes = [n["id"] for n in graph["nodes"]]
    if len(set(nodes)) != len(nodes):
        # 原来 meta 按 id 去重、nodes 不去重:重复 id 的块**静默丢掉一个的标签**,
        # 而且产出两个同 id 同坐标的 mxCell —— mxGraphModel 的 id 是主键,那是个非法模型。
        # 判官只看到一个块,返回 []。**丢东西的错误必须响。**
        dup = sorted({i for i in nodes if nodes.count(i) > 1})
        raise ValueError(f"块 id 重复: {dup} —— id 是主键,重了会有一个块被静默丢掉")
    meta = {n["id"]: n for n in graph["nodes"]}
    edges = graph["edges"]
    for i, e in enumerate(edges):
        for end in ("source", "target"):
            if not isinstance(e, dict) or end not in e:
                raise ValueError(f"第 {i + 1} 条边缺 {end} —— 每条边都要有 source 和 target")
            if e[end] not in meta:
                raise ValueError(f"边指向不存在的块: {e[end]}")

    if not nodes:
        raise ValueError("图里一个块都没有 —— nodes 至少要有一个 {id, label}")
    for n in graph["nodes"]:
        if not n.get("label"):
            raise ValueError(f"块 {n.get('id')!r} 没有 label —— 每个块都要有字")
        if _CTRL.search(str(n["label"])):
            raise ValueError(f"块 {n['id']!r} 的标签里有控制字符(XML 1.0 不允许)—— "
                             "产出的 .drawio 会打不开。把标签里的不可见字符清掉。")
    for e in edges:
        if _CTRL.search(str(e.get("label") or "")):
            raise ValueError(f"边 {e['source']}→{e['target']} 的标签里有控制字符,同上")
    back = _back_edges(nodes, edges)
    rank = _ranks(nodes, edges, back)
    layers = _order(nodes, edges, back, rank)

    size = {n: _fit(meta[n]["label"], meta[n].get("kind", DEFAULT_KIND)) for n in nodes}
    row_h = {r: max(size[n][1] for n in ns) for r, ns in layers.items()}
    width = max(sum(size[n][0] for n in ns) + HGAP * (len(ns) - 1)
                for ns in layers.values())

    geo = {}
    cols = _columns(sorted(layers), row_h, width, rank, edges, back)
    for ci, col in enumerate(cols):
        x0, y = ci * (width + COLGAP), 0
        for r in col:
            row = layers[r]
            span = sum(size[n][0] for n in row) + HGAP * (len(row) - 1)
            x = x0 + (width - span) / 2              # 每层在本栏内居中,一栏就是一根轴
            for n in row:
                w, h = size[n]
                geo[n] = (_snap(x), _snap(y + (row_h[r] - h) / 2), w, h)
                x += w + HGAP
            y += row_h[r] + VGAP

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<mxfile host="drawio_layout.py">',
           f'  <diagram id="g" name={quoteattr(graph.get("title", "diagram"))}>',
           '    <mxGraphModel dx="1000" dy="700" grid="1" gridSize="10" page="0">',
           '      <root>', '        <mxCell id="0"/>',
           '        <mxCell id="1" parent="0"/>']
    for n in nodes:
        style = KINDS.get(meta[n].get("kind", DEFAULT_KIND), KINDS[DEFAULT_KIND])
        x, yy, w, h = geo[n]
        out.append(f'        <mxCell id={quoteattr(n)} value={quoteattr(meta[n]["label"])} '
                   f'style="{style}fontSize=12;" vertex="1" parent="1">'
                   f'<mxGeometry x="{x}" y="{yy}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    # 回边和换栏边:给显式转折点,让它们**沿着图的外缘绕**,别走直线。
    #
    # 上一版这两类边是直连的,drawio 的正交路由就让它们从图中间穿过去 —— 回边横穿两栏、
    # 压着 maybe_compact 和 _chat 两个块。当时我在判官里给它们开了豁免,理由是
    # 「换栏边天生又长又往上」。**理由对,结论错了:天生又长又往上的东西不该豁免,
    # 该不让它走直线。** 判官只看两端坐标,看不见中间那段,所以豁免掉的正是它看不见的那部分。
    #
    # 绕法就是纸质流程图几十年的走法:先向下走到所有块底下,再横过去,再向上接进目标。
    # 回边走最左边的外缘(左边一定是空的);换栏边走两栏之间那条 COLGAP 宽的空隙
    # (那里按构造就没有块)。两段横走都在 ybot 上,那也在所有块底下。
    ybot = max(g[1] + g[3] for g in geo.values()) + 40
    xleft = min(g[0] for g in geo.values()) - 40
    col_of = {n: ci for ci, col in enumerate(cols) for r in col for n in layers[r]}

    def _detour(e, i):
        s, t = geo[e["source"]], geo[e["target"]]
        sx, tx = s[0] + s[2] / 2, t[0] + t[2] / 2
        ty = t[1] + t[3] / 2
        if i in back:                                    # 回边:绕最左边
            via = xleft
        elif col_of[e["target"]] != col_of[e["source"]]:  # 换栏:走栏间那条空隙
            via = col_of[e["target"]] * (width + COLGAP) - COLGAP / 2
        else:
            return ""
        pts = [(sx, ybot), (via, ybot), (via, ty)]
        return ('<Array as="points">'
                + "".join(f'<mxPoint x="{_snap(px)}" y="{_snap(py)}"/>' for px, py in pts)
                + "</Array>")

    for i, e in enumerate(edges):
        dashed = "dashed=1;strokeColor=#D55E00;" if e.get("kind") == "exception" else ""
        out.append(f'        <mxCell id="e{i}" value={quoteattr(e.get("label", ""))} '
                   f'style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;{dashed}" '
                   f'edge="1" parent="1" source={quoteattr(e["source"])} '
                   f'target={quoteattr(e["target"])}>'
                   f'<mxGeometry relative="1" as="geometry">{_detour(e, i)}'
                   f'</mxGeometry></mxCell>')
    out += ['      </root>', '    </mxGraphModel>', '  </diagram>', '</mxfile>']
    return "\n".join(out)


def _selfcheck():
    import os, tempfile
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import drawiocheck

    g = {"title": "loop", "nodes": [
        {"id": "s", "label": "开始", "kind": "start"},
        {"id": "g", "label": "还有活?", "kind": "gate"},
        {"id": "a", "label": "干活 A"}, {"id": "b", "label": "干活 B"},
        {"id": "t", "label": "结束", "kind": "terminal"},
        {"id": "x", "label": "出错中止", "kind": "terminal"}],
        "edges": [
        {"source": "s", "target": "g"}, {"source": "g", "target": "a", "label": "是"},
        {"source": "g", "target": "b", "label": "是"}, {"source": "a", "target": "t"},
        {"source": "b", "target": "t"}, {"source": "t", "target": "g", "label": "回边"},
        {"source": "a", "target": "x", "label": "Exception", "kind": "exception"}]}
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.drawio")
    open(p, "w", encoding="utf-8").write(build(g))
    probs = drawiocheck.check(p)
    assert probs == [], f"自己排的版没过自己的判官: {probs}"

    # 第二个样本:**故意难排**。上面那个 6 块的太软 —— 实测把 GRID / VGAP / HGAP /
    # COLGAP / MAX_ASPECT / BARYCENTER_ROUNDS / PAD / LINE_H 这些旋钮随便拧,判官
    # 一声不吭(9 个里 8 个没反应)。原因是它只有 4 层、标签全是两三个字的单行:
    # `_fit` 永远停在第一档、`_snap` 永远无事可做、`_columns` 永远不用折栏、
    # 层内最多 2 个块所以没有交叉可减。**样本软的时候,后面所有断言都是摆设。**
    # 所以这一个要同时满足:标签长到要折行(逼出 _fit/_snap/PAD/LINE_H)、层数多到
    # 必须折栏(逼出 _columns/COLGAP/MAX_ASPECT)、某一层三个块且边会交叉(逼出重心排序)。
    # 标签必须长到**最宽那一档也放不下**才会折行。14 个汉字不行 —— `_fit` 加宽到 280
    # 就收了,块高永远是 50,于是 `_snap`/`PAD`/`LINE_H` 一条都走不到。
    # 这里量过:29 字 = 343px,而三档可用 360-16=344px —— **差 1px 就放下了**,
    # 于是"长标签"是假的。要 33 字以上才真折行;又得留在 MAX_LABEL=42 以内,
    # 否则红的是「文字过长」而不是折行。这一档的余量很窄,改标签时重跑一遍旋钮灵敏度。
    _L = "这一步要做的事情说明得长到必须折成两行才放得进去否则测不到折"   # 30 字,加前缀 33~34
    hard = {"title": "hard", "nodes": [
        {"id": "n0", "label": "开始:" + _L, "kind": "start"},
        {"id": "n1", "label": "第一步 " + _L},
        {"id": "g1", "label": "条件甲成立吗?", "kind": "gate"},
        {"id": "p1", "label": "分支一 " + _L}, {"id": "p2", "label": "分支二"},
        {"id": "p3", "label": "分支三 " + _L},
        {"id": "m1", "label": "汇合 " + _L}, {"id": "m2", "label": "汇合之后再算一遍"},
        {"id": "n2", "label": "第二步"}, {"id": "n3", "label": "第三步 " + _L},
        {"id": "n4", "label": "第四步"}, {"id": "n5", "label": "第五步 " + _L},
        {"id": "n6", "label": "第六步"}, {"id": "n7", "label": "第七步 " + _L},
        {"id": "t", "label": "结束", "kind": "terminal"},
        {"id": "x", "label": "出错中止", "kind": "terminal"}],
        "edges": [
        {"source": "n0", "target": "n1"}, {"source": "n1", "target": "g1"},
        # 三条分支 + 交叉的汇合(p1→m2、p3→m1):不减交叉就会拧成麻花
        {"source": "g1", "target": "p1", "label": "是"},
        {"source": "g1", "target": "p2", "label": "否"},
        {"source": "g1", "target": "p3", "label": "其他"},
        {"source": "p1", "target": "m2"}, {"source": "p2", "target": "m1"},
        {"source": "p3", "target": "m1"}, {"source": "p2", "target": "m2"},
        {"source": "m1", "target": "n2"}, {"source": "m2", "target": "n2"},
        {"source": "n2", "target": "n3"}, {"source": "n3", "target": "n4"},
        {"source": "n4", "target": "n5"}, {"source": "n5", "target": "n6"},
        {"source": "n6", "target": "n7"}, {"source": "n7", "target": "t"},
        {"source": "t", "target": "n1", "label": "回边"},
        {"source": "n3", "target": "x", "label": "Exception", "kind": "exception"}]}
    ph = os.path.join(d, "hard.drawio")
    open(ph, "w", encoding="utf-8").write(build(hard))
    probs = drawiocheck.check(ph)
    assert probs == [], f"难排的样本没过判官: {probs}"

    # 第三个样本:**又高又窄**。上面那个有三条分支,横向被撑到 880px,比例才 1.45 ——
    # 它永远不需要折栏,于是 COLGAP / MAX_ASPECT / `_columns` 整条路走不到。
    # 折栏只有在「高得放不下」时才发生,所以这里要的是一根没有分支的长链。
    tall = {"title": "tall",
            "nodes": [{"id": "c0", "label": "开始", "kind": "start"}]
                     + [{"id": f"c{i}", "label": f"第 {i} 步"} for i in range(1, 13)]
                     + [{"id": "ct", "label": "结束", "kind": "terminal"}],
            "edges": [{"source": f"c{i}", "target": f"c{i + 1}"} for i in range(12)]
                     + [{"source": "c12", "target": "ct"}]}
    pt = os.path.join(d, "tall.drawio")
    open(pt, "w", encoding="utf-8").write(build(tall))
    probs = drawiocheck.check(pt)
    assert probs == [], f"长链样本没过判官(它必须折栏才放得下): {probs}"

    # 反向:回边确实被认出来了(否则秩会算错,图就摊平成一条链)
    back = _back_edges([n["id"] for n in g["nodes"]], g["edges"])
    assert len(back) == 1, f"回边应恰好 1 条,实际 {back}"
    rank = _ranks([n["id"] for n in g["nodes"]], g["edges"], back)
    assert rank["s"] == 0 and rank["t"] > rank["a"] == rank["b"] > rank["g"], f"秩不对: {rank}"
    print(f"selfcheck ok — 排出来的图 drawiocheck 0 问题,回边 1 条,秩 {rank}")


if __name__ == "__main__":
    # 同 drawiocheck:输出全是中文,Windows 控制台默认编码装不下,会在打印那一行炸掉。
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--selfcheck" in sys.argv:
        _selfcheck()
        raise SystemExit(0)
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    # 拿到的是**一句能照着改的话**,不是调用栈。整套设计的前提就是这个:模型只会读
    # 最后一行,而 `KeyError: 'edges'` 那一行既不说哪错了、也不说该怎么改。
    try:
        graph = json.load(open(sys.argv[1], encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"读不了 {sys.argv[1]}: {exc}")
    try:
        xml = build(graph)
    except ValueError as exc:
        raise SystemExit(str(exc))
    open(sys.argv[2], "w", encoding="utf-8").write(xml)
    print(f"{len(graph['nodes'])} 块 {len(graph['edges'])} 边 -> {sys.argv[2]}")
