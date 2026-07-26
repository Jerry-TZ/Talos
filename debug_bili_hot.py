import urllib.request
import urllib.parse
import json

url = "https://api.bilibili.com/x/web-interface/popular"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com"
}

req = urllib.request.Request(url, headers=headers)

try:
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    print("完整响应:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"请求失败: {e}")
