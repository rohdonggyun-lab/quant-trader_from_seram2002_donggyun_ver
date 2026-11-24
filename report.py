"""결과 보고서 만들기

    python report.py

config.STRATEGIES 의 전략마다 data/<book>/trades.csv 와 state.json 을 읽어서 results/<book>.md 를 만들고,
전략들을 나란히 놓은 results/compare.md 를 만든다. 평가액은 받아 둔 KRX 시세의 마지막 종가로 계산한다.
(data와 results 폴더는 git에 올리지 않고 각자 컴퓨터에만 둔다)
보고서는 매번 data에서 다시 만드므로 직접 고치지 않는다. 메모는 results/journal.md에 따로 적는다.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

import config
import momentum as M

ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / 'data'
RESULTS_DIR = ROOT / 'results'


def won(x):
    return f'{x:+,.0f}'


def read_trades(path):
    if not path.exists():
        return []
    with open(path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def pair_trades(rows):
    """매도마다 그 종목의 매수(여러 번이면 합친 것)와 짝지어 한 건의 거래로 만든다."""
    open_buys, trades = {}, []
    for r in rows:
        code = r['code']
        if r['side'] == 'BUY':
            b = open_buys.setdefault(code, {'time': r['time'], 'qty': 0, 'cost': 0.0, 'reason': r['reason']})
            b['qty'] += int(r['qty'])
            b['cost'] += float(r['amount']) + float(r['fee'])
        elif r['side'] == 'SELL':
            b = open_buys.get(code)
            sold = int(r['qty'])
            cost = b['cost'] * sold / b['qty'] if b and b['qty'] else 0.0
            profit = float(r['profit'] or 0)
            days = ''
            if b:
                days = (datetime.strptime(r['time'][:10], '%Y-%m-%d')
                        - datetime.strptime(b['time'][:10], '%Y-%m-%d')).days
                b['qty'] -= sold
                b['cost'] -= cost
                if b['qty'] <= 0:
                    open_buys.pop(code)
            trades.append({'buy': b['time'][:10] if b else '', 'sell': r['time'][:10], 'name': r['name'],
                           'code': code, 'qty': sold, 'profit': profit, 'rate': profit / cost * 100 if cost else 0,
                           'days': days, 'buy_reason': b['reason'] if b else '', 'sell_reason': r['reason'],
                           'cost': float(r['fee']) + float(r['tax'])})
    return trades


def last_close():
    """받아 둔 시세의 마지막 거래일과 종목별 종가"""
    files = sorted(M.DAILY.glob('*.csv.gz'))
    if not files:
        return '', {}
    import pandas as pd
    d = pd.read_csv(files[-1], dtype={'ticker': str}, usecols=['ticker', 'close'])
    return files[-1].name[:8], dict(zip(d['ticker'], d['close']))


def load_book(spec, closes):
    book_dir = DATA_ROOT / spec['book']
    if not (book_dir / 'state.json').exists():
        return None
    state = json.loads((book_dir / 'state.json').read_text(encoding='utf-8'))
    rows = read_trades(book_dir / 'trades.csv')
    trades = pair_trades(rows)
    value = sum(p['qty'] * closes.get(c, p['avg_price']) for c, p in state['positions'].items())
    return dict(spec=spec, state=state, rows=rows, trades=trades, equity=state['cash'] + value)


def build(b, close_day):
    spec, state, rows, trades = b['spec'], b['state'], b['rows'], b['trades']
    capital = spec['capital']
    wins = [t for t in trades if t['profit'] > 0]
    total = sum(t['profit'] for t in trades)
    kind = '모의투자' if spec['account'] == 'mock' else '가상 장부'

    out = [f'# {kind} 결과 — {spec["name"]} ({spec["book"]})\n',
           f'`python report.py`로 `data/{spec["book"]}/`에서 자동 생성한 보고서입니다. '
           f'직접 고치지 말고, 메모는 [journal.md](journal.md)에 적습니다.\n',
           f'생성 시각: {datetime.now():%Y-%m-%d %H:%M}\n',
           '## 요약\n', '| 항목 | 값 |\n|---|---|',
           f'| 규칙 | 모멘텀 {spec["lookback"]}일, {spec["hold"]}종목 보유, {spec["hold"] * 2}위 밖이면 매도 |',
           f'| 첫 거래 | {rows[0]["time"][:10] if rows else "-"} |',
           f'| 시작 자금 | {capital:,}원 |',
           f'| 평가 자산 ({close_day} 종가 기준) | {b["equity"]:,.0f}원 ({b["equity"] / capital - 1:+.2%}) |',
           f'| 현금 | {state["cash"]:,.0f}원 |',
           f'| 마지막 교체 신호일 | {state.get("last_signal") or "-"} |',
           f'| 실현손익 (판 종목) | {won(total)}원 |',
           f'| 판 거래 수 | {len(trades)}건 (이익 {len(wins)}, 손실 {len(trades) - len(wins)}) |']
    if trades:
        out.append(f'| 승률 | {len(wins) / len(trades) * 100:.1f}% |')
        days = [t['days'] for t in trades if t['days'] != '']
        if days:
            out.append(f'| 평균 보유 기간 | {sum(days) / len(days):.1f}일 |')
        out.append(f'| 매도 비용 합계 (수수료, 세금) | {sum(t["cost"] for t in trades):,.0f}원 |')
    out.append('')

    out.append('## 보유 종목\n')
    out.append('| 종목 | 수량 | 매입가 | 매입원가 | 매수일 |\n|---|---|---|---|---|')
    for code, p in state['positions'].items():
        out.append(f'| {p["name"]}({code}) | {p["qty"]:,} | {p["avg_price"]:,.0f} | {p["cost"]:,.0f} | {p["time"][:10]} |')
    out.append('')

    out.append('## 판 거래\n')
    out.append('| 매수일 | 매도일 | 보유 | 종목 | 수량 | 손익 | 수익률 | 매수 이유 | 매도 이유 |')
    out.append('|---|---|---|---|---|---|---|---|---|')
    for t in trades:
        out.append(f'| {t["buy"]} | {t["sell"]} | {t["days"]}일 | {t["name"]}({t["code"]}) | {t["qty"]:,} | '
                   f'{won(t["profit"])} | {t["rate"]:+.2f}% | {t["buy_reason"]} | {t["sell_reason"]} |')
    note = ('손익은 체결가 기준이고, 수수료와 매도 세금은 config 값으로 추정한 것입니다.' if spec['account'] == 'mock'
            else '가상 장부는 그날 종가에 슬리피지 0.05%를 불리하게 붙여 체결한 것으로 계산합니다.')
    out.append(f'\n{note}\n')
    return '\n'.join(out)


def compare(books, close_day):
    out = ['# 전략 비교\n', f'생성 시각: {datetime.now():%Y-%m-%d %H:%M}, 평가는 {close_day} 종가 기준\n',
           '실험 로직은 모의투자가 아니라 **그림자**(같은 로직의 가상 장부)와 비교합니다. '
           '가상 장부는 종가 체결이라 모의투자보다 유리하게 나옵니다.\n',
           '| 전략 | 계좌 | 첫 거래 | 평가 자산 | 수익률 | 실현손익 | 판 거래 | 보유 |',
           '|---|---|---|---|---|---|---|---|']
    for b in books:
        s = b['spec']
        out.append(f'| {s["name"]} | {"모의" if s["account"] == "mock" else "가상"} | '
                   f'{b["rows"][0]["time"][:10] if b["rows"] else "-"} | {b["equity"]:,.0f}원 | '
                   f'{b["equity"] / s["capital"] - 1:+.2%} | {won(sum(t["profit"] for t in b["trades"]))}원 | '
                   f'{len(b["trades"])}건 | {len(b["state"]["positions"])}종목 |')
    out.append('\n로직을 바꾸는 판단은 최소 3개월 이상 지난 뒤, 수익률과 최대낙폭을 함께 보고 합니다.\n')
    return '\n'.join(out)


def main():
    close_day, closes = last_close()
    books = [b for b in (load_book(s, closes) for s in config.STRATEGIES) if b]
    if not books:
        print('장부가 없습니다. run.py 를 먼저 실행하세요.')
        return
    RESULTS_DIR.mkdir(exist_ok=True)
    for b in books:
        path = RESULTS_DIR / f'{b["spec"]["book"]}.md'
        path.write_text(build(b, close_day), encoding='utf-8')
        print(f'{path.relative_to(ROOT)} 생성')
    path = RESULTS_DIR / 'compare.md'
    path.write_text(compare(books, close_day), encoding='utf-8')
    print(f'{path.relative_to(ROOT)} 생성')


if __name__ == '__main__':
    main()
