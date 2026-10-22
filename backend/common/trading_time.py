from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple

class FuturesTradingCalendar:
    """
    国内期货交易时段与交易日计算器：
    支持夜盘归属判定、盘中交易时段判定、休市跨度检测。
    """
    # 典型国内商品夜盘时段 (21:00 ~ 23:00)
    NIGHT_START = time(21, 0, 0)
    NIGHT_END = time(23, 0, 0)

    # 2026 年国务院节假日安排；期货交易所法定假期及调休周末均休市。
    # https://www.gov.cn/zhengce/content/202511/content_7047090.htm
    HOLIDAY_RANGES_2026 = (
        (date(2026, 1, 1), date(2026, 1, 3)),
        (date(2026, 2, 15), date(2026, 2, 23)),
        (date(2026, 4, 4), date(2026, 4, 6)),
        (date(2026, 5, 1), date(2026, 5, 5)),
        (date(2026, 6, 19), date(2026, 6, 21)),
        (date(2026, 9, 25), date(2026, 9, 27)),
        (date(2026, 10, 1), date(2026, 10, 7)),
    )

    @classmethod
    def is_trading_day(cls, day: date) -> bool:
        return day.weekday() < 5 and not any(start <= day <= end
                                              for start, end in cls.HOLIDAY_RANGES_2026)

    @classmethod
    def previous_trading_day(cls, day: date) -> date:
        day -= timedelta(days=1)
        while not cls.is_trading_day(day):
            day -= timedelta(days=1)
        return day

    @classmethod
    def _night_session_day(cls, day: date) -> bool:
        if not cls.is_trading_day(day):
            return False
        next_weekday = day + timedelta(days=1)
        while next_weekday.weekday() >= 5:
            next_weekday += timedelta(days=1)
        return cls.is_trading_day(next_weekday)

    # 白盘时段
    MORNING_1_START = time(9, 0, 0)
    MORNING_1_END = time(10, 15, 0)

    MORNING_2_START = time(10, 30, 0)
    MORNING_2_END = time(11, 30, 0)

    AFTERNOON_START = time(13, 30, 0)
    AFTERNOON_END = time(15, 0, 0)

    NIGHT_SESSION_PRODUCTS = {
        "CU", "AL", "ZN", "PB", "NI", "SN", "AU", "AG", "RB", "HC", "SS",
        "FU", "BU", "RU", "SP", "AO", "BR", "SC", "LU", "NR", "BC",
        "A", "B", "C", "CS", "M", "Y", "P", "L", "V", "PP", "J", "JM",
        "I", "EG", "EB", "PG", "SR", "CF", "CY", "OI", "RM", "TA", "MA",
        "FG", "SA", "PF", "UR", "PX", "SH", "PR",
    }

    @classmethod
    def has_night_session(cls, instrument: str) -> bool:
        from backend.common.product_specs import extract_product
        return extract_product(instrument) in cls.NIGHT_SESSION_PRODUCTS

    @classmethod
    def today_analysis_available(cls, dt: datetime, instrument: str,
                                 market_trading_day: str | None = None) -> bool:
        """日盘结束后关闭今日分析；下一交易日夜盘开盘后重新开放。"""
        from backend.common.product_specs import extract_product, get_exchange

        weekday = dt.weekday()
        if not cls.is_trading_day(dt.date()):
            return False
        if market_trading_day and market_trading_day != cls.get_trading_day(dt):
            return False
        product = extract_product(instrument)
        exchange = get_exchange(instrument)
        day_start = time(9, 30) if exchange == "CFFEX" else time(9)
        day_end = time(15, 15) if product in {"T", "TF", "TS", "TL"} else time(15)
        current_time = dt.time()
        if current_time < day_start:
            return cls.has_night_session(instrument) and weekday > 0
        if current_time < day_end:
            return True
        return (cls.has_night_session(instrument) and weekday < 4
                and current_time >= cls.NIGHT_START
                and cls._night_session_day(dt.date()))

    @classmethod
    def next_open_target(cls, instrument: str, now: datetime | None = None,
                         trading_day: str | None = None) -> dict[str, str]:
        """冻结下一交易日首个实际开盘；节前无夜盘时使用节后日盘。"""
        from backend.common.product_specs import get_exchange
        now = now or datetime.now()
        day = datetime.strptime(trading_day or cls.get_trading_day(now), '%Y%m%d').date()
        while True:
            day += timedelta(days=1)
            if not cls.is_trading_day(day):
                continue
            previous = cls.previous_trading_day(day)
            night = cls.has_night_session(instrument) and cls._night_session_day(previous)
            start = datetime.combine(previous, time(21)) if night else datetime.combine(
                day, time(9, 30) if get_exchange(instrument) == 'CFFEX' else time(9))
            if start > now:
                return {'session': 'NIGHT' if night else 'DAY',
                        'label': '下一交易日夜盘' if night else '下一交易日日盘',
                        'open_time': start.strftime('%H:%M'), 'start_time': start.isoformat(),
                        'trading_day': day.strftime('%Y%m%d')}

    @classmethod
    def current_session_target(cls, dt: datetime, instrument: str) -> dict[str, str]:
        """返回当前连续交易小节；处于休市间隔时返回下一小节。"""
        options = cls.analysis_session_options(dt, instrument)
        return next(item for item in options if item["status"] != "PASSED")

    @classmethod
    def next_session_open_after(cls, dt: datetime, instrument: str) -> datetime:
        """返回指定小节结束后的下一次实际开盘时间。"""
        options = cls.analysis_session_options(dt + timedelta(seconds=1), instrument)
        return min(datetime.fromisoformat(item["start_time"]) for item in options
                   if datetime.fromisoformat(item["start_time"]) > dt)

    @classmethod
    def analysis_session_options(cls, dt: datetime, instrument: str) -> list[dict[str, str]]:
        """按合约生成当前交易周期的连续小节，并标记是否已经结束。"""
        from backend.common.product_specs import extract_product, get_exchange
        product = extract_product(instrument)
        exchange = get_exchange(instrument)
        current_date = dt.date()
        if exchange == "CFFEX":
            afternoon_end = time(15, 15) if product in {"T", "TF", "TS", "TL"} else time(15)
            day_parts = [(time(9, 30), time(11, 30), "日盘上午"),
                         (time(13), afternoon_end, "日盘下午")]
        else:
            day_parts = [(time(9), time(10, 15), "日盘第一节"),
                         (time(10, 30), time(11, 30), "日盘第二节"),
                         (time(13, 30), time(15), "日盘第三节")]
        last_day_end = day_parts[-1][1]

        def next_trading_day(value):
            while not cls.is_trading_day(value):
                value += timedelta(days=1)
            return value

        day_date = current_date if dt.time() < last_day_end else current_date + timedelta(days=1)
        day_date = next_trading_day(day_date)
        sessions = []
        if cls.has_night_session(instrument):
            night_date = cls.previous_trading_day(day_date)
            if cls._night_session_day(night_date):
                night_end = cls._night_end_for(product)
                night_end_date = (night_date + timedelta(days=1)
                                  if cls._night_crosses_midnight(product) else night_date)
                sessions.append((datetime.combine(night_date, cls.NIGHT_START),
                                 datetime.combine(night_end_date, night_end), "NIGHT", "夜盘"))
        for start_time, end_time, label in day_parts:
            sessions.append((datetime.combine(day_date, start_time),
                             datetime.combine(day_date, end_time), "DAY", label))

        result = []
        for start, end, session, label in sorted(sessions):
            status = "PASSED" if dt >= end else "ACTIVE" if start <= dt < end else "UPCOMING"
            result.append({
                "session": session, "label": label, "status": status,
                "start_time": start.isoformat(), "end_time": end.isoformat(),
            })
        return result

    @classmethod
    def session_end(cls, dt: datetime, instrument: str) -> datetime:
        from backend.common.product_specs import extract_product, get_exchange
        product = extract_product(instrument)
        if get_exchange(instrument) == "CFFEX":
            end = time(11, 30) if dt.time() <= time(11, 30) else time(15, 15) if product in {"T", "TF", "TS", "TL"} else time(15)
            return datetime.combine(dt.date(), end)
        if dt.hour >= 20 or dt.hour < 3:
            end = time(2, 30) if product in {"AU", "AG", "SC"} else time(1) if product in {"CU", "AL", "ZN", "PB", "NI", "SN", "SS", "BC", "AO"} else time(23)
            date = dt.date() + timedelta(days=1) if dt.hour >= 20 and end.hour < 3 else dt.date()
            return datetime.combine(date, end)
        end = time(10, 15) if dt.time() <= time(10, 15) else time(11, 30) if dt.time() <= time(11, 30) else time(15)
        return datetime.combine(dt.date(), end)

    @classmethod
    def is_trading_time(cls, dt: datetime) -> bool:
        wd = dt.weekday()
        t = dt.time()
        # 周日全天休市 (国内期货周日夜盘不存在, 夜盘从周一开始)
        if wd == 6:
            return False
        # 周六: 仅凌晨 00:00-02:30 属于周五夜盘尾段 (贵金属/原油等)
        if wd == 5:
            return t <= time(2, 30, 0) and cls._night_session_day(dt.date() - timedelta(days=1))
        if t < time(3, 0):
            return cls._night_session_day(dt.date() - timedelta(days=1))
        if not cls.is_trading_day(dt.date()):
            return False
        # 夜盘
        if cls.NIGHT_START <= t <= cls.NIGHT_END:
            return cls._night_session_day(dt.date())
        # 上午第一节
        if cls.MORNING_1_START <= t <= cls.MORNING_1_END:
            return True
        # 上午第二节
        if cls.MORNING_2_START <= t <= cls.MORNING_2_END:
            return True
        # 下午
        if cls.AFTERNOON_START <= t <= cls.AFTERNOON_END:
            return True
        return False

    @classmethod
    def is_trading_time_for(cls, dt: datetime, instrument: str) -> bool:
        """按具体合约的交易时段判定 (区分 CFFEX 与商品的日盘起止, 及各品种夜盘收盘)。

        商品: 09:00~10:15 / 10:30~11:30 / 13:30~15:00, 夜盘 21:00 起按品种至 23:00/01:00/02:30。
        CFFEX: 09:30~11:30 / 13:00~15:00 (股指), 国债 09:30~11:30 / 13:00~15:15, 无夜盘。
        """
        from backend.common.product_specs import extract_product, get_exchange
        wd = dt.weekday()
        t = dt.time()
        if wd == 6:
            return False
        product = extract_product(instrument)
        if get_exchange(instrument) == "CFFEX":
            if not cls.is_trading_day(dt.date()):
                return False
            if time(9, 30) <= t < time(11, 30):
                return True
            afternoon_end = time(15, 15) if product in {"T", "TF", "TS", "TL"} else time(15, 0)
            return time(13, 0) <= t < afternoon_end
        night_end = cls._night_end_for(product)
        crosses = cls._night_crosses_midnight(product)
        has_night = cls.has_night_session(instrument)
        # 商品: 夜盘尾段 (跨零点, 仅周五夜盘可延至周六凌晨)
        if t < time(3, 0, 0):
            return (has_night and crosses and t < night_end
                    and cls._night_session_day(dt.date() - timedelta(days=1)))
        if wd == 5:
            return False
        if not cls.is_trading_day(dt.date()):
            return False
        if t >= cls.NIGHT_START:
            # 未过零点的部分: 跨零点品种在 21:00~24:00 全程可交易
            return (has_night and cls._night_session_day(dt.date())
                    and (True if crosses else t < night_end))
        if time(9, 0) <= t < time(10, 15):
            return True
        if time(10, 30) <= t < time(11, 30):
            return True
        return time(13, 30) <= t < time(15, 0)

    @staticmethod
    def _night_end_for(product: str) -> time:
        if product in {"AU", "AG", "SC"}:
            return time(2, 30)
        if product in {"CU", "AL", "ZN", "PB", "NI", "SN", "SS", "BC", "AO"}:
            return time(1)
        return time(23)

    @staticmethod
    def _night_crosses_midnight(product: str) -> bool:
        return product in {"AU", "AG", "SC", "CU", "AL", "ZN", "PB", "NI",
                           "SN", "SS", "BC", "AO"}

    @classmethod
    def get_trading_day(cls, dt: datetime) -> str:
        """
        计算交易日：20:00 后归下一个实际交易日，休市日顺延。
        """
        day = dt.date() + timedelta(days=1) if dt.time() >= time(20) else dt.date()
        while not cls.is_trading_day(day):
            day += timedelta(days=1)
        return day.strftime("%Y%m%d")

    @classmethod
    def is_session_break(cls, dt_prev: datetime, dt_cur: datetime) -> bool:
        """
        检测两笔数据之间是否发生了跨休市（如 10:15~10:30 或 11:30~13:30 或 15:00~21:00）
        发生跨休市时，上一根 Bar 必须强制闭合切分，不得与休市后拼接。
        """
        delta_sec = (dt_cur - dt_prev).total_seconds()
        # 超过 5 分钟无数据且处于休市边界时判定为跨休市
        return delta_sec > 300
