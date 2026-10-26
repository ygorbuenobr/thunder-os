"""Read-only commercial view over an injected account reader.

Verified profile aliases are supplied by the caller from canonical CRM identity
evidence. Names, source IDs and document domains never establish this binding.
All content remains untrusted plain text. Originals preserve historical claims;
the primary sections are research, never current CRM or eligibility evidence.
"""
from collections import Counter
from collections.abc import Mapping
from copy import copy, deepcopy
from datetime import date
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit


SECTIONS = (('business', 'Empresa e mercado'), ('fit', 'Aderência'),
            ('moment', 'Momento'), ('people', 'Pessoas'), ('thesis', 'Tese e prova'),
            ('next_action', 'Próxima ação'))
_URL = re.compile(r'https?://[^\s<>|`\[\]"\']+')


def _collection_warning(value):
    """Only collection-wide discovery/coverage warnings belong to every row."""
    return isinstance(value, str) and value.split(':', 1)[0] in {
        'source_entry_limit', 'source_file_limit', 'source_total_bytes_limit',
        'source_directory_missing', 'source_directory_blocked', 'source_directory_unreadable',
        'visitor_index_missing_or_unreadable', 'visitor_index_invalid',
        'visitor_brief_missing_or_unreadable', 'account_sources_missing',
        'internal_repo_readonly_snapshot_required', 'internal_cadence_state_unavailable',
        'internal_cadence_state_limit_or_invalid'}


def _document_warning(value, doc):
    """Keep explicitly scoped catalog warnings only for the exact selected source."""
    return value in {
        'campaign_unassigned:' + str(doc['source'].get('path')),
        'account_source_missing:' + str(doc['source'].get('path')),
        'account_source_missing:' + str(doc.get('id')),
        'internal_campaign_conflict:' + str(doc.get('id'))}


def _fold(text):
    return ''.join(c for c in unicodedata.normalize('NFKD', text.casefold())
                   if not unicodedata.combining(c))


def _profile(value):
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value.strip())
        if (url.scheme not in ('https', 'http') or url.hostname not in ('linkedin.com', 'www.linkedin.com')
                or url.username or url.password or url.port
                or not re.fullmatch(r'/in/[A-Za-z0-9_%.-]+/?', url.path)):
            return None
        return 'https://www.linkedin.com' + url.path.rstrip('/').lower()
    except ValueError:
        return None


def _profiles(text):
    return {_profile(match.group().rstrip('.,;:)')) for match in _URL.finditer(text)} - {None}


def _primary_profiles(text):
    """Accept only an explicit primary block/metadata, or the H1 opening.

    Never search later research, other people, M7 or proof references. An
    explicit declaration takes precedence even when it contains no usable URL.
    Multiple distinct primary URLs remain ambiguous, rather than picking one.
    """
    lines, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            if fence is None:
                fence = marker[1][0]
            elif marker[1][0] == fence:
                fence = None
            continue
        if fence is None:
            lines.append(line)
    primary, explicit, active = set(), False, False
    first_section = next((i for i, line in enumerate(lines) if re.match(r'^\s*#{2,6}\s', line)), len(lines))
    for index, line in enumerate(lines):
        folded = _fold(line.strip())
        if re.fullmatch(r'\*\*pessoa abordada:\s*.+\*\*', folded):
            explicit, active = True, True
            primary.update(_profiles(line))
            continue
        if re.match(r'^\s*#{1,6}\s', line) or re.fullmatch(r'\s*\*\*[^*]+\*\*\s*', line):
            active = False
        if active:
            primary.update(_profiles(line))
        if index < first_section and re.match(
                r'^(?:-\s*)?(?:\*\*)?(?:linkedin_url|perfil linkedin primario|linkedin primario)(?:\*\*)?\s*:', folded):
            explicit = True
            primary.update(_profiles(line))
    if explicit:
        return primary
    opening = lines[:first_section]
    h1 = next((i for i, line in enumerate(opening) if re.match(r'^\s*#\s', line)), None)
    return _profiles('\n'.join(opening[h1:])) if h1 is not None else set()


def _historical_state(text):
    folded = _fold(text)
    return ('hubspot' in folded or 'crm' in folded or
            bool(re.search(r'e-?mail|mailbox|caixa de entrada', folded)))


