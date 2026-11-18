# quant-trader — 실시간 가상 매매로 전략 시험하기

직접 만든 매매 전략을 **실제 계좌에 주문하지 않고** 실시간 시세로 시험하는
프로그램입니다. 시세만 한국투자증권 Open API로 받아 오고, 사고파는 것은
프로그램 안의 가상 장부에서만 처리합니다.

『파이썬 증권 데이터 분석』 실습 저장소(`quant_실습`)의 8장 자동매매에서
필요한 부분(시세 조회, 알림, 시간표)만 가져와 새로 만들었습니다. 실습
저장소와 모의투자 계좌는 건드리지 않습니다.

## 구조

```
quant-trader/
├── run_paper.py              # 실행기: 장중 감시, 가상 매수·매도, 알림
├── run_paper.bat             # 작업 스케줄러 등록용 배치 파일
├── config.py                 # 종목, 가상 자금, 비용, 시간표, 사용할 전략
├── trader/
│   ├── kis_client.py         # 한국투자증권 시세 조회 전용 (주문·계좌 기능 없음)
│   ├── paper_broker.py       # 가상 장부: 현금, 보유 종목, 체결, 손익 계산
│   └── notifier.py           # 화면 + 로그 파일 + 슬랙(#paper-trading)
├── strategies/
│   ├── base.py               # 전략의 기본 틀
│   └── breakout_trailing.py  # 예시 전략: 변동성 돌파 + 고점 대비 하락 매도 + 손절
├── data/                     # 실행하면 생김 (git 추적 제외)
│   ├── state.json            # 가상 장부 (현금, 보유 종목, 오늘 성과)
│   ├── trades.csv            # 가상 매매 기록 (엑셀로 열 수 있음)
│   └── logs/                 # 날짜별 로그
├── .env.example
├── .gitignore
└── requirements.txt
```

| 부품 | 하는 일 | 바꿀 때 |
|---|---|---|
| `strategies/` | 언제 사고 언제 팔지 판단 | 새 전략 파일을 만들고 `config.py`의 `STRATEGY`만 바꿈 |
| `config.py` | 종목, 금액, 비용, 시간 | 숫자만 바꿈 |
| `trader/` | 시세, 가상 체결, 알림 | 거의 그대로 사용 |
| `run_paper.py` | 하루의 흐름 | 시간표 구조를 바꿀 때만 |

## 준비

1. 가상환경은 실습과 같은 `quant.venv`를 사용 (필요한 라이브러리는 이미 설치됨)

   ```
   C:\workspaces\quant\quant.venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. 슬랙에 공개 채널 `#paper-trading` 생성 (봇은 `chat:write.public` 권한이 있어
   초대하지 않아도 됨)

3. `.env.example`을 복사해 `.env`를 만들고 값을 채움 (git에 올리지 않음)

   | 항목 | 내용 |
   |---|---|
   | `KIS_APP_KEY`, `KIS_APP_SECRET` | 한국투자증권 모의투자 앱키 (시세 조회만 하므로 계좌번호는 필요 없음) |
   | `KIS_TOKEN_FILE` | 접근 토큰 파일. 8장 자동매매와 같은 파일을 지정하면 토큰을 함께 씀 |
   | `SLACK_BOT_TOKEN` | 8장과 같은 슬랙 봇 토큰 |
   | `PAPER_SLACK_CHANNEL` | `#paper-trading` |

## 실행

```
cd C:\workspaces\quant\quant-trader
python run_paper.py --check
python run_paper.py
```

- `--check`: 지금 시점의 종목별 판단(목표가, 현재가, 매수 신호 여부)만
  출력합니다. 가상 매매는 하지 않고, 장이 끝난 뒤에도 실행할 수 있습니다.
  설정이 맞는지 먼저 확인하는 용도입니다.
- 옵션 없이 실행: 장중 가상 매매를 하고 15:20에 스스로 종료합니다.

