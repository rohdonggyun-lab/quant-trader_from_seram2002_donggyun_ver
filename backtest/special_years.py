"""특별한 해에만 쓰는 로직 (급등장 2025, 급락장 2022)

    python backtest/special_years.py

기본 로직은 평소에 쓰고, 특별히 좋은 해·나쁜 해에만 직접 바꿔 쓸 로직을 찾는다.
그 해에만 잘하면 되므로 해당 연도로만 평가한다.

후보 로직
    급등장용  A. 코스피 200 100% (ETF로 산다고 봄)
              B. 시가총액 상위 5종목 똑같이 (주 1회 교체, 순위 밖이면 매도)
              C. 60일 모멘텀(최근 5일 제외) 상위 3종목 집중 (6위 밖이면 매도)
    급락장용  D. 전부 현금
              E. 코스피 200 인버스 50% + 현금 50%
              F. 코스피 200 인버스 100%
    인버스는 코스피 200 하루 등락률의 반대로 계산 (실제 ETF의 보수·추적오차 제외)

알아채는 신호 (매일 시장 상황 알림으로 알 수 있는 것만)
    급등장 신호 켜짐: 시장 상황 🟢 좋음 이면서 코스피가 60일선보다 10% 이상 위인 날이 5거래일 연속
    급락장 신호 켜짐: 시장 상황 🔴 안 좋음 이면서 코스피가 60일선보다 10% 이상 아래인 날이 5거래일 연속
    신호 꺼짐: 시장 상황이 🟢(급등장) / 🔴(급락장) 에서 벗어나면 기본 로직으로 돌아온다
    신호가 켜진 날 장 마감 뒤에 알고, 다음 거래일 종가에 바꾼다 (그 다음 날 수익부터 반영)
    바꿀 때마다 비용 0.35% (주식 팔고 다른 것 사는 왕복 비용 근사)

평가 (결과를 보기 전에 고정)
    이상적  : 그 해 1월부터 특별 로직을 썼다면
    현실적  : 평소엔 기본 로직, 신호가 켜진 동안만 특별 로직
    통과 기준: 현실적 수익률이 그 해 기본 로직보다 10%p 이상 높다
    참고    : 신호가 다른 해에 켜진 적이 있는지, 그때 기본 로직 대비 손익
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import momentum as M  # noqa: E402
import regime_strategies as RS  # noqa: E402

SWITCH_COST = 0.0035
EDGE, RUN = 0.10, 5
BULL_YEAR, BEAR_YEAR = 2025, 2022


def kospi200():
    vals = {}
    for f in sorted((HERE / 'data' / 'index').glob('*.csv.gz')):
        d = pd.read_csv(f, usecols=['name', 'close'])
        vals[pd.Timestamp(f.name[:8])] = float(d.loc[d['name'] == '코스피 200', 'close'].iloc[0])
    return pd.Series(vals)


def signal(kospi, state, want):
    """want='G' 또는 'B'. 켜짐/꺼짐 Series (그날 장 마감 기준)"""
    ma60 = kospi.rolling(60).mean()
    gap = kospi / ma60 - 1
    cond = (state == want) & ((gap >= EDGE) if want == 'G' else (gap <= -EDGE))
    run, on, out = 0, False, []
    for c, s in zip(cond, state):
        run = run + 1 if c else 0
        if not on and run >= RUN:
            on = True
        elif on and s != want:
            on = False
        out.append(on)
    return pd.Series(out, index=kospi.index)


def combine(base_r, special_r, active):
    """신호(active, 그날 장 마감 기준)에 따라 다음다음 날 수익부터 특별 로직. 바꿀 때마다 비용"""
    use = active.shift(2, fill_value=False)
    r = np.where(use, special_r, base_r)
    switches = (use != use.shift(1, fill_value=False)).astype(float)
    return pd.Series((1 + r) * (1 - SWITCH_COST * switches.values) - 1, index=base_r.index), use


def year_ret(r, y):
    x = r[r.index.year == y]
    return (1 + x).prod() - 1


def main():
    panel, kospi = M.load()
    P = M.prepare(panel, kospi)
    logics = RS.make_logics(P)
    mcap = panel['mcap']

    def mcap_rank(p):
        m = mcap.iloc[p].where(P['in_uni'].iloc[p]).dropna()
        return m.rank(ascending=False, method='first')

    def mom_rank(p):
        return logics['상승장']['rank'](p)                 # 60일 모멘텀 (최근 5일 제외)

    base_eq, _ = RS.simulate(P, lambda p: logics['기본'])
    days = base_eq.index
    base_r = base_eq.pct_change().fillna(0)
    k200_r = kospi200().reindex(days).pct_change().fillna(0)
    b_eq, _ = RS.simulate(P, lambda p: dict(rank=mcap_rank, k=5, sell=5, invest=1.0, filt=False))
    c_eq, _ = RS.simulate(P, lambda p: dict(rank=mom_rank, k=3, sell=6, invest=1.0, filt=False))
    cands = {
        'G': {'A 코스피 200': k200_r, 'B 시총 상위 5': b_eq.pct_change().fillna(0),
              'C 모멘텀 상위 3': c_eq.pct_change().fillna(0)},
        'B': {'D 현금': pd.Series(0.0, index=days), 'E 인버스 50%': -0.5 * k200_r, 'F 인버스 100%': -k200_r},
    }

    _, state = M.regimes(kospi)
    k = kospi.reindex(days)
    years = sorted(set(days.year))

    for want, target, label in (('G', BULL_YEAR, '🚀 급등장'), ('B', BEAR_YEAR, '🛡️ 급락장')):
        sig = signal(kospi, state, want).reindex(days).fillna(False)
        print(f'\n===== {label} 로직 ({target}년) =====')
        periods, start, prev = [], None, None
        for d, v in sig.items():
            if v and start is None:
                start = d
            if not v and start is not None:
                periods.append((start, prev))
                start = None
            prev = d
        if start is not None:
            periods.append((start, prev))
        print('신호가 켜졌던 기간 :', ', '.join(f'{a:%Y-%m-%d}~{b:%m-%d}({len(sig[a:b])}일)' for a, b in periods) or '없음')
        print(f'{target}년 기본 로직 {year_ret(base_r, target):+.1%}, 코스피 {k[k.index.year == target].iloc[-1] / k[k.index.year == target].iloc[0] - 1:+.1%}, '
              f'그 해 신호 켜진 날 {int(sig[sig.index.year == target].sum())}일')
        rows = []
        for name, sr in cands[want].items():
            ideal = year_ret(sr, target) * (1 - SWITCH_COST)
            comb, use = combine(base_r, sr, sig)
            real = year_ret(comb, target)
            row = {'로직': name, '이상적(1월부터)': ideal * 100, '현실적(신호 따라)': real * 100,
                   '기본 대비(%p)': (real - year_ret(base_r, target)) * 100}
            row['통과'] = 'O' if row['기본 대비(%p)'] >= 10 else 'X'
            others = []
            for y in years:
                if y == target or not use[use.index.year == y].any():
                    continue
                others.append(f'{y}: {(year_ret(comb, y) - year_ret(base_r, y)) * 100:+.1f}%p')
            row['다른 해에 켜졌을 때 (기본 대비)'] = ', '.join(others) or '안 켜짐'
            rows.append(row)
        print(pd.DataFrame(rows).round(1).to_string(index=False))


if __name__ == '__main__':
    main()
