"""종가 매수 반전 백테스트 (일봉)

    python backtest/close_entry_reversal.py

가설: 최근 N일 많이 빠진 유동성 상위 종목을 그날 종가에 사서 H거래일 뒤 종가에 팔면
      비용을 빼고도 평균 수익이 양수이고, 같은 날 유니버스 전체를 산 것보다 낫다.

규칙 (결과를 보기 전에 고정)
    유니버스   : 전날 거래대금 상위 100개 보통주 (우선주 제외, 오늘 거래된 종목만)
    신호       : 오늘 종가까지 최근 N일 수익률이 낮은 순. 낙폭이 MAX_DROP보다 큰 종목은 제외
    매수       : 신호 당일 종가
    매도       : 매수 후 H거래일째 종가
    포지션     : 동시에 최대 K종목, 한 칸은 처음 자금의 1/K (복리 없음)
    시장 필터  : 켜면 신호일 근사 코스피가 20일 이동평균 아래일 때는 사지 않는다
    비교 기준  : 같은 날 같은 방식으로 유니버스 전체를 산 평균

통과 기준 (모두): 평균 > 0, 평균 > 비교 기준, 거래 300회 이상, 앞/뒤 절반 평균 모두 > 0, 최대낙폭 > -30%
통과한 조합만 추가 확인: 슬리피지 2배, 신호를 전날 종가로 계산 (둘 다 평균 > 0 이어야 함)

데이터 처리
    수익률은 거래소 등락률을 이어 붙인다 (액면분할 등에도 가짜 급락이 없다).
    보유 중 거래정지·상장폐지로 시세가 없는 날은 0%로 둔다. 상장폐지 손실을 놓치므로 결과가 좋게 나오는 방향의 한계.
    동시호가 체결 물량, 상한가·하한가로 주문이 안 되는 경우는 고려하지 않는다.
"""
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from weekly_reversal import load, max_drawdown, FEE, SLIP, TAX, MAX_DROP, K, MARKET_MA, UNIVERSE_SIZE  # noqa: E402

VARIANTS = list(itertools.product([3, 5, 10], [1, 3, 5], [False, True]))   # (N, H, 시장 필터)
MIN_TRADES = 300
MAX_DD = -0.30


def run(panel, index, n, h, use_filter, slip=SLIP, lag=0):
    """lag=1 이면 신호를 전날 종가까지로 계산한다 (미래 정보 점검용)"""
    close, volume, value, rate, open_ = (panel[c] for c in ['close', 'volume', 'value', 'rate', 'open'])
    dates = close.index
    traded = (volume > 0) & close.notna() & open_.notna() & (open_ > 0)

    lr = np.log1p(rate.where(traded) / 100)
    cum = lr.fillna(0).cumsum()
    ret_n = np.expm1(cum - cum.shift(n)).shift(lag)
    ok_n = (traded.rolling(n).sum() == n).shift(lag, fill_value=False)
    in_universe = (value.where(traded).rank(axis=1, ascending=False) <= UNIVERSE_SIZE).shift(1, fill_value=False)
    can_buy = in_universe & ok_n & traded

    market_ok = pd.Series(True, index=dates)
    if use_filter:
        market_ok = (index >= index.rolling(MARKET_MA).mean()).shift(lag, fill_value=False)

    buy_cost = (1 + slip) * (1 + FEE)
    sell_keep = (1 - slip) * (1 - FEE - TAX)
    slot = 1.0 / K
    positions, trades, equity, realized = [], [], [], 0.0

    for i in range(max(n, MARKET_MA) + 1, len(dates)):
        # 1) 보유 종목: 오늘 등락률 반영, H일째면 종가 매도
        still = []
        for p in positions:
            if traded.iat[i, p['col']]:
                p['gross'] *= 1 + rate.iat[i, p['col']] / 100
            if i - p['entry_i'] >= h:
                net = p['gross'] * sell_keep / buy_cost - 1
                realized += slot * net
                trades.append({'signal': dates[p['entry_i']], 'ticker': p['ticker'], 'net': net})
            else:
                still.append(p)
        positions = still

        # 2) 오늘 종가에 새로 산다
        free = K - len(positions)
        if free > 0 and market_ok.iat[i]:
            cand = ret_n.iloc[i].where(can_buy.iloc[i])
            cand = cand[cand > -MAX_DROP].dropna().sort_values()
            held = {p['col'] for p in positions}
            for tk in cand.index[:free + len(held)]:
                col = close.columns.get_loc(tk)
                if col in held or free <= 0:
                    continue
                positions.append({'col': col, 'ticker': tk, 'entry_i': i, 'gross': 1.0})
                free -= 1

        mark = sum(slot * (p['gross'] * sell_keep / buy_cost - 1) for p in positions)
        equity.append((dates[i], 1.0 + realized + mark))

    # 비교 기준: 같은 날 유니버스 전체를 종가에 사서 H일 보유
    base = []
    for i in range(max(n, MARKET_MA) + 1, len(dates) - h):
        if not market_ok.iat[i]:
            continue
        sel = can_buy.iloc[i]
        if sel.sum() == 0:
            continue
        g = lr.iloc[i + 1:i + h + 1].sum().where(sel).dropna()
        base.append((np.exp(g) * sell_keep / buy_cost - 1).mean())
    return pd.DataFrame(trades), pd.Series(dict(equity)), float(np.mean(base)) if base else np.nan


