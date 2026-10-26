"""Render this repository's documentation without external dependencies."""
from pathlib import Path
import html
import re

ROOT = Path(__file__).resolve().parents[1]


def inline(text):
    text = html.escape(text)
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
    text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\[([^\]]+)\]\((https?://[^ )]+)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'(?<!["(])https?://[^\s<]+', lambda m: '<a href="' + m[0] + '">' + m[0] + '</a>', text)
    return text


def render(text):
    out, paragraph, table, code, listing = [], [], [], None, None
    def flush():
        if paragraph:
            out.append('<p>' + inline(' '.join(paragraph)) + '</p>')
            paragraph.clear()
    def close_list():
        nonlocal listing
        if listing:
            out.append('</' + listing + '>')
            listing = None
    def close_table():
        if not table:
            return
        out.append('<div class="table-wrap"><table>')
        for i, row in enumerate(table):
            if all(re.fullmatch(r':?-{2,}:?', c.strip()) for c in row):
                continue
            tag = 'th' if i == 0 else 'td'
            out.append('<tr>' + ''.join(f'<{tag}>{inline(c.strip())}</{tag}>' for c in row) + '</tr>')
        out.append('</table></div>')
        table.clear()
    for line in text.splitlines() + ['']:
        if line.startswith('```'):
            flush(); close_list(); close_table()
            if code is None:
                code = []
            else:
                out.append('<pre><code>' + html.escape('\n'.join(code)) + '</code></pre>')
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        if line.strip().startswith('|') and line.strip().endswith('|'):
            flush(); close_list()
            table.append(line.strip().strip('|').split('|'))
            continue
        close_table()
        if not line.strip():
            flush(); close_list()
            continue
        heading = re.match(r'^(#{1,6})\s+(.*)', line)
        if heading:
            flush(); close_list()
            n = len(heading[1])
            out.append(f'<h{n}>' + inline(heading[2]) + f'</h{n}>')
            continue
        if line.startswith('> '):
            flush(); close_list()
            out.append('<blockquote>' + inline(line[2:]) + '</blockquote>')
            continue
        item = re.match(r'^\s*(?:([-*])|\d+\.)\s+(.*)', line)
        if item:
            flush()
            typ = 'ul' if item[1] else 'ol'
            if listing != typ:
                close_list(); out.append('<' + typ + '>'); listing = typ
            out.append('<li>' + inline(item[2]) + '</li>')
            continue
        if listing:
            close_list()
        paragraph.append(line.strip())
    return '\n'.join(out)


def main():
    names = ['DESIGN_PLAN.md', 'DEPLOY_GITHUB_PAGES.md', 'VALIDATION.md']
    sections = []
    for name in names:
        path = ROOT / 'docs' / name
        if path.exists():
            sections.append('<section>' + render(path.read_text()) + '</section>')
    style = '''body{margin:0;background:#f4f6f6;color:#182a3a;font:16px/1.65 system-ui,sans-serif}header{padding:22px 5%;background:#182a3a;color:white}header a{color:#b7efe6}main{max-width:1100px;margin:36px auto;padding:36px;background:white;border-radius:12px}h1{font-size:34px;line-height:1.2}h2{margin-top:42px;color:#087f7a}h3{margin-top:28px}p,li{max-width:90ch}code{font-size:.88em;background:#edf3f2;padding:2px 4px;border-radius:3px}pre{background:#edf3f2;padding:18px;overflow:auto}pre code{padding:0}a{color:#087f7a}table{border-collapse:collapse;min-width:600px;width:100%;font-size:14px}td,th{padding:12px;text-align:left;border-bottom:1px solid #dce4e3;vertical-align:top}th{background:#edf3f2}.table-wrap{overflow:auto;margin:20px 0}section+section{border-top:3px solid #dce4e3;margin-top:64px;padding-top:40px}@media(max-width:700px){main{margin:0;padding:22px}h1{font-size:28px}}'''
    document = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MainAIger — design and implementation plan</title><style>' + style + '</style><header><b>MainAIger</b> · Design &amp; implementation <a href="index.html" style="float:right">Open prototype →</a></header><main>' + '\n'.join(sections) + '</main></html>'
    (ROOT / 'site' / 'design-plan.html').write_text(document)
    print('Rendered site/design-plan.html')


if __name__ == '__main__':
    main()
