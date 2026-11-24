"""대형주 모멘텀 백테스트 (주 1회 교체)

    python backtest/large_cap_momentum.py

가설: 시가총액 상위 종목 중 최근 몇 달 많이 오른 종목을 들고 매주 교체하면, 비용을 빼고도
      대형주를 똑같이 나눠 산 것과 코스피보다 수익이 높다.

규칙 (결과를 보기 전에 고정)
    유니버스   : 신호일 시가총액 상위 100개 보통주 (우선주 제외, 신호일 거래된 종목)
    신호       : 최근 L거래일 수익률 (가장 최근 5거래일은 빼고 계산), 높은 순
    교체       : 그 주 마지막 거래일(신호일) 종가로 순위, 다음 거래일 종가에 사고판다
    보유       : K종목. 살 때마다 그 시점 전체 자산의 1/K (복리)
    매도       : 순위가 2K위 밖으로 밀리거나 유니버스에서 빠지면 판다. 빈자리는 순위 높은 종목으로 채운다
    시장 필터  : 켜면 신호일 근사 코스피가 60일 이동평균 아래일 때 전부 팔고 현금으로 기다린다
    기간       : 모든 조합 2022년 1월 첫 교체일부터 같은 기간

비교 기준
    대형주 균등 : 같은 시가총액 상위 100종목을 매주 같은 금액으로 다시 맞춘 경우 (비용 0, 기준에 유리하게)
    근사 코스피 : 그냥 들고 있는 경우

통과 기준 (모두)
    1. 연수익률이 두 비교 기준보다 높다
    2. 앞 절반, 뒤 절반 모두 대형주 균등보다 연수익률이 높다
    3. 최대낙폭이 대형주 균등보다 10%p 넘게 깊지 않다
    4. 매도가 평균 주 2건 이하
통과한 조합만 추가 확인: 슬리피지 2배, 교체 요일을 수요일로

데이터 처리
    수익률은 거래소 등락률(배당 미포함)을 이어 붙인다. 비교 기준도 같은 방식이라 비교는 공정하다.
    거래정지로 시세가 없는 날은 0%로 두고, 그날은 팔 수 없다.
    보유 중 시세가 아예 끊긴 종목(상장폐지 등)은 마지막 가치로 판 것으로 친다. 결과가 좋게 나오는 방향의 한계.
"""
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config  # noqa: E402
# 순위 계산은 실매매(run.py)와 같은 코드를 쓴다
from momentum import load, prepare, signal_days, ranking, SKIP  # noqa: E402

FEE, SLIP, TAX = config.FEE_RATE, config.SLIPPAGE, config.STOCK_SELL_TAX

START = '2022-01-01'
MAX_LOOKBACK = 250
VARIANTS = list(itertools.product([60, 120, 250], [5, 10], [False, True]))   # (L, K, 시장 필터)
FRIDAY, WEDNESDAY = 4, 2


# ------------------------------------------------------------------ 한 조합 실행
def run(P, lookback, k, use_filter, slip=SLIP, weekday=FRIDAY):
    dates, traded, r = P['dates'], P['traded'], P['r']
    sig = signal_days(dates, weekday)
    start = max(dates.searchsorted(pd.Timestamp(START)), SKIP + MAX_LOOKBACK)
    buy_cost = (1 + slip) * (1 + FEE)
    sell_keep = (1 - slip) * (1 - FEE - TAX)

    cash, hold, trades, equity = 1.0, {}, [], {}
    pending = None                                   # 신호일에 정한 순위 (다음 거래일에 실행)

    def sell(tk, day, reason):
        nonlocal cash
        h = hold.pop(tk)
        proceeds = h['value'] * sell_keep
        cash += proceeds
        trades.append({'ticker': tk, 'buy': h['date'], 'sell': day, 'ret': proceeds / h['paid'] - 1,
                       'days': dates.get_loc(day) - dates.get_loc(h['date']), 'reason': reason})

    for p in range(start, len(dates)):
        day = dates[p]
        for tk, h in hold.items():                   # 오늘 등락률 반영
            h['value'] *= 1 + r.iat[p, h['col']]
        for tk in [t for t, h in hold.items() if P['last_seen'][t] < day]:
            sell(tk, day, '시세없음')

        if pending is not None:                      # 어제 신호 → 오늘 종가 실행
            rank, ok = pending
            pending = None
            for tk in list(hold):
                if not traded.iat[p, hold[tk]['col']]:
                    continue
                if not ok:
                    sell(tk, day, '시장필터')
                elif tk not in rank.index or rank[tk] > 2 * k:
                    sell(tk, day, '순위하락')
            if ok:
                for tk in rank.sort_values().index:
                    if len(hold) >= k:
                        break
                    col = P['tickers'].get_loc(tk)
                    if tk in hold or not traded.iat[p, col]:
                        continue
                    eq = cash + sum(h['value'] for h in hold.values())
                    amount = min(eq / k, cash)
                    if amount <= 0:
                        break
                    cash -= amount
                    hold[tk] = {'col': col, 'value': amount / buy_cost, 'paid': amount, 'date': day}

        equity[day] = cash + sum(h['value'] for h in hold.values())
        if day in sig and p + 1 < len(dates):
            ok = bool(P['market_ok'].iat[p]) if use_filter else True
            pending = (ranking(P, p, lookback), ok)

    return pd.DataFrame(trades), pd.Series(equity)


