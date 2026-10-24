"""Local Apollo research input review; no HTTP client, write port or dispatcher.

API for later server/UI composition (authenticated actor must come from server):
  prepare(id, {binding, source, prompt_proposal}, actor, expected_version=0)
  get(id, actor) -> stored snapshot, NOT a fresh provider/approval check
  approve(id, actor, expected_version=..., input_set_hash=..., expires_at=datetime)
  readback(id, actor) -> fresh exact-value comparison and approval_status
  apply(...) -> disabled, even with explicit_gate=True; writes=0

State is one JSON document in existing Repository thunder_state, CAS on every
local mutation; ApprovalPack and cadence effects are untouched. Source is the
entire UTF-8 Brain account/dossier file (no extraction, newline normalization,
CSV escaping or truncation). binding/account_slug association is supplied by the
trusted account resolver, not inferred from company names. journey_version is
bound as input; this module does not read or change journey_draft.

Trusted synchronous read ports: field_reader(team_id),
contact_reader(contact_id, team_id). They must perform fresh, bounded reads with
deadlines/cancellation owned by the transport. Run outside an async event loop.
Both return {status:'ok', complete:True, source:{provider:'apollo', account_id:
team_id, identity_verified:True, endpoint:...}, items:[...]}. Fields use Apollo
id/label/type/modality/source plus normalized read_only:False (explicit proof,
never default a missing writeability flag to False); optional meta is hashed.
Contacts preserve id/email/account_id/typed_custom_fields. A search, import or
PATCH response is not direct contact readback. No provider retry is performed.

Only contact/custom/textarea/thunder_research is eligible. The token and prompt
are a NEW-sequence proposal, not evidence of a deployed token or generated email.
Missing fields can be prepared for display but cannot be approved. Field creation,
sequence editing, enrollment, previews, generation, CRM and send are out of scope.
To enable PATCH later, reuse an existing one-shot effect boundary first; do not
turn this local approval/hash into dispatch authority or create a parallel ledger.

Official contracts: docs.apollo.io/reference/{get-a-list-of-fields,
create-a-custom-field,update-a-contact}. Long text is Apollo type 'textarea'.
"""
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat

from .db import RepositoryConflict
from .models import payload_hash


_STATE = 'apollo_research_input'
_KIND = 'apollo_inputs_v1'
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
_SHA = re.compile(r'[a-f0-9]{64}\Z')
_MAX_BYTES = 256 * 1024
_META = {'id', 'version', 'updated_at'}
_INPUTS = ('kind', 'binding', 'source', 'research', 'field_definition', 'prompt_proposal', 'pilot')
_BINDING = {'journey_id', 'journey_version', 'campaign_id', 'apollo_team_id',
    'apollo_contact_id', 'apollo_company_account_id', 'source_key', 'email', 'account_slug'}
_PROMPT = {'target', 'proposal_id', 'variable_token', 'prompt_template', 'instructions',
    'fallback', 'generation_options', 'generation_tone'}
_VISITOR_ROOT = ('_entrada', 'claude', 'cadencias-visitantes-2026-10-22')


class ApolloResearchInputError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _require(condition, code, status=400):
    if not condition:
        raise ApolloResearchInputError(code, status)


def _text(value, limit=10000, *, empty=False):
    return (isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))
        and not any(0xD800 <= ord(c) <= 0xDFFF or ord(c) == 127
            or ord(c) < 32 and c not in '\r\n\t' for c in value))


