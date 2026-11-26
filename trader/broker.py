"""장부와 브로커

Ledger      : 공통 장부 (현금, 보유 종목, 저장, 매매 기록)
MockBroker  : 모의투자 계좌에 실제로 주문한다. 체결 수량과 체결가는 계좌에서 확인한 값을 쓴다.
PaperBroker : 주문 없이 장부에만 기록한다. 그날 종가에 슬리피지만큼 불리하게 체결한 것으로 친다
              (백테스트와 같은 비용 계산).

모의투자 주문 방식 (예전 mock_broker.py 를 그대로 옮김)
- 매수: 최유리지정가로 주문 → 2초 간격으로 체결 확인 → 덜 체결되면 잔량 취소 (쫓아 사지 않는다)
- 매도: 시장가 주문 → 체결 확인. 덜 팔리면 남은 수량은 장부에 남겨 다음에 다시 판다
- 주문 응답이 끊기면 다시 보내지 않고 잔고로 체결 여부를 확인한다 (두 번 사는 사고 방지)
- 체결가: 주문 체결 조회 값, 조회가 안 되면 매수는 잔고의 평균 매입가, 매도는 주문 직전 현재가
- 손익: 체결가 기준. 수수료와 세금은 config 값으로 추정 (모의투자 계좌가 실제로 떼는 비용과 다를 수 있음)

장부는 data/<book>/state.json, 매매 기록은 data/<book>/trades.csv 에 저장해서 껐다 켜도 이어진다.
가상 장부 현금은 전략에 배정한 자금(capital)으로 시작해 따로 관리한다.
모의투자 장부 현금은 매일 시작할 때 계좌의 주문 가능 현금으로 맞춘다 (capital은 수익률 계산 기준).
모의투자 계좌는 이 프로그램의 모의투자 전략 하나 전용으로 쓴다.
"""
import csv
import json
import os
import time
from datetime import datetime
from pathlib import Path

import config
from trader.kis_client import KISError, OrderUncertain

DATA_ROOT = Path(__file__).resolve().parent.parent / 'data'
TRADE_FIELDS = ['time', 'side', 'code', 'name', 'qty', 'price', 'amount',
                'fee', 'tax', 'profit', 'reason', 'version']
CHECK_TRIES = 3         # 주문 뒤 체결 확인 횟수
CHECK_GAP = 2           # 확인 간격(초)


