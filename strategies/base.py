"""전략의 기본 틀

새 전략은 이 클래스를 상속해서 세 메서드를 채우면 된다.
실행기(run_paper.py)가 정해진 시간표에 맞춰 이 메서드들을 부른다.
"""


class Strategy:
    name = '기본 전략'

    def prepare(self, client, code, today):
        """하루에 한 번, 종목별로 판단에 필요한 값을 계산해 딕셔너리로 돌려준다.

        예: 목표가, 이동평균. 계산할 수 없으면(오늘 시세 없음 등) None.
        client: KISClient (get_price, get_ohlc 사용 가능)
        today: 'YYYYMMDD'
        """
        raise NotImplementedError

    def should_buy(self, info, price):
        """매수 신호면 매수 이유(문자열), 아니면 None"""
        raise NotImplementedError

    def should_sell(self, info, position, price):
        """매도 신호면 매도 이유(문자열), 아니면 None

        position: {'qty', 'avg_price', 'cost', 'high', 'time'}
        EXIT_TIME의 일괄 매도는 실행기가 따로 처리하므로 여기서 다루지 않아도 된다.
        """
        return None

    def describe(self, info):
        """시작할 때 로그에 남길 종목별 설명"""
        return str(info)
