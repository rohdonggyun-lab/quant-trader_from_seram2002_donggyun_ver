"""일봉 데이터 수집 (KRX Open API, 공식 경로)

    python backtest/fetch_data.py 20210104 20261120

평일마다 KOSPI + KOSDAQ 전 종목의 그날 시세를 backtest/data/daily/YYYYMMDD.csv.gz 로 저장한다.
- 한 번 호출에 그날 전 종목이 온다 (하루 = 시장 2개 = 호출 2번)
- 그날 상장돼 있던 종목이 그대로 들어 있어서 상장폐지된 종목도 포함된다 (생존편향 줄이기)
- 휴장일(두 시장 모두 0건)은 data/holidays.txt 에 적어 두고 다시 묻지 않는다
- 이미 받은 날짜는 건너뛰므로 중간에 멈춰도 다시 실행하면 이어 받는다
- 인증키 KRX_API_KEY 는 .env 에서 읽고, 화면이나 로그에 출력하지 않는다
- 공식 API라도 요청 사이에 간격을 두고, 한 번 실행에 MAX_CALLS 번을 넘기면 멈춘다
- 실패를 조용히 넘기지 않는다: 오류가 나면 멈추고, 끝에 받은 날짜 수와 빠진 날짜를 출력한다

시장 필터용 코스피 지수 대신, 코스피 종목의 시가총액 가중 등락률을 이어 붙인 근사 지수를
data/kospi_index.csv 로 만든다 (지수 API는 별도 서비스 신청이 필요해서).
"""
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
KEY = (dotenv_values(ROOT / '.env').get('KRX_API_KEY') or '').strip()

DATA = Path(__file__).resolve().parent / 'data'
DAILY = DATA / 'daily'
HOLIDAYS = DATA / 'holidays.txt'
BASE = 'https://data-dbg.krx.co.kr/svc/apis/sto/'
MARKETS = {'KOSPI': 'stk_bydd_trd', 'KOSDAQ': 'ksq_bydd_trd'}
FIELDS = {'TDD_OPNPRC': 'open', 'TDD_HGPRC': 'high', 'TDD_LWPRC': 'low', 'TDD_CLSPRC': 'close',
          'ACC_TRDVOL': 'volume', 'ACC_TRDVAL': 'value', 'FLUC_RT': 'rate', 'MKTCAP': 'mcap'}
GAP = 1.0           # 요청 사이 간격(초)
TIMEOUT = 90        # 서버 응답이 느릴 때가 있다
MAX_CALLS = 4000    # 한 번 실행의 호출 상한 (일일 한도 확인 전까지 보수적으로)

calls = 0


class FetchError(Exception):
    pass


def call(market, day):
    """한 시장의 하루치 전 종목. 일시적 오류만 세 번까지 다시 시도한다."""
    global calls
    for attempt in range(1, 4):
        if calls >= MAX_CALLS:
            raise FetchError(f'이번 실행의 호출 상한({MAX_CALLS})에 도달. 다시 실행하면 이어 받습니다.')
        calls += 1
        time.sleep(GAP)
        try:
            r = requests.get(BASE + MARKETS[market], params={'basDd': day},
                             headers={'AUTH_KEY': KEY}, timeout=TIMEOUT)
        except requests.RequestException as ex:
            print(f'  {day} {market} 연결 오류 ({type(ex).__name__}), {attempt}/3')
            time.sleep(5 * attempt)
            continue
        if r.status_code == 401:
            raise FetchError('401 Unauthorized: 인증키나 서비스 승인 상태를 확인하세요.')
        if r.status_code != 200:
            print(f'  {day} {market} HTTP {r.status_code}, {attempt}/3')
            time.sleep(5 * attempt)
            continue
        try:
            return r.json().get('OutBlock_1') or []
        except ValueError:
            raise FetchError(f'{day} {market}: JSON이 아닌 응답. 접속 제한일 수 있으니 멈춥니다.')
    raise FetchError(f'{day} {market}: 세 번 모두 실패')


def to_frame(rows, market):
    df = pd.DataFrame(rows)
    out = pd.DataFrame({'ticker': df['ISU_CD'], 'name': df['ISU_NM']})
    for src, dst in FIELDS.items():
        out[dst] = pd.to_numeric(df[src].str.replace(',', ''), errors='coerce')
    out['market'] = market
    return out


def build_index():
    """코스피 시가총액 가중 등락률로 근사 지수를 만든다 (전일 시가총액 = 오늘 시가총액 / (1+등락률))"""
    closes, level = {}, 100.0
    for f in sorted(DAILY.glob('*.csv.gz')):
        d = pd.read_csv(f, dtype={'ticker': str})
        d = d[(d['market'] == 'KOSPI') & (d['volume'] > 0)]
        prev_cap = d['mcap'] / (1 + d['rate'] / 100)
        level *= 1 + (prev_cap * d['rate'] / 100).sum() / prev_cap.sum()
        closes[pd.Timestamp(f.name[:8])] = level
    s = pd.Series(closes, name='close')
    s.index.name = 'date'
    s.to_frame().to_csv(DATA / 'kospi_index.csv')


def main(start, end):
    if not KEY:
        raise SystemExit('.env 에 KRX_API_KEY 가 없습니다.')
    DAILY.mkdir(parents=True, exist_ok=True)
    holidays = set(HOLIDAYS.read_text().split()) if HOLIDAYS.exists() else set()
    days = [d.strftime('%Y%m%d') for d in pd.bdate_range(start, end)]
    todo = [d for d in days if d not in holidays and not (DAILY / f'{d}.csv.gz').exists()]
    print(f'평일 {len(days)}일 중 받을 날 {len(todo)}일 (예상 호출 약 {len(todo) * 2}번)')

    t0, error = time.time(), None
    try:
        for i, day in enumerate(todo, 1):
            frames = {m: call(m, day) for m in MARKETS}
            sizes = {m: len(rows) for m, rows in frames.items()}
            if not any(sizes.values()):
                holidays.add(day)
                HOLIDAYS.write_text('\n'.join(sorted(holidays)))
                continue
            if not all(sizes.values()):
                raise FetchError(f'{day}: 한 시장만 비어 있음 {sizes}')
            out = pd.concat([to_frame(rows, m) for m, rows in frames.items()], ignore_index=True)
            out.to_csv(DAILY / f'{day}.csv.gz', index=False, compression='gzip')
            if i % 50 == 0:
                print(f'{i}/{len(todo)} {day} ({time.time() - t0:.0f}초, 호출 {calls}번)', flush=True)
    except FetchError as ex:
        error = ex

    got = sorted(f.name[:8] for f in DAILY.glob('*.csv.gz') if start <= f.name[:8] <= end)
    missing = [d for d in days if d not in holidays and d not in got]
    print(f'\n받은 거래일 {len(got)}일, 휴장일 {len([d for d in days if d in holidays])}일, '
          f'빠진 날 {len(missing)}일, 이번 호출 {calls}번')
    if missing:
        print('빠진 날 (앞 10개):', ', '.join(missing[:10]))
    if error:
        print('중단 :', error)
        sys.exit(1)
    build_index()
    print('완료 (근사 코스피 지수 data/kospi_index.csv 갱신)')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('사용법: python backtest/fetch_data.py 시작일(YYYYMMDD) 종료일(YYYYMMDD)')
    main(sys.argv[1], sys.argv[2])
