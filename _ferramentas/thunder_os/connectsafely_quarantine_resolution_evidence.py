"""Pinned provider readback plus separately approved inconclusive policy.

Only builds fences. Never rewrites a touch or manufactures a provider receipt.
The operator supplies the independent hash; integrity is not provider authenticity.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .connectsafely_coordination import require, digest, encoded

HISTORICAL_POST = re.compile(r'urn:li:(?:activity:[0-9]{1,25}|groupPost:[0-9]{1,25}-[0-9]{1,25})')


def pinned_json(path, pin):
    artifact = Path(path)
    require(artifact.is_absolute() and artifact.is_file() and not artifact.is_symlink(), 'social_mapping_source_invalid')
    require(artifact.stat().st_size <= 1000000, 'social_mapping_source_invalid')
    raw = artifact.read_bytes()
    require(isinstance(pin, str) and re.fullmatch(r'[a-f0-9]{64}', pin) and digest(raw) == pin, 'social_mapping_source_changed')
    return json.loads(raw)


def person_key(value):
    # The DB helper uses this same host/path normalization. Never decode URNs.
    require(isinstance(value, str) and value.strip(), 'social_person_invalid')
    parts = urlsplit(value)
    require(parts.scheme in ('http', 'https') and parts.netloc.lower() in ('linkedin.com', 'www.linkedin.com'), 'social_person_invalid')
    path = parts.path.rstrip('/').lower()
    require(re.fullmatch(r'/in/[^/\s?#]+|/posts/[^/\s?#]+|/feed/update/urn:li:activity:[0-9]{1,25}', path), 'social_person_invalid')
    if path.startswith('/posts/'):
        path = path.split('_', 1)[0]
    return path


def post_identity(value):
    if not isinstance(value, str):
        return None
    urn = value.removeprefix('https://www.linkedin.com/feed/update/').rstrip('/')
    if HISTORICAL_POST.fullmatch(urn):
        return urn
    # Exported URLs may start the activity segment immediately after the author
    # separator. Keep backfill-v1 HINT unchanged: its hashes are historical.
    hint = re.fullmatch(r'https://www\.linkedin\.com/posts/[^/?#]*[-_]activity-([0-9]{1,25})(?:-[^/?#]*)?/?(?:[?#].*)?', value)
    if hint:
        return 'urn:li:activity:' + hint[1]
    match = re.fullmatch(r'https://www\.linkedin\.com/posts/[^/?#]*-ugcPost-([0-9]{1,25})(?:-[^/?#]*)?/?', value)
    return 'urn:li:ugcPost:' + match[1] if match else None


@dataclass(frozen=True)
class ReviewedReconciliation:
    path: str
    sha256: str

    def verify(self):
        data = pinned_json(self.path, self.sha256)
        require(isinstance(data, dict) and set(data) == {'version', 'decision_ref', 'account_id', 'evidence_path', 'evidence_sha256', 'scoped_inconclusive'}, 'social_resolution_policy_invalid')
        require(data['version'] == 2 and isinstance(data['decision_ref'], str) and data['decision_ref'].strip(), 'social_resolution_policy_invalid')
        ids = data['scoped_inconclusive']
        require(isinstance(ids, list) and all(type(i) is int and i > 0 for i in ids) and len(ids) == len(set(ids)), 'social_resolution_policy_invalid')
        evidence = pinned_json(data['evidence_path'], data['evidence_sha256'])
        require(isinstance(evidence, dict) and isinstance(evidence.get('items'), list) and evidence.get('account', {}).get('id'), 'social_resolution_evidence_invalid')
        require(isinstance(data['account_id'], str) and re.fullmatch(r'[a-f0-9]{24}', data['account_id']) and evidence['account']['id'] == data['account_id'], 'social_resolution_account_mismatch')
        return data, evidence


def plan_reconciliation(rows, reviewed, *, expected_history_hash):
    require(type(reviewed) is ReviewedReconciliation, 'social_mapping_review_invalid')
    require(digest(encoded(rows)) == expected_history_hash, 'social_resolution_history_changed')
    policy, source = reviewed.verify()
    items = source['items']
    require(all(isinstance(i, dict) and type(i.get('touch_id')) is int for i in items), 'social_resolution_evidence_invalid')
    by_id = {i['touch_id']: i for i in items}
    require(len(by_id) == len(items), 'social_resolution_evidence_invalid')
    require(set(policy['scoped_inconclusive']) <= {r['id'] for r in rows}, 'social_resolution_policy_invalid')
    entries = []
    for row in rows:
        entry = dict(touch_id=row['id'], family=row['acao'], account_scope='all_accounts',
                     scope='global', post_urn=None, receipt_verified=False, identity_verified=False,
                     reason='identity_not_proven')
        item = by_id.get(row['id'])
        if item:
            require((item['original_target'], item['family'], item['historical_status']) ==
                    (row['alvo'], row['acao'], row['status']), 'social_resolution_history_mismatch')
            require(person_key(item['profile']) == person_key(row['pessoa_chave']), 'social_resolution_person_mismatch')
            if item['verdict'] == 'positive_provider_readback':
                post = post_identity(item['canonical_target'])
                effect = item.get('provider_effect')
                require(post and HISTORICAL_POST.fullmatch(post) and isinstance(effect, dict) and effect.get('type') == {'curtida':'reaction', 'comentario':'comment'}[row['acao']], 'social_resolution_evidence_invalid')
                actual = post_identity(effect.get('postUrl'))
                aliases = item.get('alias_evidence') or {}
                require(actual == post or (aliases.get('activityUrn') == post and aliases.get('ugcPostUrn') == actual and actual is not None), 'social_resolution_alias_unproven')
                original = post_identity(row['alvo'])
                require(original == post or (aliases.get('activityUrn') == post and aliases.get('ugcPostUrn') == original and original is not None), 'social_resolution_alias_unproven')
                entry.update(scope='target', post_urn=post, identity_verified=True, reason='provider_readback_target_fence')
            elif item['verdict'] == 'inconclusive':
                require(item.get('canonical_target') is None and item.get('provider_effect') is None, 'social_resolution_evidence_invalid')
                if row['id'] in policy['scoped_inconclusive']:
                    hint = post_identity(row['alvo'])
                    require(hint and hint.startswith('urn:li:activity:'), 'social_resolution_hint_required')
                    people = {person_key(row['pessoa_chave']), person_key(item['profile'])}
                    if '/posts/' in row['alvo']:
                        people.add(person_key(row['alvo']))
                        people.add('/in/' + person_key(row['alvo']).removeprefix('/posts/'))
                    entry.update(scope='scoped_inconclusive', reason='approved_inconclusive_fence',
                                 person_keys=sorted(people), target_urls=[row['alvo']], hint_post_urns=[hint])
            else:
                require(False, 'social_resolution_evidence_invalid')
        if row['id'] in policy['scoped_inconclusive']:
            require(entry['scope'] == 'scoped_inconclusive', 'social_resolution_policy_invalid')
        entries.append(entry)
    result = dict(version=2, read_only=True, runtime_authorized=False,
                  history_sha256=expected_history_hash, mapping_sha256=reviewed.sha256,
                  source_account_id=policy['account_id'],
                  retained_quarantine_count=len(rows), entries=entries,
                  global_blockers=sum(e['scope'] == 'global' for e in entries),
                  account_narrowing_authorized=False)
    return {**result, 'proposal_sha256': digest(encoded(result))}
