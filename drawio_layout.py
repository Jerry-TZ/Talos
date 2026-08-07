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
import sys
from xml.sax.saxutils import escape, quoteattr

GRID = 10
VGAP, HGAP = 60, 60
BARYCENTER_ROUNDS = 4

# 形状按 kind 分,尺寸只有三种(drawiocheck 允许至多三种),填充取 Wong 色盲安全色
KINDS = {
    "start":     ("rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#E69F00;", 180, 50),
    "terminal":  ("rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#E69F00;", 180, 50),
    "gate":      ("rhombus;whiteSpace=wrap;html=1;fillColor=#56B4E9;", 220, 70),
    "step":      ("rounded=0;whiteSpace=wrap;html=1;fillColor=none;", 200, 50),
}
DEFAULT_KIND = "step"


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


def build(graph):
    nodes = [n["id"] for n in graph["nodes"]]
    meta = {n["id"]: n for n in graph["nodes"]}
    edges = graph["edges"]
    for e in edges:
        for end in ("source", "target"):
            if e[end] not in meta:
                raise ValueError(f"边指向不存在的块: {e[end]}")

    back = _back_edges(nodes, edges)
    rank = _ranks(nodes, edges, back)
    layers = _order(nodes, edges, back, rank)

    size = {n: KINDS.get(meta[n].get("kind", DEFAULT_KIND), KINDS[DEFAULT_KIND])[1:]
            for n in nodes}
    row_h = {r: max(size[n][1] for n in ns) for r, ns in layers.items()}
    width = max(sum(size[n][0] for n in ns) + HGAP * (len(ns) - 1)
                for ns in layers.values())

    geo, y = {}, 0
    for r in sorted(layers):
        row = layers[r]
        span = sum(size[n][0] for n in row) + HGAP * (len(row) - 1)
        x = (width - span) / 2                       # 每层居中,整体才是一根轴
        for n in row:
            w, h = size[n]
            geo[n] = (_snap(x), _snap(y), w, h)
            x += w + HGAP
        y += row_h[r] + VGAP

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           f'<mxfile host="drawio_layout.py">',
           f'  <diagram id="g" name={quoteattr(graph.get("title", "diagram"))}>',
           '    <mxGraphModel dx="1000" dy="700" grid="1" gridSize="10" page="0">',
           '      <root>', '        <mxCell id="0"/>',
           '        <mxCell id="1" parent="0"/>']
    for n in nodes:
        style, _w, _h = KINDS.get(meta[n].get("kind", DEFAULT_KIND), KINDS[DEFAULT_KIND])
        x, yy, w, h = geo[n]
        out.append(f'        <mxCell id={quoteattr(n)} value={quoteattr(meta[n]["label"])} '
                   f'style="{style}fontSize=12;" vertex="1" parent="1">'
                   f'<mxGeometry x="{x}" y="{yy}" width="{w}" height="{h}" as="geometry"/></mxCell>')
    for i, e in enumerate(edges):
        dashed = "dashed=1;strokeColor=#D55E00;" if e.get("kind") == "exception" else ""
        out.append(f'        <mxCell id="e{i}" value={quoteattr(e.get("label", ""))} '
                   f'style="edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;{dashed}" '
                   f'edge="1" parent="1" source={quoteattr(e["source"])} '
                   f'target={quoteattr(e["target"])}>'
                   f'<mxGeometry relative="1" as="geometry"/></mxCell>')
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

    # 反向:回边确实被认出来了(否则秩会算错,图就摊平成一条链)
    back = _back_edges([n["id"] for n in g["nodes"]], g["edges"])
    assert len(back) == 1, f"回边应恰好 1 条,实际 {back}"
    rank = _ranks([n["id"] for n in g["nodes"]], g["edges"], back)
    assert rank["s"] == 0 and rank["t"] > rank["a"] == rank["b"] > rank["g"], f"秩不对: {rank}"
    print(f"selfcheck ok — 排出来的图 drawiocheck 0 问题,回边 1 条,秩 {rank}")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
        raise SystemExit(0)
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    graph = json.load(open(sys.argv[1], encoding="utf-8"))
    open(sys.argv[2], "w", encoding="utf-8").write(build(graph))
    print(f"{len(graph['nodes'])} 块 {len(graph['edges'])} 边 -> {sys.argv[2]}")
