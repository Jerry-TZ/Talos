with open('tools/py_report.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 修复正则：把 lines 改成 ''.join(lines)
old1 = "func_count = len(re.findall(r'^\\s*def\\s+\\w+', lines, re.MULTILINE))"
new1 = "func_count = len(re.findall(r'^\\s*def\\s+\\w+', ''.join(lines), re.MULTILINE))"

old2 = "func_names = re.findall(r'^\\s*def\\s+(\\w+)', lines, re.MULTILINE)"
new2 = "func_names = re.findall(r'^\\s*def\\s+(\\w+)', ''.join(lines), re.MULTILINE)"

content = content.replace(old1, new1)
content = content.replace(old2, new2)

with open('tools/py_report.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('已修复')
