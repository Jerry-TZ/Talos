import urllib.request
import json

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com"
}

req = urllib.request.Request("https://api.bilibili.com/x/web-interface/popular", headers=headers)
data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
items = data["data"]["list"][:5]

table_lines = []
table_lines.append(f"{'排名':<6}{'标题':<30}{'UP主':<15}{'播放量':<10}{'bvid'}")
table_lines.append("-" * 75)

for i, item in enumerate(items, start=1):
    rank = item.get("his_rank") or i
    title = item.get("title", "")[:28]
    author = item.get("owner", {}).get("name", "")
    view = item.get("stat", {}).get("view", 0)
    bvid = item.get("bvid", "")

    table_lines.append(f"{rank:<6}{title:<30}{author:<15}{view:<10}{bvid}")

print("\n".join(table_lines))
