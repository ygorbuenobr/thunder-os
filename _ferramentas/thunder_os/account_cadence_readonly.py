"""Bounded, read-only projection of individual account Markdown cadences.

This is a deliberately small Markdown dialect, not a Markdown/YAML/HTML engine:
ATX headings outside fences, scalar identity metadata, pipe tables, M1..M7
paragraphs, D<n> sections and numbered sections under 'Textos exatos'. Text and
source bytes are preserved; dates without a year are never completed from the
file date, research dates or the clock. No prototype, provider or network reads.

Source roots are explicit below. Discovery is at most two directories deep,
2,000 entries / 500 files / 512 KiB per file, plus the single explicit visitor
index (at most ten sources and its original brief). At most 128 allowlisted context
references are hashed through campaign_sheet's 64 KiB no-symlink reader. Current
hashes and historical declared hashes never prove that the author read them.
The source document itself is the only observed context. All text is untrusted
plain Markdown: the HTTP/UI consumer must escape it, never execute/render HTML.

An optional repo can be today's SQLite-enforced ReadonlyRepository, an in-memory
list_state('cadence') adapter, or {'cadences': [...]}. Ordinary filesystem
Repository instances are not called: their read methods configure WAL.

The visitor index is draft content authority, never a Registry campaign. Only
its explicit ready-for-review rows expose touches; warm/blocked/discarded rows
are status-only. The H1 company dialect, bold M1..M7 lines, inline D3 invitation
and editorial footer grammar are enabled only for those indexed paths.
"""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import unicodedata

from .campaign_sheet import _read_source
from .knowledge import KnowledgeStore


SOURCE_DIRECTORIES = (
    'contas', '_entrada/claude', 'projetos/thunder-os/cadencias',
    'projetos/thunder-os/cadencia-multicanal/contas',
    'projetos/thunder-os/cadencia-multicanal/cadencias',
)
MAC_BRAIN = Path('/Users/ygorbueno/thunder-gtm')
MAC_COMMERCIAL = Path('/Users/ygorbueno/Documents/Central/Thunder/Comercial/ABM')
MAX_BYTES, MAX_FILES, MAX_ENTRIES, MAX_STATE = 524288, 500, 2000, 500
MAX_CONTEXT = 128
MAX_TOTAL_BYTES = 16777216
VISITOR_GROUP = 'visitantes-2026-10-22'
VISITOR_DIRECTORY = '_entrada/claude/cadencias-' + VISITOR_GROUP
VISITOR_INDEX = VISITOR_DIRECTORY + '/README.md'
VISITOR_BRIEF = VISITOR_DIRECTORY + '/BRIEF.md'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}')
_HEADING = re.compile(r'^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$')
_URL = re.compile(r'https?://[^\s<>|`\[\]]+')
_PATH = re.compile(r'(?<![\w/])(?:[A-Za-z0-9_./-]+/)*[A-Za-z0-9_.-]+\.md\b')
_HASH = re.compile(r'(?i)\bSHA-?256\s*[:=]?\s*([a-f0-9]{64})\b')
_DAY = re.compile(r'^D\d{1,3}(?=$|[\s:./\u00b7\u2013\u2014-])')


def _fold(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c)).strip()


def _trim(value):
    return value.strip('\r\n')


def _identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def _unique(values):
    return list(dict.fromkeys(values))


def _urls(value):
    return _unique(m.group().rstrip('.,;:)') for m in _URL.finditer(value))


def _date(value):
    """Only complete literal dates, never consultation or modification dates."""
    if not value or re.search(r'consult|modific|acess|verific', _fold(value)):
        return None
    match = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})(?!\d)', value)
    if not match:
        return None
    literal = match[1]
    try:
        return (date.fromisoformat(literal) if '-' in literal else
                datetime.strptime(literal, '%d/%m/%Y').date()).isoformat()
    except ValueError:
        return None


def _year_ago(today):
    year = today.year - 1
    return today.replace(year=year, day=min(today.day, monthrange(year, today.month)[1]))


def _classification(*values):
    for value in values:
        text = _fold(value.strip('*_ '))
        for marker, classification in (('fato', 'fato'), ('hipotese', 'hipotese'), ('reserva', 'reserva')):
            if (re.match(r'^' + marker + r'(?:$|[: ,(])', text)
                    or re.search(r'\(' + marker + r'(?:$|[ ,)])', text)):
                return classification
    return 'nao_classificado'


def _cells(line):
    """Keep escaped pipes and inline-code pipes inside their original cell."""
    text = line.strip()
    if not text.startswith('|') or not text.endswith('|'):
        return None
    cells, part, fence, i = [], '', 0, 1
    while i < len(text) - 1:
        char = text[i]
        if char == '\\' and i + 1 < len(text) - 1:
            part += text[i:i + 2]
            i += 2
            continue
        if char == '`':
            end = i
            while end < len(text) - 1 and text[end] == '`':
                end += 1
            width = end - i
            fence = 0 if fence == width else width if not fence else fence
            part += text[i:end]
            i = end
            continue
        if char == '|' and not fence:
            cells.append(part.strip())
            part = ''
        else:
            part += char
        i += 1
    return cells + [part.strip()]