| 시간 | 동작 |
|---|---|
| ~09:05 | 대기 |
| 09:05~15:15 | 종목을 돌아가며 감시. 보유 종목은 매도 판단, 나머지는 매수 판단 |
| 매시 30분 | 가상 잔고와 오늘 성과를 슬랙으로 알림 |
| 15:15 | 보유 종목 전부 가상 매도, 장 마감 정리 알림 |
| 15:20 | 종료 |

슬랙에는 모든 메시지 앞에 `[가상]`이 붙습니다. 가상 장부는 `data/state.json`에
저장되므로 다음 날 이어서 실행하면 전날 결과에서 이어집니다. 처음부터 다시
시작하려면 `data/state.json`과 `data/trades.csv`를 지웁니다.

## 가상 체결 방식

| 항목 | 계산 | 설정 |
|---|---|---|
| 매수 체결가 | 신호 순간의 현재가 × (1 + 0.05%) | `SLIPPAGE` |
| 매도 체결가 | 현재가 × (1 - 0.05%) | `SLIPPAGE` |
| 수수료 | 매수·매도 금액의 0.015% | `FEE_RATE` |
| 매도 세금 | 일반 주식만 0.20%, ETF는 0 | `STOCK_SELL_TAX` |
| 매수 수량 | 종목당 예산(시작 시점 총자산 × 24%) 안에서 최대 | `BUDGET_PER_POSITION` |

수수료와 세율은 실전 전에 실제 값을 확인해야 합니다. 호가 물량, 일부 체결,
미체결 같은 상황은 재현하지 않으므로 실제 주문보다 결과가 조금 좋게 나옵니다.
손익은 잔고 화면 값이 아니라 매입가와 실시간 현재가로 직접 계산합니다.

## 예시 전략: `breakout_trailing.py`

8장 자동매매와 같은 매수 규칙에 장중 매도 기준을 더한 전략입니다. 같은 날
8장(시간 매도만)과 결과를 비교해 보기 위한 출발점이며, 숫자는 검증된 값이
아닙니다.

- 매수: 현재가 > 오늘 시가 + 어제 변동폭 × 0.5, 그리고 5일·10일 이동평균 위
- 매도: 매수 후 최고가에서 1.5% 하락, 또는 매입가에서 2% 하락, 또는 15:15

## 새 전략 만들기

`strategies/` 폴더에 파일을 만들고 `Strategy`를 상속해 세 메서드를 채웁니다.

```python
from strategies.base import Strategy

class MyStrategy(Strategy):
    name = '내 전략'

    def prepare(self, client, code, today):
        # 하루 한 번: 판단에 필요한 값 계산 (client.get_ohlc, client.get_price 사용 가능)
        return {...}            # 계산 못 하면 None

    def should_buy(self, info, price):
        return '매수 이유' if ... else None

    def should_sell(self, info, position, price):
        # position: qty, avg_price, cost, high(매수 후 최고가), time
        return '매도 이유' if ... else None
```

그다음 `config.py`에서 `STRATEGY = 'my_strategy.MyStrategy'`로 바꿉니다.

## 8장 자동매매와 같이 돌릴 때

- 주문과 잔고 조회를 하지 않으므로 모의투자 계좌의 종목, 현금과 겹치지 않음
- 같은 API 키라 초당 호출 제한은 나눠 씀. 그래서 한 바퀴마다 5초 쉬도록 했고
  (`LOOP_SLEEP`), 호출 제한 오류가 나면 자동으로 다시 시도함
- 중복 실행 방지 포트가 다름 (8장 47284, 이 프로그램 47285)

## 한계

- 가상 체결은 실제 체결과 다름 (위 "가상 체결 방식")
- 조회 간격 때문에 장중 가격 변화를 몇 초 단위로만 봄. 순간적인 급등락은 놓칠 수 있음
- 시세 조회는 모의투자 서버를 사용
- 이 저장소의 전략은 시험용이며, 수익이 난다고 검증한 것이 아님
