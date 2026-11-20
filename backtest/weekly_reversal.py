"""주간 반전 백테스트 (일봉)

    python backtest/weekly_reversal.py

가설: 최근 N일 많이 빠진 유동성 상위 종목을 다음 거래일 시가에 사서 H거래일 보유하면
      비용(수수료, 슬리피지, 매도세)을 빼고도 평균 수익이 양수다.

규칙 (결과를 보기 전에 고정)
    유니버스   : 신호일 거래대금 상위 100개 보통주 (우선주 제외, 거래정지 제외)
    신호       : 최근 N일 수익률이 낮은 순. 낙폭이 MAX_DROP보다 큰 종목은 제외
    매수       : 신호 다음 거래일 시가
    매도       : 매수일을 1일째로 세어 H일째 종가
    포지션     : 동시에 최대 K종목, 한 칸은 처음 자금의 1/K (복리 없음)
    시장 필터  : 켜면 신호일 코스피가 20일 이동평균 아래일 때는 사지 않는다
    비교 기준  : 같은 날 같은 방식으로 유니버스 전체를 산 평균 (반전이 시장 평균보다 나은가)

데이터 처리
    수익률은 거래소가 주는 등락률(기준가 조정 반영)을 이어 붙여서 계산한다.
    액면분할 같은 사건이 있어도 가짜 급락이 생기지 않는다.
    보유 중 시세가 끊긴 거래(상장폐지 등)는 계산할 수 없어 빠진다. 결과가 좋게 나오는 방향의 한계.
    상한가·하한가로 주문이 안 되는 경우는 고려하지 않는다.
"""
import itertools
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config  # noqa: E402  (비용 값을 실매매 설정과 같게 쓴다)

DAILY = HERE / 'data' / 'daily'

UNIVERSE_SIZE = 100
MAX_DROP = 0.20             # N일 낙폭이 20%를 넘으면 제외
K = 6                       # 동시 보유 종목 수
FEE, SLIP, TAX = config.FEE_RATE, config.SLIPPAGE, config.STOCK_SELL_TAX
MARKET_MA = 20

# 미리 정한 비교 대상 (이 밖의 조합은 돌리지 않는다)
VARIANTS = list(itertools.product([3, 5, 10], [3, 5, 10], [False, True]))   # (N, H, 시장 필터)


# ------------------------------------------------------------------ 데이터
def load():
    files = sorted(DAILY.glob('*.csv.gz'))
    if not files:
        raise SystemExit('backtest/data/daily 가 비어 있습니다. 먼저 fetch_data.py를 실행하세요.')
    frames = []
    for f in files:
        df = pd.read_csv(f, dtype={'ticker': str})
        df['date'] = pd.Timestamp(f.name[:8])
        frames.append(df)
    d = pd.concat(frames, ignore_index=True)
    d = d[d['ticker'].str.endswith('0')]            # 우선주(끝자리가 0이 아님) 제외
    panel = {c: d.pivot(index='date', columns='ticker', values=c)
             for c in ['open', 'close', 'volume', 'value', 'rate']}
    index = pd.read_csv(HERE / 'data' / 'kospi_index.csv', index_col='date', parse_dates=True)['close']
    return panel, index.reindex(panel['close'].index)


