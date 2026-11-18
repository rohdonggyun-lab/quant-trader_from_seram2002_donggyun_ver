"""종목 이름으로 ETF·ETN 여부를 가린다 (매도 세금 계산, 종목 범위 필터에 사용)

한국투자증권 거래량순위 API에서 보통주만 골라도 ETF가 섞여 나와서,
이름에 들어 있는 운용사 브랜드와 상품 표시로 구분한다.
"""
ETF_BRANDS = ('KODEX', 'TIGER', 'KBSTAR', 'RISE', 'ACE', 'SOL', 'HANARO', 'KOSEF',
              'ARIRANG', 'PLUS', 'TIMEFOLIO', 'KIWOOM', 'WON', 'BNK', 'FOCUS', 'TREX',
              'UNICORN', 'ITF', 'KCGI', 'DAISHIN343', '마이다스', '파워', '히어로즈',
              '1Q', 'TRUSTON', 'VITA', 'HK', 'KOACT', 'BITO', 'DB')
ETF_WORDS = ('ETN', '레버리지', '인버스', '선물', '2X', '3X')


def is_etf(name):
    """ETF, ETN, 레버리지, 인버스 상품이면 True"""
    n = (name or '').upper().strip()
    return n.split(' ')[0] in ETF_BRANDS or any(w in n for w in ETF_WORDS)


def kind_of(name):
    return 'etf' if is_etf(name) else 'stock'