def benchmark_equal(P, weekday=FRIDAY):
    """대형주 균등: 신호일 시가총액 상위 100종목을 다음 거래일 종가에 같은 금액으로 맞추고 한 주 보유 (비용 0)"""
    dates, traded, r = P['dates'], P['traded'], P['r']
    sig = signal_days(dates, weekday)
    start = max(dates.searchsorted(pd.Timestamp(START)), SKIP + MAX_LOOKBACK)
    level, weights, members, equity, pending = 1.0, None, None, {}, None
    for p in range(start, len(dates)):
        day = dates[p]
        if weights is not None:
            weights = weights * (1 + r.iloc[p][members].values)
            level = weights.sum()
        if pending is not None:
            members = [t for t in pending if traded.iat[p, P['tickers'].get_loc(t)]]
            weights = np.full(len(members), level / len(members))
            pending = None
        equity[day] = level
        if day in sig:
            row = P['in_uni'].iloc[p]
            pending = list(row[row].index)
    return pd.Series(equity)


# ------------------------------------------------------------------ 결과 정리
def cagr(eq):
    return (eq.iloc[-1] / eq.iloc[0]) ** (252 / (len(eq) - 1)) - 1


def mdd(eq):
    return float((eq / eq.cummax() - 1).min())


def stats(eq, split):
    dr = eq.pct_change().dropna()
    return {'연수익률': cagr(eq), '최대낙폭': mdd(eq),
            '변동성대비': dr.mean() / dr.std() * np.sqrt(252) if dr.std() > 0 else np.nan,
            '앞절반': cagr(eq[eq.index <= split]), '뒷절반': cagr(eq[eq.index >= split])}


def evaluate(trades, eq, split, bench, kospi):
    s = stats(eq, split)
    weeks = len(eq) / 5
    row = {'연수익률(%)': s['연수익률'] * 100, '최대낙폭(%)': s['최대낙폭'] * 100, '변동성대비': s['변동성대비'],
           '앞절반(%)': s['앞절반'] * 100, '뒷절반(%)': s['뒷절반'] * 100,
           '주당매도': len(trades) / weeks, '평균보유(일)': trades['days'].mean() if len(trades) else np.nan,
           '종목승률(%)': (trades['ret'] > 0).mean() * 100 if len(trades) else np.nan}
    row['통과'] = 'O' if (s['연수익률'] > bench['연수익률'] and s['연수익률'] > kospi['연수익률']
                         and s['앞절반'] > bench['앞절반'] and s['뒷절반'] > bench['뒷절반']
                         and s['최대낙폭'] >= bench['최대낙폭'] - 0.10
                         and row['주당매도'] <= 2) else 'X'
    return row


def main():
    panel, index = load()
    P = prepare(panel, index)
    bench_eq = benchmark_equal(P)
    split = bench_eq.index[len(bench_eq) // 2]
    kospi_eq = index.reindex(bench_eq.index)
    bench, kospi = stats(bench_eq, split), stats(kospi_eq, split)
    print(f'기간 {bench_eq.index[0]:%Y-%m-%d} ~ {bench_eq.index[-1]:%Y-%m-%d} ({len(bench_eq)}거래일), '
          f'앞/뒤 구분 {split:%Y-%m-%d}')
    print(f'비용: 수수료 {FEE:.3%}x2, 슬리피지 {SLIP:.2%}x2, 매도세 {TAX:.2%}\n')
    for name, s in (('대형주 균등(비용 0)', bench), ('근사 코스피', kospi)):
        print(f'{name}: 연 {s["연수익률"]:.1%}, 최대낙폭 {s["최대낙폭"]:.1%}, 변동성대비 {s["변동성대비"]:.2f}, '
              f'앞절반 {s["앞절반"]:.1%}, 뒷절반 {s["뒷절반"]:.1%}')

    rows, keep = [], {}
    for lb, k, f in VARIANTS:
        trades, eq = run(P, lb, k, f)
        keep[(lb, k, f)] = (trades, eq)
        rows.append({'L일': lb, 'K종목': k, '시장필터': 'O' if f else 'X', **evaluate(trades, eq, split, bench, kospi)})
        print(f'완료 L={lb} K={k} 필터={f}', flush=True)
    res = pd.DataFrame(rows)

    checks = []
    for (lb, k, f), _ in keep.items():
        row = res[(res['L일'] == lb) & (res['K종목'] == k) & (res['시장필터'] == ('O' if f else 'X'))].iloc[0]
        if row['통과'] != 'O':
            continue
        t2, e2 = run(P, lb, k, f, slip=SLIP * 2)
        t3, e3 = run(P, lb, k, f, weekday=WEDNESDAY)
        checks.append({'L일': lb, 'K종목': k, '시장필터': row['시장필터'],
                       '슬리피지2배': evaluate(t2, e2, split, bench, kospi)['통과'],
                       '슬리피지2배 연수익률(%)': cagr(e2) * 100,
                       '수요일교체': evaluate(t3, e3, split, bench, kospi)['통과'],
                       '수요일교체 연수익률(%)': cagr(e3) * 100})

    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    print('\n' + res.round(2).to_string(index=False))
    print('\n통과한 조합의 추가 확인:')
    print(pd.DataFrame(checks).round(2).to_string(index=False) if checks else '  통과한 조합 없음')

    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    res.round(3).to_csv(out / 'large_cap_momentum.csv', index=False, encoding='utf-8-sig')
    if checks:
        pd.DataFrame(checks).round(3).to_csv(out / 'large_cap_momentum_checks.csv', index=False, encoding='utf-8-sig')
    print(f'\n저장: {out / "large_cap_momentum.csv"}')


if __name__ == '__main__':
    main()