class _Markdown:
    def __init__(self, content):
        self.lines = content.splitlines(keepends=True)
        self.headings, self.fenced = [], set()
        fence = None
        for index, line in enumerate(self.lines):
            marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
            if fence:
                self.fenced.add(index)
                if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*', line):
                    fence = None
                continue
            if marker:
                fence = marker[1][0], len(marker[1])
                self.fenced.add(index)
                continue
            match = _HEADING.match(line)
            if match:
                self.headings.append((index, len(match[1]), match[2]))

    def end(self, heading):
        start, level, _ = heading
        return next((i for i, depth, _ in self.headings if i > start and depth <= level), len(self.lines))

    def body(self, heading):
        return _trim(''.join(self.lines[heading[0] + 1:self.end(heading)]))

    def sections(self, prefixes):
        return [h for h in self.headings if any(_fold(h[2]).startswith(p) for p in prefixes)]

    def tables(self, heading):
        lines = self.body(heading).splitlines()
        rows, index, fenced = [], 0, False
        while index + 1 < len(lines):
            if re.match(r'^\s*(`{3,}|~{3,})', lines[index]):
                fenced = not fenced
            header, separator = _cells(lines[index]), _cells(lines[index + 1])
            if not fenced and header and separator and len(header) == len(separator) and all(
                    re.fullmatch(r':?-{3,}:?', cell) for cell in separator):
                index += 2
                while index < len(lines) and (cells := _cells(lines[index])) is not None:
                    if len(cells) == len(header):
                        rows.append(dict(zip((_fold(h) for h in header), cells)))
                    index += 1
            else:
                index += 1
        return rows


def _field(row, *names):
    return next((row[name] for name in names if name in row), None)


def _metadata(md):
    result = {}
    # Only the preamble before the first second-level section is metadata.
    end = next((i for i, level, _ in md.headings if level >= 2), len(md.lines))
    for line in md.lines[:min(end, 80)]:
        match = re.fullmatch(r'(?:- )?(?:\*\*)?([\w _-]+)(?:\*\*)?:\s*(.*?)\s*', line)
        if match:
            key, value = _fold(match[1]), match[2]
            if key in {'account_id', 'campaign_id', 'company', 'empresa', 'conta', 'name', 'nome'}:
                # This scalar subset does not interpret YAML tags/collections.
                if value[:1] not in {'[', '{', '!', '&', '*', '|', '>'}:
                    value = value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'" else value
                    result[key] = value
    return result


def _identity(md, path):
    meta = _metadata(md)
    title = next((text for _, level, text in md.headings if level == 1), '')
    company = meta.get('company') or meta.get('empresa') or meta.get('conta')
    contact = None
    if not company and title.startswith('Cad\u00eancia \u00b7 '):
        pieces = title.split(' \u00b7 ')
        if len(pieces) == 3:
            company, contact = pieces[1:]
    if not company and re.match(r'^Cad[e\u00ea]ncia M[e\u00e9]xico\s*[:\u2014-]', title):
        if ' / ' in title:
            contact, company = re.split(r'\s*[:\u2014-]\s*', title, maxsplit=1)[1].rsplit(' / ', 1)
    account_id = _identifier(meta.get('account_id'))
    if not company and title.startswith('Campanha | '):
        company = title[len('Campanha | '):]
    if not company and title.startswith('Campanha: '):
        company = title[len('Campanha: '):]
    parts = PurePosixPath(path).parts
    canonical_slug = parts[1] if len(parts) == 3 and parts[0] == 'contas' and _identifier(parts[1]) else None
    return {'id': account_id or 'source-' + hashlib.sha256(path.encode()).hexdigest()[:24],
            'name': meta.get('name') or meta.get('nome') or company or title or None,
            'company': company, 'contact': contact, 'campaign_id': _identifier(meta.get('campaign_id')),
            'identity_status': 'explicit_account_id' if account_id else 'source_only',
            'brain_account_slug': canonical_slug}


