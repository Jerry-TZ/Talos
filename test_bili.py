import urllib.request
import json

url = "https://api.bilibili.com/x/web-interface/popular?pn=0&ps=10"
req = urllib.request.Request(url)
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Referer': 'https://www.bilibili.com',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Origin': 'https://www.bilibili.com',
    'Connection': 'keep-alive'
}
for k, v in headers.items():
    req.add_header(k, v)

data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode('utf-8'))
print(json.dumps(data, indent=2, ensure_ascii=False))
