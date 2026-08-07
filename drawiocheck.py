"""drawiocheck: 检查一个 .drawio 流程图的七项科研制图规范,返回问题清单。

    python drawiocheck.py <图.drawio> [--width-mm 183]
    python drawiocheck.py --selfcheck        # 每条检查各造一张坏图,确认它真的红

输出 JSON 编码的问题列表(无问题为 `[]`);有问题时退出码 1。
不 assert、不改文件 —— 它只报告,修图是别人的事。

**这个判官管版面,不管逻辑,而且它的话没有逻辑那边大。** 逻辑归 verify_*.py(图 ↔ 源码
的 ast 对得上),那是硬门;版面这边是软的。两边冲突时**永远先满足逻辑** ——
线可以让人自己拖,画错了的逻辑没人拖得回来。
曾经违反过一次:为了压长宽比,把「距上限 4 步」那个分支和「cls 未知?」那个判定
从图里删掉了。**那是拿逻辑换版面,方向反了。** 版面挤不下就折栏、就换尺寸档,
不是砍内容。

**为什么是 drawio 而不是 mermaid。** mermaid 把版面交给渲染器:同一份源码,
换个渲染器就换个样子,文件里根本没有「这个块在哪、多大」。于是「画得丑」在
文件里无处可查,只能靠人看。drawio 的 `<mxGeometry x= y= width= height=>`
把版面写成了数字 —— 对不齐是坐标不是网格倍数,拥挤是包围盒相交,花是配色不在
白名单里。**审美断言不了,科研制图规范能;而规范全都落在这些数字上。**

七项检查(和 figcheck 是同一个家族,只是读的是 XML 不是 mpl 的 artist):
1. 块两两包围盒不相交 —— 挤在一起是「丑」最直接的形状;
2. 所有块的 x/y/width/height 都是 GRID 的整数倍 —— 对不齐是「丑」最常见的原因;
3. **宽度**档位数 <= MAX_SIZES —— 每个块都不一样宽 = 乱。注意只管宽:上一版管的是
   (宽,高) 整个尺寸,于是「不许乱」被执行成了「全都一样大」,而全都一样大 + 标签
   从 4 个字到 40 个字 = 文字必然戳出框。**一条规则制造了另一条规则要治的病。**
3b. 文字必须放得进**形状本身**,不是放进外接矩形 —— 菱形在离中心 dy 处只有
   `w·(1-2|dy|/h)` 那么宽,按外接矩形算,两行字的外侧那行一定露在外面。
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
9b. 折成多栏时:相邻栏之间**不能断**,而且**不许跳着栏连**(只能连到紧邻的下一栏)。
    换栏边天生又长又往上,9 和 10 都得放它过去,而豁免要自带上限,否则长边和上行边
    全能藏进"栏间"。**曾经写的是「换栏边正好 栏数-1 条」—— 那是错的**:折的是秩区间,
    跨过分界的前向边有几条取决于分叉,四条也正常。错的前提把一张对的图判成了红。
10. 每条边纵向不跨过两层、横向不超过半张图宽 —— 相连的块要挨着。用"层"和"图宽"作单位,
    不用绝对像素:三岔判定最外侧那条分支天生就在斜下方老远,那是分岔的样子,不是毛病;
12. 菱形必须有 >=2 条出边(下面长宽比那条是 13) —— 只有一条出边的菱形是把顺序执行的一步画成了问号;
11. 全图至少 MIN_FILLS 种填充色 —— 一种颜色 = 没有视觉层次,判定和普通步骤分不开。
"""
import json
import math
import re
import sys
import xml.etree.ElementTree as ET

GRID = 10                 # drawio 默认网格
MAX_SIZES = 3             # 允许几种不同的块**宽**(高度随文字行数走,不计种类)
PAD, LINE_H = 16, 16      # 文字左右留白 / 12px 字的行高
MIN_FONTSIZE = 10.0
MAX_LABEL = 42            # 一个块里最多几个字符(中文按 1 个算)
MM_PER_PX = 25.4 / 96     # drawio 的坐标单位是 CSS px,96dpi
MAX_BACK_EDGES = 1        # 向上走的边最多几条(流程图里那就是循环回边)
MIN_FILLS = 2             # 全图至少用到几种填充色
MAX_FOLD_EDGES = 2        # 每对相邻栏之间最多几条换栏边 —— 豁免的上限
COL_GAP_MIN = 100         # 块的 x 区间隔开这么多就算两栏(栏内块间距是 HGAP 量级)
MAX_ASPECT = 2.0          # 高/宽上限 —— 再高就没有版面放得下

