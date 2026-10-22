from data_collection.parsers.common import at
from data_collection.parsers.dce import parse
from .base import Collector


class DCEReceipt(Collector):
    def __init__(self, *args):
        super().__init__(*args)
        self.token = None

    def fetch(self, date, variety, log):
        if self.settings['mode'] in ('fixture', 'html'):
            return super().fetch(date, variety, log)
        context = self.context(date, variety)
        if self.settings.get('auth'):
            import json
            if self.token is None:
                auth = dict(self.settings, request=self.settings['auth']['request'])
                payload = json.loads(self.wire.request(auth, context, log))
                rules = self.settings['auth']
                if any(at(payload, path) != expected for path, expected in rules['response_checks'].items()):
                    raise ValueError('AUTH_BUSINESS_ERROR')
                try: self.token = at(payload, self.settings['auth']['token_path'])
                except (KeyError, TypeError): raise ValueError('AUTH_TOKEN_MISSING') from None
                if not isinstance(self.token, str) or not self.token: raise ValueError('AUTH_TOKEN_INVALID')
            context['token'] = self.token
        return self.wire.request(self.settings, context, log)

    def parse(self, text, date, variety):
        return self.provenance(parse(text, self.settings, date, variety))
