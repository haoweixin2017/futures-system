import logging
from decimal import Decimal
from backend.common.config import cfg
from backend.storage.mysql_client import SessionLocal
from backend.models.database import InstrumentModel

logger = logging.getLogger(__name__)

def sync_instruments_to_db():
    """
    启动时将 config/instruments.yaml 中配置的合约元数据同步到 MySQL instrument 表中。
    支持新增合约与参数更新（如价格变动单位、乘数）。
    """
    if not cfg.instruments:
        logger.info("No instruments found in configuration to sync.")
        return

    session = SessionLocal()
    synced_count = 0
    try:
        for inst in cfg.instruments:
            existing = session.query(InstrumentModel).filter_by(symbol=inst.symbol).first()
            price_tick_dec = Decimal(str(inst.price_tick))
            if existing:
                existing.exchange = inst.exchange
                existing.name = inst.name
                existing.price_tick = price_tick_dec
                existing.volume_multiple = inst.volume_multiple
            else:
                new_inst = InstrumentModel(
                    symbol=inst.symbol,
                    exchange=inst.exchange,
                    name=inst.name,
                    price_tick=price_tick_dec,
                    volume_multiple=inst.volume_multiple
                )
                session.add(new_inst)
            synced_count += 1
        session.commit()
        logger.info(f"Successfully synced {synced_count} instruments to MySQL instrument table.")
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to sync instruments to MySQL: {e}")
    finally:
        session.close()