def _dossier(md, limitations, *, indexed=False):
    found = {}
    def add(key, text, sources='', classification=''):
        if key in found:
            limitations.append('duplicate_dossier_key:' + key)
            return
        found[key] = {'key': key, 'classification': _classification(classification, sources, text),
                      'text': text, 'sources': _urls(sources + '\n' + text), 'source_text': sources}
    for section in md.sections(('dossie',)):
        for row in md.tables(section):
            key = re.match(r'^M[1-7]\b', _field(row, 'municao', 'key', 'chave', 'item', *(['codigo'] if indexed else [])) or '')
            text = _field(row, 'conteudo', 'texto')
            if key and text is not None:
                add(key[0], text, _field(row, 'fonte', 'fontes', *(['fonte e data'] if indexed else [])) or '',
                    _field(row, 'classificacao') or _classification(_field(row, 'municao', 'codigo') or ''))
        for line in md.body(section).splitlines():
            if indexed:
                bold = re.match(r'^- \*\*(M[1-7])\b([^:]*):\*\*\s*(.*)$', line)
                if bold:
                    marker = bold[2]
                    classified = re.search(r'\((hip[o\u00f3]tese[^)]*|reserva)\)', marker, re.I)
                    add(bold[1], bold[3], classification=classified[1] if classified else '')
                    continue
            match = re.match(r'^(?:- )?(M[1-7])(?:/V1)?\s*:\s*(.*)$', line)
            if match:
                add(match[1], match[2])
    for heading in md.headings:
        match = re.match(r'^(M[1-7])\b', heading[2])
        if match:
            add(match[1], md.body(heading), classification=heading[2][len(match[1]):].strip(' :\u00b7-'))
    for key in ('M1', 'M2', 'M3', 'M4', 'M5', 'M6', 'M7'):
        if key not in found:
            limitations.append('dossier_missing:' + key)
            found[key] = {'key': key, 'classification': 'nao_classificado', 'text': None, 'sources': []}
    return [found[key] for key in sorted(found)]


def _visitor_publication_date(line):
    if re.search(r'(?i)connectsafely|search-people|search-companies|hubspot|headline|perfil', line):
        return None
    marker = re.search(r'(?i)\bpublicad[oa]\b|\bfontes?\s*:|\bcontexto de\b', line)
    return _date(line[marker.start():]) if marker else None


def _research(md, dossier, today, *, indexed=False):
    rows = []
    for section in md.sections(('pesquisa abm', 'pesquisa',)):
        table = md.tables(section)
        for row in table:
            text = _field(row, 'fato', 'texto', 'conteudo')
            if text is not None:
                sources = _field(row, 'fonte', 'fontes', 'url') or ''
                rows.append((text, _urls(sources), _date(_field(row, 'data de publicacao', 'publicado em', 'data'))))
        if not table:
            for paragraph in md.body(section).split('\n\n'):
                if paragraph.strip():
                    published = re.search(r'(?i)publicad[oa] em\s*([^\n]+)', paragraph)
                    rows.append((paragraph, _urls(paragraph), _date(published[1]) if published else None))
    if indexed:
        for section in md.sections(('pessoa e empresa (com fontes)',)):
            for line in md.body(section).splitlines():
                if line.strip():
                    # Preserve each literal source line. Dates on modification or
                    # consultation claims are never publication evidence.
                    rows.append((line, _urls(line), _visitor_publication_date(line)))
    if not rows:
        rows = [(item['text'], item['sources'], _date(item.get('source_text'))) for item in dossier
                if item['text'] is not None and item['sources']]
    result = []
    for text, sources, published in rows:
        label = 'data desconhecida'
        if published and sources:
            label = 'contexto' if date.fromisoformat(published) < _year_ago(today) else 'fato'
        result.append({'text': text, 'source_url': sources[0] if sources else None,
                       'published_on': published, 'label': label, 'sources': sources})
    return result


def _touches(md, digest, limitations):
    maps = {}
    for section in md.sections(('mapa', 'calendario')):
        for row in md.tables(section):
            key = _field(row, 'toque', 'etapa', 'dia')
            if key:
                maps.setdefault(key, []).append(row)
    exact_sections = md.sections(('textos exatos',))
    touches = []
    for heading in md.headings:
        day_match = _DAY.match(heading[2])
        number_match = re.match(r'^(\d{1,2})(?:[. \u2014\u2013-])\s*(.+)', heading[2])
        numbered = number_match and any(parent[0] < heading[0] < md.end(parent)
                                       and heading[1] > parent[1] for parent in exact_sections)
        if not day_match and not numbered:
            continue
        key = day_match[0] if day_match else number_match[1]
        candidates = maps.get(key, [])
        row = candidates[0] if len(candidates) == 1 else {}
        if len(candidates) > 1:
            limitations.append('touch_map_ambiguous:' + key)
        body = md.body(heading)
        subject, body_lines, fields = None, [], {}
        for line in body.splitlines(keepends=True):
            match = re.match(r'^(?:\*\*)?(Assunto|Asunto|Canal|Data|Muni\u00e7\u00e3o|Condi\u00e7\u00e3o de parada|Status)(?:\*\*)?:[ \t]*(?:\*\*)?[ \t]*(.*?)(?:\r?\n)?$', line, re.I)
            if match:
                fields[_fold(match[1])] = match[2]
                if _fold(match[1]) in {'assunto', 'asunto'}:
                    subject = match[2]
            else:
                body_lines.append(line)
        # Trailing italic editorial annotations are metadata, retained verbatim.
        annotations = []
        while body_lines and (not body_lines[-1].strip() or re.fullmatch(r'\*\(.*\)\*\s*', body_lines[-1])):
            line = body_lines.pop()
            if line.strip():
                annotations.insert(0, line.rstrip('\r\n'))
        title_parts = heading[2].split(' \u00b7 ')
        channel = fields.get('canal')
        if not channel and day_match and len(title_parts) >= 2:
            channel = title_parts[1]
        if not channel and numbered:
            channel = number_match[2]
        channel = channel or _field(row, 'canal')
        date_text = fields.get('data') or _field(row, 'data')
        if not date_text and day_match and len(title_parts) >= 3:
            date_text = title_parts[-1]
        condition = fields.get('condicao de parada') or _field(row, 'condicao de parada', 'condicao', 'valor/condicao')
        ammunition = fields.get('municao') or _field(row, 'municao')
        touch_id = key + ':' + str(1 + sum(t['day'] == (key if day_match else None) and t['step'] == key for t in touches))
        touches.append({'id': touch_id, 'day': key if day_match else None, 'step': key,
                        'date': _date(date_text), 'date_text': date_text, 'channel': channel,
                        'ammunition': ammunition, 'stop_condition': condition, 'status': 'draft',
                        'source_status': fields.get('status'), 'subject': subject,
                        'text': _trim(''.join(body_lines)), 'source_hash': digest,
                        'source_heading': heading[2], 'annotations': annotations,
                        'source_text': body})
        if not touches[-1]['date']:
            limitations.append('touch_date_unknown:' + touch_id)
        if not condition:
            limitations.append('touch_stop_condition_unknown:' + touch_id)
    if not touches:
        limitations.append('cadence_text_missing')
    return touches


