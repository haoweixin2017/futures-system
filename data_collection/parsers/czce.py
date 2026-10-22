from .common import parse_html


def parse(text, config, trade_date, variety):
    return parse_html(text, config, trade_date, variety)
