"""시장 필터와 시장 상황에 근사 코스피 대신 실제 코스피를 써도 되는지 확인

    python backtest/real_kospi_filter.py

근사 코스피 : 코스피 종목의 시가총액 가중 등락률을 이어 붙인 것 (backtest/data/kospi_index.csv)
실제 코스피 : KRX 코스피 시리즈 지수의 '코스피' (backtest/data/index/, fetch_index.py)

같은 기본 로직(120일 모멘텀 10종목, 지수 < 60일선이면 현금)을 두 지수로 각각 돌려 비교한다.

바꾸는 기준 (결과를 보기 전에 고정) — 목적은 성과를 올리는 것이 아니라 정확한 지수를 쓰는 것이므로
"결과가 거의 같으면" 실제 코스피로 바꾼다
    1. 연수익률이 근사 코스피보다 2%p 넘게 낮지 않다
    2. 최대낙폭이 근사 코스피보다 3%p 넘게 깊지 않다
    3. 앞 절반, 뒤 절반 각각에서도 연수익률이 2%p 넘게 낮지 않다
참고로 두 지수의 시장 필터(60일선 위/아래)와 시장 상황(4가지)이 며칠이나 같았는지도 보여준다.
"""
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import momentum as M  # noqa: E402
import regime_strategies as RS  # noqa: E402


def real_kospi():
    vals = {}
    for f in sorted((HERE / 'data' / 'index').glob('*.csv.gz')):
        d = pd.read_csv(f, usecols=['name', 'close'])
        vals[pd.Timestamp(f.name[:8])] = float(d.loc[d['name'] == '코스피', 'close'].iloc[0])
    s = pd.Series(vals, name='close')
    s.index.name = 'date'
    return s


def main():
    panel, proxy = M.load()
    real = real_kospi()
    rows, curves, P_by = [], {}, {}
    for name, idx in (('근사 코스피', proxy), ('실제 코스피', real)):
        P = M.prepare(panel, idx)
        P_by[name] = P
        logics = RS.make_logics(P)
        eq, n = RS.simulate(P, lambda p, cfg=logics['기본']: cfg)
        curves[name] = eq
        print(f'완료 {name} (매매 {n}건)', flush=True)

    days = curves['근사 코스피'].index
    split = days[len(days) // 2]
    for name, eq in curves.items():
        a, b = eq[eq.index <= split], eq[eq.index >= split]
        rows.append({'필터 지수': name, '연수익률(%)': RS.cagr(eq) * 100, '최대낙폭(%)': RS.mdd(eq) * 100,
                     '앞절반(%)': RS.cagr(a) * 100, '뒷절반(%)': RS.cagr(b) * 100})
    res = pd.DataFrame(rows).set_index('필터 지수')
    p, r = res.loc['근사 코스피'], res.loc['실제 코스피']
    ok = (r['연수익률(%)'] >= p['연수익률(%)'] - 2 and r['최대낙폭(%)'] >= p['최대낙폭(%)'] - 3
          and r['앞절반(%)'] >= p['앞절반(%)'] - 2 and r['뒷절반(%)'] >= p['뒷절반(%)'] - 2)

    mp, mr = P_by['근사 코스피']['market_ok'].reindex(days), P_by['실제 코스피']['market_ok'].reindex(days)
    _, gp = M.regimes(proxy)
    _, gr = M.regimes(real)
    gp, gr = gp.reindex(days), gr.reindex(days)
    print(f'\n기간 {days[0]:%Y-%m-%d} ~ {days[-1]:%Y-%m-%d} ({len(days)}거래일), 앞/뒤 구분 {split:%Y-%m-%d}\n')
    print(res.round(2).to_string())
    print(f'\n시장 필터(60일선 위/아래)가 같은 날 : {(mp == mr).mean():.1%}')
    print(f'시장 상황(4가지)이 같은 날 : {(gp == gr).mean():.1%}')
    print(f'두 지수 하루 등락률 상관 : {proxy.pct_change().corr(real.pct_change()):.4f}')
    print(f'\n실제 코스피로 바꾸기 판정 : {"O (바꿔도 됨)" if ok else "X (그대로 둠)"}')
    print(f'\n최근 5거래일 비교\n{pd.DataFrame({"근사": proxy, "실제": real}).tail(5).round(2).to_string()}')


if __name__ == '__main__':
    main()
