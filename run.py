"""대형주 모멘텀 매매 실행기 (주 1회 교체, 모의투자 + 가상 장부)

    python run.py           오늘 할 일을 하고 종료 (교체일이면 모의투자 15:10~15:19 주문, 가상 15:31 종가 체결)
    python run.py --check   주문 없이 "오늘 교체한다면 무엇을 사고팔지"만 전략별로 출력

하루 흐름
    1. KRX Open API에서 어제까지의 시세를 받는다 (아직 안 올라왔으면 10분마다 다시, 14:30까지)
    2. 전략마다, 지난주 신호일(그 주 마지막 거래일)의 교체를 아직 안 했으면 오늘이 교체일
       - 아침에 팔 종목, 살 후보를 그 전략의 슬랙 채널로 알린다
       - 모의투자: 15:10 매도 → 15:12 매수 → 15:17 못 산 종목 다시 매수
       - 가상 장부: 15:31 그날 종가로 매도 → 매수
    3. 교체일이 아니면 보유 현황만 알리고 끝낸다

전략 목록과 시간표는 config.py, 순위 계산은 momentum.py 에 있다.
한 전략에서 오류가 나도 다른 전략은 계속 돈다.
"""
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env', override=True)        # 이 폴더의 .env만 읽는다

import config                                     # noqa: E402
import momentum as M                              # noqa: E402
from trader.broker import MockBroker, PaperBroker  # noqa: E402
from trader.kis_client import KISClient, KISError  # noqa: E402
from trader.market_data import NotReady, update, fetch_data, PROBE  # noqa: E402
from trader.notifier import Notifier              # noqa: E402
from trader import messages as MSG                # noqa: E402

LOCK_PORT = 47285       # 중복 실행 방지용
LOAD_DAYS = 300         # 계산에 읽는 최근 거래일 수 (모멘텀 최대 250+5일, 여유 포함)
ACCOUNT_LABEL = {'mock': '모의', 'paper': '가상'}


def hhmm(t=None):
    t = t or datetime.now()
    return t.hour * 100 + t.minute


def fmt(t):
    return f'{t // 100}:{t % 100:02d}'


def wait_until(t):
    while hhmm() < t:
        time.sleep(10)


