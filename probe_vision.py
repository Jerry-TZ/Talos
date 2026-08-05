"""probe_vision.py — 一次性探针:视觉模型到底能不能看出图的问题?

    python probe_vision.py <图片路径>

**不是工具,不进 TOOLS,不改 agent.py。** 先量一件事:免费的视觉模型对着一张真图,
提得出的意见是「这是一张热力图」这种废话,还是「虚线标的是周期却画在频率轴上」这种
能改的意见。前者的话,给 Talos 装视觉审查就是白装 —— 省下那三十行。

用 GLM-4.6V-Flash:免费。不用新装库(openai 那个包 agent.py 本来就在用),但**要新配一个
key** —— agent.py 的 PROVIDERS 表里那行 "glm" 只是写了变量名,不代表你有这个 key。
去 bigmodel.cn 注册拿一个,写进 .env 的 `ZHIPUAI_API_KEY=`,这个脚本会自己读。
"""
import base64
import mimetypes
import os
import sys
import time

def _load_dotenv(path: str = ".env") -> None:
    """跟 agent.py 一样从 .env 读 KEY=VALUE(真环境变量优先)—— 这样你不用每次 export。
    没直接 import agent.py:那会顺带跑完它的整个初始化(建目录、定 WORKSPACE 牢笼、
    加载 tools/)。探针不该有副作用。"""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MODEL = os.environ.get("VISION_MODEL", "glm-4.6v-flash")
BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

PROMPT = """你是科研配图的审稿人。看这张图,**只挑毛病**,不要夸。按这个顺序看:
1. 标注和数据对不对得上(标的位置、单位、数值)
2. 坐标轴:有没有单位、colorbar 有没有标签、刻度合不合理
3. 版式:panel 有没有 (a)(b)(c) 编号、字号会不会太小、中英文混用
4. 配色:有没有用 rainbow/jet、色盲能不能分辨
每条写成一句话,指明是哪个 panel。看不出问题的项就跳过,不要凑数。"""


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    if not os.path.exists(path):
        print(f"没有这个文件:{path}")
        return 2

    key = os.environ.get("ZHIPUAI_API_KEY")
    if not key:
        print("没有 ZHIPUAI_API_KEY。去 bigmodel.cn 拿一个(免费),")
        print("然后在 .env 里加一行:ZHIPUAI_API_KEY=你的key")
        return 2

    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    from openai import OpenAI
    # max_retries=0 是 agent.py 的设置 —— 它自己有重试循环,探针没有。免费档的 429
    # (code 1305「访问量过大」)是共享池挤爆,不是配额用完,退避重试几次通常就过了。
    client = OpenAI(api_key=key, base_url=BASE_URL, timeout=120, max_retries=6)

    # 调用前先说一句。没有转圈动画 —— 这台机器上 console.legacy_windows=True,
    # rich 的 Live 重画不可靠,变宽的文字会刷屏。一行静态的够用了。
    kb = len(b64) * 3 // 4096
    print(f"→ 调 {MODEL},图 {kb}KB,最长等 ~2 分钟(429 会自动退避重试,最多 6 次)…",
          flush=True)
    t0 = time.time()
    r = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            {"type": "text", "text": PROMPT},
        ]}],
    )
    print(r.choices[0].message.content)
    u = r.usage
    print(f"\n[用量 in={u.prompt_tokens} out={u.completion_tokens} 模型={MODEL} "
          f"耗时 {time.time() - t0:.0f}s]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
