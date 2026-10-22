-- SQLite 3.24+，数量原值和吨值分列；只汇总TOTAL，不汇总市场/集团/明细。
CREATE TABLE IF NOT EXISTS receipt_market (
 trade_date TEXT NOT NULL, exchange TEXT NOT NULL, variety TEXT NOT NULL,
 detail_id TEXT NOT NULL, row_type TEXT NOT NULL CHECK(row_type IN ('TOTAL','GROUP','DETAIL','MARKET')),
 parent_id TEXT NOT NULL DEFAULT '', warehouse TEXT NOT NULL DEFAULT '',
 warehouse_type TEXT NOT NULL DEFAULT '', grade TEXT NOT NULL DEFAULT '',
 warr_yesterday REAL, warr_today REAL, warr_chg REAL, unit_raw TEXT, unit_ton TEXT,
 warr_yesterday_ton REAL, warr_today_ton REAL, warr_chg_ton REAL, conversion_basis TEXT,
 is_converted_warrant INTEGER, main_contract TEXT, contract_year INTEGER, contract_month INTEGER,
 settle REAL, oi REAL, volume REAL,
 source_channel TEXT, receipt_source TEXT, market_source TEXT, raw_record TEXT,
 receipt_loaded_at TEXT, market_loaded_at TEXT, loaded_at TEXT NOT NULL,
 PRIMARY KEY(trade_date,exchange,variety,detail_id)
);
CREATE TABLE IF NOT EXISTS collection_log (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL, exchange TEXT NOT NULL,
 variety TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL,
 attempt INTEGER NOT NULL DEFAULT 0, row_count INTEGER NOT NULL DEFAULT 0,
 details TEXT NOT NULL DEFAULT '{}', loaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_checkpoint ON collection_log(trade_date,exchange,variety,channel,status);
CREATE TABLE IF NOT EXISTS variety_mapping (
 exchange TEXT NOT NULL, variety TEXT NOT NULL, exchange_code TEXT NOT NULL,
 unit_raw TEXT NOT NULL, conversion_factor REAL, conversion_basis TEXT,
 PRIMARY KEY(exchange,variety)
);
