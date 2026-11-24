"""시장 상황별 로직 백테스트

    python backtest/regime_strategies.py

시장 상황 (근사 코스피, 매일 종가 기준)
    🟢 좋음            20일선 > 60일선, 지수 > 20일선
    🟠 좋다가 나빠짐    20일선 > 60일선, 지수 <= 20일선
    🔴 안 좋음          20일선 <= 60일선, 지수 <= 20일선
    🔵 나쁘다가 좋아짐  20일선 <= 60일선, 지수 > 20일선
    3거래일 연속 같은 상태가 나와야 상황이 바뀐 것으로 친다 (그날까지의 정보만 씀)

로직 (모두 시가총액 상위 100 보통주, 주 1회 교체, 신호일 다음 거래일 종가 체결, 실매매와 같은 비용)
    기본    : 120일 모멘텀(최근 5일 제외) 상위 10종목, 20위 밖 매도, 시장 필터(지수 < 60일선이면 전부 현금)
    상승장  : 60일 모멘텀(최근 5일 제외) 상위 5종목, 10위 밖 매도, 시장 필터 없음
    하락장  : 최근 60일 변동성이 낮은 순 10종목, 20위 밖 매도, 새로 살 때 자산의 50%만 투자
    꺾일 때 : 120일 모멘텀 상위 10종목, 10위 밖 매도 (더 엄격), 새로 살 때 자산의 50%만 투자
    반등    : 최근 60일 수익률이 유니버스 하위 절반이면서 최근 20일 수익률 > 0 인 종목 중 20일 수익률 높은 순 10종목,
              20위 밖(또는 조건 탈락) 매도
    로직을 바꿔 종목 수가 줄면 순위가 낮은 종목부터 판다.

평가 (결과를 보기 전에 고정)
    로직마다 처음부터 끝까지 따로 돌린 뒤, 그 로직이 맞춰진 상황인 날들의 하루 수익률만 모아 연환산한다.
    (그날 수익률의 상황 = 전날 종가까지의 정보로 정한 상황)
    통과 기준 (상황 로직마다)
        1. 자기 상황인 날들에서 연환산 수익률이 기본 로직보다 높다
        2. 앞 절반, 뒤 절반 각각에서도 기본보다 높다 (한쪽 절반에 그 상황이 40거래일 미만이면 '판단 불가')
    규칙대로 바꾸기 (그 주 신호일의 상황에 맞는 로직을 쓴다)
        3. 연수익률이 기본보다 높고, 최대낙폭이 기본보다 5%p 넘게 깊지 않다
    기간: 2022년 1월 첫 교체일부터 (기존 백테스트와 같음)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config  # noqa: E402
from momentum import load, prepare, signal_days, SKIP  # noqa: E402

FEE, SLIP, TAX = config.FEE_RATE, config.SLIPPAGE, config.STOCK_SELL_TAX
START = '2022-01-01'
FIRST = 255                     # 계산에 필요한 최소 거래일 (기존 백테스트와 같은 시작점)
CONFIRM = 3
REGIMES = {'G': '🟢 좋음', 'GB': '🟠 좋다가 나빠짐', 'B': '🔴 안 좋음', 'BG': '🔵 나쁘다가 좋아짐'}
LOGIC_FOR = {'G': '상승장', 'GB': '꺾일 때', 'B': '하락장', 'BG': '반등'}
MIN_DAYS = 40


# ------------------------------------------------------------------ 시장 상황
def regimes(index):
    ma20, ma60 = index.rolling(20).mean(), index.rolling(60).mean()
    raw = pd.Series(np.where(ma20 > ma60, np.where(index > ma20, 'G', 'GB'),
                             np.where(index > ma20, 'BG', 'B')), index=index.index)
    raw[ma60.isna()] = ''
    conf, cur, run_state, run_len = [], '', '', 0
    for s in raw:
        run_len = run_len + 1 if s == run_state else 1
        run_state = s
        if not cur or (s != cur and run_len >= CONFIRM):
            cur = s
        conf.append(cur)
    return pd.Series(conf, index=index.index)


# ------------------------------------------------------------------ 로직별 순위
def make_logics(P):
    cum, r = P['cum'], P['r']
    vol60 = r.rolling(60).std()

    def momentum_rank(p, lookback):
        elig = P['in_uni'].iloc[p] & P['exists'].iloc[p - SKIP - lookback]
        mom = (cum.iloc[p - SKIP] - cum.iloc[p - SKIP - lookback]).where(elig).dropna()
        return mom.rank(ascending=False, method='first')

    def lowvol_rank(p):
        elig = P['in_uni'].iloc[p] & P['exists'].iloc[p - 60]
        return vol60.iloc[p].where(elig).dropna().rank(method='first')

    def rebound_rank(p):
        elig = P['in_uni'].iloc[p] & P['exists'].iloc[p - 60]
        r60 = (cum.iloc[p] - cum.iloc[p - 60]).where(elig).dropna()
        r20 = (cum.iloc[p] - cum.iloc[p - 20]).reindex(r60.index)
        pick = r20[(r60 <= r60.median()) & (r20 > 0)]
        return pick.rank(ascending=False, method='first')

    return {
        '기본': dict(rank=lambda p: momentum_rank(p, 120), k=10, sell=20, invest=1.0, filt=True),
        '상승장': dict(rank=lambda p: momentum_rank(p, 60), k=5, sell=10, invest=1.0, filt=False),
        '하락장': dict(rank=lowvol_rank, k=10, sell=20, invest=0.5, filt=False),
        '꺾일 때': dict(rank=lambda p: momentum_rank(p, 120), k=10, sell=10, invest=0.5, filt=False),
        '반등': dict(rank=rebound_rank, k=10, sell=20, invest=1.0, filt=False),
    }


# ------------------------------------------------------------------ 주 1회 교체 시뮬레이션
def simulate(P, choose, slip=SLIP):
    """choose(p) -> 로직 설정 dict. 신호일 p의 설정으로 다음 거래일에 사고판다"""
    dates, traded, r = P['dates'], P['traded'], P['r']
    sig = signal_days(dates)
    start = max(dates.searchsorted(pd.Timestamp(START)), FIRST)
    buy_cost = (1 + slip) * (1 + FEE)
    sell_keep = (1 - slip) * (1 - FEE - TAX)
    cash, hold, equity, pending, n_trades = 1.0, {}, {}, None, 0

    def sell(tk):
        nonlocal cash, n_trades
        cash += hold.pop(tk)['value'] * sell_keep
        n_trades += 1

    for p in range(start, len(dates)):
        day = dates[p]
        for h in hold.values():
            h['value'] *= 1 + r.iat[p, h['col']]
        for tk in [t for t in hold if P['last_seen'][t] < day]:
            sell(tk)
        if pending is not None:
            rank, cfg, ok = pending
            pending = None
            for tk in list(hold):
                if not traded.iat[p, hold[tk]['col']]:
                    continue
                if not ok or tk not in rank.index or rank[tk] > cfg['sell']:
                    sell(tk)
            extra = sorted((t for t in hold if traded.iat[p, hold[t]['col']]), key=lambda t: rank.get(t, 1e9))
            while len(hold) > cfg['k'] and extra:      # 종목 수가 줄었으면 순위 낮은 것부터
                sell(extra.pop())
            if ok:
                for tk in rank.sort_values().index:
                    if len(hold) >= cfg['k']:
                        break
                    col = P['tickers'].get_loc(tk)
                    if tk in hold or not traded.iat[p, col]:
                        continue
                    eq = cash + sum(h['value'] for h in hold.values())
                    amount = min(eq * cfg['invest'] / cfg['k'], cash)
                    if amount <= 0:
                        break
                    cash -= amount
                    hold[tk] = {'col': col, 'value': amount / buy_cost}
        equity[day] = cash + sum(h['value'] for h in hold.values())
        if day in sig and p + 1 < len(dates):
            cfg = choose(p)
            ok = bool(P['market_ok'].iat[p]) if cfg['filt'] else True
            pending = (cfg['rank'](p), cfg, ok)
    return pd.Series(equity), n_trades


# ------------------------------------------------------------------ 결과 정리
def ann(rets):
    return (1 + rets).prod() ** (252 / len(rets)) - 1 if len(rets) else np.nan


def cagr(eq):
    return (eq.iloc[-1] / eq.iloc[0]) ** (252 / (len(eq) - 1)) - 1


def mdd(eq):
    return float((eq / eq.cummax() - 1).min())


def main():
    panel, index = load()
    P = prepare(panel, index)
    idx = index.reindex(P['dates'])
    reg = regimes(idx)
    logics = make_logics(P)

    curves = {}
    for name, cfg in logics.items():
        curves[name], n = simulate(P, lambda p, cfg=cfg: cfg)
        print(f'완료 {name} (매매 {n}건)', flush=True)
    switch_cfg = lambda p: logics[LOGIC_FOR.get(reg.iat[p], '기본')] if reg.iat[p] else logics['기본']  # noqa: E731
    curves['규칙대로 바꾸기'], n = simulate(P, switch_cfg)
    print(f'완료 규칙대로 바꾸기 (매매 {n}건)', flush=True)

    days = curves['기본'].index
    split = days[len(days) // 2]
    label = reg.shift(1).reindex(days)              # 그날 수익률의 상황 = 전날까지 정보로 정한 상황
    rets = {k: v.pct_change().fillna(0) for k, v in curves.items()}

    print(f'\n기간 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d} ({len(days)}거래일), 앞/뒤 구분 {split:%Y-%m-%d}')
    cnt = label.value_counts()
    print('상황별 거래일 :', ', '.join(f'{REGIMES[k]} {cnt.get(k, 0)}일' for k in REGIMES))
    changes = (reg.reindex(days) != reg.reindex(days).shift()).sum() - 1
    print(f'상황이 바뀐 횟수 : {changes}번 (약 {len(days) / max(changes, 1):.0f}거래일에 한 번)\n')

    # 전체 성과
    rows = []
    for k, eq in curves.items():
        rows.append({'로직': k, '연수익률(%)': cagr(eq) * 100, '최대낙폭(%)': mdd(eq) * 100,
                     '앞절반(%)': cagr(eq[eq.index <= split]) * 100, '뒷절반(%)': cagr(eq[eq.index >= split]) * 100})
    whole = pd.DataFrame(rows)

    # 상황별 연환산 수익률
    rows = []
    for k in curves:
        row = {'로직': k}
        for g, gname in REGIMES.items():
            m = label == g
            row[gname] = ann(rets[k][m]) * 100
        rows.append(row)
    by_regime = pd.DataFrame(rows)

    # 통과 판정
    rows = []
    for g, lname in LOGIC_FOR.items():
        verdict, detail = [], []
        for part, mask in (('전체', label.notna()), ('앞', label.index <= split), ('뒤', label.index > split)):
            m = (label == g) & mask
            if m.sum() < MIN_DAYS and part != '전체':
                verdict.append(None)
                detail.append(f'{part} {m.sum()}일(판단 불가)')
                continue
            a, b = ann(rets[lname][m]), ann(rets['기본'][m])
            verdict.append(a > b)
            detail.append(f'{part} {a * 100:+.1f}% vs 기본 {b * 100:+.1f}% ({m.sum()}일)')
        ok = all(v for v in verdict if v is not None) and verdict[0]
        undecided = any(v is None for v in verdict)
        rows.append({'상황': REGIMES[g], '로직': lname,
                     '통과': ('O' if ok else 'X') + (' (일부 판단 불가)' if undecided and ok else ''),
                     '내용': ' / '.join(detail)})
    verdicts = pd.DataFrame(rows)
    sw, base = curves['규칙대로 바꾸기'], curves['기본']
    sw_ok = cagr(sw) > cagr(base) and mdd(sw) >= mdd(base) - 0.05

    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    pd.set_option('display.max_colwidth', 200)
    print('전체 기간 성과 (비용 포함)')
    print(whole.round(1).to_string(index=False))
    print('\n상황별 연환산 수익률 (%, 그 상황인 날들만 모음)')
    print(by_regime.round(1).to_string(index=False))
    print('\n상황 로직 판정')
    print(verdicts.to_string(index=False))
    print(f'\n규칙대로 바꾸기 판정 : {"O" if sw_ok else "X"} '
          f'(연 {cagr(sw):.1%} vs 기본 {cagr(base):.1%}, 최대낙폭 {mdd(sw):.1%} vs 기본 {mdd(base):.1%})')

    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    whole.round(3).to_csv(out / 'regime_whole.csv', index=False, encoding='utf-8-sig')
    by_regime.round(3).to_csv(out / 'regime_by_state.csv', index=False, encoding='utf-8-sig')
    verdicts.to_csv(out / 'regime_verdicts.csv', index=False, encoding='utf-8-sig')
    reg.rename('state').to_csv(out / 'regime_daily.csv', encoding='utf-8-sig')
    print(f'\n저장: {out}')


if __name__ == '__main__':
    main()