class ApolloResearchInput:
    def __init__(self, repository, knowledge_store, *, field_reader=None,
            contact_reader=None, clock=None):
        self.repo, self.knowledge = repository, knowledge_store
        self.field_reader, self.contact_reader = field_reader, contact_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _actor(actor):
        _require(actor == 'ygor', 'human_authority_required', 403)

    def _now(self):
        now = self.clock()
        _require(isinstance(now, datetime) and now.utcoffset() is not None, 'clock_invalid', 503)
        return now

    @staticmethod
    def _id(ident):
        _require(isinstance(ident, str) and bool(_ID.fullmatch(ident)), 'input_id_invalid')

    @staticmethod
    def _hash(row):
        return payload_hash({key: row[key] for key in _INPUTS})

    def get(self, ident, actor):
        """Stored local review snapshot. Use readback to assess current approval."""
        self._actor(actor)
        self._id(ident)
        row = self.repo.get_state(_STATE, ident)
        _require(row is not None, 'input_missing', 404)
        _require(row['input_set_hash'] == self._hash(row), 'input_integrity_mismatch', 409)
        return row

    def _save(self, ident, row, expected_version):
        try:
            return self.repo.put_states_atomic([(_STATE, ident,
                {k: v for k, v in row.items() if k not in _META}, expected_version)])[0]
        except RepositoryConflict:
            raise ApolloResearchInputError('version_conflict', 409) from None

    def _source(self, source, binding):
        _require(isinstance(source, Mapping) and set(source) == {'path', 'content_hash'}, 'source_invalid')
        relative = source['path']
        _require(_text(relative, 500) and '\\' not in relative
            and not PurePosixPath(relative).is_absolute(), 'source_path_invalid')
        parts = tuple(relative.split('/'))
        slug = binding['account_slug']
        allowed = (len(parts) == 3 and parts[:2] == ('contas', slug)
            and parts[2] in {'ficha.md', 'dossie.md', 'cadencia.md'}) or (
            parts == (*_VISITOR_ROOT, slug, 'cadencia.md'))
        _require(allowed, 'source_path_invalid')
        _require(isinstance(source['content_hash'], str) and bool(_SHA.fullmatch(source['content_hash'])),
            'source_hash_invalid')
        try:
            raw = self._source_bytes(parts)
            _require(len(raw) <= _MAX_BYTES, 'source_too_large')
            value = raw.decode('utf-8')
        except (OSError, UnicodeError):
            raise ApolloResearchInputError('source_unavailable', 409) from None
        _require(hashlib.sha256(raw).hexdigest() == source['content_hash'], 'source_hash_conflict', 409)
        _require(_text(value, _MAX_BYTES), 'source_text_invalid')
        return value

    def _source_bytes(self, parts):
        """Walk canonical root and allowed relative path using no-follow FDs.

        Never resolve/check a path and reopen it later: renaming a directory or
        replacing an entry with a symlink cannot redirect an already opened FD.
        Read bytes rather than KnowledgeStore.read's universal-newline text so
        CRLF and the reviewed raw source hash remain exact. No knowledge writes.
        """
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root = self.knowledge.root
        _require(root.is_absolute(), 'source_path_invalid')
        directory = os.open(root.anchor, flags)
        try:
            for part in (*root.parts[1:], *parts[:-1]):
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory)
            with os.fdopen(descriptor, 'rb') as stream:
                _require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'source_unavailable', 409)
                return stream.read(_MAX_BYTES + 1)
        finally:
            os.close(directory)

    @staticmethod
    def _envelope(reader, args, team, endpoint):
        if reader is None:
            return None
        try:
            result = reader(*args)
        except Exception:
            # Never expose provider bodies, headers or exception text to the UI.
            return None
        if not isinstance(result, Mapping):
            return None
        source = result.get('source')
        if (result.get('status') != 'ok' or result.get('complete') is not True
                or not isinstance(source, Mapping) or source.get('provider') != 'apollo'
                or source.get('account_id') != team or source.get('identity_verified') is not True
                or source.get('endpoint') != endpoint or not isinstance(result.get('items'), list)):
            return None
        return result['items']

    def _field(self, binding):
        team = binding['apollo_team_id']
        items = self._envelope(self.field_reader, (team,), team, '/api/v1/fields?source=custom')
        if items is None or any(not isinstance(row, Mapping) for row in items):
            return None
        matches = [row for row in items if row.get('label') == 'thunder_research']
        if len(matches) != 1:
            return None
        field = matches[0]
        if (not isinstance(field.get('id'), str) or not _ID.fullmatch(field['id'])
                or field.get('type') != 'textarea' or field.get('modality') != 'contact'
                or field.get('source') != 'custom' or field.get('read_only') is not False
                or 'team_id' in field and field['team_id'] != team
                or sum(row.get('id') == field['id'] for row in items) != 1):
            return None
        definition = {key: deepcopy(field[key]) for key in
            ('id', 'label', 'type', 'modality', 'source', 'read_only')}
        if 'meta' in field:
            definition['meta'] = deepcopy(field['meta'])
        try:
            encoded = json.dumps(definition, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > _MAX_BYTES:
                return None
        except (ValueError, TypeError, UnicodeError):
            return None
        return definition

    def _contact(self, binding):
        team, contact_id = binding['apollo_team_id'], binding['apollo_contact_id']
        items = self._envelope(self.contact_reader, (contact_id, team), team,
            '/api/v1/contacts/' + contact_id)
        if items is None or len(items) != 1 or not isinstance(items[0], Mapping):
            return None
        row = items[0]
        if (row.get('id') != contact_id or row.get('email') != binding['email']
                or row.get('account_id') != binding['apollo_company_account_id']):
            return None
        # A contradictory explicit team must never be overridden by the envelope.
        if 'team_id' in row and row['team_id'] != team:
            return None
        return row

    def prepare(self, ident, data, actor, *, expected_version=0):
        self._actor(actor)
        self._id(ident)
        _require(type(expected_version) is int and expected_version >= 0, 'version_invalid')
        _require(isinstance(data, Mapping) and set(data) == {'binding', 'source', 'prompt_proposal'},
            'input_invalid')
        binding, prompt = deepcopy(data['binding']), deepcopy(data['prompt_proposal'])
        _require(isinstance(binding, Mapping) and set(binding) == _BINDING, 'binding_invalid')
        for key in _BINDING - {'journey_version', 'email', 'account_slug'}:
            _require(isinstance(binding[key], str) and bool(_ID.fullmatch(binding[key])), 'binding_invalid')
        _require(type(binding['journey_version']) is int and binding['journey_version'] > 0, 'binding_invalid')
        _require(isinstance(binding['account_slug'], str)
            and bool(re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', binding['account_slug'])), 'binding_invalid')
        _require(_text(binding['email'], 254) and bool(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', binding['email'])),
            'binding_invalid')
        _require(isinstance(prompt, Mapping) and set(prompt) == _PROMPT, 'prompt_invalid')
        _require(prompt['target'] == 'new_sequence_only', 'new_sequence_only')
        _require(isinstance(prompt['proposal_id'], str) and bool(_ID.fullmatch(prompt['proposal_id'])), 'prompt_invalid')
        token = prompt['variable_token']
        _require(_text(token, 200) and bool(re.fullmatch(r'\{\{[A-Za-z0-9_. -]+\}\}', token)), 'variable_token_invalid')
        _require(all(_text(prompt[k], empty=k in ('fallback', 'generation_tone'))
            for k in ('prompt_template', 'instructions', 'fallback', 'generation_tone')), 'prompt_invalid')
        _require(token in prompt['prompt_template'], 'research_token_missing')
        _require(isinstance(prompt['generation_options'], list), 'prompt_invalid')
        try:
            encoded = json.dumps(prompt, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise ApolloResearchInputError('prompt_invalid') from None
        try:
            prompt_bytes = encoded.encode('utf-8')
        except UnicodeError:
            raise ApolloResearchInputError('prompt_invalid') from None
        _require(len(prompt_bytes) <= _MAX_BYTES, 'prompt_too_large')
        value = self._source(data['source'], binding)
        field = self._field(binding)
        row = {'kind': _KIND, 'binding': binding, 'source': deepcopy(data['source']),
            'research': {'value_exact': value, 'value_hash': payload_hash({'value_exact': value}),
                'variable_token': token, 'field_id': field['id'] if field else None,
                'field_definition_hash': payload_hash(field) if field else None, 'modality': 'contact'},
            'field_definition': field, 'field_status': 'verified' if field else 'unavailable',
            'prompt_proposal': prompt, 'token_binding_status': 'proposal_only',
            'pilot': {'max_leads': 10, 'e1_execution_mode': 'manual_email', 'human_review_location': 'apollo'},
            'approval': None, 'capability': 'prepare_only', 'executable': False,
            'apply_enabled': False, 'final_email_parity': False}
        row['input_set_hash'] = self._hash(row)
        row['contact_binding_status'] = 'verified' if self._contact(binding) is not None else 'unavailable'
        return self._save(ident, row, expected_version)

    def _current(self, row):
        self._source(row['source'], row['binding'])
        current = self._field(row['binding'])
        _require(current is not None and row['field_definition'] is not None,
            'field_definition_unavailable', 409)
        _require(current == row['field_definition'], 'field_definition_changed', 409)
        contact = self._contact(row['binding'])
        _require(contact is not None, 'contact_binding_unavailable', 409)
        return contact

    def approve(self, ident, actor, *, expected_version, input_set_hash, expires_at):
        row = self.get(ident, actor)
        _require(type(expected_version) is int and row['version'] == expected_version, 'version_conflict', 409)
        _require(input_set_hash == row['input_set_hash'], 'input_hash_conflict', 409)
        now = self._now()
        _require(isinstance(expires_at, datetime) and expires_at.utcoffset() is not None
            and expires_at > now, 'approval_expiry_invalid')
        self._current(row)
        approval = row['approval']
        if (approval and approval['input_set_hash'] == input_set_hash
                and datetime.fromisoformat(approval['expires_at']) > now):
            # Provider reads happened outside the transaction; a concurrent edit
            # cannot turn an old snapshot into an idempotent successful approval.
            with self.repo.transaction():
                latest = self.get(ident, actor)
                _require(latest['version'] == expected_version, 'version_conflict', 409)
                return latest
        row['approval'] = {'kind': _KIND, 'approved_by': actor, 'approved_at': now.isoformat(),
            'expires_at': expires_at.isoformat(), 'input_set_hash': input_set_hash,
            'input_version': row['version'], 'dispatch_authorized': False}
        return self._save(ident, row, expected_version)

    def readback(self, ident, actor):
        row = self.get(ident, actor)
        approval = row['approval']
        approval_status = ('not_approved' if not approval else
            'expired' if datetime.fromisoformat(approval['expires_at']) <= self._now() else 'approved')
        result = {'input_id': ident, 'version': row['version'], 'input_set_hash': row['input_set_hash'],
            'status': 'unavailable', 'reason': None, 'approval_status': approval_status,
            'readback_verified': False, 'delivery_verified': False, 'writes': 0,
            'observed_at': self._now().isoformat(), 'capability': 'prepare_only',
            'executable': False, 'apply_enabled': False}
        try:
            contact = self._current(row)
            _require(self.get(ident, actor)['version'] == row['version'], 'version_conflict', 409)
        except ApolloResearchInputError as exc:
            result['reason'] = exc.code
            if approval:
                result['approval_status'] = 'stale'
            return result
        fields = contact.get('typed_custom_fields')
        value = fields.get(row['research']['field_id']) if isinstance(fields, Mapping) else None
        if not isinstance(value, str) or not _text(value, _MAX_BYTES, empty=True):
            result['reason'] = 'research_value_unavailable'
            return result
        exact = value == row['research']['value_exact']
        result.update(status='verified' if exact else 'mismatch',
            reason=None if exact else 'research_value_mismatch', readback_verified=exact,
            value_hash=payload_hash({'value_exact': value}))
        return result

    def apply(self, ident, actor, *, expected_version, input_set_hash, explicit_gate=False):
        """No write port: a true caller gate still cannot enable this implementation."""
        row = self.get(ident, actor)
        _require(type(expected_version) is int and row['version'] == expected_version, 'version_conflict', 409)
        _require(input_set_hash == row['input_set_hash'], 'input_hash_conflict', 409)
        return {'status': 'disabled', 'reason': 'existing_one_shot_patch_integration_required',
            'capability': 'prepare_only', 'executable': False, 'apply_enabled': False, 'writes': 0}
