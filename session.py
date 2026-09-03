"""
session.py — Talos 的本地会话存储 (仿 Claude Code:一个会话一个 JSONL,存在项目里)。

文件名是 `<时间戳id>__<标题slug>.jsonl`,标题由第一句 prompt 自动生成,所以
`.talos/sessions/` 里一眼能看出每个会话在聊什么。这是可替换的存储层 —— 想换 SQLite
只改这个文件,`agent.py` 不用动。
"""

from __future__ import annotations

import glob
import json
import os
import re
import time
import warnings

HOME = os.path.realpath(os.environ.get("TALOS_HOME") or os.path.dirname(os.path.abspath(__file__)))
SESS_DIR = os.path.join(HOME, ".talos", "sessions")   # sessions follow the agent, not the cwd
BATCH = "batch-"     # 跑批(`once()`)会话的 slug 前缀 —— 见 `list_sessions` 的注释

def _slug(text: str, n: int = 24) -> str:
    """把第一句 prompt 压成文件名安全的短标题(保留中英数字)。"""
    s = re.sub(r"[^\w一-鿿]+", "-", (text or "").strip())[:n].strip("-")
    return s or "chat"

def _first_user(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""

def _parse_name(path: str) -> tuple[str, str]:
    """'<id>__<slug>.jsonl' -> (id, slug);  旧格式 '<id>.jsonl' -> (id, '')。"""
    base = os.path.splitext(os.path.basename(path))[0]
    sid, _sep, slug = base.partition("__")
    return sid, slug

def _claim(sid: str) -> str:
    """`new()` 发号时抢的占位符。**故意不叫 .jsonl** —— 这里所有的 glob 都只捡 `*.jsonl`
    (`_path_for` / `list_sessions` / `recall.py`),所以占位符不会变成一条空会话。"""
    return os.path.join(SESS_DIR, sid + ".claim")

class Session:
    def __init__(self, sid: str, slug: str = "", batch: bool = False):
        self.sid = sid
        self.slug = slug
        self.batch = batch          # 只影响首次 save 起的名字;之后身份就写在文件名里了

    @classmethod
    def new(cls, batch: bool = False) -> "Session":
        """秒级时间戳**不保证唯一**,而这里的 id 是会话的全部身份。

        同一秒起两个会话就是同一个 sid。两个文件都写得出来(slug 不同),
        `list_sessions()` 里两条都看得见 —— 但 `_path_for` 是前缀匹配、只返回排序第一个,
        于是 **`/resume` 谁都只能回到第一个,第二个会话永远打不开**。它没丢,是够不着。
        撞了就往后加序号;顺带 `_path_for` 改成优先精确匹配,否则 `-2` 会排在 `__` 前面,
        拿完整 sid 去找反而找到那个带序号的。

        但那个检查**只看已落盘的文件**,而 `new()` 从不落盘:两个都还没 `save()` 的会话
        照样是同一个 sid(并发起会话、或者起了半天不说话都长这样),save 完还是只能打开
        第一个。改法是**发号的那一刻就占住号** —— `open(..., "x")`(= O_CREAT|O_EXCL,原子)
        抢一个 `<sid>.claim`,同进程的第二次 `new()` 和另一个进程的 `new()` 被同一个机制挡住,
        不用再维护一张只管得住同进程的内存登记表(CI 和 benchmark 就是并排起进程)。
        抢到之后还要**回头看一眼盘**:占位符在 save 成功后就还回去了,只靠"抢到了"会盖掉
        一个已经落盘的会话。
        代价 / 边界:起了会话一直不 save 就退出,会留一个 0 字节的 `.claim`。而这不罕见 ——
        **每一次没产生对话就结束的启动都留一个**(敲错命令、看一眼就退、CLI 参数打错)。
        实测就是这么发现的:一条 `--history`(它其实不是 CLI 参数)启动了 REPL 又退出,
        当场留下一个。

        所以发号前扫一遍:占位符的名字就是**那一秒**,`20260812-143138.claim` 只可能挡住
        `20260812-143138` 这一秒的会话,而那一秒永远不会再来。**不是当前这一秒的占位符,
        对任何人都不再有意义** —— 这是精确判据,不是"过期多久算旧"的启发式,所以不需要
        atexit、也不需要赌进程能正常收尾。清的是自己的垃圾,`*.claim` 不是任何一条 glob
        认的会话文件。

        另外 `new()` 会创建 `.talos/sessions/`:目录写不了的话崩在起会话时,而不是崩在
        第一轮 save —— 反正都是崩,早点崩看得清。"""
        # **UTC,不用本地时间。** 这一整套(发号、扫占位符、`resolve` 的前缀匹配)都建立
        # 在「时间只会往前走,过去的那一秒不会再来」上。本地时间**每年会违反一次**:
        # 夏令时秋季回拨,本地钟真的退回一小时,那一小时里起的会话跟一小时前撞号,
        # 而扫占位符那步只认「不是当前这一秒就删」,会把还活着的号放出来。
        # UTC 没有回拨。NTP 大幅校正仍然能撞,但那不是每年都发生的事,记在 FINDINGS 里。
        # 文件名的可读性代价:时间戳变成 UTC,看文件名对不上本地时钟 —— `/history`
        # 显示的时间走 mtime,不受影响。
        base = sid = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        os.makedirs(SESS_DIR, exist_ok=True)
        for stale in glob.glob(os.path.join(SESS_DIR, "*.claim")):
            # 时间戳是定宽的,所以「文件名以这一秒开头」恰好等于「它是这一秒或它的 -N 变体」
            if not os.path.basename(stale).startswith(base):
                try:
                    os.remove(stale)                       # 别的秒留下的:对谁都不再有意义
                except OSError:
                    pass                                   # 删不掉就算了,它只是垃圾
        n = 1
        while True:
            try:
                with open(_claim(sid), "x"):               # 原子抢号:同一秒只有一个抢得到
                    pass
            except FileExistsError:
                pass                                       # 号被别人占着(同进程或另一个进程)
            else:
                if _path_for(sid) is None:                 # 抢到了,再确认盘上没有同名会话
                    return cls(sid, batch=batch)
                os.remove(_claim(sid))                     # 这个号已经属于一个落了盘的会话
            n += 1
            sid = f"{base}-{n}"

    @property
    def path(self) -> str:
        name = f"{self.sid}__{self.slug}" if self.slug else self.sid
        return os.path.join(SESS_DIR, name + ".jsonl")

    def save(self, messages: list) -> None:
        """先写临时文件再原子替换,**最后**才删旧的。

        上一版的顺序是「先 `os.remove(old)`,再 `open(new, "w")`」—— 中间任何一次失败
        (磁盘满、权限、被占用)都是**两个文件都没有**,整个会话没了。而且 `"w"` 是
        先截断再写,写到一半崩掉留下的是半截文件:`load()` 会跳过坏行,所以它不报错,
        它只是**安静地少了后半段**。删除是这个项目里唯一没有撤销的动作,顺序不能反。"""
        old = self.path
        if not self.slug:                                  # 首次保存 → 用第一句 prompt 起名
            self.slug = (BATCH if self.batch else "") + _slug(_first_user(messages))
        os.makedirs(SESS_DIR, exist_ok=True)
        tmp = self.path + ".tmp"                           # 不以 .jsonl 结尾,不会被任何 glob 捡走
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for m in messages:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)                     # 原子:要么旧的完整,要么新的完整
        except BaseException:
            # 半成品自己收走。save() 每轮都跑,一个稳定复现的序列化失败会把目录堆满。
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        # 走到这里**保存已经成功了**:新文件完整地躺在盘上。下面全是收尾,收尾失败不许改写
        # 这个事实 —— 上一版 `os.remove(old)` 在 try 外面,旧文件删不掉(Windows 上一个不带
        # delete-sharing 的句柄就够了)就把 `PermissionError` 抛给调用方,而调用方看到的是
        # 「保存失败」:重试、或者放弃,两个都是拿一份**已经写好的**数据去赌。而 REPL 那条路
        # 压根没接这个异常(`agent.py` 每轮末尾直接 `sess.save(messages)`)—— 抛出去就是整个
        # REPL 退出,理由还是一个已经成功的保存。
        try:
            os.remove(_claim(self.sid))                    # 占位符还回去:号现在由文件本身占着
        except OSError:
            pass
        if old != self.path:                               # 起名后文件名变了(含旧格式迁移)
            try:
                os.remove(old)
            except FileNotFoundError:
                pass
            except OSError as e:
                # **不回滚新文件**:它是这一轮唯一完整的那份,为了删掉一个旧副本把它撤掉,
                # 是拿真数据换整洁;何况回滚本身也会失败(同一把锁)。代价是同一个 sid 在盘上
                # 留了两个文件,`_path_for` 挑带 slug 的那个(见那边的注释),旧的只是垃圾。
                # 告警是留给人的:垃圾不会自己消失,而无声的垃圾没人会去删。
                warnings.warn(f"旧会话文件删不掉,留在磁盘上:{old} ({e})")

    def load(self) -> list:
        # A single corrupt line must not brick a resume — skip it, don't crash the whole session.
        # Only dict messages survive; a poisoned file can't smuggle in a bare string or list.
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not ln.strip():
                    continue
                try:
                    m = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(m, dict) and isinstance(m.get("role"), str):
                    out.append(m)
        return out

