"""国内期货历史数据自动拉取与导入脚本 (CLI 入口)。

核心实现已迁移至 backend/storage/history_backfill.py, 本脚本保留命令行用法:
    python scripts/import_history.py                 # 回补配置文件关注合约的全部周期
    python scripts/import_history.py JM2701 FU2610   # 指定合约
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import logging

from backend.common.config import cfg
from backend.storage import history_backfill

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("HistoryImporter")


def main():
    symbols = sys.argv[1:] or [inst.symbol for inst in cfg.instruments]
    logger.info("开始回补历史K线: %s (周期: 1m/5m/15m/30m/60m/1d)", symbols)
    for summary in history_backfill.backfill_watchlist(symbols):
        logger.info("完成 %s: %s", summary["symbol"], summary.get("periods"))


if __name__ == "__main__":
    main()
