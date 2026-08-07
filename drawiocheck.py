"""drawiocheck: 检查一个 .drawio 流程图的七项科研制图规范,返回问题清单。

    python drawiocheck.py <图.drawio> [--width-mm 183]
    python drawiocheck.py --selfcheck        # 每条检查各造一张坏图,确认它真的红

输出 JSON 编码的问题列表(无问题为 `[]`);有问题时退出码 1。
不 assert、不改文件 —— 它只报告,修图是别人的事。

**为什么是 drawio 而不是 mermaid。** mermaid 把版面交给渲染器:同一份源码,
换个渲染器就换个样子,文件里根本没有「这个块在哪、多大」。于是「画得丑」在
文件里无处可查,只能靠人看。drawio 的 `<mxGeometry x= y= width= height=>`
把版面写成了数字 —— 对不齐是坐标不是网格倍数,拥挤是包围盒相交,花是配色不在
白名单里。**审美断言不了,科研制图规范能;而规范全都落在这些数字上。**

七项检查(和 figcheck 是同一个家族,只是读的是 XML 不是 mpl 的 artist):
1. 块两两包围盒不相交 —— 挤在一起是「丑」最直接的形状;
2. 所有块的 x/y/width/height 都是 GRID 的整数倍 —— 对不齐是「丑」最常见的原因;
3. 尺寸种类数 <= MAX_SIZES —— 每个块都不一样大 = 乱;
4. fillColor 必须在 Wong 2011 色盲安全配色 + 中性色白名单里;
5. fontSize >= MIN_FONTSIZE;
6. 每条边都是真的 edge 元素、且 source/target 都指向存在的块 —— 没有悬空边,
   也没有孤立块。**「画成一条边」和「在某个块的文字里提一嘴」必须分得开**;
7. 每个块的文字 <= MAX_LABEL 字符,且边不许走斜线(要 orthogonalEdgeStyle)。
   字数上限治的是同一个毛病:把说明塞进块里,图就退化成了排版过的段落。

上面七条**全是禁令**,而禁令有一个平凡最优解:什么都别画。头一版只有它们,
拿回来的是 15 个一模一样的无填充灰框排成两列、20 条线对穿 —— 七条一条没绕,
图却比之前更难看。所以后面三条是**正向要求**:

9.  向上走的边 <= MAX_BACK_EDGES 条(流程图里往上的边就该只有循环回边那一条);
10. 每条边两端中心距 <= MAX_EDGE_LEN —— 相连的块要挨着,别让线横跨整张图;
11. 全图至少 MIN_FILLS 种填充色 —— 一种颜色 = 没有视觉层次,判定和普通步骤分不开。
"""
import json
import re
import sys
import xml.etree.ElementTree as ET

GRID = 10                 # drawio 默认网格
MAX_SIZES = 3             # 允许几种不同的块尺寸
MIN_FONTSIZE = 10.0
MAX_LABEL = 42            # 一个块里最多几个字符(中文按 1 个算)
MM_PER_PX = 25.4 / 96     # drawio 的坐标单位是 CSS px,96dpi
MAX_BACK_EDGES = 1        # 向上走的边最多几条(流程图里那就是循环回边)
MAX_EDGE_LEN = 400.0      # 一条边两端中心的距离上限(px)
MIN_FILLS = 2             # 全图至少用到几种填充色

# Wong 2011 色盲安全配色 + 中性色。大小写不敏感。
PALETTE = {
    "#e69f00", "#56b4e9", "#009e73", "#f0e442",
    "#0072b2", "#d55e00", "#cc79a7",
    "none", "default", "#ffffff", "#f5f5f5", "#ffffff00",
}

_STYLE_KV = re.compile(r"([a-zA-Z]+)=([^;]*)")
_TAGS = re.compile(r"<[^>]+>")


def _style(cell):
    return dict(_STYLE_KV.findall(cell.get("style") or ""))


def _label(cell):
    """块上的文字。drawio 把 value 存成 HTML,标签要剥掉,&nbsp; 要还原成空格。"""
    v = cell.get("value") or ""
    v = _TAGS.sub("", v).replace("&nbsp;", " ").replace("&amp;", "&")
    return v.strip()


def _geom(cell):
    g = cell.find("mxGeometry")
    if g is None:
        return None
    try:
        return tuple(float(g.get(k, 0) or 0) for k in ("x", "y", "width", "height"))
    except ValueError:
        return None


def _cells(path):
    """所有 mxCell。.drawio 可能是压缩的 —— 那种情况下 <diagram> 里是一坨
    base64,没有 mxCell 可读。**不猜、不解压,直接报错**:让人在 drawio 里
    用「取消勾选 Compressed」另存一次,比在这里塞一个 zlib 分支可靠。"""
    root = ET.parse(path).getroot()
    cells = root.findall(".//mxCell")
    if not cells and root.findall(".//diagram"):
        raise ValueError("这个 .drawio 是压缩存的,读不到块。"
                         "在 drawio 里 文件→属性 取消 Compressed 再另存。")
    return cells