def _exact(sid: str) -> list:
    """盘上**精确**属于这个 sid 的会话文件,新格式(带 slug)排前面。

    同一个 sid 命中两个文件只会是「改名时旧的没删掉」(见 `save()` 尾巴)。这时带 slug 的
    那个是刚写完的,旧格式的 `<sid>.jsonl` 是残留 —— 而 `.`(0x2E)排在 `_`(0x5F)前面,
    纯按名字排 `sorted()[0]` 恰好挑中那块垃圾。"""
    # escape: a sid of "*" would otherwise match every session and pick an arbitrary one.
    hits = glob.glob(os.path.join(SESS_DIR, glob.escape(sid) + "*.jsonl"))
    return sorted((p for p in hits if _parse_name(p)[0] == sid),
                  key=lambda p: (not _parse_name(p)[1], p))

def _path_for(sid: str) -> str | None:
    # 精确的 sid 优先于前缀命中。前缀匹配是给用户敲一半 id 用的,可它同时让**完整**的
    # sid 也变成前缀:`20260811-120000` 会命中 `20260811-120000-2__x.jsonl`,而 `-`(0x2D)
    # 排在 `_`(0x5F)前面 —— 拿完整 id 去找,`sorted()[0]` 给回来的是那个带序号的别人。
    hits = sorted(glob.glob(os.path.join(SESS_DIR, glob.escape(sid) + "*.jsonl")))
    return (_exact(sid) or hits or [None])[0]

