from docx import Document
doc = Document('report.docx')
# 读取 agent.py 对应的那一行
for i, row in enumerate(doc.tables[0].rows):
    if 'agent.py' in row.cells[0].text:
        print(f'行{i}: {row.cells[0].text[:60]} | {row.cells[1].text} | {row.cells[2].text} | {row.cells[3].text[:60]}')
        break
