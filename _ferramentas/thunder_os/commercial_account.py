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
    return {'text': text, 'classification': classification or original.get('classification') or 'nao_classificado',
            'source_url': source_url, 'published_on': published,
            'context_only': older or original.get('label') == 'contexto'}


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
            profile = self._binding(row, contexts.get(key))
            if counts[key] > 1:
                result[key] = self._empty('ambiguous', ['duplicate_source_key'])
            elif profile is None:
                result[key] = self._empty('unlinked', ['verified_profile_or_crm_binding_missing_or_mismatched'])
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
            return None
        ids = ('company_id', 'contact_id', 'domain')
        aliases = context.get('verified_linkedin_urls')
        profile = _profile(row.get('linkedin_url'))
        if (any(not isinstance(context.get(k), str) or not context[k].strip() for k in ids)
                or not isinstance(aliases, (list, tuple)) or not profile
                or profile not in {_profile(value) for value in aliases}
                or row.get('hubspot_id') and row['hubspot_id'] != context['contact_id']):
            return None
        return profile

    @staticmethod
    def _project_document(doc, limitations):
        tabs = doc.get('tabs') or {}
        sections = {key: [] for key, _ in SECTIONS}
        group = None
        for research in tabs.get('research', []):
            text = research.get('text')
            if not isinstance(text, str) or not text.strip():
                continue
            folded = _fold(text.strip().strip('*# '))
            if _historical_state(text):
                limitations.append('historical_crm_and_email_claims_only_in_originals')
                if text.strip().startswith(('**', '#')):
                    group = None
                continue
            # Heading-only lines establish the narrow visitor dialect's groups.
            if text.strip().startswith(('**', '#')):
                if folded == 'empresa':
                    group = 'business'
                elif folded in ('fit e oferta', 'aderencia', 'fit'):
                    group = 'fit'
                elif folded.startswith(('pessoa', 'por que ele', 'por que ela')):
                    group = 'people'
                else:
                    group = None
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
                sections[selected].append(_item(text, research))
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
                sections[target].append(_item(text, entry))
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
                    sections['next_action'].append(_item(text))
        # A document's planned cadence dates do not date an operator's task.
        sections['next_action'].extend([
            _item('Responsável pela próxima ação manual: não informado.'),
            _item('Data da próxima ação manual: não informada.')])
        limitations.extend(('manual_next_action_owner_unknown', 'manual_next_action_date_unknown'))
        sections['next_action'].extend(reserved_actions)
        for key, label in SECTIONS:
            if not sections[key]:
                sections[key].append(_item('Pendente de pesquisa: ' + label + '.'))
                limitations.append('commercial_section_pending:' + key)
        limitations.extend(doc.get('limitations', []))
        source = doc['source']
        return {'status': 'available', 'company': doc.get('company'), 'contact': doc.get('contact'),
                'binding_kind': 'verified_linkedin_and_hubspot_company',
                'source': {key: source.get(key) for key in ('path', 'sha256', 'updated_at')},
                'sections': [{'key': key, 'label': label, 'items': sections[key]} for key, label in SECTIONS],
                'originals': {'account_id': doc['id'], 'source_text': doc['source_text'], 'tabs': deepcopy(tabs)},
                'limitations': list(dict.fromkeys(limitations))}
