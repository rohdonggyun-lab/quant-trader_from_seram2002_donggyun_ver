"""한국투자증권 Open API 시세 조회 전용 클라이언트

가상 매매에는 시세만 필요하므로 주문, 잔고 같은 계좌 기능은 넣지 않았다.
계좌번호도 쓰지 않는다. (quant_실습\\ch8\\kis.py에서 시세 부분만 가져옴)

.env 값
    KIS_APP_KEY, KIS_APP_SECRET   모의투자 앱키, 시크릿
    KIS_TOKEN_FILE                접근 토큰 저장 파일 (선택)
        8장 자동매매와 같은 파일을 지정하면 토큰을 함께 쓴다.
        예: C:\\workspaces\\quant\\quant_실습\\ch8\\.kis_token_mock.json
"""
import os
import json
import time
from pathlib import Path

import requests

BASE_URL = 'https://openapivts.koreainvestment.com:29443'   # 모의투자 서버
INTERVAL = 1.0                                              # 호출 사이 최소 간격(초)
RATE_LIMIT_CODES = ('EGW00201', 'EGW00215')                 # 호출 제한: 잠시 뒤 재시도


class KISError(Exception):
    """API가 실패 응답을 돌려줬을 때"""


class KISClient:
    def __init__(self):
        self.app_key = self._env('KIS_APP_KEY')
        self.app_secret = self._env('KIS_APP_SECRET')
        default = Path(__file__).resolve().parent.parent / 'data' / '.kis_token_mock.json'
        self.token_file = Path(os.getenv('KIS_TOKEN_FILE') or default)
        self.session = requests.Session()           # 연결 재사용
        self._token = None
        self._token_expires = 0.0
        self._last_call = 0.0

    @staticmethod
    def _env(name):
        value = (os.getenv(name) or '').strip().strip('"').strip("'")
        if not value:
            raise KISError(f'.env 파일에 {name} 값이 없습니다.')
        return value

    # ------------------------------------------------------------ 토큰
    def _get_token(self):
        """접근 토큰. 만료 10분 전까지는 저장된 토큰을 다시 쓴다."""
        if self._token and self._token_expires > time.time() + 600:
            return self._token
        if self.token_file.exists():
            try:
                saved = json.loads(self.token_file.read_text(encoding='utf-8'))
                if saved.get('expires_at', 0) > time.time() + 600:
                    self._token = saved['access_token']
                    self._token_expires = saved['expires_at']
                    return self._token
            except (ValueError, KeyError, OSError):
                pass
        res = self.session.post(
            BASE_URL + '/oauth2/tokenP',
            json={'grant_type': 'client_credentials',
                  'appkey': self.app_key, 'appsecret': self.app_secret},
            timeout=10)
        data = self._json(res)
        if 'access_token' not in data:
            raise KISError('토큰 발급 실패: '
                           f"{data.get('error_code', res.status_code)} "
                           f"{data.get('error_description', '')}")
        self._token = data['access_token']
        self._token_expires = time.time() + int(data.get('expires_in', 86400))
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(json.dumps({
            'access_token': self._token, 'expires_at': self._token_expires,
        }), encoding='utf-8')
        return self._token

    # ------------------------------------------------------------ 공통 호출
    @staticmethod
    def _json(res):
        try:
            return res.json()
        except ValueError:
            raise KISError(f'응답을 해석할 수 없습니다 (HTTP {res.status_code})')

    def _get(self, path, tr_id, params):
        for attempt in range(4):
            gap = INTERVAL - (time.time() - self._last_call)
            if gap > 0:
                time.sleep(gap)
            headers = {
                'content-type': 'application/json; charset=utf-8',
                'authorization': 'Bearer ' + self._get_token(),
                'appkey': self.app_key, 'appsecret': self.app_secret,
                'tr_id': tr_id, 'custtype': 'P',
            }
            try:
                res = self.session.get(BASE_URL + path, headers=headers,
                                       params=params, timeout=10)
            finally:
                self._last_call = time.time()
            data = self._json(res)
            if data.get('rt_cd') == '0':
                return data
            if data.get('msg_cd') in RATE_LIMIT_CODES and attempt < 3:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise KISError(f"{data.get('msg_cd', res.status_code)} "
                           f"{data.get('msg1', '').strip()}")

    # ------------------------------------------------------------ 시세
    def get_price(self, code):
        """현재가 정보"""
        out = self._get('/uapi/domestic-stock/v1/quotations/inquire-price',
                        'FHKST01010100',
                        {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code})['output']
        return {
            'code': code,
            'price': int(out['stck_prpr']),
            'open': int(out['stck_oprc']),
            'high': int(out['stck_hgpr']),
            'low': int(out['stck_lwpr']),
            'rate': float(out['prdy_ctrt']),
        }

    def get_ohlc(self, code):
        """최근 약 30거래일 일별 시세 (최신 날짜가 맨 앞)"""
        data = self._get('/uapi/domestic-stock/v1/quotations/inquire-daily-price',
                         'FHKST01010400',
                         {'FID_COND_MRKT_DIV_CODE': 'J', 'FID_INPUT_ISCD': code,
                          'FID_PERIOD_DIV_CODE': 'D', 'FID_ORG_ADJ_PRC': '0'})
        rows = []
        for r in data.get('output', []):
            if not r.get('stck_bsop_date'):
                continue
            rows.append({
                'date': r['stck_bsop_date'],
                'open': int(r['stck_oprc']), 'high': int(r['stck_hgpr']),
                'low': int(r['stck_lwpr']), 'close': int(r['stck_clpr']),
            })
        rows.sort(key=lambda x: x['date'], reverse=True)
        return rows