# Deliberately bounded documentary status phrases, never an eligibility checker.
# Ordinary business descriptions (e.g. "vende para clientes") do not match.
_CURRENT_STATE = re.compile(
    r'\b(?:(?:is_verb|nao e|sao|era|eram|continua sendo) (?:um[a]? )?(?:cliente[s]?|concorrente[s]?|bebidas)'
    r'|(?:sem|nenhum|nao (?:ha|tem|possui|existem?)|tem|possui|ha|com) '
    r'(?:negocios? abertos?|exclusoes)'
    r'|(?:nao )?esta excluid[ao]'
    r'|(?:cliente|concorrente|exclusoes):\s*(?:sim|nao|ativo|inativo))\b')


def _state_fold(text):
    # Preserve the accented verb before accent folding: Portuguese conjunction
    # "e clientes" describes a market and must not become a client-status claim.
    return _fold(re.sub(r'\bé\b', 'is_verb', text, flags=re.IGNORECASE))


def _without_current_state(text):
    """Select literal sentence fragments; originals retain all removed claims."""
    if not _CURRENT_STATE.search(_state_fold(text)):
        return [text]
    return [sentence for sentence in re.split(r'(?<=[.!?;])\s+', text)
            if sentence.strip() and not _CURRENT_STATE.search(_state_fold(sentence))]


def _item(text, original=None, classification=None):
    original = original or {}
    published = original.get('published_on')
    try:
        observed = date.fromisoformat(published)
        today = date.today()
        cutoff = today.replace(year=today.year - 1, day=min(today.day, 28)) if (
            today.month == 2 and today.day == 29) else today.replace(year=today.year - 1)
        older = observed < cutoff
    except (ValueError, TypeError):
        published, older = None, False
    sources = original.get('sources')
    source_url = original.get('source_url')
    if source_url is None and isinstance(sources, list) and sources:
        source_url = sources[0]
    return {'text': text, 'classification': classification or original.get('classification') or ('fato' if original.get('label') == 'fato' else 'nao_classificado'),
            'source_url': source_url, 'published_on': published,
            'context_only': older or original.get('label') == 'contexto'}


def _documentary_items(tabs):
    """Resolve explicit citations inside this document only, without network or CRM inference."""
    citations, labels = {}, {}
    pattern = re.compile(r'(?:Fonte|Base):\s*([^,\n]+),\s*(\d{2}/\d{2}/\d{4})', re.I)
    for entry in tabs.get('research', []):
        text = entry.get('text', '')
        for match in pattern.finditer(text):
            urls = list(_URL.finditer(text[match.end():]))
            if urls:
                raw = match[2].split('/')
                try:
                    published = date.fromisoformat('-'.join(reversed(raw))).isoformat()
                except ValueError:
                    continue
                key = (_fold(match[1]), published)
                citations.setdefault(key, set()).add(urls[0].group().rstrip('.,;)'))
                labels[key] = match[1].strip()

    def item(text, original=None, classification=None):
        original = dict(original or {})
        matches = [(name, published, next(iter(urls))) for (name, published), urls in citations.items()
                   if len(urls) == 1 and name in _fold(text)
                   and '/'.join(reversed(published.split('-'))) in text]
        if len(matches) == 1:
            name, published, url = matches[0]
            original['source_url'] = original.get('source_url') or url
            original['published_on'] = original.get('published_on') or published
        # Literal citations are documentary facts, never fresh qualification.
        folded = _fold(text)
        kind = classification or original.get('classification')
        if not kind or kind == 'nao_classificado':
            kind = ('hipotese' if 'hipotese' in folded else 'reserva' if 'reserva' in folded
                    else 'fato' if original.get('label') == 'fato' or re.search(r'\bfato\b', folded)
                    else 'nao_classificado')
        result = _item(text, original, kind)
        if len(matches) == 1:
            result['source_label'] = labels[(matches[0][0], matches[0][1])]
        return result
    return item