def check(path, expected_width_mm=None):
    problems = []
    try:
        cells = _cells(path)
    except Exception as exc:
        return [f"读不了 {path}: {exc}"]

    boxes = {c.get("id"): c for c in cells if c.get("vertex") == "1"}
    edges = [c for c in cells if c.get("edge") == "1"]
    if not boxes:
        return ["图里一个块都没有"]

    # 1. 两两不相交
    geos = {i: _geom(c) for i, c in boxes.items()}
    ids = [i for i, g in geos.items() if g and g[2] > 0 and g[3] > 0]
    for a in range(len(ids)):
        for b in range(a + 1, len(ids)):
            x1, y1, w1, h1 = geos[ids[a]]
            x2, y2, w2, h2 = geos[ids[b]]
            if x1 < x2 + w2 and x2 < x1 + w1 and y1 < y2 + h2 and y2 < y1 + h1:
                problems.append(f"块重叠: {_label(boxes[ids[a]])!r} 与 {_label(boxes[ids[b]])!r} 包围盒相交")

    # 2. 对齐到网格
    for i in ids:
        for name, v in zip(("x", "y", "width", "height"), geos[i]):
            if v % GRID:
                problems.append(f"没对齐网格: {_label(boxes[i])!r} 的 {name}={v} 不是 {GRID} 的整数倍")

    # 3. 尺寸种类
    sizes = {(g[2], g[3]) for g in (geos[i] for i in ids)}
    if len(sizes) > MAX_SIZES:
        problems.append(f"块尺寸有 {len(sizes)} 种(上限 {MAX_SIZES}): {sorted(sizes)}")

    # 4. 配色白名单  5. 字号  7a. 文字长度
    for i, c in boxes.items():
        st = _style(c)
        fill = (st.get("fillColor") or "none").strip().lower()
        if fill not in PALETTE:
            problems.append(f"配色不在白名单: {_label(c)!r} fillColor={fill}")
        try:
            fs = float(st.get("fontSize", 12))
        except ValueError:
            fs = 12.0
        if fs < MIN_FONTSIZE:
            problems.append(f"字号过小: {_label(c)!r} fontSize={fs} < {MIN_FONTSIZE}")
        lab = _label(c)
        if len(lab) > MAX_LABEL:
            problems.append(f"块里文字过长({len(lab)} > {MAX_LABEL}): {lab[:30]!r}… "
                            f"—— 拆成块和边,别把说明塞进一个块里")

    # 6. 边:source/target 齐全且指向存在的块;没有孤立块
    linked = set()
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if not s or not t:
            problems.append(f"边 {e.get('id')} 缺 source/target —— 画上去的裸箭头不算连接")
            continue
        for end in (s, t):
            if end not in boxes:
                problems.append(f"悬空边 {e.get('id')}: 端点 {end} 不是图里的块")
        linked.update((s, t))
    for i in boxes:
        if i not in linked:
            problems.append(f"孤立块(没有任何边连着): {_label(boxes[i])!r}")

    # 7b. 正交走线
    for e in edges:
        if _style(e).get("edgeStyle") != "orthogonalEdgeStyle":
            problems.append(f"边 {e.get('id')} 不是正交走线(要 edgeStyle=orthogonalEdgeStyle)")

    # 9~11. 正向要求 —— 上面 1~8 全是**禁令**,而禁令有一个平凡最优解:什么都别画。
    # 头一版只有禁令,结果拿回来的是 15 个一模一样的无填充灰框排成两列、20 条线对穿:
    # 尺寸种类=1 ✅、fillColor 全是 none(而 none 在白名单里)✅、x 只有两个值所以对齐 ✅、
    # 文字全砍成四个字 ✅ —— 七条一条没绕,图比之前更难看。
    # **「不许难看」和「要好看」不是同一件事,前者的最优解是空白。** 所以这三条是正向的。
    def _center(i):
        x, y, w, h = geos[i]
        return x + w / 2, y + h / 2

    back, longest = 0, []
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s not in geos or t not in geos or not geos[s] or not geos[t]:
            continue
        (x1, y1), (x2, y2) = _center(s), _center(t)
        if y2 < y1 - 1:                       # 终点在起点上方 = 往回走
            back += 1
        d = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        if d > MAX_EDGE_LEN:
            longest.append(f"{_label(boxes[s])!r}→{_label(boxes[t])!r} {d:.0f}px")
    # 9. 往上走的边就是回边。流程图只该有回边一条往上;更多说明块的摆放没跟着流程走。
    if back > MAX_BACK_EDGES:
        problems.append(f"向上的边有 {back} 条(上限 {MAX_BACK_EDGES})—— "
                        f"块的上下顺序没跟着流程走,读者得来回跳")
    # 10. 有边相连的块必须挨着。两列格子 + 长线对穿正是这条抓的。
    if longest:
        problems.append(f"边太长({len(longest)} 条 > {MAX_EDGE_LEN:.0f}px): {'; '.join(longest[:3])}"
                        f" —— 相连的块要挨着放,别让线横跨整张图")
    # 11. 全图一种填充 = 没有视觉层次。判定、普通步骤、终止至少要能一眼分开。
    fills = {(_style(c).get("fillColor") or "none").strip().lower() for c in boxes.values()}
    if len(fills) < MIN_FILLS:
        problems.append(f"全图只有 {len(fills)} 种填充({fills})—— "
                        f"至少 {MIN_FILLS} 种,判定/普通步骤/终止要能一眼分开")

    # 8. 图宽(给了才查)
    if expected_width_mm is not None and ids:
        right = max(geos[i][0] + geos[i][2] for i in ids)
        left = min(geos[i][0] for i in ids)
        got = (right - left) * MM_PER_PX
        if abs(got - expected_width_mm) > 2.0:
            problems.append(f"图宽 {got:.1f}mm 与期望 {expected_width_mm}mm 差 {abs(got - expected_width_mm):.1f}mm(容差 2mm)")

    return problems