# 哪些问题**只有排版工具改得了**。画图的那个只交 JSON(块、边、kind、标签),坐标不归它;
# 冲着它报坐标的毛病,它做不了任何有效动作,只会掉头去读判官的源码找原因(实测:
# 读了六七遍 drawiocheck.py、派了五个子 agent,一步没往前走)。
# **一条对方无法执行的意见,不会让他改对,只会让他去查你为什么这么说。**
_LAYOUT_OWNED = ("块重叠", "没对齐网格", "块宽有", "不是正交走线", "向上的边",
                 "边太长", "断栏", "跳着栏连", "换栏边", "往下一栏拉了", "长宽比", "图宽")

# Wong 2011 色盲安全配色 + 中性色。大小写不敏感。
PALETTE = {
    "#e69f00", "#56b4e9", "#009e73", "#f0e442",
    "#0072b2", "#d55e00", "#cc79a7",
    "none", "default", "#ffffff", "#f5f5f5", "#ffffff00",
}

def _text_w(s):
    """12px 下的文字宽度。中日韩全宽 12,拉丁 6.6 —— 粗但方向对。"""
    return sum(12.0 if ord(ch) > 0x2E80 else 6.6 for ch in s)


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

    # 3. 宽度档位数。只管宽 —— 见文件头那条注释,管死整个尺寸会逼出文字出框。
    widths = {g[2] for g in (geos[i] for i in ids)}
    if len(widths) > MAX_SIZES:
        problems.append(f"块宽有 {len(widths)} 种(上限 {MAX_SIZES}): {sorted(widths)}")

    # 4. 配色白名单  5. 字号  7a. 文字长度
    for i, c in boxes.items():
        if i not in ids:            # 没有 mxGeometry / 宽高非数字的块:第 6 条会报它,
            continue                # 但这里先解包就直接 TypeError,判官变成 traceback
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
        # 3b. 文字放不放得进这个形状(菱形按腰线收窄算,不按外接矩形)
        lab = _label(c)
        _x, _y, bw, bh = geos[i]
        if bw and bh and lab:
            n_ln = max(1, math.ceil(_text_w(lab) / max(bw - PAD, 1)))
            avail = bw * (1 - (n_ln - 1) * LINE_H / bh) if "rhombus" in (c.get("style") or "") else bw
            if n_ln * LINE_H > bh - PAD:
                problems.append(f"文字竖着放不下: {lab[:24]!r} 要 {n_ln} 行 = {n_ln * LINE_H}px,"
                                f"块只有 {bh:.0f}px 高 —— 把框加高或者把说明挪出去")
            if _text_w(lab) / n_ln > avail - PAD:
                problems.append(f"文字放不进形状: {lab[:24]!r} 每行要 {_text_w(lab) / n_ln:.0f}px,"
                                f"这个形状只给得出 {avail - PAD:.0f}px —— 把框放宽或者把说明挪出去")
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

    # 分栏:长流程在论文里本来就折成两三栏排,栏与栏之间靠留白分开。
    # 只有这一个信号可用(文件里没有"栏"这个东西),而它够用 —— 分栏本来就是靠留白看出来的:
    # 栏内相邻块隔几十 px,栏间是刻意留出的空白。
    def _columns():
        spans = sorted((g[0], g[0] + g[2]) for g in geos.values() if g)
        if not spans:
            return []
        cols, cur = [], list(spans[0])
        for lo, hi in spans[1:]:
            if lo - cur[1] > COL_GAP_MIN:
                cols.append(tuple(cur))
                cur = [lo, hi]
            else:
                cur[1] = max(cur[1], hi)
        cols.append(tuple(cur))
        return cols

    cols = _columns()
    if len(cols) > 1:
        _top = min(g[1] for g in geos.values() if g)
        _fig_h = max(g[1] + g[3] for g in geos.values() if g) - _top or 1
        def _tall(lo, hi):                       # 这一栏纵向铺开了吗
            ys = [(g[1], g[1] + g[3]) for g in geos.values() if g and lo <= g[0] + g[2] / 2 <= hi]
            return ys and (max(b for _, b in ys) - min(a for a, _ in ys)) >= 0.5 * _fig_h
        if not all(_tall(lo, hi) for lo, hi in cols):
            cols = [(min(g[0] for g in geos.values() if g),
                     max(g[0] + g[2] for g in geos.values() if g))]   # 不是折栏,当一栏查

    def _col_of(i):
        x = geos[i][0] + geos[i][2] / 2
        return next((k for k, (lo, hi) in enumerate(cols) if lo <= x <= hi), -1)

    # 上限必须跟着块的大小走,不能是个绝对数。块宽按文字量变成 200~360 之后,
    # 同一行**相邻**两块的中心距本身就有 420px —— 一个 400 的绝对上限等于在说
    # 「相邻也算太远」,这条规则就永远满足不了了。规则想说的是「别隔着老远连」,
    # 那它的单位就该是块,不是像素。
    _bw = max((g[2] for g in geos.values() if g), default=0)
    _bh = max((g[3] for g in geos.values() if g), default=0)
    _fig_w = max((g[0] + g[2] for g in geos.values() if g), default=1)         - min((g[0] for g in geos.values() if g), default=0) or 1

    back, longest = 0, []
    fold_pairs, jumps = {}, []
    for e in edges:
        s, t = e.get("source"), e.get("target")
        if s not in geos or t not in geos or not geos[s] or not geos[t]:
            continue
        (x1, y1), (x2, y2) = _center(s), _center(t)
        cs, ct = _col_of(s), _col_of(t)
        if ct == cs + 1:                      # 换栏:往右跨一栏。天生又长又往上,两条都不算它头上
            fold_pairs.setdefault(cs, []).append(e.get("id"))
            continue
        if ct > cs + 1:                       # 跨了不止一栏 —— 这不是折栏,这是乱连
            jumps.append(f"{_label(boxes[s])!r}→{_label(boxes[t])!r} 跨了 {ct - cs} 栏")
            continue
        if y2 < y1 - 1:                       # 终点在起点上方 = 往回走
            back += 1
            continue                          # 回边天生要跨过整张图,长度不算它头上
        # 「太长」的真正含义是**跨了不止一层**,或者**横穿了半张图**。
        # 原来用欧氏距离 + 一个绝对上限,单位就错了:一个三岔判定,它最外侧那个分支
        # 天生就在斜下方老远 —— 那不是"隔着老远连",那是分岔本来的样子。
        # 换成两个各自有意义的量:纵向跨度按"层"算,横向跨度按"图宽"算。
        if abs(y2 - y1) > 2 * (_bh + 60):
            longest.append(f"{_label(boxes[s])!r}→{_label(boxes[t])!r} 纵向跨了 {abs(y2 - y1):.0f}px(超过两层)")
        # 横向的阈值要有个以块为单位的下限:一张只有两块宽的小图里,挪到隔壁那一格
        # 本身就占掉半张图宽 —— 没有这个下限,小图永远过不了。
        elif abs(x2 - x1) > max(0.5 * _fig_w, 1.5 * _bw):
            longest.append(f"{_label(boxes[s])!r}→{_label(boxes[t])!r} 横穿了 {abs(x2 - x1) / _fig_w:.0%} 张图宽")
    # 9b. 换栏边必须正好 栏数-1 条。**豁免必须自带一个上限,否则豁免自己就是逃逸口** ——
    # 不然"往右跨一栏就免检"等于把长边和上行边随便藏进栏间。少一条 = 有一栏接不上,
    # 多一条 = 借着分栏在栏间乱连。
    # 原来这里写的是「换栏边正好 栏数-1 条」。**那条规则建立在一个错误的前提上** ——
    # 我以为折栏只会切出一条接续边。不对:折的是**秩区间**,而跨过那条分界的前向边
    # 有几条完全取决于图的分叉,四条五条都正常。前提错了,规则就把一张对的图判红了。
    # 换成两条站得住的:相邻栏之间不能断,而且不许跳着栏连。
    if len(cols) > 1:
        if missing := [k for k in range(len(cols) - 1) if not fold_pairs.get(k)]:
            problems.append(f"第 {[k + 1 for k in missing]} 栏和下一栏之间没有连接 —— 断栏了,读者跳不过去")
        if jumps:
            problems.append(f"有 {len(jumps)} 条边跳着栏连(跨了不止一栏): {'; '.join(jumps[:3])}"
                            f" —— 换栏只能连到紧邻的下一栏")
        # **上限在这儿。** 上一版写了「豁免要自带上限」的注释,配的却是「至少一条 + 不许
        # 跳栏」—— 两条都只管下界。审计当场造了一张两栏图,挂十条横穿全图的上行边,
        # 判官返回 []:9 和 10 被整个绕过去了。写了注释不等于加了上限。
        if over := [k for k, v in fold_pairs.items() if len(v) > MAX_FOLD_EDGES]:
            problems.append(f"第 {[k + 1 for k in over]} 栏往下一栏拉了 "
                            f"{max(len(fold_pairs[k]) for k in over)} 条线(上限 {MAX_FOLD_EDGES})"
                            f" —— 换栏边免检长度和上行,这个免检不能变成藏线的地方")
    # 9. 往上走的边就是回边。流程图只该有回边一条往上;更多说明块的摆放没跟着流程走。
    if back > MAX_BACK_EDGES:
        problems.append(f"向上的边有 {back} 条(上限 {MAX_BACK_EDGES})—— "
                        f"块的上下顺序没跟着流程走,读者得来回跳")
    # 10. 有边相连的块必须挨着。两列格子 + 长线对穿正是这条抓的。
    if longest:
        problems.append(f"边太长({len(longest)} 条): {'; '.join(longest[:3])}"
                        f" —— 相连的块要挨着放,别让线横跨整张图")
    # 12. 菱形 = 判定,判定必须有分支。只有一条出边的菱形是把「顺序执行的一步」
    # 画成了问号 —— 读者会停下来找另一条路,而根本没有。形状是有语义的,不是装饰。
    one_way = [_label(boxes[i]) for i in boxes
               if "rhombus" in (boxes[i].get("style") or "")
               and sum(1 for e in edges if e.get("source") == i) < 2]
    if one_way:
        problems.append(f"菱形只有一条出边({len(one_way)} 个): {'、'.join(x[:18] for x in one_way[:3])}"
                        f" —— 菱形是判定,不分支就该画成方框")
    # 11. 全图一种填充 = 没有视觉层次。判定、普通步骤、终止至少要能一眼分开。
    fills = {(_style(c).get("fillColor") or "none").strip().lower() for c in boxes.values()}
    if len(fills) < MIN_FILLS:
        problems.append(f"全图只有 {len(fills)} 种填充({fills})—— "
                        f"至少 {MIN_FILLS} 种,判定/普通步骤/终止要能一眼分开")
    # 13. 长宽比。排版工具把块摆整齐了,却摆成了一条 25 格的直筒:700×2280,1:3.3。
    # 双栏 183mm 宽就要 600mm 高,没有任何版面放得下。**排版救不了块太多** ——
    # 这条逼的是删块,而删块只能由懂内容的那一方来做。
    if ids:
        w = max(geos[i][0] + geos[i][2] for i in ids) - min(geos[i][0] for i in ids)
        h = max(geos[i][1] + geos[i][3] for i in ids) - min(geos[i][1] for i in ids)
        if w > 0 and h / w > MAX_ASPECT:
            problems.append(f"长宽比 1:{h / w:.1f} 超过 1:{MAX_ASPECT}({w:.0f}×{h:.0f}px)"
                            f" —— 版面放不下,砍掉次要的块或把并行的步骤并成一层")

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
  <mxGeometry x="40" y="40" width="200" height="60" as="geometry"/></mxCell>
