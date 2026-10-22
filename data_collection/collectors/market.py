import json
from pathlib import Path

from data_collection.config import contract_identity
from data_collection.parsers.common import at, number
from .base import Collector


class Market(Collector):
    channel = 'market'

    def fetch(self, date, variety, log):
        source = self.settings['market']
        if self.settings['mode'] == 'fixture':
            return Path(source['fixture']).read_text(encoding='utf-8')
        return self.wire.request(source, self.context(date, variety), log)

    def parse(self, text, date, variety):
        source = self.settings['market']
        parser = source['parser']
        try:
            payload = at(json.loads(text), parser['records_path'])
            rows = [{k: raw[v] for k,v in parser['fields'].items()} for raw in payload]
            rows = [r for r in rows if r['variety'].upper() == variety and r['is_main'] == parser['main_value']]
            if len(rows) != 1 or rows[0]['trade_date'] != date:
                raise ValueError('MARKET_MAIN_OR_DATE_INVALID')
            row = rows[0]
            product, year, month = contract_identity(row['main_contract'], self.exchange, date)
            if product != variety: raise ValueError('MARKET_CONTRACT_MISMATCH')
            return [dict(row, exchange=self.exchange, variety=variety, contract_year=year, contract_month=month,
                         market_source='fixture:market' if self.settings['mode'] == 'fixture' else source['base_url'] + source['request']['path'],
                         **{k: number(row[k]) for k in ('settle', 'oi', 'volume')})]
        except (KeyError, TypeError):
            raise ValueError('MARKET_SCHEMA_INVALID') from None

    def validate(self, rows, date, variety, calendar):
        errors = ['NEGATIVE_MARKET_QUANTITY'] if any(r['oi'] < 0 or r['volume'] < 0 for r in rows) else []
        return rows, {'ok': not errors, 'errors': errors, 'checks': {'market': 'FAIL' if errors else 'PASS'}}

    def upsert(self, rows):
        self.store.upsert_market(rows)
