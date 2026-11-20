"""일봉 데이터 수집 (pykrx, KRX 로그인 필요)

    python backtest/fetch_data.py 20210104 20261118

날짜마다 KOSPI + KOSDAQ 전 종목의 그날 시세를 backtest/data/daily/YYYYMMDD.csv.gz 로 저장한다.
- 그날 상장돼 있던 종목이 그대로 들어 있어서 상장폐지된 종목도 포함된다 (생존편향 줄이기)
- 이미 받은 날짜는 건너뛰므로 중간에 끊겨도 다시 실행하면 이어 받는다
- KRX_ID, KRX_PW는 .env에서 읽는다 (값은 출력하지 않는다)
"""
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')

from pykrx import stock  # noqa: E402  (로그인 환경변수를 읽은 뒤에 불러온다)

DATA = Path(__file__).resolve().parent / 'data'
DAILY = DATA / 'daily'
COLS = ['open', 'high', 'low', 'close', 'volume', 'value', 'rate', 'mcap']


def main(start, end):
    DAILY.mkdir(parents=True, exist_ok=True)

    index = stock.get_index_ohlcv(start, end, '1001')      # 코스피 지수: 거래일 목록이자 시장 필터용
    index = index.iloc[:, :4]
    index.columns = ['open', 'high', 'low', 'close']
    index.index.name = 'date'
    index.to_csv(DATA / 'kospi_index.csv')
    days = [d.strftime('%Y%m%d') for d in index.index]
    print(f'거래일 {len(days)}일 ({days[0]} ~ {days[-1]})')

    t0 = time.time()
    for i, day in enumerate(days, 1):
        path = DAILY / f'{day}.csv.gz'
        if path.exists():
            continue
        frames = []
        for market in ('KOSPI', 'KOSDAQ'):
            for attempt in range(3):
                try:
                    df = stock.get_market_ohlcv_by_ticker(day, market=market)
                    break
                except Exception as ex:                      # 일시적 오류는 다시 시도
                    print(f'{day} {market} 실패 ({type(ex).__name__}), 다시 시도')
                    time.sleep(3)
            else:
                raise SystemExit(f'{day} {market} 수집 실패. 나중에 다시 실행하면 이어 받습니다.')
            df = df.iloc[:, :8]
            df.columns = COLS
            df['market'] = market
            frames.append(df)
        out = pd.concat(frames)
        out.index.name = 'ticker'
        out.to_csv(path, compression='gzip')
        if i % 50 == 0:
            print(f'{i}/{len(days)} {day} ({time.time() - t0:.0f}초)')
    print('완료')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('사용법: python backtest/fetch_data.py 시작일(YYYYMMDD) 종료일(YYYYMMDD)')
    main(sys.argv[1], sys.argv[2])
