from abc import ABC, abstractmethod
from pathlib import Path

from data_collection.validators import normalize_units, validate


class Collector(ABC):
    channel = 'receipt'

    def __init__(self, exchange, settings, config, store, wire):
        self.exchange, self.settings, self.config = exchange, settings, config
        self.store, self.wire = store, wire

    def context(self, date, variety):
        code = self.settings['mapping'][variety]['exchange_code']
        return {'date': date, 'date_compact': date.replace('-', ''), 'product': code, 'product_lower': code.lower()}

    def fetch(self, date, variety, log):
        if self.settings['mode'] == 'fixture':
            return Path(self.settings['fixture']).read_text(encoding='utf-8')
        return self.wire.request(self.settings, self.context(date, variety), log)

    @abstractmethod
    def parse(self, text, date, variety):
        pass

    def validate(self, rows, date, variety, calendar):
        previous_date = calendar.previous(date)
        previous = self.store.total(previous_date, self.exchange, variety) if previous_date else None
        reference = None
        path = self.config['validation'].get('reference_file')
        if path:
            import json
            data = json.loads(Path(path).read_text(encoding='utf-8'))
            matches = [r for r in data if r['trade_date'] == date and r['exchange'] == self.exchange and r['variety'] == variety]
            if len(matches) > 1: raise ValueError('REFERENCE_DUPLICATE')
            if matches:
                reference = matches[0]
                if not reference.get('source'): raise ValueError('REFERENCE_SOURCE_MISSING')
        rows = normalize_units(rows, self.settings['mapping'])
        result = validate(rows, self.config['validation'], previous, reference,
                          self.store.history(date, self.exchange, variety, rows[0]['unit_raw']))
        return rows, result

    def upsert(self, rows):
        self.store.upsert_receipts(rows)

    def provenance(self, rows):
        source = ('fixture:' + self.exchange if self.settings['mode'] == 'fixture'
                  else self.settings['base_url'].rstrip('/') + '/' + self.settings['request']['path'].lstrip('/'))
        return [dict(r, exchange=self.exchange, source_channel='receipt', receipt_source=source) for r in rows]