# ------------------------------------------------------------------ 한 조합 실행
def run(panel, index, n, h, use_filter):
    open_, close, volume, value, rate = (panel[c] for c in ['open', 'close', 'volume', 'value', 'rate'])
    dates = close.index
    traded = (volume > 0) & open_.notna() & close.notna() & (open_ > 0)

    lr = np.log1p(rate.where(traded) / 100)          # 일 로그수익률 (거래정지 날은 비어 있음)
    cum = lr.fillna(0).cumsum()
    ret_n = np.expm1(cum - cum.shift(n))              # 최근 N일 수익률
    ok_n = traded.rolling(n).sum() == n               # N일 모두 거래된 종목만
    intraday = np.log(close / open_).where(traded)    # 하루 안의 로그수익률 (시가→종가)

    rank = value.where(traded).rank(axis=1, ascending=False)
    in_universe = rank <= UNIVERSE_SIZE

    market_ok = pd.Series(True, index=dates)
    if use_filter:
        market_ok = index >= index.rolling(MARKET_MA).mean()

    # 거래 후보와 보유 중 경로: 신호일 i, 매수 i+1(시가), 매도 i+h(종가)
    positions, trades, equity = [], [], []
    realized = 0.0
    slot = 1.0 / K
    buy_cost = (1 + SLIP) * (1 + FEE)

    for i in range(max(n, MARKET_MA), len(dates) - 1):
        today = dates[i]
        # 오늘 종가 기준으로 보유 종목 갱신 / 만기 매도
        still = []
        for p in positions:
            day = i - p['entry_i'] + 1                 # 매수일이 1일째
            if day == 1:
                p['gross'] = np.exp(intraday.iat[i, p['col']])
            else:
                p['gross'] *= 1 + rate.iat[i, p['col']] / 100 if traded.iat[i, p['col']] else 1
            if day >= h:
                net = p['gross'] * (1 - SLIP) * (1 - FEE - TAX) / buy_cost
                pnl = slot * (net - 1)
                realized += pnl
                trades.append({'signal': p['signal'], 'ticker': p['ticker'], 'net': net - 1,
                               'gross': p['gross'] - 1})
            else:
                still.append(p)
        positions = still
        mark = sum(slot * (p['gross'] * (1 - SLIP) * (1 - FEE - TAX) / buy_cost - 1) for p in positions)
        equity.append((today, 1.0 + realized + mark))

        # 오늘 종가에 신호를 계산하고, 내일 시가에 산다 (오늘 아직 안 산 칸만)
        free = K - len(positions)
        if free <= 0 or not market_ok.iat[i]:
            continue
        nxt = i + 1
        cand = ret_n.iloc[i].where(in_universe.iloc[i] & ok_n.iloc[i])
        cand = cand[(cand > -MAX_DROP) & traded.iloc[nxt]].dropna().sort_values()
        held = {p['col'] for p in positions}
        for tk in cand.index:
            if free <= 0:
                break
            col = close.columns.get_loc(tk)
            if col in held:
                continue
            positions.append({'col': col, 'ticker': tk, 'entry_i': nxt, 'signal': today, 'gross': 1.0})
            free -= 1

    # 비교 기준: 같은 방식으로 유니버스 전체를 산 평균
    base = []
    for i in range(max(n, MARKET_MA), len(dates) - h):
        if not market_ok.iat[i]:
            continue
        sel = in_universe.iloc[i] & ok_n.iloc[i] & traded.iloc[i + 1]
        if sel.sum() == 0:
            continue
        g = intraday.iloc[i + 1] + lr.iloc[i + 2:i + h + 1].sum()
        g = g.where(sel).dropna()
        net = np.exp(g) * (1 - SLIP) * (1 - FEE - TAX) / buy_cost - 1
        base.append(net.mean())
    return pd.DataFrame(trades), pd.Series(dict(equity)), float(np.mean(base)) if base else np.nan


# ------------------------------------------------------------------ 결과 정리
def max_drawdown(eq):
    return float((eq / eq.cummax() - 1).min()) if len(eq) else np.nan


def summarize(trades, eq, base, split):
    row = {'거래수': len(trades)}
    if trades.empty:
        return row
    row['평균(%)'] = trades['net'].mean() * 100
    row['중앙값(%)'] = trades['net'].median() * 100
    row['승률(%)'] = (trades['net'] > 0).mean() * 100
    row['최악(%)'] = trades['net'].min() * 100
    row['시장평균(%)'] = base * 100
    row['초과(%)'] = row['평균(%)'] - row['시장평균(%)']
    row['최대낙폭(%)'] = max_drawdown(eq) * 100
    row['총수익(%)'] = (eq.iloc[-1] - 1) * 100
    for name, part in zip(('앞절반', '뒷절반'), (trades[trades['signal'] < split],
                                               trades[trades['signal'] >= split])):
        row[f'평균 {name}(%)'] = part['net'].mean() * 100 if len(part) else np.nan
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
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    print('\n' + res.round(2).to_string(index=False))
    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    res.round(3).to_csv(out / 'weekly_reversal.csv', index=False, encoding='utf-8-sig')
    print(f'\n저장: {out / "weekly_reversal.csv"}')


if __name__ == '__main__':
    main()