def open_session(sid: str) -> "Session | None":
    """Bind to an existing session (recovering its slug from the filename)."""
    path = _path_for(sid)
    if not path:
        return None
    return Session(*_parse_name(path))

def list_sessions(batch: bool | None = None) -> list:
    """[(sid, mtime, title, n_msgs), ...] newest first。`batch=False` 只要人自己开的会话。

    跑批和人开的会话**同住一个目录、同一套命名**,区别只在 slug 前缀。这是刻意的:
    `recall.py:149` 和 `benchmarks/recall/recall_benchmark.py` 都是平铺 glob
    `sessions/*.jsonl`,**放进子目录这两边就都看不见了** —— 而「无人值守跑完什么都不留」
    要修的正是这个:跑批的轨迹要能被往事检索捞到、能被燃尽表抽成语料。
    需要区分的只有一处(`latest_sid`),所以区分做在名字上,不做在目录上。"""
    rows = []
    for path in glob.glob(os.path.join(SESS_DIR, "*.jsonl")):
        sid, slug = _parse_name(path)
        if batch is not None and slug.startswith(BATCH) != batch:
            continue
        first, n = "", 0
        # 坏行**跳过**,不是就此不数。上一版把 `json.loads` 放在 `try` 里、`except` 在整个
        # 循环外面:第 3 行坏掉,后面 96 行一条都不数,`/history` 报 3 条而 `load()` 读回 99 条。
        # 同一个文件在两个地方给出两个数,而这里没有任何报错 —— 它只是**默默少数**。
        # 判据(能不能读)和读法必须跟 `load()` 一致:那边跳过坏行,这边也跳过。
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if not ln.strip():
                        continue
                    try:
                        m = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(m, dict) or not isinstance(m.get("role"), str):
                        continue
                    n += 1
                    if not first and m["role"] == "user" and isinstance(m.get("content"), str):
                        first = m["content"]
        except OSError:
            pass                                    # 文件读不了:这一条仍然列出来,只是没有摘要
        rows.append((sid, os.path.getmtime(path), first or slug, n))
    return sorted(rows, key=lambda r: r[1], reverse=True)

