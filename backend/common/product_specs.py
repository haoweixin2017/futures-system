"""国内期货品种合约规格表:合约乘数与最小变动价位。
用于全市场合约同步时填充 instrument 规格,以及模拟盘保证金估算。
未收录品种使用默认值 (乘数10, 最小变动1)。
"""

from typing import Dict, Optional, Tuple

# product -> (volume_multiple, price_tick)
PRODUCT_SPECS: Dict[str, Tuple[int, float]] = {
    # 上期所 SHFE
    "CU": (5, 10), "AL": (5, 5), "ZN": (5, 5), "PB": (5, 1), "NI": (1, 10),
    "SN": (1, 10), "AU": (1000, 0.02), "AG": (15, 1), "RB": (10, 1),
    "WR": (10, 1), "HC": (10, 1), "SS": (10, 5), "FU": (10, 1), "BU": (10, 1),
    "RU": (10, 5), "SP": (10, 2), "AO": (20, 1), "BR": (5, 5),
    # 能源中心 INE
    "SC": (1000, 0.1), "LU": (10, 1), "NR": (10, 5), "BC": (5, 10), "EC": (50, 0.1),
    # 大商所 DCE
    "C": (10, 1), "CS": (10, 1), "A": (10, 1), "B": (10, 1), "M": (10, 1),
    "Y": (10, 2), "P": (10, 2), "FB": (10, 0.5), "BB": (500, 0.05), "L": (5, 1),
    "V": (5, 1), "PP": (5, 1), "J": (100, 0.5), "JM": (60, 0.5), "I": (100, 0.5),
    "EG": (10, 1), "EB": (5, 1), "PG": (20, 1), "RR": (10, 1), "JD": (10, 1),
    "LH": (16, 5),
    # 郑商所 CZCE
    "WH": (20, 1), "PM": (50, 1), "RI": (20, 1), "JR": (20, 1), "LR": (20, 1),
    "SR": (10, 1), "CF": (5, 5), "CY": (5, 5), "AP": (10, 1), "CJ": (5, 5),
    "OI": (10, 1), "RS": (10, 1), "RM": (10, 1), "TA": (5, 2), "MA": (10, 1),
    "FG": (20, 1), "SA": (20, 1), "PF": (5, 2), "UR": (20, 1), "SF": (5, 5),
    "SM": (5, 5), "PK": (5, 2), "PX": (5, 2), "SH": (30, 2), "PR": (15, 1),
    # 广期所 GFEX
    "SI": (5, 5), "LC": (1, 0.02), "PS": (3, 1),
    # 中金所 CFFEX
    "IF": (300, 0.2), "IH": (300, 0.2), "IC": (200, 0.2), "IM": (200, 0.2),
    "T": (10000, 0.005), "TF": (10000, 0.005), "TS": (10000, 0.005), "TL": (10000, 0.005),
}

# 主要品种归属交易所 (用于 TickData.exchange 兜底, 权威来源是全市场合约同步)
PRODUCT_EXCHANGE: Dict[str, str] = {}
_SHFE = ["CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "RB", "WR", "HC", "SS", "FU", "BU", "RU", "SP", "AO", "BR"]
_INE = ["SC", "LU", "NR", "BC", "EC"]
_DCE = ["C", "CS", "A", "B", "M", "Y", "P", "FB", "BB", "L", "V", "PP", "J", "JM", "I", "EG", "EB", "PG", "RR", "JD", "LH"]
_CZCE = ["WH", "PM", "RI", "JR", "LR", "SR", "CF", "CY", "AP", "CJ", "OI", "RS", "RM", "TA", "MA", "FG", "SA", "PF", "UR", "SF", "SM", "PK", "PX", "SH", "PR"]
_GFEX = ["SI", "LC", "PS"]
_CFFEX = ["IF", "IH", "IC", "IM", "T", "TF", "TS", "TL"]
for _p in _SHFE:
    PRODUCT_EXCHANGE[_p] = "SHFE"
for _p in _INE:
    PRODUCT_EXCHANGE[_p] = "INE"
for _p in _DCE:
    PRODUCT_EXCHANGE[_p] = "DCE"
for _p in _CZCE:
    PRODUCT_EXCHANGE[_p] = "CZCE"
for _p in _GFEX:
    PRODUCT_EXCHANGE[_p] = "GFEX"
for _p in _CFFEX:
    PRODUCT_EXCHANGE[_p] = "CFFEX"


def extract_product(symbol: str) -> str:
    """提取品种前缀: JM2701 -> JM, AP2610 -> AP, IC2609 -> IC"""
    letters = []
    for ch in symbol.upper():
        if ch.isalpha():
            letters.append(ch)
        else:
            break
    return "".join(letters)


def get_spec(symbol: str) -> Tuple[int, float]:
    """返回 (合约乘数, 最小变动价位), 未收录品种返回默认 (10, 1)"""
    product = extract_product(symbol)
    if product in PRODUCT_SPECS:
        return PRODUCT_SPECS[product]
    # 中金所国债品种 T/TF/TS/TL 前缀为单字母, 已在上面覆盖
    return (10, 1.0)


def get_exchange(symbol: str) -> str:
    product = extract_product(symbol)
    return PRODUCT_EXCHANGE.get(product, "")


def to_ctp_instrument(symbol: str) -> str:
    """把内部合约代码规范化为 CTP 接口要求的大小写。

    CTP 对合约代码**大小写敏感**, 且各交易所规则不同:
      - CZCE / CFFEX: 大写 (SR601 / IF2601)
      - SHFE / INE / DCE / GFEX: 小写 (fu2611 / sc2601 / jm2701)

    内部统一用大写存储, 凡是要传给 CTP 的地方 (报单 / 撤单 / **行情订阅**) 都必须
    经此转换。行情订阅若用错大小写不会报错, 只会「订阅成功但永远没有推送」——
    实测订阅 `FU2611` 无任何行情, 订阅 `fu2611` 立即收到, 属静默失败。
    """
    s = (symbol or "").strip()
    if not s:
        return s
    return s.upper() if get_exchange(s) in ("CZCE", "CFFEX") else s.lower()