def _visitor_calendar(brief):
    if brief is None:
        return {}
    calendars = brief.sections(('calendario (dias uteis no brasil',))
    if len(calendars) != 1:
        return {}
    text = brief.body(calendars[0])
    years = set(re.findall(r'/((?:19|20)\d{2})\b', text))
    if len(years) != 1:
        return {}
    result = {}
    for match in re.finditer(r'\b(D\d{1,3})\s+[^\d\r\n]*?(\d{2})/(\d{2})(?:/\d{4})?', text):
        try:
            result[match[1]] = date(int(next(iter(years))), int(match[3]), int(match[2])).isoformat()
        except ValueError:
            return {}
    return result


def _visitor_footer(line):
    return bool(re.match(r'^(?:\*\(|\*?Contagem:|-(?: Contagem:| CTA:| Parada:| Depois:| Se o convite| Aceite))', line))


def _visitor_copy(body):
    """Remove only explicit subject/label and trailing editorial source lines."""
    lines, annotations, subject = body.splitlines(keepends=True), [], None
    while lines and (not lines[-1].strip() or _visitor_footer(lines[-1])):
        line = lines.pop()
        if line.strip():
            annotations.insert(0, line.rstrip('\r\n'))
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].strip() == '**E-mail**':
        annotations.insert(0, lines.pop(0).rstrip('\r\n'))
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        match = re.fullmatch(r'\*\*(?:Assunto|Asunto):\*\*[ \t]*(.*?)(?:\r?\n)?', line)
        if match:
            subject = match[1]
            lines.pop(index)
        break
    condition = None
    for annotation in annotations:
        match = re.search(r'(?i)\b(?:parar se|para se houver|parada:|depois:)[^\r\n]*', annotation)
        if match:
            condition = match[0]
            condition = condition[:-2] if condition.endswith(')*') else condition[:-1] if condition.endswith('*') else condition
            break
    return _trim(''.join(lines)), subject, annotations, condition