# ------------------------------------------------------------------ 전략 하나
class Runner:
    def __init__(self, spec, client, check=False):
        self.spec, self.client = spec, client
        self.lookback, self.hold = spec['lookback'], spec['hold']
        label = '점검' if check else ACCOUNT_LABEL[spec['account']]
        self.note = Notifier(spec['channel'], f'{label}·{spec["name"]} {M.VERSION}')
        self.note.quiet = check                     # 점검은 슬랙으로 보내지 않는다
        if spec['account'] == 'mock':
            self.broker = MockBroker(spec['book'], spec['capital'], self.note, M.VERSION, client)
        else:
            self.broker = PaperBroker(spec['book'], spec['capital'], self.note, M.VERSION)
        self.d = None           # 오늘의 결정 (교체일일 때만)
        self.events = []        # 오늘 사고판 내역 (교체 완료 메시지용)
        self.ok = True          # 시작 준비가 됐는지

    @property
    def mock(self):
        return self.spec['account'] == 'mock'

    def plan(self, P, signal, names):
        """오늘 교체할 일이 있으면 self.d 에 결정을 담고 True"""
        if self.broker.state.get('last_signal') == f'{signal:%Y-%m-%d}':
            self.d = None
            return False
        self.d = M.decide(P, signal, list(self.broker.positions), self.lookback, self.hold)
        self.d.update(names=names, signal=signal, late=P['dates'][-1] > signal)
        return True

    def prices(self, known=None):
        """보유 종목 현재가 {code: price}. known 에 있으면 다시 조회하지 않는다"""
        out = dict(known or {})
        for code, p in self.broker.positions.items():
            if code not in out:
                try:
                    out[code] = self.client.get_price(code)['price']
                except KISError:
                    out[code] = p['avg_price']
        return out

    # -------------------------------------------------------------- 교체
    def sell_all(self, prices):
        for code, why in self.d['sells']:
            p = self.broker.positions.get(code)
            if not p:
                continue
            q = prices.get(code) or self.client.get_price(code)
            if q['volume'] <= 0 or q['price'] <= 0:
                self.note.warn(f'매도 건너뜀 : {p["name"]} 오늘 거래 없음 (거래정지 등). 다음 교체 때 다시 판단')
                continue
            name = p['name']
            try:
                profit, sold = self.broker.sell(code, q['price'], why)
                f = self.broker.last_fill
                self.events.append({'side': '매도', 'name': name, 'qty': sold, 'amount': f['amount'],
                                    'profit': profit, 'rate': f['rate']})
                self.note.log(f'매도 : {name}({code}) {sold:,}주 x {f["avg"]:,.0f}, 손익 {profit:+,}원 | {why}')
            except KISError as ex:
                self.note.warn(f'매도 실패 : {ex} 다음 교체 때 다시 판단')

    def buy_round(self, prices, skip, stop=None):
        """빈자리를 순위순으로 채운다. prices: {code: quote} (없으면 조회). skip: 거래가 없어 건너뛸 종목"""
        d = self.d

        def quote(c):
            if c not in prices:
                prices[c] = self.client.get_price(c)
            return prices[c]

        target = self.broker.equity({c: quote(c)['price'] for c in self.broker.positions}) / self.hold
        for code in d['buys']:
            if len(self.broker.positions) >= self.hold or (stop and hhmm() >= stop):
                break
            if code in self.broker.positions or code in skip:
                continue
            name = d['names'].get(code, code)
            q = quote(code)
            if q['volume'] <= 0 or q['price'] <= 0:
                self.note.warn(f'매수 건너뜀 : {name} 오늘 거래 없음 (거래정지 등)')
                skip.add(code)
                continue
            reason = f'모멘텀 {int(d["rank"][code])}위 ({d["mom"][code]:+.1%}, 신호일 {d["signal"]:%m/%d})'
            filled = self.broker.buy(code, name, q['price'], target, reason)
            if filled:
                f = self.broker.last_fill
                self.events.append({'side': '매수', 'name': name, 'qty': filled, 'amount': f['amount'],
                                    'note': f'모멘텀 {int(d["rank"][code])}위 {d["mom"][code]:+.0%}'})
                self.note.log(f'매수 : {name}({code}) {filled:,}주 x {f["avg"]:,.0f} | {reason}')

    def finish(self, prices=None):
        self.broker.state['last_signal'] = f'{self.d["signal"]:%Y-%m-%d}'
        self.broker.save()
        if self.d['market_ok'] and len(self.broker.positions) < self.hold:
            self.note.warn(f'{self.hold}종목 중 {len(self.broker.positions)}종목만 보유 중. 빈자리는 다음 교체 때 채움')
        self.note.post(*MSG.result(self, self.prices({c: q['price'] for c, q in (prices or {}).items()})))
        self.note.flush()

    def rebalance_mock(self):
        """15:10 매도 → 15:12 매수 → 15:17 다시 매수 (현재가로 주문)"""
        self.sell_all({})
        skip = set()
        if self.d['market_ok']:
            wait_until(config.BUY_TIME)
            self.buy_round({}, skip, config.STOP_TIME)
            if len(self.broker.positions) < self.hold and hhmm() < config.STOP_TIME:
                wait_until(config.RETRY_TIME)
                self.buy_round({}, skip, config.STOP_TIME)   # 미체결 종목과 다음 순위 종목을 다시 시도
        self.finish()

    def rebalance_paper(self):
        """15:31 그날 종가로 매도 → 매수"""
        prices = {}
        for code in list(self.broker.positions):
            prices[code] = self.client.get_price(code)
        self.sell_all(prices)
        if self.d['market_ok']:
            self.buy_round(prices, set())
        self.finish(prices)


# ------------------------------------------------------------------ 데이터
def get_data(client, notes, wait):
    """어제까지 시세를 받는다. 성공하면 True. wait=False면 한 번만 시도한다."""
    first = notes[0]
    while True:
        try:
            got = update(client)
            if got:
                first.log(f'시세 받음 : {", ".join(got)}')
            return True
        except NotReady as ex:
            if not wait or hhmm() >= config.DATA_DEADLINE:
                for n in notes:
                    n.warn(f'시세 없음 : {ex}' + (f' {fmt(config.DATA_DEADLINE)}까지 기다렸지만 받지 못함' if wait else ''))
                return False
            first.log(f'{ex} {config.DATA_RETRY_MIN}분 뒤 다시 시도합니다.')
            time.sleep(config.DATA_RETRY_MIN * 60)
        except (fetch_data.FetchError, KISError, OSError) as ex:
            for n in notes:
                n.warn(f'시세 받기 실패 : {ex}')
            return False


def load_signal():
    panel, index = M.load(LOAD_DAYS)
    P = M.prepare(panel, index)
    signal = M.latest_signal(P['dates'], datetime.now())
    if signal is None:
        raise RuntimeError('신호일을 찾지 못했습니다 (데이터 부족).')
    return P, signal, P['names']


def guarded(runner, what, fn):
    """한 전략의 오류가 다른 전략을 멈추지 않게"""
    try:
        fn()
    except (KISError, OSError) as ex:
        runner.note.warn(f'{what} 중 오류 : {ex}')
        runner.note.flush()


def warn_all(runners, message, footer=''):
    for r in runners:
        r.note.warn(message)
        r.note.flush(footer)


