"""직접 거래 감지 (한국투자증권 앱 등에서 모의계좌로 직접 사고판 것)

모의투자 서버의 체결 내역 조회는 합계만 주고 건별 내역을 주지 않아서, 계좌 잔고를 5분마다 비교한다.
- 비교 기준(스냅샷)은 장부(data/<book>/state.json)의 'manual' 에 저장해서 껐다 켜도 이어진다
- 처음에는 이 프로그램이 산 종목만 기준으로 삼는다 → 그 밖의 보유 종목은 직접 산 것으로 알린다
- 프로그램이 주문하기 직전에 한 번 확인하고(tick), 주문이 끝나면 기준을 새로 잡는다(rebase)
  → 프로그램 자신의 매매는 직접 거래로 알리지 않는다
- 매수 단가는 평균 매입가 변화로 계산하고, 매도 단가는 그때의 현재가로 추정한다
"""
from datetime import datetime

from trader import messages as MSG
from trader.kis_client import KISError


class ManualWatch:
    def __init__(self, client, broker, note):
        self.client, self.broker, self.note = client, broker, note
        self.paused = False                         # 프로그램이 주문하는 동안은 확인하지 않는다
        st = broker.state.setdefault('manual', {})
        if 'snapshot' not in st:                    # 처음: 프로그램이 산 종목만 기준
            st['snapshot'] = {c: {'qty': p['qty'], 'avg': p['avg_price'], 'name': p['name']}
                              for c, p in broker.positions.items()}
        today = datetime.now().strftime('%Y-%m-%d')
        if st.get('date') != today:
            st['date'], st['events'] = today, []
        broker.save()
        self.state = st

    def _held(self):
        return {c: {'qty': v['qty'], 'avg': v['avg_price'], 'name': v['name']}
                for c, v in self.client.get_balance().items()}

    def tick(self):
        """잔고를 기준과 비교해서 바뀐 종목을 알린다. 알린 건수를 돌려준다"""
        try:
            held = self._held()
        except KISError as ex:
            self.note.log(f'직접 거래 확인 실패 : {ex}')
            return 0
        snap, events = self.state['snapshot'], []
        for code in sorted(set(held) | set(snap)):
            old, new = snap.get(code, {'qty': 0, 'avg': 0}), held.get(code, {'qty': 0, 'avg': 0})
            delta = new['qty'] - old['qty']
            if not delta:
                continue
            name = new.get('name') or old.get('name') or code
            if delta > 0:
                price = (new['avg'] * new['qty'] - old['avg'] * old['qty']) / delta
                side = '매수'
            else:
                try:
                    price = self.client.get_price(code)['price']
                except KISError:
                    price = old['avg']
                side = '매도'
            events.append({'time': datetime.now().strftime('%H:%M'), 'side': side, 'code': code, 'name': name,
                           'qty': abs(delta), 'price': price, 'amount': abs(delta) * price,
                           'estimated': side == '매도'})
        self.state['snapshot'] = held
        if events:
            self.state['events'] += events
            self.broker.save()
            self.note.post(*MSG.manual_trades(events))
        else:
            self.broker.save()
        return len(events)

    def rebase(self):
        """프로그램이 주문한 뒤: 지금 잔고를 새 기준으로"""
        try:
            self.state['snapshot'] = self._held()
            self.broker.save()
        except KISError as ex:
            self.note.log(f'직접 거래 기준 갱신 실패 : {ex}')

    def summary(self):
        """장 마감 뒤: 오늘 직접 한 거래와, 프로그램이 관리하지 않는 보유 종목"""
        events = self.state.get('events', [])
        try:
            held = self.client.get_balance()
        except KISError as ex:
            self.note.log(f'직접 거래 정리 실패 : {ex}')
            return
        manual = {c: v for c, v in held.items() if c not in self.broker.positions}
        if not events and not manual:
            return
        prices = {}
        for c in manual:
            try:
                prices[c] = self.client.get_price(c)['price']
            except KISError:
                prices[c] = manual[c]['avg_price']
        self.note.post(*MSG.manual_summary(events, manual, prices))
