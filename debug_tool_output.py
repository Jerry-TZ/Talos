import re
import os

def process_file(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    total_lines = len(lines)
    func_count = len(re.findall(r'^\s*def\s+\w+', ''.join(lines), re.MULTILINE))
    func_names = re.findall(r'^\s*def\s+(\w+)', ''.join(lines), re.MULTILINE)
    return {
        'file': file_path,
        'lines': total_lines,
        'functions': func_count,
        'func_names': func_names
    }

files = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d != '.venv' and d != '__pycache__']
    for file in files:
        if file.endswith('.py'):
            files.append(os.path.join(root, file))

files.sort()

# 打印前 5 个文件的数据
for f in files[:5]:
    data = process_file(f)
    print(f'{f}: {data["lines"]} 行, {data["functions"]} 个函数')
