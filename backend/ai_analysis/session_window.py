"""盘中画线的结果可用窗口；证据截点保持冻结，盘前窗口保持原样。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))
WINDOW_POLICY = 'RESULT_AVAILABLE_10M'


def wall_time(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return value.astimezone(BEIJING).replace(tzinfo=None) if value.tzinfo else value


def completion_time(value):
    """数据库 completed_at 的无时区时间为 UTC。"""
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(BEIJING)


def chart_for_completion(chart, completed_at):
    result = deepcopy(chart)
    if not isinstance(chart, dict) or chart.get('kind') == 'PRE_OPEN' or not completed_at:
        return result
    try:
        start = completion_time(completed_at)
        session_end = wall_time(chart.get('session_end_time') or chart['forecast_end_time']).replace(tzinfo=BEIJING)
        cutoff = chart.get('evidence_cutoff_time') or chart['forecast_start_time']
        wall_time(cutoff)
    except (KeyError, TypeError, ValueError, OverflowError):
        return result
    result.update(window_policy=WINDOW_POLICY, horizon_minutes=10,
                  evidence_cutoff_time=cutoff,
                  session_end_time=chart.get('session_end_time') or chart['forecast_end_time'],
                  forecast_start_time=start.isoformat(),
                  forecast_end_time=min(start+timedelta(minutes=10),session_end).isoformat())
    return result