def _visitor_touches(md, digest, limitations, brief):
    maps = {}
    for section in md.sections(('mapa dos toques',)):
        for row in md.tables(section):
            if _identifier(row.get('toque')):
                maps.setdefault(row['toque'], []).append(row)
    calendars = _visitor_calendar(brief)
    parents = md.sections(('cadencia',))
    touches = []
    for heading in md.headings:
        day = _DAY.match(heading[2])
        if not day or not any(parent[0] < heading[0] < md.end(parent) and heading[1] > parent[1] for parent in parents):
            continue
        key, body = day[0], md.body(heading)
        parts = re.split(r' [\u00b7|] ', heading[2])[1:]
        rows = maps.get(key, [])
        row = rows[0] if len(rows) == 1 else {}
        if len(rows) > 1:
            limitations.append('touch_map_ambiguous:' + key)
        channel = next((part for part in parts if re.search(r'(?i)e-mail|linkedin', part)), row.get('canal'))
        dates = [(part, _date(part)) for part in parts if re.search(r'\d{2}/\d{2}', part)]
        heading_date = next((parsed for _, parsed in dates if parsed), None)
        map_date = _date(row.get('data'))
        parsed_date = heading_date or map_date
        date_provenance = 'heading' if heading_date else 'map' if map_date else None
        if heading_date and map_date and heading_date != map_date:
            parsed_date, date_provenance = None, None
            limitations.append('touch_date_conflict:' + key)
        elif parsed_date is None and key in calendars:
            parsed_date, date_provenance = calendars[key], 'original_brief_calendar'
        subject_heading = next((re.split(r'(?i)assunto:\s*', part, maxsplit=1)[1] for part in parts
                                if re.match(r'(?i)assunto:', part)), None)
        marker = re.search(r'(?im)^\*\*(?:Nota do convite LinkedIn|Convite LinkedIn)\b[^\r\n]*(?:\r?\n|$)', body) if key == 'D3' else None
        fragments = [(body[:marker.start()] if marker else body, channel, None)]
        if marker:
            fragments.append((body[marker.end():], 'LinkedIn', marker[0].rstrip('\r\n')))
        for fragment, fragment_channel, invitation_marker in fragments:
            text, subject, annotations, condition = _visitor_copy(fragment)
            if not text:
                limitations.append('touch_text_missing:' + key)
                continue
            if invitation_marker is None and fragment_channel:
                fragment_channel = re.split(r'(?i) \+ convite linkedin', fragment_channel, maxsplit=1)[0]
            reserved = bool(invitation_marker and re.search(r'(?i)n[a\u00e3]o enviar|reserva', invitation_marker))
            touch_id = key + ':' + str(1 + sum(t['day'] == key for t in touches))
            touches.append({'id': touch_id, 'day': key, 'step': key, 'date': parsed_date,
                            'date_text': dates[0][0] if dates else row.get('data'),
                            'date_provenance': date_provenance, 'channel': fragment_channel,
                            'ammunition': row.get('municao'), 'stop_condition': condition,
                            'status': 'reserved' if reserved else 'draft', 'reviewable': not reserved,
                            'status_reason': invitation_marker if reserved else None,
                            'subject': subject if subject is not None else subject_heading if not invitation_marker else None,
                            'text': text, 'source_hash': digest, 'source_heading': heading[2],
                            'source_marker': invitation_marker, 'annotations': annotations, 'source_text': fragment})
            if date_provenance == 'original_brief_calendar':
                touches[-1]['date_source'] = VISITOR_BRIEF
            if parsed_date is None:
                limitations.append('touch_date_unknown:' + touch_id)
            if condition is None:
                limitations.append('touch_stop_condition_unknown:' + touch_id)
    if not touches:
        limitations.append('cadence_text_missing')
    return touches


def _safe_reference(reference):
    parts = reference.split('/')
    if (len(reference) > 500 or not reference.endswith('.md') or '\\' in reference
            or any(ord(c) < 32 or ord(c) == 127 for c in reference)
            or any(not p or p.startswith('.') for p in parts)):
        return False
    return ((len(parts) == 2 and parts[0] in {'eu', 'thunder'})
            or (len(parts) == 3 and parts[0] == 'skills' and parts[2] == 'SKILL.md')
            or (len(parts) == 4 and parts[:2] == ['conhecimento', 'skills'] and parts[3] == 'SKILL.md')
            or (len(parts) == 3 and parts[0] == 'contas' and parts[2] in {'ficha.md', 'campanha.md', 'cadencia.md'})
            or reference in {'projetos/thunder-os/fichas-campanhas.md',
                             'conhecimento/prospeccao/angulos.md',
                             'projetos/thunder-os/cadencia-multicanal/contexto-comercial.md'})


def _context(md, path, digest, root, limitations, pending, *, brain_alias=False, usage='observed'):
    result = [{'path': path, 'sha256': digest, 'expected_sha256': None, 'observed_sha256': digest,
               'status': 'observed', 'usage': usage}]
    seen, cache, store = set(), {}, KnowledgeStore(root)
    for index, line in enumerate(md.lines):
        if index in md.fenced:
            continue
        matches = list(_PATH.finditer(line))
        for position, match in enumerate(matches):
            literal = match[0]
            reference = literal[len('BRAIN/'):] if brain_alias and literal.startswith('BRAIN/') else literal
            if reference == path:
                continue
            end = matches[position + 1].start() if position + 1 < len(matches) else len(line)
            declared = _HASH.search(line[match.end():end])
            expected = declared[1].lower() if declared else None
            if (reference, expected) in seen:
                continue
            if len(seen) >= MAX_CONTEXT:
                limitations.append('context_reference_limit')
                pending.append({'text': 'context_reference_limit', 'blocking': True})
                return result

            seen.add((reference, expected))
            observed, status = None, 'outside_allowlist'
            if _safe_reference(reference):
                try:
                    if reference not in cache:
                        raw, _ = _read_source(store, reference)
                        cache[reference] = hashlib.sha256(raw).hexdigest()
                    observed = cache[reference]
                    status = 'provenance_unverified' if expected is None else 'ok' if observed == expected else 'hash_mismatch'
                except FileNotFoundError:
                    status = 'missing_file'
                except (OSError, ValueError, UnicodeError):
                    status = 'unavailable'
            result.append({'path': reference, 'sha256': observed, 'expected_sha256': expected,
                           'observed_sha256': observed, 'status': status,
                           'reference_text': literal,
                           'usage': 'referenced_not_proven_read'})
            if status != 'ok':
                limitations.append('context_' + status + ':' + reference)
                pending.append({'text': reference, 'blocking': True, 'reason': 'context_' + status})
    return result


