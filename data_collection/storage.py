import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


def stamp():
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


class Store:
    def __init__(self, path):
        if str(path) != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript((Path(__file__).parent / 'db/schema.sql').read_text(encoding='utf-8'))
        self.columns = {r['name'] for r in self.db.execute('PRAGMA table_info(receipt_market)')}

    def __enter__(self): return self
    def __exit__(self, *args): self.db.close()

    def _upsert(self, row):
        fields = [key for key in row if key in self.columns]
        # Column names come only from the local schema, never remote input.
        updates = ','.join(f'{f}=excluded.{f}' for f in fields if f not in ('trade_date', 'exchange', 'variety', 'detail_id'))
        self.db.execute(f"INSERT INTO receipt_market ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)}) "
                        f'ON CONFLICT(trade_date,exchange,variety,detail_id) DO UPDATE SET {updates}', [row[f] for f in fields])

    def upsert_receipts(self, rows):
        with self.db:
            keys = {(r['trade_date'], r['exchange'], r['variety']) for r in rows}
            for key in keys:
                # A corrected same-day report replaces the complete receipt snapshot,
                # removing withdrawn details but preserving the independent market row.
                self.db.execute("DELETE FROM receipt_market WHERE trade_date=? AND exchange=? AND variety=? AND row_type!='MARKET'", key)
            for source in rows:
                row = dict(source, loaded_at=stamp(), receipt_loaded_at=stamp())
                market = self.db.execute("SELECT * FROM receipt_market WHERE trade_date=? AND exchange=? AND variety=? AND row_type='MARKET'",
                                         (row['trade_date'], row['exchange'], row['variety'])).fetchone()
                if market:
                    row.update({k: market[k] for k in ('main_contract', 'contract_year', 'contract_month', 'settle', 'oi', 'volume', 'market_source', 'market_loaded_at')})
                self._upsert(row)

    def upsert_market(self, rows):
        with self.db:
            for row in rows:
                now = stamp()
                self._upsert(dict(row, detail_id='__MARKET__', row_type='MARKET', source_channel='market', loaded_at=now, market_loaded_at=now))
                fields = [f for f in ('main_contract', 'contract_year', 'contract_month', 'settle', 'oi', 'volume', 'market_source') if f in row]
                self.db.execute('UPDATE receipt_market SET ' + ','.join(f'{f}=?' for f in fields) + ',market_loaded_at=?,loaded_at=? WHERE trade_date=? AND exchange=? AND variety=?',
                                [row[f] for f in fields] + [now, now, row['trade_date'], row['exchange'], row['variety']])

    def rows(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM receipt_market ORDER BY trade_date,exchange,variety,detail_id')]

    def log(self, date, exchange, variety, channel, status, attempt=0, row_count=0, details=None):
        with self.db:
            self.db.execute('INSERT INTO collection_log(trade_date,exchange,variety,channel,status,attempt,row_count,details,loaded_at) VALUES(?,?,?,?,?,?,?,?,?)',
                            (date, exchange, variety, channel, status, attempt, row_count, json.dumps(details or {}, ensure_ascii=False), stamp()))

    def logs(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM collection_log ORDER BY id')]

    def complete(self, date, exchange, variety, channel):
        row = self.db.execute('SELECT status FROM collection_log WHERE trade_date=? AND exchange=? AND variety=? AND channel=? AND status IN (\'SUCCESS\',\'FAILED\',\'REJECTED\') ORDER BY id DESC LIMIT 1',
                              (date, exchange, variety, channel)).fetchone()
        return row is not None and row['status'] == 'SUCCESS'

    def total(self, date, exchange, variety):
        row = self.db.execute("SELECT * FROM receipt_market WHERE trade_date=? AND exchange=? AND variety=? AND row_type='TOTAL'", (date, exchange, variety)).fetchone()
        return dict(row) if row else None

    def history(self, date, exchange, variety, unit):
        return [r[0] for r in self.db.execute("SELECT warr_today FROM receipt_market WHERE trade_date<? AND exchange=? AND variety=? AND row_type='TOTAL' AND unit_raw=? ORDER BY trade_date DESC LIMIT 60", (date, exchange, variety, unit))]

    def mappings(self, exchanges):
        with self.db:
            for exchange, config in exchanges.items():
                for variety, mapping in config['mapping'].items():
                    self.db.execute('INSERT INTO variety_mapping VALUES(?,?,?,?,?,?) ON CONFLICT(exchange,variety) DO UPDATE SET exchange_code=excluded.exchange_code,unit_raw=excluded.unit_raw,conversion_factor=excluded.conversion_factor,conversion_basis=excluded.conversion_basis',
                                    (exchange, variety, mapping['exchange_code'], mapping['unit_raw'], mapping.get('conversion_factor'), mapping.get('conversion_basis')))
