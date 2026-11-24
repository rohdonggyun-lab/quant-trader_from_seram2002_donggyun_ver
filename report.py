"""모의투자 결과 보고서 만들기

    python report.py

data/<BOOK>/trades.csv 와 state.json 을 읽어서 results/<BOOK>.md 를 새로 만든다.
(data와 results 폴더는 git에 올리지 않고 각자 컴퓨터에만 둔다)
보고서는 매번 data에서 다시 만드므로 직접 고치지 않는다. 메모는 results/journal.md에 따로 적는다.
"""
import csv
import json
from datetime import datetime
from pathlib import Path

import config

ROOT = Path(__file__).resolve().parent
BOOK_DIR = ROOT / 'data' / config.BOOK
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


def build():
    rows = read_trades(BOOK_DIR / 'trades.csv')
    trades = pair_trades(rows)
    state = json.loads((BOOK_DIR / 'state.json').read_text(encoding='utf-8'))
    wins = [t for t in trades if t['profit'] > 0]
    total = sum(t['profit'] for t in trades)
    held_cost = sum(p['cost'] for p in state['positions'].values())

    out = [f'# 모의투자 결과 — 대형주 모멘텀 ({config.BOOK})\n',
           f'`python report.py`로 `data/{config.BOOK}/`에서 자동 생성한 보고서입니다. '
           f'직접 고치지 말고, 메모는 [journal.md](journal.md)에 적습니다.\n',
           f'생성 시각: {datetime.now():%Y-%m-%d %H:%M}\n',
           '## 요약\n', '| 항목 | 값 |\n|---|---|',
           f'| 첫 거래 | {rows[0]["time"][:10] if rows else "-"} |',
           f'| 시작 자금 | {config.CAPITAL:,}원 |',
           f'| 현금 | {state["cash"]:,.0f}원 |',
           f'| 보유 종목 매입원가 | {held_cost:,.0f}원 ({len(state["positions"])}종목) |',
           f'| 마지막 교체 신호일 | {state.get("last_signal") or "-"} |',
           f'| 실현손익 (판 종목) | {won(total)}원 |',
           f'| 판 거래 수 | {len(trades)}건 (이익 {len(wins)}, 손실 {len(trades) - len(wins)}) |']
    if trades:
        out.append(f'| 승률 | {len(wins) / len(trades) * 100:.1f}% |')
        days = [t['days'] for t in trades if t['days'] != '']
        if days:
            out.append(f'| 평균 보유 기간 | {sum(days) / len(days):.1f}일 |')
        out.append(f'| 매도 비용 합계 (수수료, 세금) | {sum(t["cost"] for t in trades):,.0f}원 |')
    out.append('\n평가손익은 현재가가 필요해서 여기 넣지 않았습니다. 슬랙의 보유 현황 알림을 보세요.\n')

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
    out.append('\n손익은 체결가 기준이고, 수수료와 매도 세금은 config 값으로 추정한 것입니다.\n')
    return '\n'.join(out)


def main():
    if not (BOOK_DIR / 'state.json').exists():
        print(f'data/{config.BOOK} 에 장부가 없습니다. run.py 를 먼저 실행하세요.')
        return
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / f'{config.BOOK}.md'
    path.write_text(build(), encoding='utf-8')
    print(f'{path.relative_to(ROOT)} 생성')


if __name__ == '__main__':
    main()
