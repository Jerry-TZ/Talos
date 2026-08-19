"""界面层那两个文件的判据 —— 它们各自靠一句**承诺**活着,而那句承诺没人查。

`console_ui.py` 开头写着「The CORE (agent.py) never prints or reads input itself」,
`talos_watch.py` 开头写着「只看,不碰:没有输入框、不写任何文件、不调 agent 的任何函数」。
两句都不是描述,是**约束**:前者撑着「换皮只改一个文件」,也撑着「权限确认框是唯一的
输入路径」;后者的理由写在它自己的注释里 —— 权限确认那条路径上审出过四个 bug,
再实现一套输入界面等于给那四个 bug 第二次机会。

第三十四节那一天,五个真问题里四个是「代码说对没有」。**这三条查的就是那个** ——
不是功能对不对,是文件开头那句话还算不算数。

全部用 `ast` 静态读源码,**一个都不 import**:`console_ui` 要 `rich`,`talos_watch` 要
`tkinter`,任何一个装不上,importorskip 就把判据变成「没被执行」—— 那是第一种形状,
而这个文件正是为了防它写的。副作用是查得更准:拿正则去捞 `ui.` 开头的调用,会把注释里
写的 `console_ui.py` 数成一个叫 `py` 的界面函数(真发生了),ast 不会。
"""
import ast
import inspect
import io
import os

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tree(name):
    return ast.parse(io.open(os.path.join(HOME, name), encoding="utf-8").read(), name)


def _top(node):
    """这个顶层节点对外的名字 —— 嵌套函数算在它外面那层头上。"""
    return getattr(node, "name", "<模块级>")


# 允许自己 print/input 的地方,连理由一起记。**逐个记,不是记一条规则** ——
# 「界面前」这种说法太软,软到任何新增的 print 都能自称属于它。
_TALKS_DIRECTLY = {
    "_load_dotenv": "跑在 stdout 切成 UTF-8 之前,而且 rich 还没 import;走界面就是循环依赖",
    "approve_tools": "`--approve-tools` 子命令,repl 没起来,ui 是 None;confirm= 可注入",
    "_selfcheck": "`--selfcheck` 刻意不带依赖 —— 它要能在没装 rich 的机器上跑",
    "<模块级>": "`if __name__ == \"__main__\"` 里的几个 CLI 分支,同样在 repl 之前",
}


def test_the_core_never_grows_its_own_way_to_talk_to_you():
    """`agent.py` 不许在 agent 循环里自己 print / input。

    文档里那句「never prints or reads input itself」**字面上是假的** —— 今天 agent.py 里
    有 4 处直接 print/input。它们全是对的:都在 repl 起来之前(bootstrap 和 CLI 入口),
    那时候 `ui` 还是 `None`,而且其中两处的注释写明了「走界面反而会崩」。真正的规矩比
    文档那句更严也更可查:**能自己说话的位置是一张列出来的名单,循环里一个都不许有。**

    为什么是「枚举全部、逐个归档」而不是「枚举禁止的」:禁止清单永远落后一步(第二十九节),
    而这里合法的那一侧是**闭集** —— repl 之前的入口就那么几个。新加一个 print,不管加在
    哪儿,都得先在这张表里给它写个理由,也就是**必须有人看它一眼**。这才是这条判据要买的
    东西:不是拦住 print,是拦住「没人看的 print」。

    循环里混进一个 print 会赔掉两样:换皮不再是改一个文件(网页版 UI 那些字直接掉进
    stdout),以及更要紧的 —— 一个不经过 `ui.preview` / `ui.ask` 的 `input()` 就是一条
    绕过权限确认框的输入路径,而那个框是整个安全边界。"""
    found = {}
    for node in _tree("agent.py").body:
        names = sorted({n.func.id for n in ast.walk(node)
                        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id in ("print", "input")})
        if names:
            found.setdefault(_top(node), set()).update(names)

    strayed = {k: sorted(v) for k, v in found.items() if k not in _TALKS_DIRECTLY}
    assert not strayed, (
        f"这些地方绕开界面自己说话了:{strayed}\n"
        "如果它跑在 repl 之前(ui 还是 None),把它加进 _TALKS_DIRECTLY 并写上理由;\n"
        "如果它在 agent 循环里,改成调 ui.note / ui.answer / ui.ask —— "
        "循环里的 input() 是一条绕过权限确认框的输入路径,而那个框是整个安全边界。")

    gone = sorted(set(_TALKS_DIRECTLY) - set(found))
    assert not gone, (
        f"这几处的 print/input 没了,名单该跟着收:{gone}\n"
        "留着一条不再对应任何代码的豁免,下一个撞上同名函数的人就白拿一张通行证。")


def _sig(fn: ast.FunctionDef) -> inspect.Signature:
    """从 ast 拼出签名 —— 只为判断「这么调能不能对上」,默认值具体是什么无所谓。"""
    P = inspect.Parameter
    a = fn.args
    pos = a.posonlyargs + a.args
    pad = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    params = [P(x.arg, P.POSITIONAL_ONLY if i < len(a.posonlyargs) else P.POSITIONAL_OR_KEYWORD,
                default=(P.empty if d is None else 0))
              for i, (x, d) in enumerate(zip(pos, pad))]
    if a.vararg:
        params.append(P(a.vararg.arg, P.VAR_POSITIONAL))
    params += [P(x.arg, P.KEYWORD_ONLY, default=(P.empty if d is None else 0))
               for x, d in zip(a.kwonlyargs, a.kw_defaults)]
    if a.kwarg:
        params.append(P(a.kwarg.arg, P.VAR_KEYWORD))
    return inspect.Signature(params)


