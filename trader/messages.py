"""슬랙 알림 문구 (교체 예고, 교체 완료, 보유 현황)

run.py 의 Runner 를 받아서 (미리보기 글, 블록 목록)을 만든다.
"""
from datetime import datetime

import pandas as pd

from trader import slack_format as F

WEEKDAY = '월화수목금토일'
ACCOUNT_NAME = {'mock': '모의투자', 'paper': '가상 장부'}


def day(ts):
    ts = pd.Timestamp(ts)
    return f'{ts:%m/%d}({WEEKDAY[ts.weekday()]})'


def short(name, n=8):
    """표가 넘치지 않게 종목 이름을 자른다 (한글 n자 정도)"""
    out = ''
    for ch in name:
        if F.width(out + ch) > n * 2:
            return out + '…'
        out += ch
    return out


def holdings_table(r, prices):
    pos = list(r.broker.positions.items())
    manual = list(r.broker.manual_positions.items())
    if not pos and not manual:
        return '보유 종목 없음 (현금 100%)'
    rows = []
    for (code, p), tag in [(x, '') for x in pos] + [(x, ' (직접)') for x in manual]:
        now = prices.get(code, p['avg_price'])
        rows.append([short(p['name'], 7) + tag, f'{p["qty"]:,}', f'{p["avg_price"]:,.0f} → {now:,.0f}',
                     F.pct(now * p['qty'] / p['cost'] - 1 if p['cost'] else 0)])
    return F.table(['종목', '수량', '매입가 → 현재가', '손익'], rows, right={1, 2, 3})


def summary(r, prices, extra=()):
    eq = r.broker.equity(prices)
    pairs = [('평가 자산', f'{F.won(eq)}  ({F.signed_pct(eq / r.broker.capital - 1)})')]
    pairs += list(extra)
    pairs += [('현금', F.won(r.broker.cash)), ('보유', f'{len(r.broker.positions)} / {r.hold}종목')]
    return F.fields(pairs)


# ------------------------------------------------------------------ 교체 예고 (아침)
def plan(r, when):
    d = r.d
    names, rank, mom = d['names'], d['rank'], d['mom']
    filt = '시장 필터 통과' if d['market_ok'] else '시장 필터 걸림'
    ctx = f'{ACCOUNT_NAME[r.spec["account"]]} · 신호일 {day(d["signal"])} · {filt} · {when}'
    if d['late']:
        ctx += ' · 늦은 교체'
    blocks = [F.header(f'🗓️ 오늘 교체 예정 · {r.spec["name"]}'), F.context(ctx)]
    rows = []
    for code, why in d['sells']:
        p = r.broker.positions.get(code, {})
        rows.append(['매도', short(names.get(code, p.get('name', code))),
                     f'{int(rank[code])}위' if code in rank.index else '-', why.split(' (')[0]])
    for code in d['buys'][:d['free']]:
        rows.append(['매수', short(names.get(code, code)), f'{int(rank[code])}위', f'모멘텀 {F.pct(mom[code], 0)}'])
    if rows:
        blocks.append(F.code(F.table(['구분', '종목', '순위', '이유'], rows, right={2})))
        if len(d['buys']) > d['free'] and d['free']:
            spare = ', '.join(short(names.get(c, c), 6) for c in d['buys'][d['free']:])
            blocks.append(F.context(f'못 사는 종목이 있으면 다음 순위로 채움 : {spare}'))
    elif not d['market_ok']:
        blocks.append(F.text('코스피가 60일 평균 아래라서 오늘은 *사지 않습니다*.'))
    else:
        blocks.append(F.text('바꿀 종목이 없습니다. 지금 보유 종목을 그대로 둡니다.'))
    if d['keep']:
        blocks.append(F.context(f'계속 보유 {len(d["keep"])}종목 : '
                                + ', '.join(short(names.get(c, c), 6) for c in d['keep'])))
    return f'🗓️ 오늘 교체 예정 · {r.spec["name"]} (매도 {len(d["sells"])}, 매수 {min(d["free"], len(d["buys"]))})', blocks