def _research_fields(sections):
    """Presentation suggestions; no decision, customer, exclusion or confirmation values."""
    fields, conflicts = {}, set()
    for item in sections['business']:
        text = item['text']
        if (not item.get('source_url') or not item.get('published_on')
                or item.get('classification') in ('hipotese', 'hypothesis', 'reserva')):
            continue
        folded = _fold(text)
        values = {}
        if re.search(r'\b(?:pais|sede)\s*:\s*brasil\b|\bsede no brasil\b|\b(?:empresa|industria|tecelagem) (?:b2b )?brasileira\b', folded) and not re.search(r'\bnao\b|\b(?:sede|pais) (?:incert|nao confirm)', folded):
            values['country'] = 'BR'
        elif re.search(r'\bpais\s*:\s*(?!brasil\b)\w+|\bsede (?:na|no|em) (?:argentina|equador|mexico|portugal)\b', folded):
            values['country'] = 'OTHER'
        if re.search(r'\bb2b\b', folded) and not re.search(r'\b(?:talvez|possivel\w*|confirmar|incert\w*|nao confirmad\w*)\b|\bb2b\s+nao\b', folded):
            values['is_b2b'] = 'false' if re.search(r'\bnao(?:\s+\w+){0,4}\s+b2b\b|\bnao (?:vende |atende )?para empresas', folded) else 'true'
        if re.search(r'\btecelagem\b|\bindustria b2b\b', folded) and not re.search(r'\bnao\b|\btalvez\b', folded):
            values['sector'] = 'industria_b2b'
        # Never annualize a semester, invent a year, or turn a bound/estimate into revenue.
        revenue = re.search(r'(?:faturamento|receita)\s+anual\s+(?:de\s+)?r\$\s*([\d.,]+)\s*(bi(?:lhoes|lhao)?|mi(?:lhoes|lhao)?)?\s*(?:em|de)\s*(20\d{2})\b', folded)
        if revenue and not re.search(r'estimad|acima|cerca|mais de|aproximad', folded):
            amount = float(revenue[1].replace('.', '').replace(',', '.'))
            amount *= 1_000_000_000 if (revenue[2] or '').startswith('bi') else 1_000_000 if revenue[2] else 1
            values['annual_revenue_brl'] = int(amount) if amount.is_integer() else amount
        for key, value in values.items():
            if key in fields and fields[key]['value'] != value:
                conflicts.add(key)
            fields.setdefault(key, {'value': value, 'source_url': item['source_url'],
                                   'source_date': item['published_on'], 'text': text,
                                   'reference_year': revenue[3] if key == 'annual_revenue_brl' else None})
    return {key: value for key, value in fields.items() if key not in conflicts}


