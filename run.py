"""대형주 모멘텀 매매 실행기 (모의투자 계좌, 주 1회 교체)

    python run.py           오늘 할 일을 하고 종료 (교체일이면 15:10~15:19에 주문)
    python run.py --check   주문 없이 "오늘 교체한다면 무엇을 사고팔지"만 출력

하루 흐름
    1. KRX Open API에서 어제까지의 시세를 받는다 (아직 안 올라왔으면 10분마다 다시, 14:30까지)
    2. 지난주 신호일(그 주 마지막 거래일)의 교체를 아직 안 했으면 오늘이 교체일
       - 아침에 팔 종목, 살 후보를 슬랙으로 알린다
       - 15:10 팔 종목 매도 → 15:12 살 종목 매수 → 15:17 못 산 종목 다시 매수
    3. 교체일이 아니면 보유 현황만 알리고 끝낸다

규칙은 momentum.py, 시간표와 자금은 config.py 에 있다.
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
from trader.broker import Broker                  # noqa: E402
from trader.kis_client import KISClient, KISError  # noqa: E402
from trader.market_data import NotReady, update, fetch_data, PROBE  # noqa: E402
from trader.notifier import Notifier              # noqa: E402

LOCK_PORT = 47285       # 중복 실행 방지용
LOAD_DAYS = 300         # 계산에 읽는 최근 거래일 수 (모멘텀 120+5일, 여유 포함)


def hhmm(t=None):
    t = t or datetime.now()
    return t.hour * 100 + t.minute


def fmt(t):
    return f'{t // 100}:{t % 100:02d}'


def wait_until(t):
    while hhmm() < t:
        time.sleep(10)


def won(x):
    return f'{x:,.0f}원'


# ------------------------------------------------------------------ 데이터
def get_data(client, note, wait):
    """어제까지 시세를 받는다. 성공하면 True. wait=False면 한 번만 시도한다."""
    while True:
        try:
            got = update(client)
            if got:
                note.log(f'시세 받음 : {", ".join(got)}')
            return True
        except NotReady as ex:
            if not wait or hhmm() >= config.DATA_DEADLINE:
                note.send(f'시세 없음 : {ex} ({fmt(config.DATA_DEADLINE)}까지 기다렸지만 받지 못함)'
                          if wait else f'시세 없음 : {ex}')
                return False
            note.log(f'{ex} {config.DATA_RETRY_MIN}분 뒤 다시 시도합니다.')
            time.sleep(config.DATA_RETRY_MIN * 60)
        except (fetch_data.FetchError, KISError, OSError) as ex:
            note.send(f'시세 받기 실패 : {ex}')
            return False


def plan(broker, today):
    """오늘의 신호일, 계산 결과, 결정을 돌려준다"""
    panel, index = M.load(LOAD_DAYS)
    P = M.prepare(panel, index)
    signal = M.latest_signal(P['dates'], today)
    if signal is None:
        raise RuntimeError('신호일을 찾지 못했습니다 (데이터 부족).')
    d = M.decide(P, signal, list(broker.positions))
    p = P['dates'].get_loc(signal)
    d['mom'] = M.momentum(P, p)
    d['names'] = P['names']
    d['signal'] = signal
    d['late'] = P['dates'][-1] > signal              # 신호일 뒤에 이미 지나간 거래일이 있음
    return d


def describe(d, broker):
    names, rank, mom = d['names'], d['rank'], d['mom']

    def label(c):
        r = f'{int(rank[c])}위' if c in rank.index else '순위 밖'
        return f'{names.get(c, c)}({c}) {r} {mom.get(c, float("nan")):+.1%}'

    lines = [f'신호일 {d["signal"]:%Y-%m-%d}, 시장 필터 {"통과 (매수 가능)" if d["market_ok"] else "걸림 (전부 매도, 매수 안 함)"}']
    lines.append('계속 보유 : ' + (', '.join(label(c) for c in d['keep']) or '없음'))
    lines.append('매도 : ' + (', '.join(f'{label(c)} - {why}' for c, why in d['sells']) or '없음'))
    lines.append(f'매수 빈자리 {d["free"]}개, 후보(순위순) : '
                 + (', '.join(label(c) for c in d['buys']) if d['free'] else '없음'))
    return '\n'.join(lines)


# ------------------------------------------------------------------ 주문
def buy_round(client, broker, note, d, skip):
    """빈자리를 순위순으로 채운다. skip: 거래가 없어 건너뛸 종목 (이 함수가 채운다)"""
    prices = {c: client.get_price(c)['price'] for c in broker.positions}
    target = broker.equity(prices) / M.HOLD
    for code in d['buys']:
        if len(broker.positions) >= M.HOLD or hhmm() >= config.STOP_TIME:
            break
        if code in broker.positions or code in skip:
            continue
        name = d['names'].get(code, code)
        q = client.get_price(code)
        if q['volume'] <= 0 or q['price'] <= 0:
            note.send(f'매수 건너뜀 : {name}({code}) 오늘 거래 없음 (거래정지 등)')
            skip.add(code)
            continue
        reason = f'모멘텀 {int(d["rank"][code])}위 ({d["mom"][code]:+.1%}, 신호일 {d["signal"]:%m/%d})'
        filled = broker.buy(code, name, q['price'], target, reason)
        if filled:
            p = broker.positions[code]
            note.send(f'매수 : {name}({code}) {filled:,}주 x {p["avg_price"]:,.0f} = {won(p["cost"])} | {reason}')


def rebalance(client, broker, note, d):
    if hhmm() >= config.STOP_TIME:
        note.send(f'교체 시각({fmt(config.SELL_TIME)})이 지나서 시작했습니다. 다음 실행 때 교체합니다.')
        return
    note.log(f'{fmt(config.SELL_TIME)}까지 기다립니다.')
    wait_until(config.SELL_TIME)
    probe = client.get_price(PROBE)
    if probe['volume'] <= 0:
        note.send('오늘은 거래가 없는 날(휴장)로 보여 주문하지 않습니다. 다음 실행 때 교체합니다.')
        return

    for code, why in d['sells']:
        p = broker.positions.get(code)
        if not p:
            continue
        cost_per_share = p['cost'] / p['qty']
        try:
            price = client.get_price(code)['price']
            profit, sold = broker.sell(code, price, why)
            rate = profit / (cost_per_share * sold)
            note.send(f'매도 : {p["name"]}({code}) {sold:,}주, 손익 {profit:+,}원 ({rate:+.1%}) | {why}')
        except KISError as ex:
            note.send(f'매도 실패 : {ex} (다음 교체 때 다시 판단)')

    skip = set()
    if d['market_ok']:
        wait_until(config.BUY_TIME)
        buy_round(client, broker, note, d, skip)
        if len(broker.positions) < M.HOLD and hhmm() < config.STOP_TIME:
            wait_until(config.RETRY_TIME)
            buy_round(client, broker, note, d, skip)        # 미체결 종목과 다음 순위 종목을 다시 시도

    broker.state['last_signal'] = f'{d["signal"]:%Y-%m-%d}'
    broker.save()
    if d['market_ok'] and len(broker.positions) < M.HOLD:
        note.send(f'주의 : {M.HOLD}종목 중 {len(broker.positions)}종목만 보유 중입니다. 빈자리는 다음 교체 때 채웁니다.')
    note.send('교체 끝\n' + holdings(client, broker))


def holdings(client, broker):
    prices, lines = {}, []
    for code, p in broker.positions.items():
        try:
            prices[code] = client.get_price(code)['price']
        except KISError:
            prices[code] = p['avg_price']
        rate = prices[code] * p['qty'] / p['cost'] - 1
        lines.append(f'  {p["name"]}({code}) {p["qty"]:,}주, 매입 {p["avg_price"]:,.0f} → 현재 {prices[code]:,} ({rate:+.1%}), '
                     f'{p["time"][:10]} 매수')
    eq = broker.equity(prices)
    head = (f'평가 자산 {won(eq)} ({eq / config.CAPITAL - 1:+.2%}), 현금 {won(broker.cash)}, '
            f'보유 {len(broker.positions)}종목')
    return head + ('\n' + '\n'.join(lines) if lines else '')


# ------------------------------------------------------------------ 실행
def check(client):
    note = Notifier(config.CHANNEL, f'점검·대형주 모멘텀 {M.VERSION}')
    note.send = note.log                            # 점검은 슬랙으로 보내지 않는다
    broker = Broker(client, note, M.VERSION)
    get_data(client, note, wait=False)
    d = plan(broker, datetime.now())
    done = broker.state.get('last_signal') == f'{d["signal"]:%Y-%m-%d}'
    print(f'\n오늘 할 일 : {"이번 주 교체는 이미 끝남 (보유 현황만 알림)" if done else "교체일 (15:10부터 주문)"}')
    print(describe(d, broker))
    print('\n장부 :', holdings(client, broker))
    try:
        held = client.get_balance()
        print('계좌 보유 :', ', '.join(f'{v["name"]}({c}) {v["qty"]:,}주' for c, v in held.items()) or '없음')
    except KISError as ex:
        print('계좌 조회 실패 :', ex)


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

    note = Notifier(config.CHANNEL, f'모의·대형주 모멘텀 {M.VERSION}')
    note.log(f'슬랙 알림 : {note.status()}')
    broker = Broker(client, note, M.VERSION)
    for attempt in range(1, 6):                     # 시작 준비 실패 시 30초 간격으로 재시도
        try:
            broker.reconcile()
            break
        except (KISError, OSError) as ex:
            if attempt == 5:
                note.send(f'계좌 조회 실패, 오늘은 실행하지 않습니다 : {ex}')
                return
            note.log(f'계좌 조회 실패 ({attempt}/5) : {ex} → 30초 뒤 다시 시도')
            time.sleep(30)

    if not get_data(client, note, wait=True):
        note.send('오늘은 교체하지 않습니다.\n' + holdings(client, broker))
        return
    d = plan(broker, datetime.now())
    if broker.state.get('last_signal') == f'{d["signal"]:%Y-%m-%d}':
        note.send('이번 주 교체는 끝났습니다.\n' + holdings(client, broker))
        return

    msg = '오늘 교체합니다' + (' (늦은 교체: 원래는 신호일 다음 거래일)' if d['late'] else '')
    note.send(f'{msg}. {fmt(config.SELL_TIME)} 매도, {fmt(config.BUY_TIME)} 매수\n' + describe(d, broker))
    rebalance(client, broker, note, d)


if __name__ == '__main__':
    try:
        main()
    except KISError as e:
        print('실패 :', e)
        sys.exit(1)
    except KeyboardInterrupt:
        print('사용자가 중단했습니다. 장부(data/momentum)는 그대로 남고, 모의투자 계좌의 보유 종목은 직접 확인하세요.')