def summarize(trades, eq, base, split):
    row = {'거래수': len(trades)}
    if trades.empty:
        return row
    net = trades['net']
    first, second = net[trades['signal'] < split], net[trades['signal'] >= split]
    row.update({
        '평균(%)': net.mean() * 100, '중앙값(%)': net.median() * 100, '승률(%)': (net > 0).mean() * 100,
        '최악(%)': net.min() * 100, '비교기준(%)': base * 100, '초과(%)': (net.mean() - base) * 100,
        '최대낙폭(%)': max_drawdown(eq) * 100, '총수익(%)': (eq.iloc[-1] - 1) * 100,
        '앞절반(%)': first.mean() * 100, '뒷절반(%)': second.mean() * 100,
    })
    row['통과'] = 'O' if (net.mean() > 0 and net.mean() > base and len(trades) >= MIN_TRADES
                         and first.mean() > 0 and second.mean() > 0 and max_drawdown(eq) > MAX_DD) else 'X'
    return row


def main():
    panel, index = load()
    dates = panel['close'].index
    split = dates[len(dates) // 2]
    print(f'기간 {dates[0]:%Y-%m-%d} ~ {dates[-1]:%Y-%m-%d} ({len(dates)}거래일), 앞/뒤 구분 {split:%Y-%m-%d}')
    print(f'비용: 수수료 {FEE:.3%}x2, 슬리피지 {SLIP:.2%}x2, 매도세 {TAX:.2%}\n')

    rows = []
    for n, h, f in VARIANTS:
        trades, eq, base = run(panel, index, n, h, f)
        rows.append({'N일': n, '보유H일': h, '시장필터': 'O' if f else 'X', **summarize(trades, eq, base, split)})
        print(f'완료 N={n} H={h} 필터={f}', flush=True)
    res = pd.DataFrame(rows)

    checks = []
    for _, r in res[res['통과'] == 'O'].iterrows():
        args = (panel, index, r['N일'], r['보유H일'], r['시장필터'] == 'O')
        t2, _, _ = run(*args, slip=SLIP * 2)
        t3, _, _ = run(*args, lag=1)
        checks.append({'N일': r['N일'], '보유H일': r['보유H일'], '시장필터': r['시장필터'],
                       '슬리피지2배 평균(%)': t2['net'].mean() * 100,
                       '전날신호 평균(%)': t3['net'].mean() * 100,
                       '전날신호 거래수': len(t3)})

    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    print('\n' + res.round(2).to_string(index=False))
    print('\n통과한 조합의 추가 확인:')
    print(pd.DataFrame(checks).round(2).to_string(index=False) if checks else '  통과한 조합 없음')

    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    res.round(3).to_csv(out / 'close_entry_reversal.csv', index=False, encoding='utf-8-sig')
    if checks:
        pd.DataFrame(checks).round(3).to_csv(out / 'close_entry_reversal_checks.csv', index=False, encoding='utf-8-sig')
    print(f'\n저장: {out / "close_entry_reversal.csv"}')


if __name__ == '__main__':
    main()