class CommercialAccountReadonly:
    def __init__(self, accounts):
        self.accounts = accounts

    @staticmethod
    def _empty(status, limitations):
        return {'status': status, 'company': None, 'contact': None,
                'binding_kind': None,
                'source': {'path': None, 'sha256': None, 'updated_at': None},
                'sections': [], 'originals': {'account_id': None, 'source_text': None, 'tabs': {}},
                'limitations': list(dict.fromkeys(limitations))}

    def project(self, row, qualification_context=None, sheet=None):
        key = row.get('source_key', '_single') if isinstance(row, Mapping) else '_single'
        lead = {**row, 'source_key': key} if isinstance(row, Mapping) else {'source_key': key}
        return self.project_many([lead], {key: qualification_context}, sheet)[key]

    def project_many(self, rows, contexts, sheet=None):
        """One bounded catalog per call, at most 200 rows and 500 documents.

        A copy of the existing reader reuses its private catalog for detail
        parsing; neither the injected instance nor persistent state is changed.
        Other injected ports list once and read each detail once (those ports
        own their internal costs). Context hashing remains in the account reader.
        No result is cached across calls. No approval or ICP authority is emitted.
        """
        if not isinstance(rows, (list, tuple)) or len(rows) > 200 or not isinstance(contexts, Mapping):
            raise ValueError('commercial_rows_limit_or_invalid')
        if any(not isinstance(r, Mapping) or not isinstance(r.get('source_key'), str)
               or not r['source_key'] or len(r['source_key']) > 200 for r in rows):
            raise ValueError('commercial_source_key_invalid')
        counts = Counter(r['source_key'] for r in rows)
        result, profiles = {}, {}
        for row in rows:
            key = row['source_key']
            profile, binding_status = self._binding(row, contexts.get(key))
            if counts[key] > 1:
                result[key] = self._empty('ambiguous', ['duplicate_source_key'])
            elif profile is None:
                result[key] = self._empty(binding_status, [
                    'canonical_profile_ambiguous' if binding_status == 'ambiguous' else
                    'verified_profile_or_crm_binding_missing_or_mismatched'])
            else:
                profiles[key] = profile
        if not profiles:
            return result
        limitations = ['binding_via_verified_profile_and_crm_not_markdown_domain',
                       'markdown_company_domain_unverified',
                       'research_is_not_current_crm_or_eligibility', 'markdown_is_untrusted_text']
        try:
            from .account_cadence_readonly import AccountCadenceReadonly
            reader = self.accounts
            if type(reader) is AccountCadenceReadonly:
                reader = copy(reader)
                catalog_records, catalog_warnings = reader._catalog()
                collection_warnings = [v for v in catalog_warnings if _collection_warning(v)]
                # account() appends parser warnings: each detail needs its own
                # list, otherwise one document contaminates subsequent details.
                reader._catalog = lambda: (catalog_records, list(collection_warnings))
            catalog = reader.list_accounts()
            records = catalog['accounts']
            if not isinstance(records, list) or len(records) > 500:
                raise ValueError('account_catalog_invalid')
            catalog_warnings = catalog.get('limitations', [])
            limitations.extend(v for v in catalog_warnings if _collection_warning(v))
            if any(value in limitations for value in (
                    'source_entry_limit', 'source_file_limit', 'source_total_bytes_limit')):
                raise ValueError('account_catalog_incomplete')
            matches = {profile: [] for profile in set(profiles.values())}
            ambiguous_profiles = set()
            for record in records:
                doc = reader.account(record['id'])
                text = doc.get('source_text')
                if not isinstance(text, str):
                    if (record.get('source') or {}).get('path'):
                        raise ValueError('account_source_unreadable')
                    continue
                if len(text.encode('utf-8')) > 524288:
                    raise ValueError('account_source_too_large')
                primary_profiles = _primary_profiles(text)
                found = set(matches) & primary_profiles
                if not found:
                    continue
                if len(primary_profiles) != 1:
                    ambiguous_profiles.update(found)
                    continue
                doc = {**doc, 'limitations': [v for v in doc.get('limitations', [])
                    if v not in catalog_warnings or _collection_warning(v) or _document_warning(v, doc)]}
                source = doc.get('source', {})
                if (doc.get('id') != record['id'] or source != record.get('source')
                        or not isinstance(source.get('path'), str)
                        or doc.get('source_status') != 'observed'
                        or hashlib.sha256(text.encode('utf-8')).hexdigest() != source.get('sha256')):
                    raise ValueError('account_source_changed_or_invalid')
                for profile in found:
                    matches[profile].append(doc)
            for key, profile in profiles.items():
                matched = matches[profile]
                if profile in ambiguous_profiles:
                    result[key] = self._empty('ambiguous', limitations + ['primary_profile_ambiguous'])
                elif len(matched) != 1:
                    result[key] = self._empty('ambiguous' if matched else 'unlinked', limitations + [
                        'multiple_profile_sources' if matched else 'profile_source_not_found'])
                else:
                    result[key] = self._project_document(matched[0], list(limitations))
        except Exception:
            for key in profiles:
                result[key] = self._empty('unavailable', ['account_reader_or_source_unavailable'])
        return result

    @staticmethod
    def _binding(row, context):
        """Accept only the caller's verified CRM IDs/domain and profile aliases."""
        if not isinstance(row, Mapping) or not isinstance(context, Mapping):
            return None, 'unlinked'
        ids = ('company_id', 'contact_id', 'domain')
        aliases = context.get('verified_linkedin_urls')
        if (any(not isinstance(context.get(k), str) or not context[k].strip() for k in ids)
                or not isinstance(aliases, (list, tuple))
                or row.get('hubspot_id') and row['hubspot_id'] != context['contact_id']):
            return None, 'unlinked'
        canonical = {_profile(value) for value in aliases} - {None}
        supplied = row.get('linkedin_url')
        profile = _profile(supplied)
        # A supplied CSV URL constrains canonical identity; it cannot override it.
        if supplied and (profile is None or profile not in canonical):
            return None, 'unlinked'
        if len(canonical) > 1:
            return None, 'ambiguous'
        if not canonical:
            return None, 'unlinked'
        return next(iter(canonical)), 'available'

    @staticmethod
    def _project_document(doc, limitations):
        tabs = doc.get('tabs') or {}
        documentary_item = _documentary_items(tabs)
        sections = {key: [] for key, _ in SECTIONS}
        group, historical_block, business_source = None, False, None
        for research in tabs.get('research', []):
            text = research.get('text')
            if not isinstance(text, str) or not text.strip():
                continue
            # The reader may return a heading and body as one paragraph. Read
            # heading lines individually, retaining every selected literal body.
            for line in text.splitlines():
                if not line.strip():
                    continue
                folded = _fold(line.strip().strip('*# '))
                heading = re.fullmatch(r'\s*(?:\*\*[^*]+\*\*|#{1,6}\s+.+)\s*', line)
                if heading:
                    if _historical_state(line):
                        historical_block, group = True, None
                        limitations.append('historical_crm_and_email_claims_only_in_originals')
                        continue
                    if folded == 'empresa':
                        group = 'business'
                    elif folded in ('fit e oferta', 'aderencia', 'fit'):
                        group = 'fit'
                    elif folded.startswith(('pessoa', 'por que ele', 'por que ela')):
                        group = 'people'
                    else:
                        group = None
                    # Unknown subheadings do not reopen a historical block.
                    if group is not None:
                        historical_block = False
                    continue
                if historical_block or _historical_state(line):
                    limitations.append('historical_crm_and_email_claims_only_in_originals')
                    continue
                selected = group
                if selected is None:
                    if re.match(r'^-?\s*(empresa|tecelagem|modelo de venda|mercado|vende|o que vende)', folded):
                        selected = 'business'
                    elif re.match(r'^-?\s*(fit|oferta|aderencia)', folded):
                        selected = 'fit'
                    elif re.match(r'^-?\s*(cargo|pessoa|escopo comercial|desde quando)', folded):
                        selected = 'people'
                    elif re.match(r'^-?\s*(sinal|gatilho|fato|momento)', folded):
                        selected = 'moment'
                if selected:
                    original = research
                    if selected == 'business' and 'mesma fonte' in folded and business_source:
                        original = {**research, **business_source}
                    entry = documentary_item(line, original)
                    if selected == 'business' and entry.get('source_url') and entry.get('published_on'):
                        business_source = {k: entry[k] for k in ('source_url', 'published_on')}
                    sections[selected].append(entry)
        for entry in tabs.get('dossier', []):
            text, key = entry.get('text'), entry.get('key')
            if not isinstance(text, str) or not text.strip():
                continue
            if _historical_state(text):
                limitations.append('historical_crm_and_email_claims_only_in_originals')
                continue
            target = {'M1': 'moment', 'M2': 'thesis', 'M3': 'thesis', 'M4': 'thesis',
                      'M5': 'next_action', 'M6': 'thesis', 'M7': 'people'}.get(key)
            if target:
                sections[target].append(documentary_item(text, entry))
        thesis = doc.get('thesis')
        if isinstance(thesis, str) and thesis.strip() and not _historical_state(thesis):
            sections['thesis'].insert(0, _item(thesis, classification='hipotese'))
        reserved_actions = sections['next_action']
        sections['next_action'] = []
        for pending in tabs.get('before_approval', []):
            text = pending.get('text')
            if isinstance(text, str) and text.strip() and pending.get('blocking') is True:
                if _historical_state(text):
                    limitations.append('historical_crm_and_email_claims_only_in_originals')
                else:
                    # File references and self-review stay in the original audit,
                    # rather than masquerading as commercial tasks.
                    if not re.search(r'(?:^|\s)(?:/|[\w/-]+\.(?:md|json|py)\b)|versao anterior|autocritica', _fold(text)):
                        sections['next_action'].append({**documentary_item(text),
                            'owner': 'Ygor', 'due_label': 'Antes do D0', 'display_only': True})
        sections['next_action'].extend(reserved_actions)
        for key, label in SECTIONS:
            retained = []
            for item in sections[key]:
                fragments = _without_current_state(item['text'])
                if fragments != [item['text']]:
                    limitations.append('current_commercial_state_claims_only_in_originals')
                retained.extend({**item, 'text': fragment} for fragment in fragments)
            sections[key] = retained
            if not sections[key]:
                sections[key].append(_item('Pendente de pesquisa: ' + label + '.'))
                limitations.append('commercial_section_pending:' + key)
        limitations.extend(doc.get('limitations', []))
        source = doc['source']
        return {'status': 'available', 'company': doc.get('company'), 'contact': doc.get('contact'),
                'binding_kind': 'verified_linkedin_and_hubspot_company',
                'source': {key: source.get(key) for key in ('path', 'sha256', 'updated_at')},
                'sections': [{'key': key, 'label': label, 'items': sections[key]} for key, label in SECTIONS],
                'research_fields': _research_fields(sections),
                'originals': {'account_id': doc['id'], 'source_text': doc['source_text'], 'tabs': deepcopy(tabs)},
                'limitations': list(dict.fromkeys(limitations))}
