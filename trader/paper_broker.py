"""가상 장부 (페이퍼 브로커)

실제 계좌에 주문하지 않고, 가상 현금과 보유 종목을 직접 관리한다.
상태는 data/state.json에, 매매 기록은 data/trades.csv에 저장해서
프로그램을 껐다 켜도 이어진다.

체결 가격은 신호가 난 순간의 현재가에 SLIPPAGE만큼 불리하게 잡고,
수수료(FEE_RATE)와 일반 주식 매도 세금(STOCK_SELL_TAX)을 뺀다.
호가 물량, 일부 체결 같은 상황은 재현하지 않으므로 실제보다 결과가 조금 좋게 나온다.
"""
import csv
import json
import os
from datetime import datetime
from pathlib import Path

import config

DATA_DIR = Path(__file__).resolve().parent.parent / 'data'
STATE_FILE = DATA_DIR / 'state.json'
TRADES_FILE = DATA_DIR / 'trades.csv'
TRADE_FIELDS = ['time', 'side', 'code', 'name', 'qty', 'price', 'amount',
                'fee', 'tax', 'profit', 'reason']


class PaperBroker:
    def __init__(self):
        DATA_DIR.mkdir(exist_ok=True)
        if STATE_FILE.exists():
            self.state = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        else:
            self.state = {'cash': config.INITIAL_CASH, 'positions': {},
                          'date': '', 'today': {}}

    # ------------------------------------------------------------ 저장
    def _save(self):
        tmp = STATE_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        os.replace(tmp, STATE_FILE)            # 저장 중에 꺼져도 파일이 깨지지 않게

    def _record(self, row):
        new = not TRADES_FILE.exists()
        with open(TRADES_FILE, 'a', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)

    # ------------------------------------------------------------ 하루 시작
    def start_day(self, today):
        """날짜가 바뀌었으면 오늘의 기록을 새로 시작한다."""
        if self.state['date'] != today:
            self.state['date'] = today
            self.state['today'] = {'traded': [], 'realized': 0, 'wins': 0,
                                   'losses': 0,
                                   'start_equity': self.state['cash'] + sum(
                                       p['qty'] * p['avg_price']
                                       for p in self.state['positions'].values())}
            self._save()

    # ------------------------------------------------------------ 조회
    @property
    def cash(self):
        return self.state['cash']

    @property
    def positions(self):
        return self.state['positions']

    def traded_today(self, code):
        return code in self.state['today'].get('traded', [])

    def equity(self, prices):
        """총자산 = 현금 + 보유 종목 평가금액 (prices: {code: 현재가})"""
        return self.cash + sum(p['qty'] * prices.get(code, p['avg_price'])
                               for code, p in self.positions.items())

    # ------------------------------------------------------------ 가상 체결
    def buy(self, code, price, budget, reason=''):
        """budget 안에서 살 수 있는 만큼 가상 매수. 체결 수량을 돌려준다."""
        name, _ = config.SYMBOLS[code]
        fill = price * (1 + config.SLIPPAGE)
        qty = int(min(budget, self.cash) // (fill * (1 + config.FEE_RATE)))
        if qty <= 0:
            return 0
        amount = round(fill * qty)
        fee = round(amount * config.FEE_RATE)
        self.state['cash'] -= amount + fee
        self.positions[code] = {
            'qty': qty, 'avg_price': round(fill, 2),
            'cost': amount + fee,                 # 수수료까지 포함한 매수 금액
            'high': price,                        # 매수 후 최고가 (전략에서 사용)
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        self.state['today'].setdefault('traded', []).append(code)
        self._save()
        self._record({'time': self.positions[code]['time'], 'side': 'BUY',
                      'code': code, 'name': name, 'qty': qty,
                      'price': round(fill, 2), 'amount': amount, 'fee': fee,
                      'tax': 0, 'profit': '', 'reason': reason})
        return qty

    def sell(self, code, price, reason=''):
        """보유 수량 전부 가상 매도. 실현 손익(원)을 돌려준다."""
        p = self.positions.pop(code)
        name, kind = config.SYMBOLS.get(code, (code, 'stock'))
        fill = price * (1 - config.SLIPPAGE)
        amount = round(fill * p['qty'])
        fee = round(amount * config.FEE_RATE)
        tax = round(amount * config.STOCK_SELL_TAX) if kind == 'stock' else 0
        proceeds = amount - fee - tax
        profit = proceeds - p['cost']
        self.state['cash'] += proceeds
        today = self.state['today']
        today['realized'] = today.get('realized', 0) + profit
        today['wins' if profit > 0 else 'losses'] = \
            today.get('wins' if profit > 0 else 'losses', 0) + 1
        self._save()
        self._record({'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      'side': 'SELL', 'code': code, 'name': name,
                      'qty': p['qty'], 'price': round(fill, 2),
                      'amount': amount, 'fee': fee, 'tax': tax,
                      'profit': profit, 'reason': reason})
        return profit, p

    def update_high(self, code, price):
        """보유 종목의 매수 후 최고가를 갱신한다."""
        p = self.positions.get(code)
        if p and price > p['high']:
            p['high'] = price
            self._save()

    # ------------------------------------------------------------ 보고
    def summary(self, prices):
        """현재 가상 잔고와 오늘 성과를 문자열로"""
        eq = self.equity(prices)
        t = self.state['today']
        lines = [f'가상 잔고 : 총자산 {eq:,.0f}원 (현금 {self.cash:,.0f}원), '
                 f'처음 대비 {eq - config.INITIAL_CASH:+,.0f}원']
        for code, p in self.positions.items():
            name = config.SYMBOLS.get(code, (code,))[0]
            now = prices.get(code, p['avg_price'])
            pnl = now * p['qty'] - p['cost']
            lines.append(f'  {name}({code}) {p["qty"]:,}주, 매입가 {p["avg_price"]:,.0f}, '
                         f'현재가 {now:,}, 평가손익 {pnl:+,.0f}원')
        n = t.get('wins', 0) + t.get('losses', 0)
        lines.append(f'오늘 : 실현손익 {t.get("realized", 0):+,.0f}원, 매도 {n}건'
                     + (f' (이익 {t.get("wins", 0)}, 손실 {t.get("losses", 0)})' if n else ''))
        return '\n'.join(lines)