def now_str():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class Ledger:
    account = ''

    def __init__(self, book, capital, note, version=''):
        self.book, self.capital, self.note, self.version = book, capital, note, version
        data_dir = DATA_ROOT / book
        data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = data_dir / 'state.json'
        self.trades_file = data_dir / 'trades.csv'
        if self.state_file.exists():
            self.state = json.loads(self.state_file.read_text(encoding='utf-8'))
        else:
            self.state = {'cash': capital, 'positions': {}, 'last_signal': ''}

    # ------------------------------------------------------------ 저장
    def save(self):
        tmp = self.state_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.state_file)            # 저장 중에 꺼져도 파일이 깨지지 않게

    def _record(self, row):
        new = not self.trades_file.exists()
        with open(self.trades_file, 'a', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new:
                w.writeheader()
            w.writerow({**row, 'version': self.version})

    # ------------------------------------------------------------ 조회
    @property
    def cash(self):
        return self.state['cash']

    @property
    def positions(self):
        return self.state['positions']

    def equity(self, prices):
        """현금 + 보유 종목 평가액 (prices: {code: 현재가}, 없으면 매입가)"""
        return self.cash + sum(p['qty'] * prices.get(c, p['avg_price']) for c, p in self.positions.items())

    def reconcile(self):
        """계좌와 장부 맞추기 (가상 장부는 할 일 없음)"""

    # ------------------------------------------------------------ 장부 기록
    def _book_buy(self, code, name, qty, avg, reason, order_no=''):
        amount = round(avg * qty)
        fee = round(amount * config.FEE_RATE)
        t = now_str()
        self.state['cash'] -= amount + fee
        self.last_fill = {'avg': avg, 'amount': amount + fee}
        if code in self.positions:                   # 같은 날 다시 사서 수량이 늘어난 경우
            p = self.positions[code]
            p['cost'] += amount + fee
            p['qty'] += qty
            p['avg_price'] = round(p['cost'] / p['qty'], 2)
        else:
            self.positions[code] = {'qty': qty, 'avg_price': round(avg, 2), 'cost': amount + fee,
                                    'name': name, 'time': t, 'order_no': order_no}
        self.save()
        self._record({'time': t, 'side': 'BUY', 'code': code, 'name': name, 'qty': qty,
                      'price': round(avg, 2), 'amount': amount, 'fee': fee, 'tax': 0, 'profit': '',
                      'reason': reason})

    def _book_sell(self, code, qty, avg, reason):
        p = self.positions[code]
        amount = round(avg * qty)
        fee = round(amount * config.FEE_RATE)
        tax = round(amount * config.STOCK_SELL_TAX)
        cost = p['cost'] * qty / p['qty']
        profit = round(amount - fee - tax - cost)
        self.state['cash'] += amount - fee - tax
        self.last_fill = {'avg': avg, 'amount': amount - fee - tax, 'rate': profit / cost if cost else 0.0}
        if qty >= p['qty']:
            self.positions.pop(code)
        else:                                        # 일부만 팔림: 남은 수량은 다음에 다시 판다
            p['qty'] -= qty
            p['cost'] -= cost
        self.save()
        self._record({'time': now_str(), 'side': 'SELL', 'code': code, 'name': p.get('name', code),
                      'qty': qty, 'price': round(avg, 2), 'amount': amount, 'fee': fee, 'tax': tax,
                      'profit': profit, 'reason': reason})
        return profit


class PaperBroker(Ledger):
    """가상 장부: price(그날 종가)에 슬리피지만큼 불리하게 체결"""
    account = 'paper'

    def buy(self, code, name, price, budget, reason=''):
        fill = price * (1 + config.SLIPPAGE)
        qty = int(min(budget, self.cash) // (fill * (1 + config.FEE_RATE)))
        if qty <= 0:
            self.note.log(f'{name}({code}) 살 돈이 모자람 (예산 {budget:,.0f}, 현금 {self.cash:,.0f}, 종가 {price:,})')
            return 0
        self._book_buy(code, name, qty, fill, reason)
        return qty

    def sell(self, code, price, reason=''):
        qty = self.positions[code]['qty']
        return self._book_sell(code, qty, price * (1 - config.SLIPPAGE), reason), qty


class MockBroker(Ledger):
    """모의투자 계좌에 실제 주문"""
    account = 'mock'

    def __init__(self, book, capital, note, version, client):
        super().__init__(book, capital, note, version)
        self.client = client

    def _held(self, code):
        return self.client.get_balance().get(code)

    def reconcile(self):
        held = self.client.get_balance()
        for code in list(self.positions):
            p = self.positions[code]
            if code not in held:                    # 장부에는 있는데 계좌에는 없음
                self.positions.pop(code)
                self.note.warn(f'장부의 {p.get("name", code)}({code}) {p["qty"]:,}주가 '
                               f'계좌에 없어 장부에서 지웠습니다. 체결 내역을 확인하세요.')
            elif held[code]['qty'] < p['qty']:
                self.note.warn(f'{p.get("name", code)}({code}) 장부 {p["qty"]:,}주, '
                               f'계좌 {held[code]["qty"]:,}주. 계좌 수량으로 맞춥니다.')
                p['cost'] = p['cost'] * held[code]['qty'] / p['qty']
                p['qty'] = held[code]['qty']
        self.save()
        others = [f'{v["name"]}({c}) {v["qty"]:,}주' for c, v in held.items() if c not in self.positions]
        if others:                                  # 직접 산 종목: 직접 거래 알림이 따로 알린다
            self.note.log('이 프로그램이 사지 않은 종목 (건드리지 않음) : ' + ', '.join(others))
        self.sync_cash()

    def sync_cash(self):
        """현금은 계좌 기준으로 맞춘다 (수수료·세금 추정 차이, 직접 거래로 쓴 돈을 반영)"""
        account_cash = self.client.get_buyable_cash()
        gap = account_cash - self.state['cash']
        if gap:
            self.note.log(f'장부 현금을 계좌에 맞춤 : {self.state["cash"]:,.0f}원 → {account_cash:,.0f}원 ({gap:+,.0f}원)')
            self.state['cash'] = account_cash
            self.save()

    def buy(self, code, name, price, budget, reason=''):
        """최유리지정가 매수. 체결된 수량을 돌려준다 (0이면 못 삼)"""
        before = (self._held(code) or {}).get('qty', 0)
        account_cash = self.client.get_buyable_cash(code)
        money = min(budget, self.cash, account_cash)
        qty = int(money // (price * (1 + config.SLIPPAGE) * (1 + config.FEE_RATE)))
        if qty <= 0:
            self.note.log(f'{name}({code}) 살 돈이 모자람 (예산 {budget:,.0f}, 장부 현금 {self.cash:,.0f}, '
                          f'계좌 주문가능 {account_cash:,.0f}, 현재가 {price:,})')
            return 0
        order = None
        try:
            order = self.client.order('buy', code, qty, best=True)   # 최유리지정가
        except OrderUncertain as ex:
            self.note.warn(f'{name}({code}) 매수 주문 응답 없음, 잔고로 확인합니다 ({ex})')
        held_qty = before
        for _ in range(CHECK_TRIES):
            time.sleep(CHECK_GAP)
            held_qty = (self._held(code) or {}).get('qty', 0)
            if held_qty - before >= qty:
                break
        if held_qty - before < qty and order and order.get('order_no'):
            try:
                self.client.cancel(order)                # 미체결 잔량 취소
            except KISError:
                pass                                     # 이미 다 체결됐으면 취소할 것이 없다
            time.sleep(CHECK_GAP)
            held_qty = (self._held(code) or {}).get('qty', 0)
        filled = held_qty - before
        if filled <= 0:
            self.note.warn(f'매수 미체결 : {name}({code}) {qty:,}주. 다음 교체 때 다시 시도')
            return 0

        fill = self.client.get_fill(order['order_no'], code) if order else None
        avg = fill['avg_price'] if fill and fill['qty'] else \
            (self._held(code) or {}).get('avg_price') or price
        note = '' if filled == qty else f' (주문 {qty:,}주 중 일부 체결, 나머지 취소)'
        self._book_buy(code, name, filled, avg, reason + note, order['order_no'] if order else '')
        return filled

    def sell(self, code, price, reason=''):
        """시장가 매도. (손익, 판 수량)을 돌려준다. 팔 수 없으면 KISError"""
        p = self.positions[code]
        held = self._held(code)
        if not held:                                     # 이미 팔렸거나 계좌에 없음
            self.positions.pop(code)
            self.save()
            raise KISError(f'{p.get("name", code)}({code})이 계좌에 없어 장부에서 지웠습니다.')
        qty = min(p['qty'], held['sellable'])
        if qty <= 0:
            raise KISError(f'{p.get("name", code)}({code}) 매도 가능 수량이 없습니다.')
        order = None
        try:
            order = self.client.order('sell', code, qty)  # 시장가
        except OrderUncertain as ex:
            self.note.warn(f'{p.get("name", code)}({code}) 매도 주문 응답 없음, 잔고로 확인합니다 ({ex})')
        remain = held['qty']
        for _ in range(CHECK_TRIES):
            time.sleep(CHECK_GAP)
            remain = (self._held(code) or {}).get('qty', 0)
            if remain <= held['qty'] - qty:
                break
        sold = min(held['qty'] - remain, p['qty'])
        if sold <= 0:
            raise KISError(f'{p.get("name", code)}({code}) 매도가 체결되지 않았습니다.')

        fill = self.client.get_fill(order['order_no'], code) if order else None
        avg = fill['avg_price'] if fill and fill['qty'] else price
        profit = self._book_sell(code, sold, avg, reason + ('' if fill else ' (체결가는 주문 직전 현재가로 추정)'))
        return profit, sold
