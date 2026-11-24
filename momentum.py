"""대형주 모멘텀 전략의 계산 부분 (백테스트와 실매매가 같이 쓴다)

기간(LOOKBACK)과 종목 수(HOLD)는 전략마다 config.STRATEGIES 에서 바꿔 쓸 수 있다. 아래는 기본값.

규칙 v1 (2026-11-24 확정, backtest/large_cap_momentum.py 에서 검증한 조합)
    유니버스   : 신호일 시가총액 상위 100개 보통주 (우선주 제외, 신호일 거래된 종목)
    신호       : 최근 120거래일 수익률 (가장 최근 5거래일은 빼고 계산), 높은 순
    교체       : 매주 마지막 거래일(신호일) 종가로 순위를 매기고, 다음 거래일에 사고판다
    보유       : 10종목. 살 때마다 그 시점 전체 자산의 1/10
    매도       : 순위가 20위 밖으로 밀리거나 유니버스에서 빠지면 판다
    시장 필터  : 신호일 근사 코스피가 60일 이동평균 아래면 전부 팔고 현금으로 기다린다

데이터는 backtest/data/daily/YYYYMMDD.csv.gz (KRX Open API 일별 전 종목 시세)와
backtest/data/kospi_index.csv (코스피 종목 시가총액 가중 근사 지수)를 쓴다.
"""
from pathlib import Path

import numpy as np
import pandas as pd

VERSION = 'v1'
UNIVERSE_SIZE = 100
LOOKBACK = 120
SKIP = 5                    # 최근 5거래일은 모멘텀 계산에서 뺀다
HOLD = 10                   # 보유 종목 수 K
SELL_RANK = 2 * HOLD        # 순위가 이보다 밀리면 판다
MARKET_MA = 60
SIGNAL_WEEKDAY = 4          # 금요일 (그 주 금요일 이하 요일 중 마지막 거래일이 신호일)

DATA = Path(__file__).resolve().parent / 'backtest' / 'data'
DAILY = DATA / 'daily'


# ------------------------------------------------------------------ 데이터
def load(last_days=None):
    """일별 시세를 종목별 표로 읽는다. last_days를 주면 최근 그만큼의 거래일만 읽는다."""
    files = sorted(DAILY.glob('*.csv.gz'))
    if last_days:
        files = files[-last_days:]
    if not files:
        raise FileNotFoundError(f'{DAILY} 에 시세 파일이 없습니다.')
    frames = []
    for f in files:
        df = pd.read_csv(f, dtype={'ticker': str}, usecols=['ticker', 'name', 'close', 'volume', 'rate', 'mcap'])
        df['date'] = pd.Timestamp(f.name[:8])
        frames.append(df)
    d = pd.concat(frames, ignore_index=True)
    d = d[d['ticker'].str.endswith('0')]            # 우선주 제외
    panel = {c: d.pivot(index='date', columns='ticker', values=c) for c in ['close', 'volume', 'rate', 'mcap']}
    panel['names'] = d.drop_duplicates('ticker', keep='last').set_index('ticker')['name']
    index = pd.read_csv(DATA / 'kospi_index.csv', index_col='date', parse_dates=True)['close']
    return panel, index


def prepare(panel, index):
    close, volume, rate, mcap = (panel[c] for c in ['close', 'volume', 'rate', 'mcap'])
    traded = (volume > 0) & close.notna()
    r = (rate / 100).where(traded).fillna(0.0)
    cum = np.log1p(r).cumsum()
    in_uni = mcap.where(traded).rank(axis=1, ascending=False) <= UNIVERSE_SIZE
    exists = close.notna()
    last_seen = exists[::-1].idxmax()               # 종목별 마지막 시세 날짜
    # 지수는 전체 기간으로 이동평균을 낸 뒤 시세 날짜에 맞춘다 (일부 기간만 읽어도 값이 같게)
    market_ok = (index >= index.rolling(MARKET_MA).mean()).reindex(close.index).fillna(False).astype(bool)
    return dict(dates=close.index, tickers=close.columns, traded=traded, r=r, cum=cum,
                in_uni=in_uni, exists=exists, last_seen=last_seen, market_ok=market_ok,
                names=panel.get('names'))


def signal_days(dates, weekday=SIGNAL_WEEKDAY):
    """주마다 weekday 이하 요일 중 마지막 거래일"""
    s = pd.Series(dates, index=dates)
    s = s[s.dt.weekday <= weekday]
    wk = s.dt.isocalendar()
    return set(s.groupby([wk['year'], wk['week']]).max())


