"""가상 매매 실행기

    python run_paper.py           장중 가상 매매 (09:05~15:15, 15:20 종료)
    python run_paper.py --check   지금 시점의 종목별 판단만 출력 (가상 매매 안 함, 언제든 실행 가능)

실제 계좌에는 주문하지 않는다. 시세만 한국투자증권 API로 받아 오고,
사고파는 것은 data/state.json의 가상 장부에서만 처리한다.
"""
import os
import sys
import time
import socket
import importlib
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=True)        # 이 폴더의 .env만 읽는다

import config                                     # noqa: E402
from trader.kis_client import KISClient, KISError  # noqa: E402
from trader.paper_broker import PaperBroker      # noqa: E402
from trader.notifier import Notifier             # noqa: E402

LOCK_PORT = 47285       # 중복 실행 방지용 (8장 자동매매는 47284)


def load_strategy():
    module_name, class_name = config.STRATEGY.rsplit('.', 1)
    module = importlib.import_module('strategies.' + module_name)
    return getattr(module, class_name)()


def hhmm(t):
    return t.hour * 100 + t.minute


class PaperTrader:
    def __init__(self):
        self.client = KISClient()
        self.broker = PaperBroker()
        self.note = Notifier()
        self.strategy = load_strategy()
        self.infos = {}             # 종목별 하루 한 번 계산한 값
        self.no_data = set()
        self.prices = {}            # 마지막으로 본 현재가
        self.budget = 0

    # ------------------------------------------------------------ 준비
    def prepare_day(self):
        today = datetime.now().strftime('%Y%m%d')
        self.broker.start_day(today)
        equity = self.broker.equity({})
        self.budget = int(equity * config.BUDGET_PER_POSITION)
        self.note.log('슬랙 알림 : ' + self.note.status())
        self.note.send(f'가상 매매 시작 : {self.strategy.name}\n'
                       f'총자산 {equity:,.0f}원, 종목당 {config.BUDGET_PER_POSITION:.0%} '
                       f'= {self.budget:,}원, 보유 {len(self.broker.positions)}종목')

    def info(self, code):
        """종목별 판단 값 (하루 한 번 계산)"""
        if code not in self.infos:
            today = datetime.now().strftime('%Y%m%d')
            info = self.strategy.prepare(self.client, code, today)
            if info is None:
                if code not in self.no_data:
                    self.no_data.add(code)
                    self.note.log(f'{config.SYMBOLS[code][0]}({code}) : 오늘 시세가 아직 없습니다.')
                return None
            self.infos[code] = info
            self.note.log(f'{config.SYMBOLS[code][0]}({code}) : {self.strategy.describe(info)}')
        return self.infos[code]

    # ------------------------------------------------------------ 한 바퀴
    def scan(self, can_buy=True):
        """모든 종목을 한 번씩 보고 가상 매수, 매도를 판단한다."""
        for code, (name, _) in config.SYMBOLS.items():
            info = self.info(code)
            if info is None:
                continue
            price = self.client.get_price(code)['price']
            self.prices[code] = price
            pos = self.broker.positions.get(code)
            if pos:                                        # 보유 중: 매도 판단
                self.broker.update_high(code, price)
                reason = self.strategy.should_sell(info, pos, price)
                if reason:
                    self.sell(code, price, reason)
            elif (can_buy and not self.broker.traded_today(code)
                  and len(self.broker.positions) < config.MAX_POSITIONS):
                reason = self.strategy.should_buy(info, price)
                if reason:
                    qty = self.broker.buy(code, price, self.budget, reason)
                    if qty:
                        p = self.broker.positions[code]
                        self.note.send(f'매수 : {name}({code}) {qty:,}주, '
                                       f'체결가 {p["avg_price"]:,.0f}원 - {reason}')

    def sell(self, code, price, reason):
        name = config.SYMBOLS.get(code, (code,))[0]
        profit, p = self.broker.sell(code, price, reason)
        rate = profit / p['cost'] * 100
        self.note.send(f'매도 : {name}({code}) {p["qty"]:,}주, 매입가 {p["avg_price"]:,.0f} '
                       f'→ {price:,}원, 손익 {profit:+,}원 ({rate:+.2f}%) - {reason}')

    def sell_all(self, reason):
        for code in list(self.broker.positions):
            price = self.client.get_price(code)['price']
            self.prices[code] = price
            self.sell(code, price, reason)

    # ------------------------------------------------------------ 하루 실행
    def run(self):
        self.prepare_day()
        reported_hour = None
        last_error = ''
        while True:
            now = datetime.now()
            t = hhmm(now)
            if now.weekday() >= 5:
                self.note.log('주말에는 실행하지 않습니다.')
                return
            try:
                if config.BUY_START <= t < config.EXIT_TIME:
                    self.scan(can_buy=True)
                    if now.minute >= config.REPORT_MINUTE and reported_hour != now.hour:
                        reported_hour = now.hour
                        self.note.send(self.broker.summary(self.prices))
                elif config.EXIT_TIME <= t < config.STOP_TIME:
                    self.sell_all(f'{config.EXIT_TIME // 100}:{config.EXIT_TIME % 100:02d} 일괄 매도')
                    self.note.send('장 마감 정리\n' + self.broker.summary(self.prices))
                    self.note.send('가상 매매를 종료합니다.')
                    return
                elif t >= config.STOP_TIME:
                    self.note.send('가상 매매를 종료합니다.')
                    return
                if last_error:
                    self.note.send('오류에서 복구되어 계속 감시합니다.')
                last_error = ''
            except (KISError, OSError) as ex:
                if str(ex) != last_error:                  # 같은 오류는 한 번만 알림
                    last_error = str(ex)
                    self.note.send(f'오류 : {ex}')
                time.sleep(10)
            time.sleep(config.LOOP_SLEEP)

    def check(self):
        """지금 시점의 종목별 판단만 출력 (가상 매매 안 함)"""
        today = datetime.now().strftime('%Y%m%d')
        print(f'전략 : {self.strategy.name}')
        print(f'가상 장부 : 현금 {self.broker.cash:,.0f}원, 보유 {len(self.broker.positions)}종목')
        for code, (name, _) in config.SYMBOLS.items():
            info = self.strategy.prepare(self.client, code, today)
            if info is None:
                print(f'{name}({code}) : 오늘 시세가 없습니다 (장 시작 전이거나 휴장일).')
                continue
            price = self.client.get_price(code)['price']
            buy = self.strategy.should_buy(info, price)
            print(f'{name}({code}) : {self.strategy.describe(info)}, 현재가 {price:,}'
                  f'  →  {"매수 신호: " + buy if buy else "대기"}')


def main():
    trader = PaperTrader()
    if '--check' in sys.argv:
        trader.check()
        return
    lock = socket.socket()                          # 같은 프로그램이 두 개 돌지 않게
    try:
        lock.bind(('127.0.0.1', LOCK_PORT))
    except OSError:
        trader.note.log('가상 매매가 이미 실행 중입니다. 이 창은 종료합니다.')
        return
    trader.run()


if __name__ == '__main__':
    try:
        main()
    except KISError as e:
        print('시작 실패 :', e)
        sys.exit(1)
    except KeyboardInterrupt:
        print('사용자가 중단했습니다. 가상 장부(data/state.json)는 그대로 남습니다.')
