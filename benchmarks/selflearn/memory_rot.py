#!/usr/bin/env python
"""复盘写进 memory.md 的行里,有多少条今天已经不成立了。

**为什么要有这个脚本。** 第四十八节量出「一条好记忆值 +50 个百分点」,但那三行是我
挑过的。要回答「复盘写出来的行有多大比例有用」,得先看**它们还对不对** ——
一条断言错误坐标的记忆不是没用,是**负资产**:`retrieve()` 把 memory.md
**整份**放进系统提示词,它每一轮都在场,每一轮都在说一句假话。

**只查机械可核对的那一类,不做判断题。** 「这条规则通不通用」是判断,单评审的判断不是
测量(这个项目对标注数据要求两个独立评审)。而「`check_permission` 是不是在第 1179 行」
不需要任何判断 —— 去文件里数一下就有答案。所以这个脚本只查两种声明:

  ① **行号**:`xxx(约1179行)` / `约1200-1205、1210-1213行`
  ② **路径**:`talos-public\\tools\\figcheck.py`、`workspace\\test_figcheck.py`

**数少了不数多。** `约1200-1205、1210-1213行` 这种写法里,「行」只跟在最后一段后面,
所以前一段被漏掉 —— 报出来的声明数比实际少。漏掉的那几个跟报出来的一起失效,
所以这个偏差只会让结论**更保守**,不会造出一条假发现。宁可少报。

行号那一类的判法是「宽进」:把这条记忆里提到的**所有**标识符都拿去比,只要有**任何一个**
落在声明行号的 ±TOL 内就算成立。这样不用解析「哪个号对应哪个名字」——
一个只为两条记忆写的括号解析器,是给自己找的第二个 bug。

    .venv/Scripts/python.exe benchmarks/selflearn/memory_rot.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

TOL = 12          # 「约」给多少宽容。写下时四条全是 0 偏差,所以这个数松紧都不影响结论

_LINENO = re.compile(r"(?:约|第)?\s*(\d{3,5})\s*(?:-\s*(\d{3,5}))?\s*行")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]{2,})?")
_PATH = re.compile(r"[A-Za-z0-9_.\-]+(?:[\\/][A-Za-z0-9_.\-]+)+\.(?:py|md|json|txt|bat|log)")
_PYFILE = re.compile(r"\b([A-Za-z0-9_\-]+\.py)\b")


def _src(name: str):
    p = os.path.join(ROOT, name)
    if not os.path.exists(p):
        return None
    return open(p, encoding="utf-8", errors="replace").read().splitlines()


def check_line_claims(text: str) -> list:
    """-> [(声明的行号, 名字, 文件, 今天实际在第几行, 成立吗), ...]

    **候选名字只取紧贴括号前面那一小段。** 第一版拿整行的标识符去比,而这两条记忆各提了
    十几个名字 —— 在一个 3124 行的文件里,任何一个数字附近总能找到其中之一,
    于是它把两条**确实已经失效**的声明judge成了成立。手查的结果对不上才发现。
    **一条没有区分力的判据,和没有判据是一回事,而它看起来像有。**"""
    lines_of = {}
    out = []
    for m in _LINENO.finditer(text):
        nums = [int(g) for g in m.groups() if g]
        head = text[max(0, m.start() - 40):m.start()]      # 括号前 40 字,不再往前
        names = [i for i in _IDENT.findall(head) if not i.endswith(".py")][-3:]
        f = _PYFILE.search(text)
        fname = f.group(1) if f else "agent.py"            # 没点名就默认 agent.py
        if fname not in lines_of:
            lines_of[fname] = _src(fname)
        lines = lines_of[fname]
        for n in nums:
            hit = (None, None)
            for d in names:
                base = d.split(".")[-1]                    # `ui.ask` → 在源码里找 `ask(`
                at = [i for i, ln in enumerate(lines or [], 1)
                      if re.search(r"\b%s\s*\(" % re.escape(base), ln.split("#")[0])]
                if at:
                    near = min(at, key=lambda i: abs(i - n))
                    if hit[1] is None or abs(near - n) < abs(hit[1] - n):
                        hit = (d, near)
            out.append((n, hit[0], fname, hit[1], hit[1] is not None and abs(hit[1] - n) <= TOL))
    return out


def check_paths(text: str) -> list:
    """-> [(路径, 还在吗), ...]。路径按仓库根解;`talos-public\\x` 里那层前缀脱掉。

    占位符(`xxx`、`<工具名>`)和 `..\\` 开头的相对路径**不查**:前者本来就不是一个路径,
    后者要知道当时的 cwd 才解得开。查不了就别报,报错等于给自己造一条假发现。"""
    out = []
    for p in set(_PATH.findall(text)):
        rel = p.replace("\\", "/")
        if rel.startswith("..") or re.search(r"\bxxx\b|<|\*", rel):
            continue
        for prefix in ("talos-public/", "./"):
            if rel.startswith(prefix):
                rel = rel[len(prefix):]
        out.append((p, os.path.exists(os.path.join(ROOT, rel))))
    return out


def main() -> int:
    import recall as R
    rows = []
    for ln in open(R.MEMORY_FILE, encoding="utf-8").read().splitlines():
        s = ln.strip().lstrip("-*# ").strip()
        if len(s) < 4:
            continue
        body, src, born = R.strip_tag(s)
        if src == "user":                       # 手写的不查 —— 这个脚本问的是「复盘写得怎么样」
            continue
        rows.append((born, body))

    n_claims = n_bad = n_lines_with_claims = 0
    print("复盘写进 memory.md 的行:%d 条\n" % len(rows))
    for born, body in rows:
        lc, pc = check_line_claims(body), check_paths(body)
        if not lc and not pc:
            continue
        n_lines_with_claims += 1
        print("[%s] %s%s" % (born or "?", body[:64], "…" if len(body) > 64 else ""))
        for n, name, fname, actual, ok in lc:
            n_claims += 1
            n_bad += not ok
            print("   行号 声明 %s 在 %s:%d,今天在 %s   %s"
                  % (name or "?", fname, n, actual if actual else "找不到",
                     "成立" if ok else "**已失效,差 %d 行**" % abs((actual or 0) - n)))
        for p, ok in pc:
            n_claims += 1
            n_bad += not ok
            print("   路径 %-40s %s" % (p, "还在" if ok else "**没了**"))
        print()

    print("%d 条复盘行里,%d 条带可核对的坐标(行号或路径)" % (len(rows), n_lines_with_claims))
    print("机械可核对的声明 %d 条,今天不成立的 %d 条(%.0f%%)"
          % (n_claims, n_bad, 100.0 * n_bad / max(1, n_claims)))
    print("注:`retrieve()` 把 memory.md 整份放进系统提示词 —— 这 %d 条**每一轮都在场**。"
          % n_bad)
    # 「这条规则通不通用」是判断题,这个脚本**不答**:单评审的判断不是测量
    # (这个项目对标注数据要求两个独立评审,见 benchmarks/recall/)。
    # 它只答「这句话今天还成立吗」—— 那个不需要判断,去文件里数一下就有答案。
    return 0


if __name__ == "__main__":
    sys.exit(main())
