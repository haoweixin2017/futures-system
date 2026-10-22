"""交易状态同步持久化；版本校验防止多个进程覆盖同一账户。"""
import json
from functools import wraps
from sqlalchemy import update, func
from backend.models.database import TradingStateModel
from backend.storage.mysql_client import SessionLocal


def atomic(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            if self._transaction is None:
                return method(self, *args, **kwargs)
            with self._transaction():
                return method(self, *args, **kwargs)
    return wrapped


class TradingStateStore:
    def save_market(self, key, payload, version):
        # 估值 Tick 只传送可变字段；历史订单、成交和条件单保留在原 JSON 中。
        args = [TradingStateModel.payload]
        for field in ("account", "positions", "prices", "times", "risk"):
            args.extend(("$." + field, func.json_extract(json.dumps(payload[field], ensure_ascii=False), "$")))
        with SessionLocal() as session:
            result = session.execute(update(TradingStateModel).where(
                TradingStateModel.state_key == key, TradingStateModel.version == version
            ).values(payload=func.json_set(*args), version=version + 1))
            if result.rowcount != 1:
                raise RuntimeError("账户已被另一进程更新，请重新加载后再操作")
            session.commit()
        return version + 1

    def load(self, key):
        with SessionLocal() as session:
            row = session.get(TradingStateModel, key)
            return (json.loads(row.payload), row.version) if row else (None, 0)

    def keys(self, prefix):
        with SessionLocal() as session:
            return [row[0] for row in session.query(TradingStateModel.state_key)
                    .filter(TradingStateModel.state_key.startswith(prefix)).all()]

    def save(self, key, payload, version):
        encoded = json.dumps(payload, ensure_ascii=False)
        with SessionLocal() as session:
            if version == 0:
                session.add(TradingStateModel(state_key=key, payload=encoded, version=1))
            else:
                result = session.execute(update(TradingStateModel).where(
                    TradingStateModel.state_key == key, TradingStateModel.version == version
                ).values(payload=encoded, version=version + 1))
                if result.rowcount != 1:
                    raise RuntimeError("账户已被另一进程更新，请重新加载后再操作")
            session.commit()
        return version + 1