<mxCell id="b" value="干活" style="rounded=1;fillColor=#0072B2;fontSize=12;" vertex="1" parent="1">
  <mxGeometry x="40" y="160" width="200" height="60" as="geometry"/></mxCell>
<mxCell id="c" value="结束" style="rounded=1;fillColor=#E69F00;fontSize=12;" vertex="1" parent="1">
  <mxGeometry x="40" y="280" width="200" height="60" as="geometry"/></mxCell>
<mxCell id="e1" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="a" target="b">
  <mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e2" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="b" target="c">
  <mxGeometry relative="1" as="geometry"/></mxCell>
<mxCell id="e3" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" source="c" target="a">
  <mxGeometry relative="1" as="geometry"/></mxCell>
</root></mxGraphModel></diagram></mxfile>"""
# 三个块一条链 + 一条回边 —— 这就是「向上的边只该有回边那一条」的合法形状,
# 干净样本必须长成真流程图的样子,否则正向那三条根本没有能通过的基准。

# 三栏样本:左 a→b,换栏 b→c,中 c→d,换栏 d→e,右 e→f,回边 f→a。栏间留白 140px > COL_GAP_MIN。
# 必须是三栏 —— 两栏测不出「跳着栏连」,而那正是豁免的上限所在。
def _box(i, x, y, fill):
    return (f'<mxCell id="{i}" value="{i}" style="rounded=1;fillColor={fill};fontSize=12;" '
            f'vertex="1" parent="1"><mxGeometry x="{x}" y="{y}" width="200" height="60" '
            f'as="geometry"/></mxCell>')


def _edge(i, s, t):
    return (f'<mxCell id="{i}" style="edgeStyle=orthogonalEdgeStyle;" edge="1" parent="1" '
            f'source="{s}" target="{t}"><mxGeometry relative="1" as="geometry"/></mxCell>')


_FOLD_EDGE = _edge("ef2", "d", "e")            # 中栏→右栏那条接续
_JUMP_EDGE = _edge("ez", "a", "f") + "</root>"  # 左栏直接跳到右栏 —— 跳级

_TWOCOL = ("<mxfile><diagram><mxGraphModel><root>"
           '<mxCell id="0"/><mxCell id="1" parent="0"/>'
           + _box("a", 40, 40, "#0072B2") + _box("b", 40, 160, "#0072B2")
           + _box("c", 380, 40, "#E69F00") + _box("d", 380, 160, "#E69F00")
           + _box("e", 720, 40, "#009E73") + _box("f", 720, 160, "#009E73")
           + _edge("e1", "a", "b") + _edge("e2", "b", "c") + _edge("e3", "c", "d")
           + _FOLD_EDGE
           + _edge("e4", "e", "f") + _edge("e5", "f", "a")
           + "</root></mxGraphModel></diagram></mxfile>")

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
    "长宽比": ('y="280"', 'y="1280"'),
    "菱形只有一条出边": ('id="b" value="干活" style="rounded=1;',
                          'id="b" value="干活" style="rhombus;'),
    # 文字放不进形状:同一段字,换成菱形就该红(圆角矩形放得下,菱形放不下)
    "文字放不进形状": ('value="干活" style="rounded=1;',
                       'value="check_permission(state, cls, name, args)" style="rhombus;'),
}


def _selfcheck():
    import os, tempfile
    d = tempfile.mkdtemp()
    p = os.path.join(d, "t.drawio")

    def run(xml):
        open(p, "w", encoding="utf-8").write(xml)
        return check(p)

    assert run(_GOOD) == [], f"干净的图不该报问题: {run(_GOOD)}"
    # 9b 在 _GOOD 上根本跑不到 —— 它只有一栏。分栏的检查得有分栏的样本,而且必须是**三栏**:
    # 两栏测不出「跳着栏连」,而那正是这条豁免的上限所在。两边都要试。
    assert run(_TWOCOL) == [], f"干净的三栏图不该报问题: {run(_TWOCOL)}"
    assert _FOLD_EDGE in _TWOCOL, "自检样本对不上换栏边"
    assert any("断栏" in g for g in run(_TWOCOL.replace(_FOLD_EDGE, "", 1))), "删掉换栏边却没报断栏"
    assert any("跳着栏连" in g for g in run(_TWOCOL.replace("</root>", _JUMP_EDGE, 1))), \
        "跨两栏直连却没报"
    for want, (old, new) in _BAD.items():
        assert old in _GOOD, f"自检样本对不上: {old!r}"          # 改坏之前先确认改的是真东西
        got = run(_GOOD.replace(old, new, 1))
        assert any(want in g for g in got), f"把 {want} 弄坏了却没报:{got}"
    # 尺寸种类:三种以内放行,第四种才红
    assert 'width="200"' in _GOOD, "自检样本对不上块宽"     # 先确认改的是真东西
    four = _GOOD.replace('width="200"', 'width="210"', 1)
    assert not any("块宽" in g for g in run(four)), "两种宽度不该报"
    many = (_TWOCOL.replace('width="200"', 'width="210"', 1)
                   .replace('width="200"', 'width="220"', 1)
                   .replace('width="200"', 'width="230"', 1))     # 四种宽 > MAX_SIZES
    assert any("块宽" in g for g in run(many)), "四种宽度该报却没报"
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
    # 分两拨报,**因为它们要对不同的人说话**。
    #
    # 画图的那个只交一份 JSON —— 块、边、kind、标签。坐标是 drawio_layout.py 算的,
    # 它一个字都改不了。而上一轮判官冲着它喊「边太长:纵向跨了 950px」,那是坐标的事:
    # 于是它只好反过来去读判官的源码找原因,读了六七遍、派了五个子 agent,一步没往前走。
    # **一条它无法执行的意见,不会让它改对,只会让它去查你为什么这么说。**
    #
    # 所以退出码只看「你能改的」那一拨。排版那拨照样打印 —— 给人看,不是给它看。
    yours = [p for p in out if not any(k in p for k in _LAYOUT_OWNED)]
    theirs = [p for p in out if p not in yours]
    print(json.dumps(yours, ensure_ascii=False, indent=2))
    if theirs:
        print("\n[排版工具的事,改 JSON 没用,不用管]", file=sys.stderr)
        for p in theirs:
            print("  · " + p, file=sys.stderr)
    raise SystemExit(1 if yours else 0)