def post_status(runners):
    """교체가 없는 전략들의 보유 현황. 같은 채널의 전략은 메시지 하나로 묶는다"""
    groups = {}
    for r in runners:
        groups.setdefault(r.spec['channel'], []).append(r)
    for group in groups.values():
        try:
            prices = {}
            for r in group:
                prices = r.prices(prices)
            group[0].note.post(*MSG.status(group, prices))
        except (KISError, OSError) as ex:
            warn_all(group, f'보유 현황 알림 중 오류 : {ex}')


# ------------------------------------------------------------------ 실행
def check(client):
    runners = [Runner(s, client, check=True) for s in config.STRATEGIES]
    get_data(client, [r.note for r in runners], wait=False)
    P, signal, names = load_signal()
    idle = []
    for r in runners:
        if r.plan(P, signal, names):
            r.note.post(*MSG.plan(r, '점검'))
        else:
            idle.append(r)
    if idle:
        post_status(idle)
    try:
        held = client.get_balance()
        print('\n모의계좌 보유 :', ', '.join(f'{v["name"]}({c}) {v["qty"]:,}주' for c, v in held.items()) or '없음')
    except KISError as ex:
        print('\n모의계좌 조회 실패 :', ex)


def main():
    client = KISClient()
    if '--check' in sys.argv:
        check(client)
        return

    lock = socket.socket()                          # 같은 프로그램이 두 개 돌지 않게
    try:
        lock.bind(('127.0.0.1', LOCK_PORT))
    except OSError:
        print('이미 실행 중입니다. 이 창은 종료합니다.')
        return
    if datetime.now().weekday() >= 5:
        print('주말에는 실행하지 않습니다.')
        return

    runners = [Runner(s, client) for s in config.STRATEGIES]
    for r in runners:
        r.note.log(f'슬랙 알림 : {r.note.status()}')
        for attempt in range(1, 6):                 # 계좌 조회 실패 시 30초 간격으로 재시도
            try:
                r.broker.reconcile()
                break
            except (KISError, OSError) as ex:
                if attempt == 5:
                    r.ok = False
                    r.note.warn(f'계좌 조회 실패, 오늘은 이 전략을 돌리지 않습니다 : {ex}')
                    break
                r.note.log(f'계좌 조회 실패 ({attempt}/5) : {ex} → 30초 뒤 다시 시도')
                time.sleep(30)
        r.note.flush()
    runners = [r for r in runners if r.ok]

    if not get_data(client, [r.note for r in runners], wait=True):
        for r in runners:
            r.note.flush('오늘은 교체하지 않습니다.')
        post_status(runners)
        return
    P, signal, names = load_signal()
    todo, idle = [], []
    for r in runners:
        if r.plan(P, signal, names):
            when = (f'{fmt(config.SELL_TIME)} 매도 · {fmt(config.BUY_TIME)} 매수' if r.mock
                    else f'{fmt(config.PAPER_TIME)} 종가 체결')
            r.note.post(*MSG.plan(r, when))
            todo.append(r)
        else:
            idle.append(r)
    if idle:
        post_status(idle)
    if not todo:
        return

    mocks = [r for r in todo if r.mock]
    papers = [r for r in todo if not r.mock]

    if mocks:
        if hhmm() >= config.STOP_TIME:
            warn_all(mocks, f'교체 시각({fmt(config.SELL_TIME)})이 지나서 시작했습니다. 다음 실행 때 교체합니다.')
        else:
            mocks[0].note.log(f'{fmt(config.SELL_TIME)}까지 기다립니다.')
            wait_until(config.SELL_TIME)
            if client.get_price(PROBE)['volume'] <= 0:
                warn_all(todo, '오늘은 거래가 없는 날(휴장)로 보여 주문하지 않습니다. 다음 실행 때 교체합니다.')
                return
            for r in mocks:
                guarded(r, '모의투자 교체', r.rebalance_mock)

    if papers:
        if hhmm() >= config.PAPER_DEADLINE:
            warn_all(papers, f'가상 체결 시각({fmt(config.PAPER_TIME)})이 지나서 시작했습니다. 다음 실행 때 교체합니다.')
            return
        papers[0].note.log(f'{fmt(config.PAPER_TIME)}까지 기다립니다 (종가 체결).')
        wait_until(config.PAPER_TIME)
        if client.get_price(PROBE)['volume'] <= 0:
            warn_all(papers, '오늘은 거래가 없는 날(휴장)로 보여 체결하지 않습니다. 다음 실행 때 교체합니다.')
            return
        for r in papers:
            guarded(r, '가상 교체', r.rebalance_paper)


if __name__ == '__main__':
    try:
        main()
    except KISError as e:
        print('실패 :', e)
        sys.exit(1)
    except KeyboardInterrupt:
        print('사용자가 중단했습니다. 장부(data/)는 그대로 남고, 모의투자 계좌의 보유 종목은 직접 확인하세요.')
