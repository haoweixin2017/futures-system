from data_collection.parsers.czce import parse
from .base import Collector


class CZCEReceipt(Collector):
    def parse(self, text, date, variety):
        return self.provenance(parse(text, self.settings, date, variety))
