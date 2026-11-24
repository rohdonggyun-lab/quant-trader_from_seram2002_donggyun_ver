"""슬랙 메시지 꾸미기 (Block Kit)

- 제목(header), 작은 설명(context), 2열 요약칸(fields), 구분선(divider), 고정폭 표(code)
- 한글은 고정폭 글꼴에서 두 칸을 차지하므로 표 줄 맞춤을 화면 폭 기준으로 한다
- 고정폭 표 안에서는 색을 쓸 수 없어서 이익 ▲, 손실 ▼ 로 표시한다
"""
import unicodedata


def width(s):
    """화면에서 차지하는 칸 수 (한글 등 넓은 글자는 2칸)"""
    return sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in str(s))


def pad(s, w, right=False):
    s = str(s)
    gap = ' ' * max(w - width(s), 0)
    return gap + s if right else s + gap


def table(header, rows, right=()):
    """고정폭 표 문자열. right: 오른쪽 정렬할 열 번호"""
    cols = [header] + rows
    ws = [max(width(r[i]) for r in cols) for i in range(len(header))]
    lines = ['  '.join(pad(c, ws[i], i in right) for i, c in enumerate(r)).rstrip() for r in cols]
    lines.insert(1, '─' * max(width(lines[0]), 10))
    return '\n'.join(lines)


def pct(x, digits=1):
    """+3.2% / −2.1% (▲▼ 붙임)"""
    mark = '▲' if x > 0 else ('▼' if x < 0 else ' ')
    return f'{mark}{abs(x):.{digits}%}'


def signed_pct(x, digits=2):
    return f'{x:+.{digits}%}'.replace('-', '−')


def won(x, sign=False):
    return (f'{x:+,.0f}' if sign else f'{x:,.0f}').replace('-', '−') + '원'


# ------------------------------------------------------------------ 블록
def header(text):
    return {'type': 'header', 'text': {'type': 'plain_text', 'text': text[:150], 'emoji': True}}


def context(text):
    return {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': text[:3000]}]}


def fields(pairs):
    """[(이름, 값)] → 2열 요약칸 (최대 10칸)"""
    return {'type': 'section', 'fields': [{'type': 'mrkdwn', 'text': f'*{k}*\n{v}'} for k, v in pairs[:10]]}


def text(md):
    return {'type': 'section', 'text': {'type': 'mrkdwn', 'text': md[:3000]}}


def code(body):
    return text(f'```{body[:2990]}```')


def divider():
    return {'type': 'divider'}


def plain(blocks):
    """로그 파일에 남길 글자 형태"""
    out = []
    for b in blocks:
        if b['type'] == 'header':
            out.append(b['text']['text'])
        elif b['type'] == 'context':
            out.append(b['elements'][0]['text'])
        elif b['type'] == 'section' and 'fields' in b:
            out.append(' | '.join(f['text'].replace('*', '').replace('\n', ' ') for f in b['fields']))
        elif b['type'] == 'section':
            out.append(b['text']['text'].strip('`'))
    return '\n'.join(out)