# ------------------------------------------------------------------ 교체 완료
def result(r, prices):
    d, ev = r.d, r.events
    sells = [e for e in ev if e['side'] == '매도']
    buys = [e for e in ev if e['side'] == '매수']
    filt = '시장 필터 통과' if d['market_ok'] else '시장 필터 걸림'
    title = f'🔄 주간 교체 완료 · {r.spec["name"]}'
    blocks = [F.header(title),
              F.context(f'{ACCOUNT_NAME[r.spec["account"]]} · 신호일 {day(d["signal"])} · {filt}'),
              summary(r, prices, [('이번 교체', f'매도 {len(sells)} · 매수 {len(buys)}')])]
    if ev:
        rows = []
        for e in ev:
            if e['side'] == '매도':
                rows.append(['▼매도', short(e['name']), f'{e["qty"]:,}주', F.won(e['amount']), f'손익 {F.pct(e["rate"])}'])
            else:
                rows.append(['▲매수', short(e['name']), f'{e["qty"]:,}주', F.won(e['amount']), e['note']])
        blocks += [F.divider(), F.code(F.table(['구분', '종목', '수량', '금액', '비고'], rows, right={2, 3}))]
        realized = sum(e['profit'] for e in sells)
        how = '최유리지정가 매수, 시장가 매도' if r.mock else '그날 종가 ± 0.05%로 체결'
        blocks.append(F.context(f'실현손익 {F.won(realized, sign=True)} · {how}'))
    else:
        blocks.append(F.text('사고판 종목이 없습니다.' + ('' if d['market_ok'] else ' (시장 필터)')))
    return f'{title} (매도 {len(sells)}, 매수 {len(buys)})', blocks


# ------------------------------------------------------------------ 보유 현황 (교체일이 아닌 날)
def status(runners, prices):
    today = datetime.now()
    title = f'📈 오늘의 보유 현황 · {day(today)}'
    blocks = [F.header(title), F.context('이번 주 교체 끝 · 다음 교체는 다음 주 첫 거래일')]
    if len(runners) == 1:
        r = runners[0]
        blocks[1] = F.context(f'{r.spec["name"]} · {ACCOUNT_NAME[r.spec["account"]]} · 이번 주 교체 끝 · '
                              f'다음 교체는 다음 주 첫 거래일')
        blocks += [summary(r, prices), F.code(holdings_table(r, prices))]
        return title, blocks
    rows, eqs = [], []
    for r in runners:
        eq = r.broker.equity(prices)
        eqs.append(eq / r.broker.capital - 1)
        rows.append([r.spec['name'], F.won(eq), F.signed_pct(eqs[-1]), f'{len(r.broker.positions)}'])
    body = F.table(['전략', '평가 자산', '누적', '보유'], rows, right={1, 2, 3})
    if len(runners) == 2:
        gap = f'{(eqs[1] - eqs[0]) * 100:+.2f}%p'.replace('-', '−')
        body += f'\n\n차이 ({runners[1].spec["name"]} − {runners[0].spec["name"]}) : {gap}'
    blocks.append(F.code(body))
    for r in runners:
        blocks += [F.divider(), F.text(f'*{r.spec["name"]}*'), F.code(holdings_table(r, prices))]
    return title, blocks


# ------------------------------------------------------------------ 시장 상황 (매일)
# 백테스트(backtest/regime_strategies.py, 2022-01 ~ 2026-11)에서 그 상황에 가장 나았던 로직
BEST_LOGIC = {'G': '⚪ 기본', 'GB': '🟠 꺾일 때 (기본보다 손실이 작았음)', 'B': '⚪ 기본 (시장 필터로 현금)',
              'BG': '⚪ 기본'}


