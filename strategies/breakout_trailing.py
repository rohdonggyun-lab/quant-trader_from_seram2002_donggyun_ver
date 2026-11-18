"""예시 전략: 변동성 돌파 + 고점 대비 하락 매도 (+ 손절)

매수는 8장 자동매매와 같다.
    현재가 > 오늘 시가 + (어제 고가 - 어제 저가) × K
    그리고 현재가 > 5일 이동평균, 현재가 > 10일 이동평균
매도는 8장과 달리 장중에도 판다.
    매수 후 최고가에서 TRAIL_PCT 이상 내려오면 매도 (이익을 지키는 장치)
    매입가에서 STOP_PCT 이상 내려가면 매도 (손실을 줄이는 장치)
    둘 다 아니면 실행기가 EXIT_TIME(15:15)에 매도

같은 날 8장 자동매매(시간 매도만)와 결과를 비교해 보는 용도로 넣은 예시다.
숫자는 검증된 값이 아니므로 직접 바꿔 가며 시험한다.
"""
from strategies.base import Strategy

K = 0.5             # 변동폭에 곱하는 비율
TRAIL_PCT = 0.015   # 매수 후 최고가 대비 1.5% 하락 시 매도
STOP_PCT = 0.02     # 매입가 대비 2% 하락 시 매도


class BreakoutTrailing(Strategy):
    name = f'변동성 돌파 (K={K}) + 고점 대비 {TRAIL_PCT:.1%} 하락 매도 + {STOP_PCT:.0%} 손절'

    def prepare(self, client, code, today):
        rows = client.get_ohlc(code)                 # 최신 날짜가 맨 앞
        if len(rows) < 11 or rows[0]['date'] != today:
            return None
        today_row, prev = rows[0], rows[1:]
        last = prev[0]
        return {
            'open': today_row['open'],
            'target': today_row['open'] + (last['high'] - last['low']) * K,
            'ma5': sum(r['close'] for r in prev[:5]) / 5,
            'ma10': sum(r['close'] for r in prev[:10]) / 10,
        }

    def should_buy(self, info, price):
        if price > info['target'] and price > info['ma5'] and price > info['ma10']:
            return f'돌파 (현재가 {price:,} > 목표가 {info["target"]:,.0f})'
        return None

    def should_sell(self, info, position, price):
        if price <= position['high'] * (1 - TRAIL_PCT):
            return f'고점 {position["high"]:,} 대비 {TRAIL_PCT:.1%} 하락'
        if price <= position['avg_price'] * (1 - STOP_PCT):
            return f'매입가 대비 {STOP_PCT:.0%} 손절'
        return None

    def describe(self, info):
        return (f'목표가 {info["target"]:,.0f}, 5일선 {info["ma5"]:,.0f}, '
                f'10일선 {info["ma10"]:,.0f}')
