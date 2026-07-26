import urllib.request
import json

url = "https://api.bilibili.com/x/web-interface/popular"
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.bilibili.com"
}
req = urllib.request.Request(url, headers=headers)
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    print(json.dumps(data, indent=2, ensure_ascii=False))