def test_every_screen_the_core_asks_for_actually_exists():
    """`agent.py` 调的每个 `ui.X` 都得在 `console_ui.py` 里,而且**这么调得能对上**。

    两个文件之间只有一份靠函数名对上的契约,查它的东西一个都没有 —— 昨天往里加
    `ask_again` 的时候也没有。少一个函数或者签名改了,**不会在启动时报错**,会在真跑到
    那条路径的时候炸;而 `ui.ask_again` 只在权限确认框里你敲了看不懂的东西时才走到,
    `ui.denied` 只在拒绝之后才走到 —— 越是这种路径,越是没人替你先撞一遍。

    连签名一起对,是因为只对名字太软:`ask_again(typed)` 改成 `ask_again()` 名字还在,
    而调用方照旧传一个参数,照旧到运行时才炸。"""
    used = {}
    for n in ast.walk(_tree("agent.py")):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "ui"):
            used.setdefault(n.func.attr, set()).add(
                (len(n.args), tuple(sorted(k.arg for k in n.keywords if k.arg)),
                 any(k.arg is None for k in n.keywords) or any(
                     isinstance(x, ast.Starred) for x in n.args)))
    assert used, "一个 ui.X 都没找到 —— 这条判据自己瞎了(是不是 agent.py 改了 import 的别名?)"

    have = {f.name: f for f in _tree("console_ui.py").body
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = sorted(set(used) - set(have))
    assert not missing, (
        f"agent.py 调了这些界面函数,console_ui.py 里没有:{missing}\n"
        "少一个不会在启动时报错,会在真跑到那条路径的时候炸。")

    bad = []
    for name, shapes in sorted(used.items()):
        for npos, kw, unpacked in sorted(shapes):
            if unpacked:
                continue                     # 带 *args/**kwargs 的调用,静态对不出来
            try:
                _sig(have[name]).bind(*([0] * npos), **{k: 0 for k in kw})
            except TypeError as e:
                bad.append(f"ui.{name}({npos} 个位置参数, kw={list(kw)}) 对不上 "
                           f"{name}{_sig(have[name])} —— {e}")
    assert not bad, "调用方式和界面函数的签名对不上:\n  " + "\n  ".join(bad)


def test_the_watch_window_cannot_write_or_ask():
    """`talos_watch.py` 只读:不写文件、不读输入、不碰 agent 的任何函数。

    它自己的注释里写了为什么(权限确认那条路径上审出过四个 bug,再实现一套输入界面
    等于给那四个 bug 第二次机会),但那只是**说**了。这条是查。

    查法从 import 开始,因为那是唯一收敛的入口:「不写文件」这件事的**写法是开集** ——
    `open(w)`、`pathlib.write_text`、`shutil.copy`、`os.replace`、`subprocess` 里再起一个
    python……禁止清单列不完(第二十九节:枚举合法的落后一步,而这里连非法的都枚举不完)。
    但它**能 import 什么**是闭集,今天是 5 个,里面没有 pathlib、没有 shutil、没有
    subprocess,也没有 agent/recall/session。把这 5 个钉死之后,剩下的写文件通道就只剩
    `open()` 和 `os.*` 两条,而这两条各自也是闭集,逐个查得完。

    所以这条判据的形状是:**先把可达面锁小,再在小面上查干净。** 加一个 import 就红 ——
    那正是要的:新开一条通道,得有人看一眼它通向哪儿。"""
    tree = _tree("talos_watch.py")

    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported |= {(n.module or "") + "." + a.name for a in n.names}
    assert imported == {"collections", "json", "os", "tkinter", "tkinter.ttk"}, (
        f"观察窗的 import 变了:{sorted(imported)}\n"
        "这张表是「只读」这句话唯一的支点 —— 锁住它才谈得上查写操作。\n"
        "要加,先回答:新加的这个能不能写文件、能不能读输入、会不会把 agent 拉进来。\n"
        "(agent / recall / session / console_ui 一律不行:观察窗只读 .talos/ 下的 jsonl。)")

    writes = [(n.lineno, len(n.args)) for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "open" and (len(n.args) > 1 or any(
                  k.arg == "mode" for k in n.keywords))]
    assert not writes, (
        f"这些 open() 带了 mode —— 观察窗不许写任何文件,第 {[l for l, _ in writes]} 行")

    asks = sorted({n.func.id for n in ast.walk(tree)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "input"}
                  | {"Entry" for n in ast.walk(tree)
                     if isinstance(n, ast.Attribute) and n.attr == "Entry"})
    assert not asks, (
        f"观察窗长出输入口了:{asks}\n"
        "权限确认框那条路径上审出过四个 bug,而全部测试都是针对 console 那一套写的 ——"
        "再实现一套输入界面,等于给那四个 bug 第二次发生的机会。想换输入界面改 console_ui.py。")

    reads_only = {"listdir", "path", "getmtime", "isdir", "exists",
                  "join", "basename", "dirname", "abspath"}
    touched = {n.attr for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
               and n.value.id == "os"}
    touched |= {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                and isinstance(n.value.value, ast.Name) and n.value.value.id == "os"}
    assert touched <= reads_only, (
        f"观察窗用了会动文件系统的 os 函数:{sorted(touched - reads_only)}")
