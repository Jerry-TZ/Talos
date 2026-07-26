import urllib.request
import json
from urllib.parse import urlencode

url = 'https://api.bilibili.com/x/web-interface/popular'
params = {'pn': 1, 'ps': 10}

req = urllib.request.Request(
    url + '?' + urlencode(params),
    headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://www.bilibili.com'
    }
)

try:
    with urllib.request.urlopen(req, timeout=10) as response:
        data = json.loads(response.read().decode('utf-8'))
    
    print("完整响应:")
    print(json.dumps(data, indent=2, ensure_ascii=False))
    print("\n")
    print("data['data'] 的类型:", type(data.get('data')))
    print("data['data'] 的 keys:", data.get('data', {}).keys() if isinstance(data.get('data'), dict) else 'N/A')
    
except Exception as e:
    print(f"请求失败: {str(e)}")
    import traceback
    traceback.print_exc()
