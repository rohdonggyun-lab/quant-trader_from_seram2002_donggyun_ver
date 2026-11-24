"""업종(섹터) 아이디어 백테스트 — 코스피 업종지수로 "업종을 통째로 산다"고 보고 계산

    python backtest/sector_rotation.py

데이터: backtest/data/index/ (fetch_index.py, KRX 코스피 시리즈 지수 일별시세)
업종 21개: 음식료·담배, 섬유·의류, 종이·목재, 화학, 제약, 비금속, 금속, 기계·장비, 전기전자, 의료·정밀기기,
          운송장비·부품, 유통, 전기·가스, 건설, 운송·창고, 통신, 금융, 일반서비스, 오락·문화, IT 서비스, 부동산
          (증권·보험은 금융에 포함돼서 빼고, '제조'는 여러 업종을 합친 것이라 뺀다.
           나중에 생긴 업종은 계산에 필요한 기간이 쌓인 뒤부터 후보가 된다)
방어 업종: 금융, 통신, 음식료·담배
시장 상황: 실제 코스피 지수로 계산 (momentum.regimes 와 같은 규칙: 20일선·60일선, 3일 연속 확정)
교체: 주 1회 (그 주 마지막 거래일 종가로 판단, 다음 거래일 종가 체결), 비용은 주식과 같게 (매도세 포함, 보수적)
기간: 2022년 1월 첫 교체일부터 (다른 백테스트와 같음)

가설과 통과 기준 (결과를 보기 전에 고정)
  A. 섹터 모멘텀 : 최근 120거래일(최근 5일 제외) 가장 강한 업종 3개를 같은 금액으로 보유, 6위 밖이면 판다
     통과: 연수익률이 업종 균등 보유(비용 0)와 코스피보다 높고, 앞 절반과 뒤 절반에서도 각각 그렇다
  B. 상황별 섹터 : 🟢·🔵 → A와 같은 규칙 / 🟠 → 방어 업종 3개 / 🔴 → 현금
     통과: 앞 절반과 뒤 절반 각각에서, 연수익률이 A보다 높거나
           (A보다 2%p 이상 낮지 않으면서 최대낙폭이 A보다 5%p 이상 얕다)
  C. 뒤따라 오르기 : 한 업종의 20일 수익률이 코스피보다 10%p 이상 높아진 날(직전 20거래일 안에 같은 일이 없을 때)을
     찾아, 그날 최근 60일 수익률이 가장 낮은 업종 3개(주도 업종 제외)의 다음 20거래일 수익률을 코스피와 비교
     통과: 그런 날 15번 이상, 소외 업종 평균이 코스피를 이긴 비율 55% 이상,
           평균 초과수익이 앞 절반과 뒤 절반 모두 양수
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config  # noqa: E402
import momentum as M  # noqa: E402

FEE, SLIP, TAX = config.FEE_RATE, config.SLIPPAGE, config.STOCK_SELL_TAX
INDEX_DIR = HERE / 'data' / 'index'
SECTORS = ['음식료·담배', '섬유·의류', '종이·목재', '화학', '제약', '비금속', '금속', '기계·장비', '전기전자',
           '의료·정밀기기', '운송장비·부품', '유통', '전기·가스', '건설', '운송·창고', '통신', '금융', '일반서비스',
           '오락·문화', 'IT 서비스', '부동산']
DEFENSIVE = ['금융', '통신', '음식료·담배']
KOSPI = '코스피'
START = '2022-01-01'
FIRST = 255
LOOKBACK, SKIP, TOP, SELL_RANK = 120, 5, 3, 6
EVENT_GAP, EVENT_EDGE, LAG_WINDOW, FWD = 20, 0.10, 60, 20


# ------------------------------------------------------------------ 데이터
def load():
    frames = []
    for f in sorted(INDEX_DIR.glob('*.csv.gz')):
        d = pd.read_csv(f, usecols=['name', 'close', 'rate'])
        d['date'] = pd.Timestamp(f.name[:8])
        frames.append(d)
    if not frames:
        raise SystemExit('backtest/data/index 가 비어 있습니다. 먼저 fetch_index.py 를 실행하세요.')
    d = pd.concat(frames, ignore_index=True)
    close = d.pivot(index='date', columns='name', values='close')
    rate = d.pivot(index='date', columns='name', values='rate') / 100
    return close, rate


# ------------------------------------------------------------------ 주 1회 교체 시뮬레이션 (업종 단위)
def simulate(close, rate, target_fn):
    """target_fn(p, held) -> 보유할 업종 목록(순서는 우선순위) 또는 None(현금)"""
    dates = close.index
    sig = M.signal_days(dates)
    start = max(dates.searchsorted(pd.Timestamp(START)), FIRST)
    buy_cost, sell_keep = (1 + SLIP) * (1 + FEE), (1 - SLIP) * (1 - FEE - TAX)
    cash, hold, equity, pending = 1.0, {}, {}, None
    for p in range(start, len(dates)):
        r = rate.iloc[p]
        for s in hold:
            hold[s] *= 1 + (0 if np.isnan(r[s]) else r[s])
        if pending is not None:
            target = pending
            pending = None
            for s in list(hold):
                if s not in target:
                    cash += hold.pop(s) * sell_keep
            for s in target:
                if len(hold) >= TOP:
                    break
                if s in hold:
                    continue
                eq = cash + sum(hold.values())
                amount = min(eq / TOP, cash)
                if amount <= 0:
                    break
                cash -= amount
                hold[s] = amount / buy_cost
        equity[dates[p]] = cash + sum(hold.values())
        if dates[p] in sig and p + 1 < len(dates):
            pending = target_fn(p, set(hold)) or []
    return pd.Series(equity)


def momentum_ranks(close, p):
    past = close.iloc[p - SKIP - LOOKBACK]
    now = close.iloc[p - SKIP]
    mom = (now / past - 1)[SECTORS].dropna()
    return mom.rank(ascending=False, method='first')


def keep_or_top(rank, held):
    """보유 중이고 6위 안이면 계속 보유, 빈자리는 순위순으로"""
    keep = sorted((s for s in held if s in rank.index and rank[s] <= SELL_RANK), key=lambda s: rank[s])[:TOP]
    rest = [s for s in rank.sort_values().index if s not in keep]
    return keep + rest[:TOP - len(keep)]


def equal_weight(close, rate):
    """업종 균등: 매주 같은 금액으로 다시 맞춤 (비용 0)"""
    dates = close.index
    sig = M.signal_days(dates)
    start = max(dates.searchsorted(pd.Timestamp(START)), FIRST)
    level, w, eq, pending = 1.0, None, {}, None
    for p in range(start, len(dates)):
        if w is not None:
            w = w * (1 + rate.iloc[p][w.index].fillna(0))
            level = w.sum()
        if pending is not None:
            w = pd.Series(level / len(pending), index=pending)
            pending = None
        eq[dates[p]] = level
        if dates[p] in sig:
            pending = [s for s in SECTORS if not np.isnan(close.iloc[p].get(s, np.nan))]
    return pd.Series(eq)


# ------------------------------------------------------------------ 결과 정리
def cagr(eq):
    return (eq.iloc[-1] / eq.iloc[0]) ** (252 / (len(eq) - 1)) - 1


def mdd(eq):
    return float((eq / eq.cummax() - 1).min())


def halves(eq, split):
    a, b = eq[eq.index <= split], eq[eq.index >= split]
    return (cagr(a), mdd(a)), (cagr(b), mdd(b))


def main():
    close, rate = load()
    dates = close.index
    missing = [s for s in SECTORS + [KOSPI] if s not in close.columns]
    if missing:
        print('데이터에 없는 지수 :', missing)
    first_day = {s: close[s].first_valid_index() for s in SECTORS if s in close.columns}
    late = {s: f'{d:%Y-%m-%d}' for s, d in first_day.items() if d > dates[0]}
    if late:
        print('나중에 생긴 업종 :', late)

    kospi = close[KOSPI]
    _, reg = M.regimes(kospi)

    eq_a = simulate(close, rate, lambda p, held: keep_or_top(momentum_ranks(close, p), held))

    def b_target(p, held):
        g = reg.iat[p]
        if g == 'B':
            return None
        if g == 'GB':
            return DEFENSIVE
        return keep_or_top(momentum_ranks(close, p), held)
    eq_b = simulate(close, rate, b_target)
    eq_ew = equal_weight(close, rate)
    days = eq_a.index
    eq_k = kospi.reindex(days)
    split = days[len(days) // 2]

    rows = []
    for name, eq in (('A 섹터 모멘텀', eq_a), ('B 상황별 섹터', eq_b), ('업종 균등 (비용 0)', eq_ew), ('코스피', eq_k)):
        (c1, m1), (c2, m2) = halves(eq, split)
        rows.append({'전략': name, '연수익률(%)': cagr(eq) * 100, '최대낙폭(%)': mdd(eq) * 100,
                     '앞절반(%)': c1 * 100, '앞절반 낙폭(%)': m1 * 100, '뒷절반(%)': c2 * 100, '뒷절반 낙폭(%)': m2 * 100})
    res = pd.DataFrame(rows).set_index('전략')

    a_ok = all(cagr(eq_a) > cagr(x) for x in (eq_ew, eq_k))
    for part in range(2):
        a_h = halves(eq_a, split)[part][0]
        a_ok &= all(a_h > halves(x, split)[part][0] for x in (eq_ew, eq_k))
    b_ok = True
    for part in range(2):
        (ca, ma), (cb, mb) = halves(eq_a, split)[part], halves(eq_b, split)[part]
        b_ok &= cb > ca or (cb >= ca - 0.02 and mb >= ma + 0.05)

    # C. 뒤따라 오르기
    r20 = close.pct_change(20)
    r60 = close.pct_change(LAG_WINDOW)
    edge = r20[SECTORS].sub(r20[KOSPI], axis=0)
    events, last = [], -10 ** 9
    for p in range(max(dates.searchsorted(pd.Timestamp(START)), FIRST), len(dates) - FWD):
        e = edge.iloc[p].dropna()
        if e.empty or e.max() < EVENT_EDGE or p - last <= EVENT_GAP:
            continue
        last = p
        leader = e.idxmax()
        lag = r60.iloc[p][SECTORS].drop(leader).dropna().nsmallest(3).index
        fwd = close.iloc[p + FWD] / close.iloc[p] - 1
        events.append({'날짜': dates[p], '주도 업종': leader, '주도 초과(%p)': e.max() * 100,
                       '소외 업종': ', '.join(lag), '소외 20일(%)': fwd[lag].mean() * 100,
                       '코스피 20일(%)': fwd[KOSPI] * 100, '주도 20일(%)': fwd[leader] * 100})
    ev = pd.DataFrame(events)
    if len(ev):
        ev['초과(%p)'] = ev['소외 20일(%)'] - ev['코스피 20일(%)']
        first, second = ev[ev['날짜'] <= split], ev[ev['날짜'] > split]
        c_ok = (len(ev) >= 15 and (ev['초과(%p)'] > 0).mean() >= 0.55
                and len(first) and len(second) and first['초과(%p)'].mean() > 0 and second['초과(%p)'].mean() > 0)
    else:
        first = second = ev
        c_ok = False

    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', 30)
    print(f'기간 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d} ({len(days)}거래일), 앞/뒤 구분 {split:%Y-%m-%d}\n')
    print(res.round(1).to_string())
    print(f'\nA 섹터 모멘텀 판정 : {"O" if a_ok else "X"}')
    print(f'B 상황별 섹터 판정 : {"O" if b_ok else "X"}')
    print(f'\nC 뒤따라 오르기 : 해당 시점 {len(ev)}번')
    if len(ev):
        print(f'  소외 업종이 코스피를 이긴 비율 {(ev["초과(%p)"] > 0).mean():.0%}, 평균 초과 {ev["초과(%p)"].mean():+.2f}%p '
              f'(앞 {first["초과(%p)"].mean():+.2f}%p / 뒤 {second["초과(%p)"].mean():+.2f}%p)')
        print(f'  참고: 같은 기간 주도 업종의 다음 20일 평균 초과 {(ev["주도 20일(%)"] - ev["코스피 20일(%)"]).mean():+.2f}%p')
        print(ev.round(1).to_string(index=False))
    print(f'C 뒤따라 오르기 판정 : {"O" if c_ok else "X"}')

    out = HERE / 'results'
    out.mkdir(exist_ok=True)
    res.round(3).to_csv(out / 'sector_ab.csv', encoding='utf-8-sig')
    ev.round(3).to_csv(out / 'sector_c_events.csv', index=False, encoding='utf-8-sig')
    print(f'\n저장: {out}')


if __name__ == '__main__':
    main()
