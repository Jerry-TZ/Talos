"""README 里那些**能被读者当场核对**的数字,不许烂掉。

README 第一屏有一张四个文件的行数表,底下紧跟着一句:

    行数自己数,别信我:`wc -l agent.py console_ui.py recall.py session.py`

**这是一句邀请核对的话,也就是一句承诺。** 而今天真去数,四行全错
(1821/160/303/134 → 2837/214/506/271)。一份请你验证的表,验证下来一条都不对,
比不写这句话更糟 —— 它把「可核对」当卖点,却是第一个烂掉的地方。

发现方式是冒烟:让 Talos 用一句话总结 README,它老老实实转述了「2638 行 Python,
130 个离线测试」。**模型没说错,是文档在说假话。**

判据只钉表格,不钉散文里的数字:表格是机器能重算的,散文里那句「四个文件」不是。
"""
import io
import os
import re

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# `| `agent.py` | 2837 | 循环 + 工具 + 权限门 + 自学习 |`
_ROW = re.compile(r"^\|\s*`([\w.]+\.py)`\s*\|\s*(\d+)\s*\|", re.M)

# README 正文说「压进四个文件」。这四个就是那四个 —— 表里少一行、多一行都得有人看一眼。
_CORE = ("agent.py", "console_ui.py", "recall.py", "session.py")


def test_the_line_counts_readme_invites_you_to_check_are_the_real_ones():
    """README 那张行数表 == `wc -l` 数出来的。

    这条判据的成本几乎是零,而它挡的是一类**只有读者会撞上、作者永远撞不上**的错:
    写的人不会去数自己刚写的数字,读的人第一件事就是去数。

    容差 0 行 —— 不是苛刻,是这张表的全部意义就在于**它敢让你去数**。
    一旦允许「差不多就行」,那句「行数自己数,别信我」就该删掉。"""
    readme = io.open(os.path.join(HOME, "README.md"), encoding="utf-8").read()
    claimed = {name: int(n) for name, n in _ROW.findall(readme)}

    # **先钉「表里有哪几行」,再钉「每行的数对不对」。** 只钉后者的话,整张表可以一行行
    # 消失而判据照绿 —— 上一版就是这样,是变异测试(抽掉 `agent.py` 那行)当场逮到的:
    # 剩下三行还对,断言全过。这跟 `_TALKS_DIRECTLY` 是同一个形状:**枚举全部,要求
    # 每个都在**。真要增删核心文件,先来这里改一行 —— 也就是必须有人看它一眼。
    assert set(claimed) == set(_CORE), (
        f"README 那张表覆盖的文件变了:多了 {sorted(set(claimed) - set(_CORE))}、"
        f"少了 {sorted(set(_CORE) - set(claimed))}\n"
        "README 正文写着「压进四个文件」,表和那句话得是同一件事;"
        "真改了核心文件集合,连同这里一起改。")

    wrong = []
    for name, said in sorted(claimed.items()):
        p = os.path.join(HOME, name)
        if not os.path.exists(p):
            wrong.append(f"{name}:表里有,仓库里没有")
            continue
        real = sum(1 for _ in io.open(p, encoding="utf-8"))
        if real != said:
            wrong.append(f"{name}:表里写 {said},实际 {real}({real - said:+d})")
    assert not wrong, (
        "README 的行数表跟真实行数对不上:\n  " + "\n  ".join(wrong)
        + "\n表底下写着「行数自己数,别信我」—— 读者照做的第一件事就是发现这个。")

    # 第一屏那句「N 行 Python」是这张表四行的和。**上一版没钉它**,理由写的是
    # 「散文里的数字机器重算不了」—— 而这个数恰恰算得出来,边界划错了地方。
    # 代价当天就到了:表修好了,这句还是旧的,冒烟里模型照着念了「约 3800 行」。
    # 一份文档里两个应该相等的数字,只查其中一个,等于没查。
    total = re.search(r"(\d[\d,]*)\s*行 Python", readme)
    assert total, "README 第一屏那句「N 行 Python」没了或者改了写法 —— 这半条判据自己瞎了"
    said, real = int(total.group(1).replace(",", "")), sum(claimed.values())
    assert said == real, (
        f"README 第一屏写「{said} 行 Python」,而下面那张表加起来是 {real}({real - said:+d})。\n"
        "同一份文档里两个该相等的数字对不上 —— 读者先看到的是第一屏那个。")
