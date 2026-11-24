"""기본 로직 + 코스피 200을 같이 들고 가기

    python backtest/blend_kospi200.py

왜: 기본 로직(120일 모멘텀 10종목, 시장 필터)은 하락장에 강하지만, 2025년처럼 초대형주 몇 개가
    끌어올린 해에는 코스피를 크게 놓쳤다 (+24.5% vs +75.7%). 지수를 일부 같이 들면 이 약점이 줄어드는지 본다.

방식
    자금을 두 바구니로 나눈다. 모멘텀 바구니 = 기본 로직 그대로, 지수 바구니 = 코스피 200 (ETF로 산다고 봄)
    매달 첫 교체일에 두 바구니를 목표 비중으로 다시 맞춘다
    지수 바구니 '필터 있음'이면 기본 로직과 같은 시장 필터(코스피 < 60일선이면 현금)를 지수 바구니에도 적용
    비용: 지수 바구니 매매는 수수료+슬리피지(ETF라 매도세 없음), 비중 맞출 때 모멘텀 바구니에서 판 금액에는 매도세 포함
    ETF 운용보수(연 0.05% 안팎)와 배당은 넣지 않는다 (서로 상쇄되는 방향)

비교할 조합 (이 밖은 돌리지 않음): 지수 비중 30%, 50% x 지수 바구니 시장 필터 있음/없음 = 4개

통과 기준 (결과를 보기 전에 고정, 모두 만족)
    1. 2025년 수익률이 기본 로직보다 10%p 이상 높다 (약점 보완)
    2. 전체 연수익률이 기본 로직보다 5%p 넘게 낮지 않다
    3. 전체 최대낙폭이 기본 로직보다 5%p 넘게 깊지 않다
    4. 2022년 수익률이 기본 로직보다 5%p 넘게 낮지 않다 (하락장 강점 유지)
기간: 2022년 1월 첫 교체일부터 (다른 백테스트와 같음). 연도별 수익은 각 해 첫 거래일 대비 마지막 거래일.
"""
import itertools
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import config  # noqa: E402
import momentum as M  # noqa: E402
import regime_strategies as RS  # noqa: E402

FEE, SLIP, TAX = config.FEE_RATE, config.SLIPPAGE, config.STOCK_SELL_TAX
VARIANTS = list(itertools.product([0.3, 0.5], [False, True]))     # (지수 비중, 지수 바구니 필터)


def kospi200():
    vals = {}
    for f in sorted((HERE / 'data' / 'index').glob('*.csv.gz')):
        d = pd.read_csv(f, usecols=['name', 'close'])
        vals[pd.Timestamp(f.name[:8])] = float(d.loc[d['name'] == '코스피 200', 'close'].iloc[0])
    return pd.Series(vals)


def blend(P, mom_eq, k200, w, use_filter):
    """모멘텀 바구니 수익(mom_eq)과 코스피 200을 섞는다. 반환: 합친 자산 곡선"""
    days = mom_eq.index
    sig = M.signal_days(P['dates'])
    mom_r = mom_eq.pct_change().fillna(0)
    idx_r = k200.reindex(days).pct_change().fillna(0)
    a, b = 1 - w, w                     # 모멘텀 바구니, 지수 바구니 금액
    invested = bool(P['market_ok'].loc[days[0]]) if use_filter else True   # 지수 바구니가 지수를 들고 있는지
    pend_in, pend_rebal = None, False
    month_done, out = None, {}
    for i, day in enumerate(days):
        if i:
            a *= 1 + mom_r.iat[i]
            if invested:
                b *= 1 + idx_r.iat[i]
        if pend_in is not None:                         # 신호일 다음 거래일: 지수 바구니 들어가기/나오기
            if pend_in != invested:
                b *= 1 - (FEE + SLIP)
                invested = pend_in
            pend_in = None
        if pend_rebal:                                  # 매달 첫 교체: 목표 비중으로
            total = a + b
            move = b - total * w                        # +면 지수 → 모멘텀, -면 모멘텀 → 지수
            if move < 0:
                a -= -move
                b += -move * (1 - (FEE + SLIP + TAX))   # 모멘텀 바구니에서 주식을 팔아 옮김
            elif move > 0:
                b -= move
                a += move * (1 - (FEE + SLIP) * (1 if invested else 0))
            pend_rebal = False
        out[day] = a + b
        if day in sig:
            p = P['dates'].get_loc(day)
            pend_in = bool(P['market_ok'].iat[p]) if use_filter else True
            if month_done != (day.year, day.month):
                month_done = (day.year, day.month)
                pend_rebal = True
    return pd.Series(out)


def year_ret(eq, y):
    e = eq[eq.index.year == y]
    return e.iloc[-1] / e.iloc[0] - 1 if len(e) > 1 else float('nan')


def main():
    panel, kospi = M.load()
    P = M.prepare(panel, kospi)
    logics = RS.make_logics(P)
    base, _ = RS.simulate(P, lambda p: logics['기본'])
    k200 = kospi200()
    days = base.index
    years = sorted(set(days.year))

    curves = {'기본 로직': base, '코스피 200 보유': k200.reindex(days) / k200.reindex(days).iloc[0]}
    for w, f in VARIANTS:
        curves[f'지수 {w:.0%}' + (' (필터 있음)' if f else ' (필터 없음)')] = blend(P, base, k200, w, f)

    rows = []
    for name, eq in curves.items():
        row = {'전략': name, '연수익률(%)': RS.cagr(eq) * 100, '최대낙폭(%)': RS.mdd(eq) * 100}
        for y in years:
            row[f'{y}(%)'] = year_ret(eq, y) * 100
        rows.append(row)
    res = pd.DataFrame(rows).set_index('전략')
    b = res.loc['기본 로직']
    verdict = {}
    for name in res.index[2:]:
        r = res.loc[name]
        verdict[name] = ('O' if (r['2025(%)'] >= b['2025(%)'] + 10 and r['연수익률(%)'] >= b['연수익률(%)'] - 5
                                 and r['최대낙폭(%)'] >= b['최대낙폭(%)'] - 5 and r['2022(%)'] >= b['2022(%)'] - 5)
                         else 'X')
    res['통과'] = pd.Series(verdict)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    print(f'기간 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d} ({len(days)}거래일), 2026년은 11/23까지\n')
    print(res.round(1).fillna('').to_string())
    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    res.round(3).to_csv(out / 'blend_kospi200.csv', encoding='utf-8-sig')
    print(f'\n저장: {out / "blend_kospi200.csv"}')


if __name__ == '__main__':
    main()
