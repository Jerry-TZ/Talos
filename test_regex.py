import re

content = '''    def _load_dotenv(path: str = ".env") -> None:
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    def _env_block() -> str:
    def make_client():'''

matches = re.findall(r'^\s*def\s+\w+', content, re.MULTILINE)
print(f'匹配到 {len(matches)} 个函数定义')
print(matches)
