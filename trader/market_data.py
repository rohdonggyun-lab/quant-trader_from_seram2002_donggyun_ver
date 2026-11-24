"""매일 아침 KRX Open API에서 빠진 날의 시세를 받아 둔다 (backtest/fetch_data.py 의 함수를 그대로 쓴다)

KRX 데이터는 다음 날 아침에 올라온다. 두 시장 모두 0건인 날이
- 한국투자증권 일별 시세(삼성전자)에 있는 날이면: KRX에 아직 안 올라온 것 → NotReady (나중에 다시)
- 없는 날이면: 휴장일 → holidays.txt 에 적는다
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backtest'))
import fetch_data  # noqa: E402

PROBE = '005930'        # 휴장일 확인에 쓰는 종목 (삼성전자)


class NotReady(Exception):
    """KRX에 아직 올라오지 않은 날이 있음"""


def update(client, today=None):
    """어제까지 빠진 거래일을 받는다. 받은 날 목록을 돌려준다.

    FetchError (API 오류), NotReady (아직 안 올라옴)를 일으킬 수 있다.
    """
    today = today or date.today()
    if not fetch_data.KEY:
        raise fetch_data.FetchError('.env 에 KRX_API_KEY 가 없습니다.')
    fetch_data.DAILY.mkdir(parents=True, exist_ok=True)
    have = sorted(f.name[:8] for f in fetch_data.DAILY.glob('*.csv.gz'))
    if not have:
        raise fetch_data.FetchError('시세 파일이 없습니다. 먼저 backtest/fetch_data.py 로 과거 데이터를 받으세요.')
    holidays = set(fetch_data.HOLIDAYS.read_text().split()) if fetch_data.HOLIDAYS.exists() else set()
    start = pd.Timestamp(have[-1]) + timedelta(days=1)
    end = pd.Timestamp(today) - timedelta(days=1)
    todo = [d.strftime('%Y%m%d') for d in pd.bdate_range(start, end)]
    todo = [d for d in todo if d not in holidays]
    got, kis_days = [], None
    for day in todo:
        frames = {m: fetch_data.call(m, day) for m in fetch_data.MARKETS}
        sizes = {m: len(rows) for m, rows in frames.items()}
        if not any(sizes.values()):
            if kis_days is None:
                kis_days = {r['date'] for r in client.get_ohlc(PROBE)}
            if day in kis_days:
                raise NotReady(f'{day} 시세가 KRX에 아직 올라오지 않았습니다.')
            if not kis_days or day < min(kis_days):
                raise fetch_data.FetchError(f'{day}: 휴장일인지 확인할 수 없습니다 (한국투자증권 일별 시세 범위 밖). '
                                            f'backtest/fetch_data.py 로 직접 받아 주세요.')
            holidays.add(day)
            fetch_data.HOLIDAYS.write_text('\n'.join(sorted(holidays)))
            continue
        if not all(sizes.values()):
            raise fetch_data.FetchError(f'{day}: 한 시장만 비어 있음 {sizes}')
        out = pd.concat([fetch_data.to_frame(rows, m) for m, rows in frames.items()], ignore_index=True)
        out.to_csv(fetch_data.DAILY / f'{day}.csv.gz', index=False, compression='gzip')
        got.append(day)
    if got:
        fetch_data.build_index()
    return got