def _pending(md):
    result = []
    for section in md.sections(('antes de aprovar', 'confirmar antes', 'pendencias')):
        for paragraph in md.body(section).splitlines():
            match = re.match(r'^\s*(?:[-*]|\d+[.])\s+(.*)', paragraph)
            if match:
                text = match[1]
                informational = re.match(r'^\[(?:contexto|informativo|nao bloqueante)\]', _fold(text))
                result.append({'text': text, 'blocking': not bool(informational)})
    return result


def _visitor_pending(md):
    result, confirmation = [], False
    for section in md.sections(('autocritica',)):
        for line in md.body(section).splitlines():
            if not line.strip():
                continue
            if 'confirmar antes de aprovar' in _fold(line):
                confirmation = True
            result.append({'text': line, 'blocking': confirmation and bool(re.match(r'^\s*\d+[.]', line))})
    return result


def _empty(account_id, campaign, limitations):
    return {'id': account_id, 'name': None, 'company': None, 'campaign_id': campaign,
            'tabs': {key: [] for key in ('research', 'dossier', 'context', 'cadence', 'before_approval')},
            'source': {'path': None, 'sha256': None}, 'limitations': limitations,
            'source_status': 'incomplete'}


class AccountCadenceReadonly:
    def __init__(self, brain_root, repo=None, *, clock=lambda: datetime.now(timezone.utc)):
        self.root, self.repo, self.clock = Path(brain_root).resolve(), repo, clock

    @staticmethod
    def _read(anchor, relative):
        """Open each component relative to an fd, never following symlinks."""
        parts = PurePosixPath(relative).parts
        if not parts or relative.startswith('/') or '..' in parts:
            raise ValueError('path_blocked')
        directory = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise ValueError('source_not_regular_or_too_large')
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('source_too_large')
                return raw.decode('utf-8'), hashlib.sha256(raw).hexdigest()
        finally:
            os.close(directory)

    def _visitor_sources(self, limitations):
        try:
            index_text, index_hash = self._read(self.root, VISITOR_INDEX)
        except (OSError, ValueError, UnicodeError):
            limitations.append('visitor_index_missing_or_unreadable:' + VISITOR_INDEX)
            return
        index = _Markdown(index_text)
        titles = [h for h in index.headings if h[1] == 1 and
                  _fold(h[2]) == 'cadencias dos visitantes da pagina (brasil)']
        rows = index.tables(titles[0]) if len(titles) == 1 else []
        folders = [row.get('pasta') for row in rows]
        if (not 1 <= len(rows) <= 10 or len(set(folders)) != len(folders)
                or any(set(row) != {'pasta', 'conta e pessoa', 'metodo', 'status'}
                       or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', row.get('pasta') or '')
                       or not _fold(row['status']).startswith(('pronta para revisao', 'morna:', 'bloqueada:', 'descartada:'))
                       for row in rows)):
            limitations.append('visitor_index_invalid:' + VISITOR_INDEX)
            return
        brief, brief_hash = None, None
        try:
            brief_text, brief_hash = self._read(self.root, VISITOR_BRIEF)
            brief = _Markdown(brief_text)
        except (OSError, ValueError, UnicodeError):
            limitations.append('visitor_brief_missing_or_unreadable:' + VISITOR_BRIEF)
        for row in rows:
            path = VISITOR_DIRECTORY + '/' + row['pasta'] + '/cadencia.md'
            status, md, digest = row['status'], None, None
            country = 'MX' if '(mexico)' in _fold(status) else 'BR'
            referenced_campaign = re.search(r'\bMX27-[A-Z0-9-]+\b', status)
            identity = {'id': 'source-' + hashlib.sha256(path.encode()).hexdigest()[:24],
                        'name': row['conta e pessoa'], 'company': None, 'contact': None,
                        'campaign_id': None, 'identity_status': 'source_only',
                        'source_group': VISITOR_GROUP, 'source_slug': row['pasta'],
                        'country': country, 'readiness': status, 'status_reason': status,
                        'reviewable': _fold(status).startswith('pronta para revisao'),
                        'method': row['metodo'], 'thesis': None,
                        'referenced_campaign_id': referenced_campaign[0] if referenced_campaign else None,
                        'source_index': {'path': VISITOR_INDEX, 'sha256': index_hash},
                        'source_brief': {'path': VISITOR_BRIEF, 'sha256': brief_hash}}
            try:
                text, digest = self._read(self.root, path)
                md = _Markdown(text)
                title = next((h[2] for h in md.headings if h[1] == 1), '')
                parts = title.split(' | ', 2)
                if len(parts) == 3:
                    identity.update(name=parts[0], company=parts[0], contact=parts[1])
                else:
                    identity['reviewable'] = False
                    limitations.append('visitor_source_heading_invalid:' + path)
                identity['source_title'] = title
                theses = md.sections(('tese (1 linha)',))
                identity['thesis'] = md.body(theses[0]) if len(theses) == 1 else None
                md.visitor_brief = brief
            except (OSError, ValueError, UnicodeError):
                identity['reviewable'] = False
                limitations.append('account_source_missing:' + path)
            record = {**identity, 'source': {'path': path, 'sha256': digest},
                      'source_status': 'observed' if md is not None else 'incomplete'}
            yield record, md

    def _sources(self, limitations):
        roots = [(self.root, relative, relative) for relative in SOURCE_DIRECTORIES]
        if self.root == MAC_BRAIN:
            roots.append((MAC_COMMERCIAL, '.', str(MAC_COMMERCIAL)))
        count, entries, total_bytes = 0, 0, 0
        for anchor, relative, display in roots:
            base = anchor / relative
            if not base.exists():
                limitations.append('source_directory_missing:' + display)
                continue
            if base.resolve() != base.absolute():
                limitations.append('source_directory_blocked:' + display)
                continue
            pending = [(base, 0)]
            while pending:
                directory, depth = pending.pop()
                try:
                    with os.scandir(directory) as scan:
                        children = []
                        for item in scan:
                            entries += 1
                            if entries > MAX_ENTRIES:
                                limitations.append('source_entry_limit')
                                return
                            children.append(item)
                    for item in sorted(children, key=lambda value: value.name):
                        if item.name.startswith(('.', '_')) or item.name in {'Historico', 'arquivo', 'backup'}:
                            continue
                        if item.is_symlink():
                            limitations.append('source_symlink_blocked:' + str(Path(item.path).relative_to(anchor)))
                        elif item.is_dir(follow_symlinks=False) and depth < 2:
                            pending.append((Path(item.path), depth + 1))
                        elif item.is_file(follow_symlinks=False) and item.name.endswith('.md') and (
                                _fold(item.name).startswith('cadencia') or item.name == 'campanha.md'):
                            if count >= MAX_FILES:
                                limitations.append('source_file_limit')
                                return
                            count += 1
                            relative_path = Path(item.path).relative_to(anchor).as_posix()
                            if anchor == self.root and relative_path.startswith(VISITOR_DIRECTORY + '/'):
                                continue
                            path = relative_path if anchor == self.root else str(Path(item.path))
                            try:
                                text, digest = self._read(anchor, relative_path)
                            except (OSError, ValueError, UnicodeError):
                                limitations.append('source_unreadable:' + path)
                                continue
                            total_bytes += len(text.encode('utf-8'))
                            if total_bytes > MAX_TOTAL_BYTES:
                                limitations.append('source_total_bytes_limit')
                                return
                            md = _Markdown(text)
                            title = next((title for _, level, title in md.headings if level == 1), '')
                            canonical_note = relative_path.startswith('contas/') and item.name == 'campanha.md'
                            if _fold(title).startswith('cadencia') or canonical_note:
                                yield _identity(md, path), md, path, digest
                            else:
                                limitations.append('source_format_unsupported:' + path)
                except OSError:
                    limitations.append('source_directory_unreadable:' + str(directory))

    def _states(self, limitations):
        if self.repo is None:
            return []
        if getattr(self.repo, 'path', None) is not None:
            from .today_readonly import ReadonlyRepository
            if not isinstance(self.repo, ReadonlyRepository):
                limitations.append('internal_repo_readonly_snapshot_required')
                return []
        try:
            rows = self.repo.get('cadences', []) if isinstance(self.repo, Mapping) else self.repo.list_state('cadence')
        except Exception:
            limitations.append('internal_cadence_state_unavailable')
            return []
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_STATE:
            limitations.append('internal_cadence_state_limit_or_invalid')
            return []
        return [row for row in rows if isinstance(row, Mapping) and _identifier(row.get('account_id'))]

    @staticmethod
    def _state_campaign(row, limitations):
        context = row.get('execution_context')
        nested = _identifier(context.get('campaign_id')) if isinstance(context, Mapping) else None
        direct = _identifier(row.get('campaign_id'))
        if nested and direct and nested != direct:
            limitations.append('internal_campaign_conflict:' + row['account_id'])
            return None
        return nested or direct

    def _catalog(self):
        limitations, records = [], []
        states = self._states(limitations)
        records.extend(self._visitor_sources(limitations))
        for identity, md, path, digest in self._sources(limitations):
            if identity['identity_status'] == 'explicit_account_id' and not identity['campaign_id']:
                campaigns = {self._state_campaign(row, limitations)
                             for row in states if row['account_id'] == identity['id']} - {None}
                if len(campaigns) == 1:
                    identity['campaign_id'] = next(iter(campaigns))
            record = {**identity, 'source': {'path': path, 'sha256': digest}, 'source_status': 'observed'}
            records.append((record, md))
        for row in states:
            campaign = self._state_campaign(row, limitations)
            if not any(r['id'] == row['account_id'] and r['campaign_id'] == campaign for r, _ in records):
                records.append(({'id': row['account_id'], 'name': None, 'company': None,
                                 'campaign_id': campaign, 'identity_status': 'internal_account_id',
                                 'source_status': 'incomplete', 'source': {'path': None, 'sha256': None}}, None))
        if not records:
            limitations.append('account_sources_missing')
        limitations.extend('campaign_unassigned:' + r['source']['path'] for r, _ in records
                           if not r['campaign_id'] and r['source']['path'])
        return records, _unique(limitations)

    def list_accounts(self, campaign=None):
        if campaign is not None and not _identifier(campaign):
            return {'accounts': [], 'limitations': ['invalid_campaign_id']}
        records, limitations = self._catalog()
        accounts = []
        for record, _ in records:
            if campaign is not None and record['campaign_id'] != campaign:
                continue
            if not record['source']['path']:
                limitations.append('account_source_missing:' + record['id'])
            if record not in accounts:
                accounts.append(record)
        return {'accounts': sorted(accounts, key=lambda r: (r['campaign_id'] or '', r['id'], r['source']['path'] or '')),
                'limitations': _unique(limitations)}

    def account(self, account_id, campaign=None):
        if not _identifier(account_id) or (campaign is not None and not _identifier(campaign)):
            return _empty(account_id, campaign, ['invalid_account_or_campaign_id'])
        records, limitations = self._catalog()
        matched = [(r, md) for r, md in records if r['id'] == account_id and
                   (campaign is None or r['campaign_id'] == campaign)]
        if len(matched) != 1:
            result = _empty(account_id, campaign, limitations + [
                'account_source_ambiguous' if matched else 'account_source_missing:' + account_id])
            result['source_paths'] = [r['source']['path'] for r, _ in matched]
            return result
        record, md = matched[0]
        if md is None:
            return {**_empty(account_id, record['campaign_id'], limitations + ['account_source_missing:' + account_id]),
                    **record}
        indexed = record.get('source_group') == VISITOR_GROUP
        if indexed and not record['reviewable']:
            context = [{'path': record['source']['path'], 'sha256': record['source']['sha256'],
                        'status': 'observed', 'usage': 'observed'},
                       {**record['source_index'], 'status': 'observed', 'usage': 'observed'}]
            if record['source_brief']['sha256'] is not None:
                context.append({**record['source_brief'], 'status': 'observed', 'usage': 'read_original_brief'})
            return {**record, 'tabs': {'research': [], 'dossier': [],
                    'context': context,
                    'cadence': [], 'before_approval': [{'text': record['status_reason'], 'blocking': True}]},
                    'source_text': ''.join(md.lines), 'source_text_readonly': True,
                    'limitations': _unique(limitations + ['source_status_only_not_reviewable',
                                                        'draft_is_not_approval_or_delivery'])}
        now = self.clock()
        today = now.date() if isinstance(now, datetime) else now
        dossier = _dossier(md, limitations, indexed=indexed)
        research = _research(md, dossier, today, indexed=indexed)
        if not research:
            limitations.append('research_missing')
        brief = getattr(md, 'visitor_brief', None)
        touches = (_visitor_touches(md, record['source']['sha256'], limitations, brief) if indexed else
                   _touches(md, record['source']['sha256'], limitations))
        pending = _visitor_pending(md) if indexed else _pending(md)
        if not pending:
            limitations.append('before_approval_not_documented')
        limitations.extend(('markdown_is_untrusted_text', 'draft_is_not_approval_or_delivery',
                            'context_references_do_not_prove_author_read',
                            'source_claims_not_independently_verified'))
        context = _context(md, record['source']['path'], record['source']['sha256'], self.root, limitations, pending)
        if indexed:
            context.append({**record['source_index'], 'status': 'observed', 'usage': 'observed'})
            if brief is not None:
                brief_context = _context(brief, VISITOR_BRIEF, record['source_brief']['sha256'], self.root,
                                         limitations, pending, brain_alias=True, usage='read_original_brief')
                context.extend(brief_context[:max(0, MAX_CONTEXT + 3 - len(context))])
                if len(context) >= MAX_CONTEXT + 3:
                    limitations.append('context_reference_limit')
                    pending.append({'text': 'context_reference_limit', 'blocking': True})
            else:
                pending.append({'text': VISITOR_BRIEF, 'blocking': True, 'reason': 'original_brief_missing'})
        return {**record, 'tabs': {'research': research, 'dossier': dossier,
                                  'context': context,
                                  'cadence': touches, 'before_approval': pending},
                'source_text': ''.join(md.lines),
                'limitations': _unique(limitations)}