def resolve(arg: str) -> str | None:
    """Turn a /history index (1-based) or an id prefix into a session id."""
    arg = (arg or "").strip()
    if not arg:
        return None
    rows = list_sessions()
    if arg.isdigit() and 1 <= int(arg) <= len(rows):      # small number = /history index
        return rows[int(arg) - 1][0]
    # 敲全了就是它,理由同 `_path_for`:撞号加的 `-2` 让 `20260811-120000` 同时是
    # `20260811-120000-2` 的**前缀**,而 rows 按 mtime 排 —— 敲全 id 反而可能回到别人那儿。
    if any(sid == arg for sid, *_ in rows):
        return arg
    for sid, *_ in rows:                                  # else match by id prefix (e.g. 20260724)
        if sid.startswith(arg):
            return sid
    return None

def delete(sid: str) -> bool:
    """删掉这个会话。**精确命中的文件一起删,不是只删第一个。**

    上一版删完还能 resume 回来:旧文件删不掉那次(见 `save()` 尾巴)盘上留了两个精确同 sid
    的文件,`delete` 只删 `_path_for` 挑中的那一个(新的),旧格式那份还在 —— 于是
    `/delete` 印「deleted」,`/resume` 照样读回旧内容。**说删了却没删,比删多了更糟。**

    只删**精确**命中,前缀命中不碰:前缀是另一个会话的 id,不是这个会话的副本。
    调用方(`--delete`)传进来的本来就是 `resolve()` 解析过的完整 sid。
    没有精确命中时才退回原来的行为(删前缀第一个),给直接调 API 的人留着。

    返回值只在**真的删干净了**才为 True —— 打印「deleted」的那行就靠它,删剩一半还报成功
    就是把上面那个谎换了个地方说。"""
    targets = _exact(sid) or ([p] if (p := _path_for(sid)) else [])
    for path in targets:
        try:
            os.remove(path)
        except OSError as e:
            warnings.warn(f"会话文件删不掉:{path} ({e})")
    return bool(targets) and not _exact(sid)

def latest_sid() -> str | None:
    """`--continue` / 不带参数的 `--view` 落在哪。**跳过跑批的。**

    一次 EXAM 或 benchmark 能留下几十条会话,而它们的时间戳永远比人最后一次对话新 ——
    不跳的话 `--continue` 就变成「续上刚才那个跑批」,而且是**静默**变的:
    banner 照样印一个会话号,人要读到第二句才发现接错了地方。
    `resolve()` 不跳,所以 `--resume <id>` / `--view <id>` 照样打得开跑批的那些。"""
    rows = list_sessions(batch=False)
    return rows[0][0] if rows else None