def market(index, logic_name):
    """index: 코스피 전체. 마지막 날(어제) 장 마감 기준 시장 상황"""
    import momentum as M
    raw, conf = M.regimes(index)
    d = index.index[-1]
    now = conf.iat[-1]
    streak = 0
    for s in reversed(conf.tolist()):
        if s != now:
            break
        streak += 1
    prev = next((s for s in reversed(conf.tolist()[:-streak]) if s), '') if streak < len(conf) else ''
    ma20, ma60 = index.rolling(20).mean().iat[-1], index.rolling(60).mean().iat[-1]
    chg = index.iat[-1] / index.iat[-2] - 1
    vol = index.pct_change().rolling(20).std() * (252 ** 0.5)
    v_now, v_mid = vol.iat[-1], vol.iloc[-250:].median()
    level = '높음' if v_now > v_mid * 1.25 else ('낮음' if v_now < v_mid * 0.8 else '보통')

    E, N = M.REGIME_EMOJI, M.REGIME_NAMES
    title = f'📊 시장 상황 · {day(d)} 장 마감 기준'
    state = f'{E[now]} *{N[now]}*  ({streak}거래일째)'
    blocks = [F.header(title),
              F.text(state + (f'\n{E[prev]} {N[prev]} → {E[now]} {N[now]} 로 바뀜' if prev and streak <= 5 else '')),
              F.fields([('코스피', f'{index.iat[-1]:,.2f}  ({F.signed_pct(chg)})'),
                        ('20일선 / 60일선', f'{ma20:,.2f} / {ma60:,.2f}'),
                        ('지수 위치', f'20일선 {"위" if index.iat[-1] > ma20 else "아래"} · '
                                     f'60일선 {"위" if index.iat[-1] > ma60 else "아래"}'),
                        ('변동성 (20일)', f'{level}  (연 {v_now:.0%}, 1년 보통 {v_mid:.0%})')]),
              F.text('*최근 10거래일* (왼쪽이 오래된 날)\n' + ' '.join(E[s] for s in conf.iloc[-10:]))]
    if raw.iat[-1] and raw.iat[-1] != now:
        run = 0
        for s in reversed(raw.tolist()):
            if s != raw.iat[-1]:
                break
            run += 1
        blocks.append(F.context(f'{E[raw.iat[-1]]} {N[raw.iat[-1]]} 쪽으로 움직이는 중 '
                                f'({run}/{M.REGIME_CONFIRM}일, {M.REGIME_CONFIRM}일 연속이면 상황이 바뀜)'))
    blocks += [F.divider(),
               F.context(f'지금 모의투자 로직 : {logic_name}  ·  백테스트상 이 상황에 가장 나았던 로직 : {BEST_LOGIC[now]}')]
    return f'{title} · {E[now]} {N[now]} ({streak}거래일째)', blocks


# ------------------------------------------------------------------ 직접 거래 (한투 앱 등)
def manual_trades(events):
    rows = [[('▲' if e['side'] == '매수' else '▼') + e['side'], short(e['name']), f'{e["qty"]:,}주',
             f'{e["price"]:,.0f}' + ('*' if e.get('estimated') else ''), F.won(e['amount'])] for e in events]
    title = f'🙋 직접 거래 감지 · {day(datetime.now())} {events[-1]["time"]}'
    blocks = [F.header(title), F.code(F.table(['구분', '종목', '수량', '단가', '금액'], rows, right={2, 3, 4}))]
    note = '프로그램이 산 종목이 아니면 프로그램은 팔지 않습니다. 쓴 현금만큼 프로그램이 쓸 돈이 줄어듭니다.'
    if any(e.get('estimated') for e in events):
        note = '* 매도 단가는 감지한 때의 현재가로 추정 · ' + note
    blocks.append(F.context(note))
    first = events[0]
    return f'🙋 직접 거래 감지 · {first["name"]} {first["qty"]:,}주 {first["side"]}' + \
        (f' 외 {len(events) - 1}건' if len(events) > 1 else ''), blocks


def manual_summary(events, manual, prices):
    title = f'🧾 오늘 직접 거래 정리 · {day(datetime.now())}'
    blocks = [F.header(title)]
    if events:
        rows = [[e['time'], ('▲' if e['side'] == '매수' else '▼') + e['side'], short(e['name']), f'{e["qty"]:,}주',
                 F.won(e['amount'])] for e in events]
        blocks.append(F.code(F.table(['시각', '구분', '종목', '수량', '금액'], rows, right={3, 4})))
    else:
        blocks.append(F.text('오늘 직접 한 거래는 없습니다.'))
    if manual:
        rows, cost, value = [], 0.0, 0.0
        for c, v in manual.items():
            now = prices.get(c, v['avg_price'])
            cost += v['avg_price'] * v['qty']
            value += now * v['qty']
            rows.append([short(v['name']), f'{v["qty"]:,}', f'{v["avg_price"]:,.0f} → {now:,.0f}',
                         F.pct(now / v['avg_price'] - 1 if v['avg_price'] else 0)])
        blocks += [F.divider(), F.text(f'*직접 보유 종목* (프로그램이 관리하지 않음) · 평가 {F.won(value)}  '
                                       f'({F.signed_pct(value / cost - 1 if cost else 0)})'),
                   F.code(F.table(['종목', '수량', '매입가 → 현재가', '손익'], rows, right={1, 2, 3}))]
    return title, blocks
