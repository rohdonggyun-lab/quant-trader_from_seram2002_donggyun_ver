"""코스피 시리즈 지수 일별 시세 수집 (KRX Open API, 공식 경로)

    python backtest/fetch_index.py

backtest/data/daily/ 에 있는 거래일마다 코스피 시리즈 지수(코스피, 코스피 200, 업종지수 등) 전체를
backtest/data/index/YYYYMMDD.csv.gz 로 저장한다. 한 번 호출에 그날 전체 지수가 온다.
- 이미 받은 날은 건너뛰므로 중간에 멈춰도 다시 실행하면 이어 받는다
- 인증키는 .env 의 KRX_API_KEY (화면에 출력하지 않음), 요청 사이 간격과 호출 상한은 fetch_data.py 와 같다
- 실패를 조용히 넘기지 않는다: 오류가 나면 멈추고 받은 날 수와 빠진 날을 출력한다
- 끝나면 실제 코스피 종가를 backtest/data/kospi.csv (date, close) 로 모은다 (시장 필터와 시장 상황에 쓴다)
"""
import sys
import time
from pathlib import Path

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_data as F  # noqa: E402

URL = 'https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd'
KOSPI = '코스피'
OUT = F.DATA / 'index'
FIELDS = {'OPNPRC_IDX': 'open', 'HGPRC_IDX': 'high', 'LWPRC_IDX': 'low', 'CLSPRC_IDX': 'close',
          'FLUC_RT': 'rate', 'ACC_TRDVAL': 'value', 'MKTCAP': 'mcap'}


def call(day):
    for attempt in range(1, 4):
        if F.calls >= F.MAX_CALLS:
            raise F.FetchError(f'이번 실행의 호출 상한({F.MAX_CALLS})에 도달. 다시 실행하면 이어 받습니다.')
        F.calls += 1
        time.sleep(F.GAP)
        try:
            r = requests.get(URL, params={'basDd': day}, headers={'AUTH_KEY': F.KEY}, timeout=F.TIMEOUT)
        except requests.RequestException as ex:
            print(f'  {day} 연결 오류 ({type(ex).__name__}), {attempt}/3')
            time.sleep(5 * attempt)
            continue
        if r.status_code == 401:
            raise F.FetchError('401 Unauthorized: 인증키나 지수 서비스 승인 상태를 확인하세요.')
        if r.status_code != 200:
            print(f'  {day} HTTP {r.status_code}, {attempt}/3')
            time.sleep(5 * attempt)
            continue
        try:
            return r.json().get('OutBlock_1') or []
        except ValueError:
            raise F.FetchError(f'{day}: JSON이 아닌 응답. 접속 제한일 수 있으니 멈춥니다.')
    raise F.FetchError(f'{day}: 세 번 모두 실패')


def to_frame(rows):
    df = pd.DataFrame(rows)
    out = pd.DataFrame({'group': df['IDX_CLSS'], 'name': df['IDX_NM']})
    for src, dst in FIELDS.items():
        out[dst] = pd.to_numeric(df[src].astype(str).str.replace(',', ''), errors='coerce')
    return out


def build_kospi():
    """받아 둔 지수 파일에서 '코스피' 종가만 모아 data/kospi.csv 로 저장한다"""
    vals = {}
    for f in sorted(OUT.glob('*.csv.gz')):
        d = pd.read_csv(f, usecols=['name', 'close'])
        row = d.loc[d['name'] == KOSPI, 'close']
        if len(row):
            vals[pd.Timestamp(f.name[:8])] = float(row.iloc[0])
    s = pd.Series(vals, name='close')
    s.index.name = 'date'
    s.to_frame().to_csv(F.DATA / 'kospi.csv')
    return s


def main():
    if not F.KEY:
        raise SystemExit('.env 에 KRX_API_KEY 가 없습니다.')
    OUT.mkdir(parents=True, exist_ok=True)
    days = sorted(f.name[:8] for f in F.DAILY.glob('*.csv.gz'))
    todo = [d for d in days if not (OUT / f'{d}.csv.gz').exists()]
    print(f'거래일 {len(days)}일 중 받을 날 {len(todo)}일')
    t0, error = time.time(), None
    try:
        for i, day in enumerate(todo, 1):
            rows = call(day)
            if not rows:
                raise F.FetchError(f'{day}: 지수가 0건 (거래일인데 비어 있음)')
            to_frame(rows).to_csv(OUT / f'{day}.csv.gz', index=False, compression='gzip')
            if i % 100 == 0:
                print(f'{i}/{len(todo)} {day} ({time.time() - t0:.0f}초)', flush=True)
    except F.FetchError as ex:
        error = ex
    got = {f.name[:8] for f in OUT.glob('*.csv.gz')}
    missing = [d for d in days if d not in got]
    print(f'\n받은 날 {len(got)}일, 빠진 날 {len(missing)}일, 이번 호출 {F.calls}번')
    if missing:
        print('빠진 날 (앞 10개):', ', '.join(missing[:10]))
    if error:
        print('중단 :', error)
        sys.exit(1)
    build_kospi()
    print('완료 (실제 코스피 data/kospi.csv 갱신)')


if __name__ == '__main__':
    main()