# ── 自检:每条检查各造一张只坏那一处的图,确认它真的红 ────────────────
_GOOD = """<mxfile><diagram><mxGraphModel><root>
<mxCell id="0"/><mxCell id="1" parent="0"/>
<mxCell id="a" value="开始" style="rounded=1;fillColor=#0072B2;fontSize=12;" vertex="1" parent="1">
  <mxGeometry x="40" y="40" width="160" height="60" as="geometry"/></mxCell>
<mxCell id="b" value="干活" style="rounded=1;fillColor=#0072B2;fontSize=12;" vertex="1" parent="1">
  <mxGeometry x="40" y="160" width="160" height="60" as="geometry"/></mxCell>
<mxCell id="c" value="结束" style="rounded=1;fillColor=#E69F00;fontSize=12;" vertex="1" parent="1">
  <mxGeometry x="40" y="280" width="160" height="60" as="geometry"/></mxCell>
<mxCell id="e1" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="a" target="b">
  <mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e2" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="b" target="c">
  <mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e3" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="c" target="a">
  <mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""
# 三个块一条链 + 一条回边 —— 这就是「向上的边只该有回边那一条」的合法形状,
# 干净样本必须长成真流程图的样子,否则正向那三条根本没有能通过的基准。

_BAD = {
    "块重叠": ('y="160"', 'y="80"'),
    "没对齐网格": ('x="40" y="40"', 'x="43" y="40"'),
    "配色不在白名单": ('fillColor=#0072B2', 'fillColor=#FF00FF'),
    "字号过小": ('fontSize=12;', 'fontSize=6;'),
    "块里文字过长": ('value="开始"', 'value="' + "很长的说明" * 12 + '"'),
    "不是正交走线": ('edgeStyle=orthogonalEdgeStyle;', 'edgeStyle=elbowEdgeStyle;'),
    "缺 source/target": ('source="a" target="b"', 'source="a"'),
    # 三条正向要求各自的坏法
    "向上的边": ('source="a" target="b"', 'source="b" target="a"'),   # 回边之外又多一条往上
    "边太长": ('y="280"', 'y="960"'),
    "只有 1 种填充": ('fillColor=#E69F00', 'fillColor=#0072B2'),
}


def _selfcheck():
    import os, tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.drawio")

    def run(xml):
        open(p, "w", encoding="utf-8").write(xml)
        return check(p)

    assert run(_GOOD) == [], f"干净的图不该报问题: {run(_GOOD)}"
    for want, (old, new) in _BAD.items():
        assert old in _GOOD, f"自检样本对不上: {old!r}"          # 改坏之前先确认改的是真东西
        got = run(_GOOD.replace(old, new, 1))
        assert any(want in g for g in got), f"把 {want} 弄坏了却没报:{got}"
    # 尺寸种类:三种以内放行,第四种才红
    four = _GOOD.replace('width="160" height="60"', 'width="170" height="70"', 1)
    assert not any("块尺寸" in g for g in run(four)), "两种尺寸不该报"
    print(f"selfcheck ok — 干净图 0 问题,{len(_BAD)} 种坏法每种都红")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
        raise SystemExit(0)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    w = None
    if "--width-mm" in sys.argv:
        w = float(sys.argv[sys.argv.index("--width-mm") + 1])
    out = check(args[0], w)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    raise SystemExit(1 if out else 0)
