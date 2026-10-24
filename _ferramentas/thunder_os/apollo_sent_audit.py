"""Pure, advisory S27 audit of observed sent-email content; never rewrites.

audit_sent_email(event, proofs_text, forbidden_phrases=DEFAULT_FORBIDDEN_PHRASES)
accepts a flat mapping. ID aliases: original_id/id/message_id/event_id. Date:
sent_at/date/created_at (original string retained, never replaced with now).
Text: body_text/text/body/content; HTML fallback: body_html/html. Multiple text,
ID or date aliases must agree. Explicit body_complete=True or
content_complete=True is mandatory; a conflicting/missing flag, empty body or
snippet alone is incomplete. source is optional provenance, not trusted identity.
The caller owns provider identity, read freshness and sent-event selection.

Returns status=flagged|no_alerts|unverified, reason, alerts, original_id, date,
body_hash (SHA-256 of exact original UTF-8 text or HTML), body_format,
body_complete and provenance_complete. Each alert has code/match/start/end;
offsets refer to inspected text, rendered from HTML where necessary. Even partial
content can produce alerts; it can never return no_alerts. Missing proofs or
provenance also yield unverified. no_alerts means only no lexical rule matched,
never that a claim is true or the email compliant/approved.

Rules: U+2014 em dash, case-insensitive literal forbidden phrases, numeric tokens
absent from proofs (whole numeric token, retaining decimal/group/percent syntax).
Numbers written as words, entailment, date interpretation, unit equivalence and
factual verification require human review. HTML entities are decoded; script and
style content is ignored. No tools, models, writes, retries or provider actions.
Limits: 1 MB each for body/proofs, 100 phrases of at most 500 chars, 2000 alerts.
"""
import hashlib
from html.parser import HTMLParser
import re


DEFAULT_FORBIDDEN_PHRASES = ('resultado garantido', 'resultados garantidos')
_MAX_BYTES = 1_000_000
_NUMBER = re.compile(r'(?<!\w)[+-]?\d+(?:[.,]\d+)*(?:[ \u00a0\u202f]\d{3})*(?:[ \t]*%)?(?!\w)')


class _Invalid(ValueError):
    pass


def _text(value, limit=_MAX_BYTES):
    return (isinstance(value, str) and len(value) <= limit
        and not any(0xD800 <= ord(c) <= 0xDFFF for c in value)
        and len(value.encode('utf-8')) <= limit)


def _alias(event, keys):
    values = [event[key] for key in keys if event.get(key) is not None]
    if not values:
        return None
    if not all(isinstance(value, str) and value == values[0] for value in values):
        raise _Invalid('ambiguous_event')
    return values[0]


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden.append(tag)
        if not self.hidden and tag in ('p', 'div', 'br', 'li', 'td', 'tr'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
        if not self.hidden and tag in ('p', 'div', 'li', 'td', 'tr'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def audit_sent_email(event, proofs_text, forbidden_phrases=DEFAULT_FORBIDDEN_PHRASES):
    """Return deterministic alerts and original provenance, never a send verdict."""
    result = {'status': 'unverified', 'reason': 'event_unverified', 'alerts': [],
        'original_id': None, 'date': None, 'body_hash': None, 'body_format': None,
        'body_complete': False, 'provenance_complete': False}
    try:
        if not isinstance(event, dict):
            return result
        ident = _alias(event, ('original_id', 'id', 'message_id', 'event_id'))
        date = _alias(event, ('sent_at', 'date', 'created_at'))
        result.update(original_id=ident, date=date)
        result['provenance_complete'] = all(_text(value, 500) and bool(value.strip())
                                             for value in (ident, date))
        body = _alias(event, ('body_text', 'text', 'body', 'content'))
        kind = 'text'
        if body is None:
            body = _alias(event, ('body_html', 'html'))
            kind = 'html'
        if not _text(body):
            result['reason'] = 'body_unavailable'
            return result
        result.update(body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest(), body_format=kind)
        flags = [event[key] for key in ('body_complete', 'content_complete') if key in event]
        result['body_complete'] = bool(flags) and all(value is True for value in flags) and bool(body.strip())
        if not isinstance(forbidden_phrases, (list, tuple)) or len(forbidden_phrases) > 100 or not all(
                _text(phrase, 500) and phrase.strip() for phrase in forbidden_phrases):
            raise _Invalid('forbidden_phrases_invalid')
        proofs_valid = _text(proofs_text) and bool(proofs_text.strip())
        inspected = body
        if kind == 'html':
            parser = _VisibleText()
            parser.feed(body)
            parser.close()
            inspected = ''.join(parser.parts)
            result['body_complete'] &= bool(inspected.strip()) and not parser.hidden
        alerts = result['alerts']

        def add(code, match):
            if len(alerts) >= 2000:
                raise _Invalid('alert_limit')
            alerts.append({'code': code, 'match': match.group(),
                           'start': match.start(), 'end': match.end()})

        for match in re.finditer('\u2014', inspected):
            add('em_dash', match)
        for phrase in dict.fromkeys(forbidden_phrases):
            for match in re.finditer(re.escape(phrase), inspected, flags=re.IGNORECASE):
                add('forbidden_phrase', match)
        if proofs_valid:
            allowed = {match.group() for match in _NUMBER.finditer(proofs_text)}
            for match in _NUMBER.finditer(inspected):
                if match.group() not in allowed:
                    add('number_not_in_proofs', match)
        alerts.sort(key=lambda alert: (alert['start'], alert['end'], alert['code']))
        if not result['body_complete']:
            result['reason'] = 'body_incomplete'
        elif not result['provenance_complete']:
            result['reason'] = 'provenance_incomplete'
        elif not proofs_valid:
            result['reason'] = 'proofs_unavailable'
        else:
            result.update(status='flagged' if alerts else 'no_alerts',
                          reason='lexical_alerts' if alerts else 'no_lexical_alerts')
    except _Invalid as error:
        result.update(status='unverified', reason=str(error))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        result.update(status='unverified', reason='invalid_event')
    return result
