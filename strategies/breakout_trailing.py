"""변동성 돌파 + 고점 대비 하락 매도 (+ 손절)  — 현재 실행 목록에는 없음

매수는 8장 자동매매와 같다.
    현재가 > 오늘 시가 + (어제 고가 - 어제 저가) × K
    그리고 현재가 > 5일 이동평균, 현재가 > 10일 이동평균
매도는 장중에도 판다.
    매수 후 최고가에서 TRAIL_PCT 이상 내려오면 매도
    매입가에서 STOP_PCT 이상 내려가면 매도
    둘 다 아니면 실행기가 15:15에 매도
"""
from strategies.base import Strategy

K = 0.5
TRAIL_PCT = 0.015
STOP_PCT = 0.02


class BreakoutTrailing(Strategy):
    name = '변동성 돌파'
    VERSION = 'v1'

    def prepare(self, client, code, today):
        rows = client.get_ohlc(code)
        if len(rows) < 11 or rows[0]['date'] != today:
            return None
        today_row, prev = rows[0], rows[1:]
        last = prev[0]
        return {
            'target': today_row['open'] + (last['high'] - last['low']) * K,
            'ma5': sum(r['close'] for r in prev[:5]) / 5,
            'ma10': sum(r['close'] for r in prev[:10]) / 10,
        }

    def should_buy(self, info, quote):
        price = quote['price']
        if price > info['target'] and price > info['ma5'] and price > info['ma10']:
            return f'돌파 (현재가 {price:,} > 목표가 {info["target"]:,.0f})'
        return None

    def should_sell(self, info, position, quote):
        price = quote['price']
        if price <= position['high'] * (1 - TRAIL_PCT):
            return f'고점 {position["high"]:,} 대비 {TRAIL_PCT:.1%} 하락'
        if price <= position['avg_price'] * (1 - STOP_PCT):
            return f'매입가 대비 {STOP_PCT:.0%} 손절'
        return None

    def describe(self, info):
        return (f'목표가 {info["target"]:,.0f}, 5일선 {info["ma5"]:,.0f}, '
                f'10일선 {info["ma10"]:,.0f}')