def ranking(P, p, lookback=LOOKBACK):
    """신호일 위치 p 의 모멘텀 순위 (1이 가장 강함)"""
    cum = P['cum']
    elig = P['in_uni'].iloc[p] & P['exists'].iloc[p - SKIP - lookback]
    mom = (cum.iloc[p - SKIP] - cum.iloc[p - SKIP - lookback]).where(elig).dropna()
    return mom.rank(ascending=False, method='first')


def momentum(P, p, lookback=LOOKBACK):
    """신호일 위치 p 의 모멘텀 수익률 (설명용)"""
    cum = P['cum']
    return np.expm1(cum.iloc[p - SKIP] - cum.iloc[p - SKIP - lookback])


# ------------------------------------------------------------------ 실매매용
def latest_signal(dates, today):
    """오늘(today)보다 앞선 주의 신호일 중 가장 최근 것. 없으면 None

    이번 주 데이터는 주가 끝나지 않아 신호일인지 아직 모르므로 쓰지 않는다.
    """
    today = pd.Timestamp(today)
    tw = today.isocalendar()[:2]
    done = [d for d in dates if d.isocalendar()[:2] < tw]
    if not done:
        return None
    sig = signal_days(pd.DatetimeIndex(done))
    return max(sig) if sig else None


def decide(P, signal_day, held, lookback=LOOKBACK, hold=HOLD):
    """신호일 기준으로 팔 종목과 살 후보를 정한다.

    held: 지금 이 전략이 들고 있는 종목 코드 목록
    돌려주는 값: dict(market_ok, rank(Series), mom(Series), sells[(code, 이유)], keep, buys[code ...] 순위순, free)
    """
    p = P['dates'].get_loc(signal_day)
    ok = bool(P['market_ok'].iat[p])
    rank = ranking(P, p, lookback)
    sell_rank = 2 * hold
    sells = []
    for code in held:
        if not ok:
            sells.append((code, '시장 필터 (근사 코스피 < 60일 평균)'))
        elif code not in rank.index:
            sells.append((code, '유니버스(시가총액 상위 100) 이탈'))
        elif rank[code] > sell_rank:
            sells.append((code, f'순위 {int(rank[code])}위 (기준 {sell_rank}위 밖)'))
    keep = [c for c in held if c not in {s[0] for s in sells}]
    free = max(hold - len(keep), 0) if ok else 0
    buys = [c for c in rank.sort_values().index if c not in held][:free + 5]   # 못 사는 종목 대비 여유 5개
    return dict(market_ok=ok, rank=rank, mom=momentum(P, p, lookback), sells=sells, keep=keep,
                buys=buys, free=free)


# ------------------------------------------------------------------ 시장 상황 (알림과 backtest/regime_strategies.py 가 같이 씀)
REGIME_NAMES = {'G': '좋음', 'GB': '좋다가 나빠지는 중', 'B': '안 좋음', 'BG': '나쁘다가 좋아지는 중'}
REGIME_EMOJI = {'G': '🟢', 'GB': '🟠', 'B': '🔴', 'BG': '🔵', '': '⚪'}
REGIME_CONFIRM = 3          # 같은 상태가 이만큼 연속으로 나와야 상황이 바뀐 것으로 친다


def regimes(index, confirm=REGIME_CONFIRM):
    """근사 코스피로 매일의 시장 상황. (그날 상태 raw, 확정된 상황 conf) 두 Series

    G  좋음                20일선 > 60일선, 지수 > 20일선
    GB 좋다가 나빠지는 중   20일선 > 60일선, 지수 <= 20일선
    B  안 좋음              20일선 <= 60일선, 지수 <= 20일선
    BG 나쁘다가 좋아지는 중 20일선 <= 60일선, 지수 > 20일선
    """
    ma20, ma60 = index.rolling(20).mean(), index.rolling(60).mean()
    raw = pd.Series(np.where(ma20 > ma60, np.where(index > ma20, 'G', 'GB'),
                             np.where(index > ma20, 'BG', 'B')), index=index.index)
    raw[ma60.isna()] = ''
    conf, cur, run_state, run_len = [], '', '', 0
    for s in raw:
        run_len = run_len + 1 if s == run_state else 1
        run_state = s
        if not cur or (s != cur and run_len >= confirm):
            cur = s
        conf.append(cur)
    return raw, pd.Series(conf, index=index.index)
