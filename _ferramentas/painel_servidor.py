#!/usr/bin/env python3
"""Painel stdlib. Autor: claude; 2026-09-25; fonte: tarefa painel-vps; validado localmente."""
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import sqlite3
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, parse_qs
from zoneinfo import ZoneInfo

import painel
import toques
from thunder_os.db import Repository, RepositoryConflict
from thunder_os.models import ContractError, StaleVersionError, as_payload, payload_hash
from thunder_os.service import AuthorizationError, ThunderService
from thunder_os.knowledge import KnowledgeStore
from thunder_os.hubspot_console import HubSpotConsole, systemd_hubspot_credential
from thunder_os.seqd_console import SEQDConsole
from thunder_os.email_readback import configured_email_readers
from thunder_os.email_canary import EmailCanary, CanaryError, restricted_mode, canary_view
from thunder_os.seqd_client import SEQDError
from thunder_os.hubspot_executor import HubSpotHTTPError
from thunder_os.integration_hubspot import HubSpotContractError
from thunder_os.integration_views import IntegrationRuntime, ProbeObservation
from thunder_os.integration_views import IntegrationViews
from thunder_os.connectsafely_console import ConnectSafelyError as ConnectSafelyConsoleError
from thunder_os.connectsafely_client import ConnectSafelyError as ConnectSafelyProviderError
from thunder_os.connection_backfill import BACKFILL_CHECKPOINT
from thunder_os.connection_company import resolution_kind
from thunder_os.connection_ledger import LIVE_CHECKPOINT, profile_kind
from thunder_os.connection_intake import ConnectionIntakeError, profile_key
from thunder_os.connection_work import WORK_LINK_KIND, canonical_connection_request, CONNECTION_ACTOR

CODE_ROOT = Path(__file__).resolve().parent.parent
RAIZ = Path(os.environ.get("THUNDER_BRAIN_ROOT", str(CODE_ROOT))).expanduser().resolve()
NONCE_TTL_SECONDS = 300
NONCE_LIMIT = 1024
NONCE_LOCK = threading.Lock()
AGENTTRAIL_INGRESS_TOKEN = Path('/etc/thunder-os/agenttrail-ingress.token')
LIMITE = 200 * 1024
COLECOES = ('itens', 'regras', 'sugestoes')
TZ = ZoneInfo('America/Sao_Paulo')


class Erro(Exception):
    def __init__(self, mensagem, status=400):
        super().__init__(mensagem)
        self.status = status


def external_effects_enabled():
    # Only an explicit operator opt-in enables commands. Missing/invalid values fail closed.
    return os.environ.get('THUNDER_EXTERNAL_EFFECTS_ENABLED', '').lower() == 'true'


def legacy_aceites_materialization_enabled():
    return os.environ.get('THUNDER_OS_LEGACY_ACEITES_MATERIALIZATION_ENABLED', '').lower() == 'true'


def release_source_sha(code_root=CODE_ROOT):
    manifest = Path(code_root) / 'thunder-os-manifest.json'
    configured = os.environ.get('THUNDER_OS_SOURCE_SHA', '')
    if not manifest.exists():
        return configured or 'unknown'
    try:
        sha = json.loads(manifest.read_text(encoding='utf-8'))['source_sha']
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise Erro('Manifesto da release inválido') from exc
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise Erro('SHA do manifesto inválido')
    if configured and configured != sha:
        raise Erro('SHA configurado diverge da release')
    return sha


def agora():
    return dt.datetime.now(TZ)


def identificador(valor):
    if not isinstance(valor, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', valor):
        raise Erro('id inválido')
    return valor


def objeto(valor):
    if not isinstance(valor, dict):
        raise Erro('Esperado objeto JSON')
    return valor


def carregar(texto):
    try:
        return json.loads(texto, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise Erro('JSON inválido') from None


def instante(valor):
    """Postgres devolve datetime; SQLite CURRENT_TIMESTAMP usa UTC sem fuso."""
    data = valor if isinstance(valor, dt.datetime) else dt.datetime.fromisoformat(str(valor).replace('Z', '+00:00'))
    return data if data.tzinfo else data.replace(tzinfo=dt.timezone.utc)


def serializar_linha(linha):
    return {k: instante(v).isoformat() if v is not None and k in
            ('criado_em', 'atualizado_em', 'aceito_em') else v for k, v in linha.items()}


def connection_panel_projection(service, *, view='actions', offset=0, limit=100):
    """Project canonical connection state without returning dossier or brain content."""
    if (view not in {'actions', 'history', 'all'} or type(offset) is not int or offset < 0
            or type(limit) is not int or not 1 <= limit <= 100):
        raise Erro('Consulta de conexões inválida')
    repository = service.repository
    links = {}
    for link in repository.list_state(WORK_LINK_KIND):
        key = (link.get('connectsafely_account_id'), link.get('profile'))
        if not all(isinstance(value, str) for value in key):
            continue
        links.setdefault(key, []).append(link)

    cadences = {}
    for cadence in repository.list_state('cadence'):
        key = (cadence.get('account_id'), cadence.get('person_key'))
        if all(isinstance(value, str) for value in key):
            cadences.setdefault(key, []).append(cadence)

    def reviewable_cadence(account, profile, research):
        try:
            expected_hash = payload_hash({'version': research['version'], 'facts': research['facts']})
            expected_sources = [
                {'source': fact['source'], 'published_at': fact['published_at'],
                 'collected_at': fact['observed_at']}
                for fact in research['facts']
            ]
        except (KeyError, TypeError, ValueError):
            return False
        if not expected_sources:
            return False
        for cadence in cadences.get((account, profile), []):
            steps = cadence.get('steps')
            if (cadence.get('status') != 'ready_for_review'
                    or cadence.get('research_hash') != expected_hash
                    or cadence.get('research_sources') != expected_sources
                    or cadence.get('actor') not in {'ygor', 'hermes'}
                    or not isinstance(steps, list) or not steps):
                continue
            try:
                if instante(cadence['updated_at']) < instante(research['updated_at']):
                    continue
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if all(isinstance(step, dict) and step.get('recipient') == profile
                   and step.get('channel') == 'linkedin'
                   and isinstance(step.get('content'), str) and step['content'].strip()
                   and step.get('status') == 'draft' for step in steps):
                return True
        return False
    def completed_research(connectsafely_id, profile, account, research):
        try:
            request = canonical_connection_request(repository, connectsafely_id, profile)
        except (ContractError, ValueError, KeyError, TypeError):
            return None
        if request.get('account_id') != account:
            return None
        for link in links.get((connectsafely_id, profile), []):
            if link.get('request_version') != request['version']:
                continue
            work = repository.get_work(link['id'])
            if work is None:
                continue
            context = work.context
            result = work.result
            created_by_monitor = any(
                event['type'] == 'work_requested'
                and event['payload'].get('actor') == CONNECTION_ACTOR
                and event['payload'].get('id') == work.id
                for event in repository.events(work.id))
            if (not created_by_monitor or work.logical_owner != CONNECTION_ACTOR
                    or work.status.value != 'succeeded'
                    or context.get('capability') != 'research'
                    or context.get('account_id') != account
                    or context.get('connectsafely_account_id') != connectsafely_id
                    or context.get('connection_profile') != profile
                    or context.get('connection_request_version') != request['version']
                    or not isinstance(result, dict)
                    or not isinstance(result.get('evidence'), list) or not result['evidence']
                    or result.get('dependency_failure')
                    or not isinstance(result.get('artifact'), dict)
                    or not isinstance(result['artifact'].get('account'), dict)
                    or result['artifact']['account'].get('domain') != account
                    or not isinstance(result.get('artifact_ref'), dict)):
                continue
            ref = result['artifact_ref']
            version = ref.get('version')
            if (ref.get('kind') != 'research' or ref.get('id') != account
                    or type(version) is not int or version < 1):
                continue
            if research and research.get('version') == version:
                dossier = research
            else:
                dossier = repository.get_state('research_history', f'{account}:v{version}')
            if (dossier and dossier.get('status') == 'validated'
                    and dossier.get('recommendation') == 'abordar'
                    and isinstance(dossier.get('account'), dict)
                    and dossier['account'].get('domain') == account):
                return version
        return None

    account_ids = {
        row['id'] for kind in (LIVE_CHECKPOINT, BACKFILL_CHECKPOINT)
        for row in repository.list_state(kind)
        if isinstance(row.get('id'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}', row['id'])
    }
    counts = dict.fromkeys(('needs_identity', 'needs_fit', 'research', 'dossier_review',
                            'approach_approval', 'no_action', 'already_contacted', 'unknown'), 0)
    entries = []
    for connectsafely_id in sorted(account_ids):
        for row in repository.list_state(profile_kind(connectsafely_id)):
            if (row.get('connectsafely_account_id') != connectsafely_id
                    or not isinstance(row.get('profile'), str)
                    or row.get('id') != row['profile']):
                continue
            try:
                if profile_key(row['profile']) != row['profile']:
                    continue
            except ConnectionIntakeError:
                continue
            account = row.get('account_id')
            if not isinstance(account, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]', account):
                account = None
            source_status = row.get('status')
            stage = 'unknown'
            detail = 'Estado incompleto ou não reconhecido; confira a origem antes de agir.'
            if source_status in {'already_contacted', 'historical', 'no_action'}:
                stage = 'already_contacted' if source_status == 'already_contacted' else 'no_action'
                detail = ('Contato anterior registrado; nenhuma nova ação sugerida.' if stage == 'already_contacted'
                          else 'Sem ação proposta nesta consulta.')
            elif source_status == 'needs_identity' or account is None:
                stage, detail = 'needs_identity', 'Empresa ainda não identificada com evidência.'
                candidate = repository.get_state(resolution_kind(connectsafely_id), row['profile'])
                if (candidate and candidate.get('status') == 'candidate'
                        and candidate.get('profile') == row['profile']
                        and candidate.get('connectsafely_account_id') == connectsafely_id
                        and isinstance(candidate.get('company_name'), str)
                        and 0 < len(candidate['company_name']) <= 200):
                    detail = ('Perfil sugere ' + candidate['company_name']
                              + '; confirme a empresa e o domínio antes de pesquisar.')
            elif source_status == 'needs_fit':
                stage, detail = 'needs_fit', 'Encaixe ainda não comprovado para iniciar pesquisa.'
            elif source_status in {'research', 'qualified_waiting_budget'}:
                stage, detail = 'research', 'Pesquisa solicitada ou aguardando; dossiê ainda não validado.'
                research = repository.get_state('research', account)
                dossier_version = completed_research(connectsafely_id, row['profile'], account, research)
                if dossier_version is not None:
                    stage, detail = 'dossier_review', 'Dossiê registrado; confira as evidências e lacunas antes de abordar.'
                    if not research or research.get('version') != dossier_version:
                        detail = 'Dossiê desta conexão concluído; pesquisa da conta atualizada depois. Revise antes de abordar.'
                    else:
                        try:
                            current = service.current_research(account)
                        except (ContractError, ValueError, KeyError, TypeError):
                            detail = 'Dossiê registrado, mas a validação atual está incompleta ou vencida.'
                        else:
                            if (current.get('version') == dossier_version
                                    and reviewable_cadence(account, row['profile'], current)):
                                stage, detail = ('approach_approval',
                                                 'Abordagem desta conexão pronta para revisão e aprovação humana; nenhum envio realizado.')
                elif research and research.get('status') == 'insufficient':
                    detail = 'Pesquisa concluída com evidência insuficiente; confira as lacunas.'
            raw_date = row.get('connected_at') or row.get('first_observed_at')
            try:
                connected_at = instante(raw_date).astimezone(dt.timezone.utc)
            except (ValueError, TypeError, OverflowError):
                connected_at = None
            item = {
                'profile': row['profile'], 'connectsafely_account_id': connectsafely_id,
                'account_id': account, 'stage': stage, 'detail': detail,
                'connected_at': connected_at.isoformat() if connected_at else None,
            }
            counts[stage] += 1
            historical = stage in {'no_action', 'already_contacted'}
            if view == 'all' or (view == 'history' and historical) or (view == 'actions' and not historical):
                entries.append((connected_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc), item))
    entries.sort(key=lambda entry: (entry[0], entry[1]['connectsafely_account_id'], entry[1]['profile']), reverse=True)
    total = len(entries)
    page = entries[offset:offset + limit]
    groups = {}
    for _, item in page:
        groups.setdefault(item['account_id'] or 'unknown', []).append(item)
    has_more = offset + len(page) < total
    return {'schema_version': 1, 'view': view, 'offset': offset, 'limit': limit,
            'total': total, 'counts': counts, 'has_more': has_more,
            'next_offset': offset + len(page) if has_more else None, 'accounts': [
        {'account_id': None if account == 'unknown' else account,
         'connections': items}
        for account, items in groups.items()]}


ABM_MOTIVOS = frozenset(('portal canonical_identity deal_properties company_association '
    'contact_association note_association task_association company_exists contact_identity '
    'unique_contact_pipeline unique_company_pipeline note_body task_completed task_subject '
    'task_timestamp portal_mismatch readback_failed').split())
ABM_STATUS = ('VERIFIED', 'NEEDS_RECONCILIATION', 'UNVERIFIED')
ABM_CATEGORIAS = ('STRUCTURAL_COMPLETE', 'NEEDS_RECONCILIATION', 'MISSING_DATA',
                  'MISSING_RESEARCH', 'UNVERIFIED')
ABM_MOTIVOS_LOTE = ABM_MOTIVOS | frozenset(('duplicate_deal duplicate_contact '
    'missing_contact missing_deal missing_company missing_note missing_task missing_research '
    'missing_source_links missing_tier_owner contact hunter_deal company note sections_complete '
    'source_links completed_invite_task tier_owner unverified '
    'approved_body_reference_not_located company_missing_or_nonunique completed_task_missing '
    'contact_missing_or_nonunique crm_readback_predates_completed_writer dossier_note_missing '
    'existing_writer_running hunter_deal_missing invite_task_url_provenance_unverified '
    'multiple_hunter_deals required_section_not_detected tier_or_owner_missing '
    'company_missing_or_duplicate contact_missing_or_duplicate '
    'contact_specific_completed_task_not_proven contact_specific_note_not_proven '
    'existing_content_needs_quality_review hunter_deal_missing_or_duplicate '
    'legacy_url_attribution_requalified note_identity_name_only_url_unverified '
    'other_contact_note_excluded other_contact_task_excluded '
    'research_claims_not_independently_verified task_correlated_by_source_minute_only '
    'task_identity_name_only_url_unverified').split())


def abm_lote(dsn):
    """Inventário opcional: falha isolada, sem invalidar observações das contas."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        with contextlib.closing(psycopg2.connect(dsn, connect_timeout=5,
                options='-c default_transaction_read_only=on -c statement_timeout=5000')) as con:
            con.set_session(readonly=True, autocommit=False)
            try:
                with con.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute('SELECT snapshot_id, tenant_id, source_sha256, observed_at, payload '
                                'FROM hermes_ops.batch_snapshots WHERE tenant_id=%s '
                                'ORDER BY observed_at DESC, snapshot_id DESC LIMIT 1', ('thunder',))
                    linha = cur.fetchone()
            finally:
                con.rollback()
        if linha is None:
            return {'available': False}
        data, payload = linha['observed_at'], linha['payload']
        if (linha['tenant_id'] != 'thunder' or type(linha['snapshot_id']) is not int
                or linha['snapshot_id'] <= 0 or not isinstance(data, dt.datetime)
                or data.utcoffset() is None
                or not re.fullmatch(r'[a-f0-9]{64}', linha['source_sha256'])
                or type(payload) is not dict or type(payload['items']) is not list
                or not 1 <= len(payload['items']) <= 500
                or type(payload['total']) is not int or payload['total'] != len(payload['items'])
                or type(payload['counts']) is not dict):
            raise ValueError('Inventário inválido')
        itens, indices, contagens = [], set(), {}
        for row in payload['items']:
            indice, slug, categoria = row['source_index'], row['account_slug'], row['category']
            motivos, ids = row['reason_codes'], row['ids']
            if (type(indice) is not int or not 1 <= indice <= 500 or indice in indices
                    or type(slug) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,99}', slug)
                    or categoria not in ABM_CATEGORIAS
                    or type(motivos) is not list or len(motivos) > 30
                    or any(type(v) is not str or not re.fullmatch(r'[a-z_]{1,64}', v) for v in motivos)
                    or type(ids) is not dict or not ids.keys() <= {'company', 'contact', 'deal', 'note', 'task'}
                    or any(type(v) is not list or len(v) > 40 or
                           any(type(x) not in (str, int) or not re.fullmatch(r'[0-9]{1,32}', str(x)) for x in v)
                           for v in ids.values())):
                raise ValueError('Item inválido')
            indices.add(indice)
            contagens[categoria] = contagens.get(categoria, 0) + 1
            itens.append(dict(source_index=indice, account_slug=slug, category=categoria,
                reason_codes=[v if v in ABM_MOTIVOS_LOTE else 'unrecognized_reason' for v in motivos],
                ids={k: [str(x) for x in v] for k, v in ids.items()}))
        if (payload['counts'] != contagens or
                any(type(v) is not int for v in payload['counts'].values())):
            raise ValueError('Contagens divergentes')
        return dict(available=True, snapshot_id=linha['snapshot_id'], source_sha256=linha['source_sha256'],
                    observed_at=data.astimezone(dt.timezone.utc).isoformat(), total=len(itens),
                    counts=contagens, items=itens)
    except Exception:
        return {'available': False}


def abm_observacao(linha):
    """Projeta somente metadados tipados; nunca abre recibos ou inclui corpos."""
    def texto(valor, padrao):
        return type(valor) is str and re.fullmatch(padrao, valor) is not None

    ids, motivos, data = linha['ids'], linha['reason_codes'], linha['observed_at']
    if (linha['tenant_id'] != 'thunder' or linha['portal_id'] != '51412240'
            or type(linha['id']) is not int or linha['id'] <= 0
            or not texto(linha['account_slug'], r'[a-z0-9][a-z0-9-]{0,99}')
            or linha['status'] not in ABM_STATUS
            or not isinstance(data, dt.datetime) or data.utcoffset() is None
            or (linha['deal_id'] is not None and not texto(linha['deal_id'], r'[0-9]{1,32}'))
            or type(ids) is not dict or set(ids) != {'company', 'contact', 'deal', 'note', 'task'}
            or any(not texto(v, r'[0-9]{1,32}') for v in ids.values())
            or ids['deal'] != linha['deal_id']
            or type(motivos) is not list or len(motivos) > len(ABM_MOTIVOS)
            or any(type(v) is not str or v not in ABM_MOTIVOS for v in motivos)):
        raise ValueError('Observação ABM inválida')
    return dict(id=linha['id'], account_slug=linha['account_slug'], status=linha['status'],
                observed_at=data.astimezone(dt.timezone.utc).isoformat(),
                deal_id=linha['deal_id'], ids={k: ids[k] for k in ('company', 'contact', 'deal', 'note', 'task')},
                reason_codes=motivos,
                hubspot_url='https://app.hubspot.com/contacts/51412240/record/0-3/' + linha['deal_id'])


def abm_status():
    """Snapshot PostgreSQL dedicado, read-only; não usa o fluxo de aceites."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        dsn = os.environ.get('THUNDER_DB_URL', '').strip()
        if not dsn:
            raise ValueError('Banco ABM ausente')
        with contextlib.closing(psycopg2.connect(dsn, connect_timeout=5,
                options='-c default_transaction_read_only=on -c statement_timeout=5000')) as con:
            con.set_session(readonly=True, autocommit=False)
            try:
                with con.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute('SELECT id, tenant_id, portal_id, account_slug, status, '
                                'observed_at, deal_id, ids, reason_codes '
                                'FROM hermes_ops.account_status WHERE tenant_id=%s '
                                'ORDER BY account_slug LIMIT 1001', ('thunder',))
                    linhas = cur.fetchall()
                    if len(linhas) > 1000:
                        raise ValueError('Limite ABM excedido')
                    contas = [abm_observacao(linha) for linha in linhas]
            finally:
                con.rollback()
        return dict(accounts=contas, batch=abm_lote(dsn), consulted_at=agora().isoformat(), counts=dict(
            total=len(contas), verified=sum(c['status'] == 'VERIFIED' for c in contas),
            reconciliation=sum(c['status'] == 'NEEDS_RECONCILIATION' for c in contas),
            unverified=sum(c['status'] == 'UNVERIFIED' for c in contas)))
    except Exception:
        raise Erro('ABM indisponível. Tente atualizar; se persistir, confira a conexão e a view do banco.', 503) from None


class Banco:
    def __init__(self, raiz, arquivo, *, initialize=True):
        self.raiz = Path(raiz).resolve()
        if not arquivo or not str(arquivo).strip():
            raise Erro('PAINEL_DB é obrigatório')
        self.arquivo = Path(arquivo).expanduser().resolve()
        if self.arquivo.is_relative_to(self.raiz) or self.arquivo.is_relative_to(CODE_ROOT):
            raise Erro('PAINEL_DB deve ficar fora do brain e do código')
        if not initialize:
            if not self.arquivo.is_file():raise Erro('Banco existente obrigatório')
            with contextlib.closing(sqlite3.connect(self.arquivo.as_uri()+'?mode=ro', uri=True)) as con:
                for nome in COLECOES:con.execute(f'SELECT id,dados,atualizado FROM {nome} LIMIT 0')
            return
        self.arquivo.parent.mkdir(parents=True, exist_ok=True)
        with self.conexao() as con:
            for nome in COLECOES:
                con.execute(f'CREATE TABLE IF NOT EXISTS {nome} (id TEXT PRIMARY KEY, dados JSON NOT NULL, atualizado TEXT NOT NULL)')

    @contextlib.contextmanager
    def conexao(self):
        con = sqlite3.connect(self.arquivo, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

    def caminho(self, relativo):
        p = self.raiz / relativo
        if p.resolve() != p or not p.is_relative_to(self.raiz):
            raise Erro('Caminho externo ou link simbólico recusado')
        return p

    @contextlib.contextmanager
    def lock(self, relativo):
        p = self.caminho(relativo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('a', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def acrescentar(self, relativo, texto):
        p = self.caminho(relativo)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open('ab+') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0, os.SEEK_END)
            if f.tell() and not texto.startswith('\n'):
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b'\n':
                    f.write(b'\n')
            f.write(texto.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())

    def evento(self, tipo, **campos):
        ev = dict(ts=agora().isoformat(timespec='seconds'), tipo=tipo, agente='painel', **campos)
        self.acrescentar('_log/atividade.jsonl', json.dumps(ev, ensure_ascii=False) + '\n')

    @staticmethod
    def colecao(nome):
        if nome not in COLECOES:
            raise Erro('Coleção não encontrada', 404)

    def ler(self, con, nome, id):
        self.colecao(nome)
        linha = con.execute(f'SELECT dados FROM {nome} WHERE id=?', (id,)).fetchone()
        return json.loads(linha[0]) if linha else None

    def gravar(self, con, nome, id, dados):
        dados = {k: v for k, v in dados.items() if k != 'id'}
        dados['atualizado'] = agora().isoformat(timespec='seconds')
        con.execute(f'INSERT INTO {nome} VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET dados=excluded.dados, atualizado=excluded.atualizado',
                    (id, json.dumps(dados, ensure_ascii=False, allow_nan=False), dados['atualizado']))
        return dict(dados, id=id)

    def conta(self, slug, obrigatoria=False):
        if not slug and not obrigatoria:
            return None
        if not isinstance(slug, str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
            raise Erro('conta inválida')
        p = self.caminho('contas/' + slug)
        if not p.is_dir():
            raise Erro('pasta da conta ausente')
        return p

    @staticmethod
    def campo(dados, nome, obrigatorio=True):
        valor = dados.get(nome, '')
        if not isinstance(valor, str) or (obrigatorio and not valor.strip()):
            raise Erro(nome + ' obrigatório e deve ser texto')
        if any(c in valor for c in ('|', '\n', '\r', '\x00')):
            raise Erro(nome + ' contém separador inválido')
        return valor.strip()

    def linhas(self, fila):
        p = self.caminho(fila)
        if not p.exists():
            return []
        saida, comentario = [], False
        for linha in p.read_text(encoding='utf-8').splitlines():
            if '<!--' in linha:
                comentario = True
            ignorar = comentario
            if '-->' in linha:
                comentario = False
            if ignorar or not linha.startswith('|'):
                continue
            campos = [c.strip() for c in linha.strip().strip('|').split('|')]
            if len(campos) == 6:
                saida.append(campos)
        return saida

    @staticmethod
    def fila(tipo):
        return 'fila-envios.md' if tipo == 'dm' else 'fila-comentarios.md'

    @staticmethod
    def lock_consumidor(tipo):
        return '_log/.unipile-enviar.lock' if tipo == 'dm' else '_log/.unipile-comentar.lock'

    def status_fila(self, dados):
        if not dados.get('fila_ref'):
            return None
        for c in reversed(self.linhas(self.fila(dados.get('tipo')))):
            if c[4] == dados['fila_ref']:
                return c[5]
        return None

    def reconciliar(self, dados):
        st = self.status_fila(dados)
        if st and st.startswith(('enviado ', 'comentado ')):
            dados.update(status='enviado', nota=st)
        elif st and st.startswith('erro:'):
            dados.update(status='erro', nota=st)
        return dados

    def toque_existente(self, dados, ref):
        try:
            toques.verificar()
            tipo = dados.get('tipo')
            perfil = dados.get('perfil_url') or toques.resolver_perfil(
                dados.get('pessoa', ''), conta=dados.get('conta', ''), raiz=self.raiz)
            alvo = ref if tipo == 'dm' else dados.get('post_url', '')
            if not alvo: return None
            return toques.ja_existe('linkedin', 'mensagem' if tipo == 'dm' else 'comentario', perfil, alvo)
        except (toques.ErroBanco, ValueError):
            # Checagem consultiva. O consumidor exige banco e identidade antes do POST.
            return None

    def bloquear_toque(self, dados, existente):
        dados.update(status='erro', nota=f"já existe toque igual (id {existente['id']})")
        dados.pop('_fila_pendente', None)
        return dados

    def aprovar(self, con, id, dados, anterior):
        tipo = dados.get('tipo')
        if tipo not in ('dm', 'comentario'):
            self.evento('aprovacao', conta=dados.get('conta', ''), texto='aprovado no painel: ' + str(dados.get('titulo', id)), ref=id)
            return dados
        with self.lock(self.lock_consumidor(tipo)):
            existente = self.toque_existente(dados, (anterior or {}).get('fila_ref', ''))
            if existente:
                return self.bloquear_toque(dados, existente)
            # Uma referência existente é autoridade, inclusive durante recuperação.
            if anterior and anterior.get('fila_ref'):
                st = self.status_fila(anterior)
                if st and (st == 'aprovado' or st.startswith(('enviado ', 'comentado '))):
                    recuperado = self.reconciliar(dict(anterior, status='na-fila'))
                    recuperado.pop('_fila_pendente', None)
                    self.evento_aprovacao(id, recuperado)
                    return recuperado
                if st and 'resultado pendente' in st:
                    raise Erro('Resultado pendente de reconciliação; não reaprovar')
            slug = self.campo(dados, 'conta', obrigatorio=(tipo == 'dm'))
            pasta = self.conta(slug, obrigatoria=(tipo == 'dm'))
            pessoa = self.campo(dados, 'pessoa', obrigatorio=(tipo == 'dm'))
            if not pessoa:
                pessoa = 'Contato'
            url = self.campo(dados, 'perfil_url' if tipo == 'dm' else 'post_url')
            u = urlsplit(url)
            if u.scheme != 'https' or u.netloc not in ('linkedin.com', 'www.linkedin.com'):
                raise Erro('URL LinkedIn inválida')
            if tipo == 'dm' and not re.search(r'^/in/[^/]+', u.path):
                raise Erro('perfil sem identificador')
            if tipo == 'comentario' and not re.search(r'(?:activity-|urn:li:activity:)\d+', unquote(url)):
                raise Erro('post sem identificador de atividade')
            texto = dados.get('texto')
            if not isinstance(texto, str) or not texto.strip():
                raise Erro('texto obrigatório')
            texto = texto.strip()
            # Os consumidores param no próximo título, por isso não aceitar truncamento.
            if re.search(r'^#{1,3}(?:\s|$)', texto, re.M) or '\x00' in texto:
                raise Erro('texto não pode conter cabeçalho Markdown')
            arquivo = f'contas/{slug}/campanha.md' if pasta else f'_entrada/claude/painel/{id}.md'
            self.caminho(arquivo)
            self.caminho(self.fila(tipo))
            self.caminho('_log/atividade.jsonl')
            # Intenção gravada sem linha na fila pode ser retomada com a mesma seção.
            mesmo_toque = anterior and all(dados.get(k) == anterior.get(k) for k in
                ('tipo', 'conta', 'pessoa', 'perfil_url', 'post_url', 'texto'))
            recuperar = anterior and anterior.get('fila_ref') and (anterior.get('_fila_pendente') or mesmo_toque)
            if recuperar:
                dados = dict(anterior)
            else:
                with self.lock(arquivo + '.lock'):
                    p = self.caminho(arquivo)
                    conteudo = p.read_text(encoding='utf-8') if p.exists() else ''
                    versoes = [int(n) for n in re.findall(r'^### Painel ' + re.escape(id) + r' v(\d+) \(', conteudo, re.M)]
                    titulo = f'Painel {id} v{max(versoes, default=0) + 1} ({agora():%Y-%m-%d})'
                    self.acrescentar(arquivo, f'\n### {titulo}\n{texto}\n')
                dados.update(fila_ref=arquivo + '#' + titulo, _fila_pendente=True)
                self.gravar(con, 'itens', id, dados)
                con.commit()  # Referência durável antes da linha executável.
            # Os valores da intenção são imutáveis no retry.
            slug = dados.get('conta', '')
            pessoa = dados.get('pessoa') or 'Contato'
            url = dados.get('perfil_url' if tipo == 'dm' else 'post_url')
            existente = self.toque_existente(dados, dados['fila_ref'])
            if existente:
                return self.bloquear_toque(dados, existente)
            self.acrescentar(self.fila(tipo), f'| {agora():%Y-%m-%d %H:%M} | {slug} | {pessoa} | {url} | {dados["fila_ref"]} | aprovado |\n')
            dados.update(status='na-fila', nota='Na fila. Sai em até 10 min.')
            dados.pop('_fila_pendente', None)
            self.gravar(con, 'itens', id, dados)
            con.commit()  # Falha no log não desfaz a fila.
            self.evento_aprovacao(id, dados)
            return dados

    def evento_aprovacao(self, id, dados):
        # Recupera também o log se houve queda depois da escrita da fila.
        ref = dados['fila_ref']
        p = self.caminho('_log/atividade.jsonl')
        if p.exists():
            for linha in p.read_text(encoding='utf-8').splitlines():
                try:
                    ev = json.loads(linha)
                    if isinstance(ev, dict) and ev.get('tipo') == 'aprovacao' and ev.get('ref') == ref:
                        return
                except ValueError:
                    pass
        self.evento('aprovacao', conta=dados.get('conta', ''), texto='aprovado no painel: ' + str(dados.get('titulo', id)), ref=ref)

    @staticmethod
    def motivo_descarte(dados):
        if dados.get('fila_ref') or dados.get('_fila_pendente'):
            return 'Item já encaminhado ou com aprovação em recuperação; confira a fila.'
        if any(dados.get(k) for k in ('needs_reconciliation', 'reconciliation_required',
                'uncertain', 'receipt_ref', 'effect_id', 'job_id', 'dispatch_started_at')):
            return 'Item com execução ou reconciliação pendente; confira o resultado antes de decidir.'
        if dados.get('status', 'pendente') != 'pendente':
            return 'Somente cartões pendentes sem encaminhamento podem ser descartados.'
        return ''

    def descartar_pendente(self, id, actor):
        if actor != 'ygor':
            raise Erro('Somente Ygor pode descartar cartões.', 403)
        id = identificador(id)
        with self.lock('_log/.painel.lock'), self.conexao() as con:
            con.execute('BEGIN IMMEDIATE')
            dados = self.ler(con, 'itens', id)
            if dados is None:
                raise Erro('Item não encontrado', 404)
            if dados.get('status') == 'descartado' and dados.get('_descarte'):
                return dict(dados, id=id)
            motivo = self.motivo_descarte(dados)
            if motivo:
                raise Erro(motivo, 409)
            quando = agora().isoformat(timespec='seconds')
            dados.update(status='descartado', decisao={'acao': 'descartar', 'quando': quando},
                         _descarte={'ator': actor, 'quando': quando, 'origem': 'painel'})
            return self.gravar(con, 'itens', id, dados)

    def salvar(self, nome, campos, id=None, criar=False, importar=False, upsert=False):
        self.colecao(nome)
        objeto(campos)
        id = identificador(id or campos.get('id') or secrets.token_hex(5))
        if any(k.startswith('_') or k == 'fila_ref' for k in campos):
            if not importar:
                raise Erro('Campos internos são geridos pelo servidor')
            campos = {k: v for k, v in campos.items() if not k.startswith('_') and k != 'fila_ref'}
        with self.lock('_log/.painel.lock'), self.conexao() as con:
            anterior = self.ler(con, nome, id)
            if nome == 'itens' and anterior and anterior.get('_descarte'):
                raise Erro('Cartão descartado é terminal; crie outro cartão para uma nova decisão.', 409)
            if criar and anterior:
                raise Erro('id já existe')
            if not criar and not importar and not upsert and anterior is None:
                raise Erro('Item não encontrado', 404)
            if importar and anterior and anterior.get('status', 'pendente') not in ('pendente', 'copiar'):
                return dict(anterior, id=id)
            if anterior and anterior.get('fila_ref') and 'tipo' in campos and campos['tipo'] != anterior.get('tipo'):
                raise Erro('Tipo de item com fila não pode mudar')
            if anterior and anterior.get('_fila_pendente'):
                protegidos = ('tipo', 'conta', 'pessoa', 'perfil_url', 'post_url', 'texto')
                if any(k in campos and campos[k] != anterior.get(k) for k in protegidos):
                    raise Erro('Aprovação pendente de recuperação; repita sem alterar o cartão')
                if campos.get('status', 'aprovado') != 'aprovado':
                    raise Erro('Aprovação pendente de recuperação')
            dados = dict(anterior or {}, **{k: v for k, v in campos.items() if k != 'id'})
            if 'conta' in dados:
                self.conta(dados['conta'])
            # Evita que edição posterior mude destinatário/referência de envio em curso.
            if anterior and anterior.get('fila_ref') and anterior.get('status') in ('na-fila', 'enviado'):
                protegidos = ('tipo', 'conta', 'pessoa', 'perfil_url', 'post_url', 'texto')
                if any(k in campos and campos[k] != anterior.get(k) for k in protegidos):
                    raise Erro('Item já encaminhado; crie outro cartão para novo texto')
                if 'status' in campos and campos['status'] not in (anterior['status'], 'aprovado'):
                    raise Erro('Status da fila é gerido pelo servidor')
            if nome == 'itens' and not importar and not upsert and campos.get('status') == 'aprovado':
                if dados.get('tipo') in ('dm', 'comentario') or not anterior or anterior.get('status') != 'aprovado':
                    dados = self.aprovar(con, id, dados, anterior)
            return self.gravar(con, nome, id, dados)

    def listar(self, nome, reconciliar=False):
        self.colecao(nome)
        with (self.lock('_log/.painel.lock') if reconciliar else contextlib.nullcontext()), self.conexao() as con:
            saida = []
            for id, bruto in con.execute(f'SELECT id,dados FROM {nome} ORDER BY id').fetchall():
                dados = json.loads(bruto)
                if reconciliar and nome == 'itens' and dados.get('status') == 'na-fila' and dados.get('fila_ref'):
                    with self.lock(self.lock_consumidor(dados.get('tipo'))):
                        novos = self.reconciliar(dict(dados))
                    if novos != dados:
                        self.gravar(con, nome, id, novos)
                        dados = novos
                saida.append(dict(dados, id=id))
            return saida

    def nomes_angulos(self):
        arquivo = self.caminho('conhecimento/prospeccao/angulos.md')
        if not arquivo.is_file():
            return {}
        nomes = {}
        try:
            for linha in arquivo.read_text(encoding='utf-8').splitlines():
                m = re.match(r'^##\s+(A\d{2}-[a-z0-9-]+)\s*\|', linha, re.I)
                if m:
                    codigo = m.group(1)
                    nomes[codigo.casefold()] = codigo.split('-', 1)[1].replace('-', ' ').title()
        except (OSError, UnicodeError):
            return {}
        return nomes

    def listar_itens(self):
        nomes = self.nomes_angulos()
        saida = self.listar('itens')
        for item in saida:
            item['descarte_bloqueio'] = self.motivo_descarte(item)
            angulo = str(item.get('angulo') or '')
            if angulo:
                item['angulo_nome'] = nomes.get(angulo.casefold(), angulo)
            if isinstance(item.get('variantes'), list):
                item['variantes'] = [dict(v, angulo_nome=nomes.get(str(v.get('angulo', '')).casefold(), v.get('angulo', '')))
                                     if isinstance(v, dict) else v for v in item['variantes']]
        return saida

    def fonte_sugestoes(self):
        pasta = self.caminho('_entrada/claude/reflexao')
        try:
            arquivos = sorted(pasta.glob('????-??-??.json'), reverse=True)
            if not arquivos:
                return None
            documento = json.loads(arquivos[0].read_text(encoding='utf-8'))
            if (not isinstance(documento, dict) or documento.get('version') != 1 or
                    not isinstance(documento.get('source_ref'), str) or
                    not isinstance(documento.get('suggestions'), list)):
                return None
            campos = ('id', 'hypothesis', 'evidence', 'recommendation', 'limitation', 'source_ref')
            sugestoes = []
            for sugestao in documento['suggestions']:
                if (not isinstance(sugestao, dict) or
                        not re.fullmatch(r'sug-[a-f0-9]{16}', str(sugestao.get('id', ''))) or
                        any(not isinstance(sugestao.get(c), str) or not sugestao[c].strip() for c in campos) or
                        not isinstance(sugestao.get('sample'), int) or
                        isinstance(sugestao.get('response_rate'), bool) or
                        not isinstance(sugestao.get('response_rate'), (int, float))):
                    return None
                sugestoes.append(dict(sugestao))
            return sugestoes
        except (OSError, UnicodeError, ValueError):
            return None

    def sugestoes(self):
        fonte = self.fonte_sugestoes()
        if fonte is None:
            return {'items': [], 'source_status': 'indisponivel'}
        decisoes = {d['id']: d for d in self.listar('sugestoes', reconciliar=False)}
        pendentes = [s for s in fonte if s['sample'] >= 20 and s['id'] not in decisoes]
        return {'items': pendentes[:1], 'source_status': 'disponivel'}

    def decidir_sugestao(self, id, corpo):
        identificador(id)
        objeto(corpo)
        if set(corpo) != {'status'} or corpo.get('status') not in ('testando', 'ignorada'):
            raise Erro('status deve ser testando ou ignorada')
        fonte = self.fonte_sugestoes()
        sugestao = next((s for s in (fonte or []) if s['id'] == id and s['sample'] >= 20), None)
        if not sugestao:
            raise Erro('Sugestão não encontrada', 404)
        with self.lock('_log/.painel.lock'), self.conexao() as con:
            anterior = self.ler(con, 'sugestoes', id)
            dados = dict(anterior or {}, status=corpo['status'], suggestion_id=id,
                         source_ref=sugestao['source_ref'])
            if corpo['status'] == 'testando':
                item_id = 'pedido-' + id
                dados['internal_item_id'] = item_id
                if self.ler(con, 'itens', item_id) is None:
                    self.gravar(con, 'itens', item_id, {
                        'tipo': 'pedido',
                        'titulo': 'Testar aprendizado',
                        'texto': sugestao['recommendation'],
                        'acao_aprovada': sugestao['recommendation'],
                        'status': 'pendente',
                        'origem': 'sugestao',
                        'sugestao_id': id,
                        'criado': agora().isoformat(timespec='seconds'),
                    })
            else:
                dados.pop('internal_item_id', None)
            return self.gravar(con, 'sugestoes', id, dados)

    @staticmethod
    def _pontuar(base, data, referencia, meia_vida=7):
        idade = max(0.0, (referencia - instante(data)).total_seconds() / 86400)
        return base * (0.5 ** int(idade // max(1, meia_vida)))

    # Sinal com peso (1 a 3) e validade em dias segue conhecimento/prospeccao/sinais.md.
    # Sem peso: comportamento antigo (base 2, meia-vida 7 dias). Peso 0 ou vencido: não pontua.
    PESO_SINAL = {1: 1, 2: 2, 3: 4}

    @classmethod
    def _peso_sinal(cls, ev, referencia):
        peso = ev.get('peso')
        if peso is None:
            return 2, 7
        try:
            peso = int(peso)
        except (TypeError, ValueError):
            return 2, 7
        if peso <= 0:
            return 0, 7
        base = cls.PESO_SINAL.get(min(peso, 3), 1)
        try:
            validade = int(ev.get('validade_dias') or 0)
        except (TypeError, ValueError):
            validade = 0
        if validade <= 0:
            return base, 7
        idade = (referencia - instante(ev['ts'])).total_seconds() / 86400
        if idade > validade:
            return 0, 7
        return base, max(7, validade // 3)

    def quentes(self):
        referencia = agora()
        grupos = {}

        def adicionar(conta, pessoa_chave, pessoa_nome, base, data, rotulo, detalhe='', meia_vida=7):
            chave = ('conta', conta) if conta else ('pessoa', pessoa_chave or pessoa_nome)
            if not chave[1]:
                return
            grupo = grupos.setdefault(chave, dict(conta=conta or '', pessoa_chave=pessoa_chave or '',
                pessoa_nome=pessoa_nome or '', pontuacao=0.0, razoes=[]))
            grupo['pontuacao'] += self._pontuar(base, data, referencia, meia_vida)
            grupo['razoes'].append((instante(data), rotulo, detalhe))
            if pessoa_nome and not grupo['pessoa_nome']:
                grupo['pessoa_nome'] = pessoa_nome

        with toques._banco() as (con, _):
            c = con.cursor()
            c.execute('SELECT * FROM toques')
            todos_toques = toques._linhas(c)
            contas_por_pessoa = {}
            for toque in sorted(todos_toques,
                                key=lambda t: instante(t.get('atualizado_em') or t['criado_em'])):
                if toque.get('conta'):
                    contas_por_pessoa[(toque.get('canal'), toque.get('pessoa_chave'))] = toque['conta']
            c.execute('SELECT * FROM respostas')
            respostas = toques._linhas(c)
            for r in respostas:
                conta = r.get('conta') or contas_por_pessoa.get((r.get('canal'), r.get('pessoa_chave')), '')
                adicionar(conta, r['pessoa_chave'], r.get('pessoa_nome'), 5,
                          r['criado_em'], 'respondeu')
            c.execute('SELECT a.*,t.conta,t.pessoa_nome FROM aceites a LEFT JOIN toques t ON t.id=a.toque_id')
            for a in toques._linhas(c):
                adicionar(a.get('conta'), a['pessoa_chave'], a.get('pessoa_nome'), 3,
                          a['aceito_em'], 'aceitou o convite')
            for t in todos_toques:
                if t.get('status') != 'feito' or t.get('acao') not in ('comentario', 'mensagem'):
                    continue
                data = instante(t.get('atualizado_em') or t['criado_em'])
                retornou = any(r['canal'] == t['canal'] and r['pessoa_chave'] == t['pessoa_chave']
                               and instante(r['criado_em']) > data for r in respostas)
                if not retornou:
                    adicionar(t.get('conta'), t['pessoa_chave'], t.get('pessoa_nome'), 1, data,
                              'tem retorno pendente')
        log = self.caminho('_log/atividade.jsonl')
        if log.is_file():
            try:
                linhas = log.read_text(encoding='utf-8').splitlines()
            except (OSError, UnicodeError):
                linhas = []
            for linha in linhas:
                try:
                    ev = json.loads(linha)
                    if isinstance(ev, dict) and ev.get('tipo') == 'sinal' and ev.get('ts'):
                        base, meia_vida = self._peso_sinal(ev, referencia)
                        if base > 0:
                            adicionar(ev.get('conta'), ev.get('pessoa_chave') or ev.get('pessoa'),
                                      ev.get('pessoa'), base, ev['ts'], 'tem sinal recente', ev.get('resumo', ''),
                                      meia_vida=meia_vida)
                except (ValueError, TypeError):
                    continue
        saida = []
        for grupo in grupos.values():
            conta = grupo['conta']
            empresa = ''
            if conta:
                ficha = self.caminho(f'contas/{conta}/ficha.md')
                try:
                    texto = ficha.read_text(encoding='utf-8') if ficha.is_file() else ''
                except (OSError, UnicodeError):
                    texto = ''
                empresa = self.extrair_ficha(texto, conta)[0]
            recentes = sorted(grupo.pop('razoes'), reverse=True)
            motivos = []
            for _, rotulo, detalhe in recentes:
                frase = detalhe if rotulo == 'tem sinal recente' and detalhe else rotulo
                if frase and frase not in motivos:
                    motivos.append(frase)
            nome = empresa or grupo['pessoa_nome'] or grupo['pessoa_chave']
            saida.append(dict(grupo, nome=nome, empresa=empresa or 'Sem conta',
                              pontuacao=round(grupo['pontuacao'], 6), motivo=' e '.join(motivos[:2])))
        return sorted(saida, key=lambda q: (-q['pontuacao'], q['nome'].casefold()))[:5]

    def materializar_aceites(self):
        # O lock é compartilhado com salvar: a leitura e a criação são indivisíveis.
        with toques._banco() as (con, _):
            c = con.cursor()
            c.execute('SELECT a.*, t.pessoa_nome, t.conta FROM aceites a '
                      'LEFT JOIN toques t ON t.id=a.toque_id ORDER BY a.aceito_em, a.pessoa_chave')
            aceites = toques._linhas(c)
        with self.lock('_log/.painel.lock'), self.conexao() as con:
            conhecidos = set()
            for (bruto,) in con.execute('SELECT dados FROM itens').fetchall():
                item = json.loads(bruto)
                for chave in (item.get('pessoa_chave'), item.get('perfil_url')):
                    if chave:
                        conhecidos.add(chave)
                        try:
                            conhecidos.add(toques.normalizar_pessoa('linkedin', chave))
                        except ValueError:
                            pass
            for aceite in aceites:
                chave = aceite['pessoa_chave']
                if chave in conhecidos:
                    continue
                pessoa = aceite.get('pessoa_nome') or chave
                conta = aceite.get('conta') or ''
                conta_nome = conta.replace('-', ' ').title() or 'Sem conta'
                if re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', conta):
                    try:
                        ficha = self.caminho('contas/' + conta + '/ficha.md')
                        if ficha.is_file():
                            conta_nome = self.extrair_ficha(ficha.read_text(encoding='utf-8'), conta)[0]
                    except (Erro, OSError, UnicodeError):
                        pass  # Cartão continua disponível mesmo sem ficha legível e segura.
                data = instante(aceite['aceito_em']).astimezone(TZ).strftime('%d/%m/%Y')
                id = 'aceite-' + hashlib.sha256(chave.encode()).hexdigest()[:32]
                # Uma colisão com id importado nunca substitui conteúdo existente.
                if self.ler(con, 'itens', id) is not None:
                    continue
                self.gravar(con, 'itens', id, dict(tipo='dm', origem='aceite', status='pendente',
                    titulo=f'Primeira mensagem para {pessoa} ({conta_nome})', conta_nome=conta_nome,
                    porque=f'Aceitou seu convite em {data}', pessoa=pessoa, conta=conta,
                    pessoa_chave=chave, perfil_url=chave if aceite['canal'] == 'linkedin' else '',
                    contexto='', texto='', versoes=[], angulo=''))
                conhecidos.add(chave)

    @staticmethod
    def respostas():
        with toques._banco() as (con, _):
            c = con.cursor()
            c.execute('SELECT * FROM respostas')
            respostas = toques._linhas(c)
            c.execute("SELECT canal,pessoa_chave,COALESCE(atualizado_em,criado_em) AS data "
                      "FROM toques WHERE status='feito' AND acao IN ('mensagem','email','whatsapp')")
            retornos = {}
            for toque in toques._linhas(c):
                chave = toque['canal'], toque['pessoa_chave']
                data = instante(toque['data'])
                if chave not in retornos or data > retornos[chave]:
                    retornos[chave] = data
        pendentes = []
        for resposta in respostas:
            retorno = retornos.get((resposta['canal'], resposta['pessoa_chave']))
            if retorno and retorno > instante(resposta['criado_em']):
                continue
            resposta = serializar_linha(resposta)
            resposta['pessoa'] = resposta.get('pessoa_nome') or resposta['pessoa_chave']
            resposta['perfil_url'] = resposta['pessoa_chave'] if resposta['canal'] == 'linkedin' else ''
            pendentes.append(resposta)
        return sorted(pendentes, key=lambda r: (instante(r['criado_em']), r['id']))

    @staticmethod
    def classificar_resposta(id, campos):
        if not re.fullmatch(r'[1-9][0-9]{0,17}', id):
            raise Erro('id da resposta inválido')
        if set(campos) != {'classificacao'} or not isinstance(campos['classificacao'], str) or campos['classificacao'] not in toques.CLASSIFICACOES:
            raise Erro('Informe apenas uma classificação válida')
        with toques._banco() as (con, m):
            c = con.cursor()
            c.execute('UPDATE respostas SET classificacao=' + m + ' WHERE id=' + m,
                      (campos['classificacao'], int(id)))
            if c.rowcount != 1:
                # ErroBanco encapsula exceções internas; 404 fica fora desse contexto.
                resposta = None
            else:
                c.execute('SELECT * FROM respostas WHERE id=' + m, (int(id),))
                resposta = toques._linhas(c)[0]
        if resposta is None:
            raise Erro('Resposta não encontrada', 404)
        return serializar_linha(resposta)

    def ficha_conta(self, slug, pessoa_chave=''):
        if slug and not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', slug):
            raise Erro('conta inválida')
        if not isinstance(pessoa_chave, str) or len(pessoa_chave) > 1000 or any(ord(c) < 32 for c in pessoa_chave):
            raise Erro('pessoa inválida')
        if not slug and not pessoa_chave.strip():
            raise Erro('Informe a conta ou a pessoa')
        ficha = ''
        if slug:
            # Valida inclusive a pasta quando ficha.md não existe.
            self.caminho('contas/' + slug)
            arquivo = self.caminho('contas/' + slug + '/ficha.md')
            if arquivo.is_file():
                ficha = arquivo.read_text(encoding='utf-8')
        nome, descricao, fatos, proximo, pessoas_ficha = self.extrair_ficha(ficha, slug)
        eventos, pessoas = [], dict(pessoas_ficha)
        with toques._banco() as (con, m):
            c = con.cursor()
            campo, valor = ('conta', slug) if slug else ('pessoa_chave', pessoa_chave)
            for tabela in ('toques', 'respostas'):
                c.execute('SELECT * FROM ' + tabela + ' WHERE ' + campo + '=' + m, (valor,))
                for linha in toques._linhas(c):
                    pessoa = linha.get('pessoa_nome') or linha['pessoa_chave']
                    pessoas[linha['pessoa_chave']] = pessoa
                    eventos.append(dict(id=linha['id'], tipo=linha['acao'] if tabela == 'toques' else 'resposta',
                        data=instante(linha.get('atualizado_em') or linha['criado_em']).isoformat(),
                        pessoa=pessoa, pessoa_chave=linha['pessoa_chave'], canal=linha['canal'],
                        texto=(linha.get('detalhe') or linha.get('alvo') or '') if tabela == 'toques' else (linha.get('resumo') or ''),
                        status=linha.get('status') or 'recebida'))
        eventos.sort(key=lambda ev: (instante(ev['data']), ev['id']), reverse=True)
        sequencia = self.carregar_sequencia(slug, eventos) if slug else []
        comite = self.extrair_comite(ficha)
        por_pessoa = {}
        for evento in eventos:
            if evento['tipo'] == 'resposta' or evento['status'] == 'feito':
                por_pessoa.setdefault(evento['pessoa_chave'], evento)
        for pessoa in comite:
            ultimo = por_pessoa.get(pessoa['pessoa_chave'])
            pessoa['status'] = ('resposta recebida' if ultimo and ultimo['tipo'] == 'resposta'
                                else 'toque feito' if ultimo else 'sem toque')
            pessoa['ultimo'] = ultimo['data'] if ultimo else ''
        candidatos = sorted((p for p in comite if p['status'] == 'sem toque'),
                            key=lambda p: (p['papel'] != 'decisor', comite.index(p)))
        brief = self.carregar_brief(slug) if slug else None
        return dict(nome=nome or (pessoas.get(pessoa_chave) if pessoa_chave else '') or 'Pessoa sem conta',
                    descricao=descricao, fatos=fatos, pessoas=[dict(pessoa=v, pessoa_chave=k) for k,v in pessoas.items()],
                    toques=eventos, proximo_passo=proximo, sequencia=sequencia,
                    proximo_na_sequencia=next((s for s in sequencia if s.get('status') != 'feito'), None),
                    comite=comite, abordar_em_seguida=candidatos[0] if candidatos else None, brief=brief)

    def carregar_sequencia(self, slug, eventos):
        arquivo = self.caminho(f'contas/{slug}/sequencia.json')
        if not arquivo.is_file():
            return []
        try:
            bruto = json.loads(arquivo.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            return []
        if not isinstance(bruto, list):
            return []
        saida = []
        disponiveis = [e for e in reversed(eventos) if e['tipo'] != 'resposta' and e['status'] == 'feito']
        for passo in bruto:
            if not isinstance(passo, dict):
                continue
            atual = {k: passo.get(k) for k in ('dia', 'canal', 'acao', 'quando', 'status')}
            if not isinstance(atual['canal'], str) or not isinstance(atual['acao'], str):
                continue
            indice = next((i for i, e in enumerate(disponiveis)
                           if e['tipo'] == atual['acao'] and e['canal'] == atual['canal']), None)
            if indice is not None:
                atual['status'] = 'feito'
                disponiveis.pop(indice)
            saida.append(atual)
        return saida

    @staticmethod
    def extrair_comite(ficha):
        comite, ativo = [], False
        for linha in ficha.splitlines():
            if re.match(r'^##\s+', linha):
                ativo = re.sub(r'^##\s*', '', linha).strip().casefold() in ('comitê', 'comite')
                continue
            if not ativo or not linha.startswith('- '):
                continue
            partes = [p.strip() for p in linha[2:].split('|')]
            if len(partes) != 4 or partes[1].casefold() not in ('decisor', 'influenciador', 'usuário', 'usuario'):
                continue
            try:
                chave = toques.normalizar_pessoa('linkedin', partes[3])
            except ValueError:
                continue
            comite.append(dict(nome=partes[0], papel=partes[1].casefold(), cargo=partes[2],
                                url=partes[3], pessoa_chave=chave))
        return comite

    def carregar_brief(self, slug):
        pasta = self.caminho(f'contas/{slug}')
        if not pasta.is_dir():
            return None
        arquivos = sorted(pasta.glob('brief-*.md'), reverse=True)
        for arquivo in arquivos:
            if not re.fullmatch(r'brief-\d{4}-\d{2}-\d{2}\.md', arquivo.name):
                continue
            seguro = self.caminho(f'contas/{slug}/{arquivo.name}')
            try:
                return {'arquivo': arquivo.name, 'conteudo': seguro.read_text(encoding='utf-8')}
            except (OSError, UnicodeError):
                return None
        return None

    @staticmethod
    def extrair_ficha(ficha, slug):
        # Markdown é devolvido como texto. A interface escapa todo conteúdo.
        linhas = ficha.splitlines()
        if linhas and linhas[0].strip() == '---':
            fim = next((i for i in range(1, len(linhas)) if linhas[i].strip() == '---'), None)
            if fim is not None:
                linhas = linhas[fim + 1:]
        nome = next((l[2:].strip() for l in linhas if l.startswith('# ')), slug.replace('-', ' ').title())
        descricao, sobre, fatos, proximo, pessoas = [], [], [], [], {}
        secao, lendo_inicio = '', True
        for linha in linhas:
            if re.match(r'^#{1,6}\s', linha):
                secao = re.sub(r'^#+\s*', '', linha).strip().casefold()
                if linha.startswith('## '):
                    lendo_inicio = False
                continue
            if not linha.strip():
                continue
            if lendo_inicio:
                if len(descricao) < 8:
                    descricao.append(linha)
            elif secao in ('sobre', 'empresa', 'o que faz', 'fatos') and len(sobre) < 8:
                sobre.append(linha)
            if 'fonte' in linha.casefold() or secao in ('fonte', 'fontes'):
                fatos.append(linha)
            if secao.startswith('próximo passo'):
                proximo.append(linha)
            if linha.startswith('|') and secao in ('pessoas', 'decisores', 'contatos'):
                colunas = [p.strip() for p in linha.strip('|').split('|')]
                chave = next((v for v in colunas if re.match(r'https?://(?:www\.)?linkedin\.com/in/', v)), '')
                if chave and colunas[0]:
                    try:
                        pessoas[toques.normalizar_pessoa('linkedin', chave)] = colunas[0]
                    except ValueError:
                        pass
        return nome, '\n'.join(sobre + descricao), fatos, '\n'.join(proximo), pessoas

    def perguntar(self, corpo):
        objeto(corpo)
        prompt = corpo.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip() or '\x00' in prompt:
            raise Erro('prompt obrigatório')
        if 'json' in corpo and not isinstance(corpo['json'], bool):
            raise Erro('json deve ser booleano')
        id = identificador(corpo['id']) if 'id' in corpo else ''
        self.evento('pergunta', **({'ref': id} if id else {}))
        try:
            comando = shlex.split(os.environ.get('HERMES_CMD', 'hermes -z'))
            if not comando:
                raise ValueError()
            r = subprocess.run([*comando, prompt], cwd=self.raiz, capture_output=True,
                               text=True, timeout=180, check=False, shell=False)
        except subprocess.TimeoutExpired:
            raise Erro('Hermes excedeu o tempo limite de 180 s', 502) from None
        except (OSError, ValueError):
            raise Erro('Não foi possível iniciar o Hermes', 502) from None
        if r.returncode:
            raise Erro('Hermes terminou com erro', 502)
        if corpo.get('json'):
            decoder = json.JSONDecoder()
            for m in re.finditer(r'\{', r.stdout):
                try:
                    obj, _ = decoder.raw_decode(r.stdout[m.start():])
                    if isinstance(obj, dict):
                        return obj
                except ValueError:
                    pass
            raise Erro('Hermes não devolveu um objeto JSON válido', 502)
        return {'texto': r.stdout.strip()}


class Handler(BaseHTTPRequestHandler):
    server_version = 'Painel'

    def log_message(self, *_):
        pass  # Não registrar prompt, cabeçalhos ou corpos.

    def responder(self, status, valor, tipo='application/json; charset=utf-8', headers=None):
        bruto = json.dumps(valor, ensure_ascii=False).encode() if tipo.startswith('application/json') else valor.encode()
        self.send_response(status)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(bruto)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        for nome, conteudo in (headers or {}).items():
            self.send_header(nome, conteudo)
        self.end_headers()
        self.wfile.write(bruto)

    def corpo(self):
        if self.headers.get('Transfer-Encoding'):
            raise Erro('Transfer-Encoding não suportado')
        comprimentos = self.headers.get_all('Content-Length', [])
        if len(comprimentos) != 1 or not re.fullmatch(r'\d+', comprimentos[0]):
            raise Erro('Content-Length inválido')
        n = int(comprimentos[0])
        if n > LIMITE:
            raise Erro('Corpo excede 200 KB', 413)
        if self.headers.get_content_type() != 'application/json':
            raise Erro('Use Content-Type application/json')
        return objeto(carregar(self.rfile.read(n)))

    def prune_nonces(self):
        issued = getattr(self.server, 'thunder_nonces', {})
        now = time.monotonic()
        for key, value in list(issued.items()):
            if not isinstance(value, tuple) or len(value) != 2 or value[1] <= now:
                issued.pop(key, None)
        return issued

    def thunder_actor(self):
        origin = self.headers.get('Origin', '')
        nonce = self.headers.get('X-Thunder-Nonce', '')
        actor = self.headers.get('X-Authenticated-User', '')
        allowed = frozenset(filter(None, os.environ.get(
            'THUNDER_OS_ACTORS', 'ygor,hermes,claude,codex').split(',')))
        remote = self.client_address[0]
        with NONCE_LOCK:
            issued = self.prune_nonces()
            entry = issued.pop(nonce, None)
        if (remote not in {'127.0.0.1', '::1'} or origin != self.server.thunder_origin
                or not nonce or not entry or entry[0] != actor or actor not in allowed
                or self.headers.get('X-Thunder-Actor')):
            raise Erro('Autorização Thunder OS inválida', 401)
        if actor == 'hermes':
            expected = getattr(self.server, 'thunder_hermes_credential', '')
            supplied = self.headers.get('X-Thunder-Agent-Credential', '')
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                raise Erro('Credencial própria do Hermes ausente ou inválida', 401)
        return actor

    def thunder_proxy_actor(self):
        actor = self.headers.get('X-Authenticated-User', '')
        allowed = frozenset(filter(None, os.environ.get(
            'THUNDER_OS_ACTORS', 'ygor,hermes,claude,codex').split(',')))
        if self.client_address[0] not in {'127.0.0.1', '::1'} or actor not in allowed:
            raise Erro('Identidade autenticada ausente', 401)
        if actor == 'hermes':
            expected = getattr(self.server, 'thunder_hermes_credential', '')
            supplied = self.headers.get('X-Thunder-Agent-Credential', '')
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                raise Erro('Credencial própria do Hermes ausente ou inválida', 401)
        return actor

    def agenttrail_ingress_actor(self):
        actor = self.thunder_proxy_actor()
        try:
            expected = AGENTTRAIL_INGRESS_TOKEN.read_text(encoding='ascii').strip()
        except OSError:
            expected = ''
        supplied = self.headers.get('X-Thunder-Agenttrail-Ingress', '')
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            raise Erro('Ingresso Agenttrail inválido', 401)
        return actor

    def new_journey_api(self, rota):
        if self.thunder_proxy_actor() != 'ygor':
            raise Erro('Somente Ygor pode preparar uma jornada.', 403)
        if rota.query:
            raise Erro('Parâmetros de jornada inválidos.')
        wizard = getattr(self.server, 'thunder_new_journey', None)
        if wizard is None:
            raise Erro('Preparação de jornada indisponível.', 503)
        prefix = '/api/os/v1/new-journey/'
        target = rota.path[len(prefix):] if rota.path.startswith(prefix) else ''
        try:
            history_match = re.fullmatch(r'([A-Za-z0-9_-]{1,100})/history/([^/]{1,600})', target)
            if history_match:
                if self.command != 'GET':
                    raise Erro('Histórico permite somente leitura.', 405)
                source_key = unquote(history_match.group(2))
                if not re.fullmatch(r'[A-Za-z0-9:_-]{1,200}', source_key):
                    raise Erro('Vínculo de histórico inválido.', 400)
                return self.responder(200, wizard.history(history_match.group(1), source_key, 'ygor'))
            if self.command == 'GET':
                if target == 'catalog':
                    return self.responder(200, wizard.catalog())
                if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', target):
                    raise Erro('Jornada não encontrada.', 404)
                value = wizard.get(target, 'ygor')
                importer = getattr(self.server, 'thunder_new_journey_import', None)
                if importer is not None:
                    previews = [p for p in importer.repo.list_state('hubspot_preview')
                        if p.get('kind') == 'journey_import_v1' and p.get('journey_id') == target
                        and p.get('journey_version') == value.get('version')]
                    value['hubspot_import_previews'] = [importer._view(p) for p in
                        sorted(previews, key=lambda p: p.get('updated_at', ''), reverse=True)[:20]]
                return self.responder(200, value)
            if self.command != 'POST':
                raise Erro('Método de jornada não permitido.', 405)
            actor = self.thunder_actor()
            if actor != 'ygor':
                raise Erro('Somente Ygor pode preparar uma jornada.', 403)
            body = self.corpo()
            if not isinstance(body, dict):
                raise Erro('Dados de jornada inválidos.')
            if target == 'create':
                if set(body) != {'campaign_id', 'name'}:
                    raise Erro('Dados de jornada inválidos.')
                return self.responder(200, wizard.create(body, actor))
            match = re.fullmatch(r'([A-Za-z0-9_-]{1,100})/(source|check|qualify|sequences|sequence|apollo-prompts|approve-step|export|export-preview|reconcile|company-preview|company-execute|company-reconcile|hubspot-preview|hubspot-execute|hubspot-reconcile)', target)
            if not match:
                raise Erro('Operação de jornada não encontrada.', 404)
            ident, action = match.groups()
            if type(body.get('expected_version')) is not int or body['expected_version'] < 1:
                raise Erro('Versão de jornada inválida.')
            version = body['expected_version']
            required = {'expected_version'}
            if action.startswith('company-'):
                reviewer = getattr(self.server, 'thunder_company_qualification', None)
                if reviewer is None:
                    raise Erro('Revisão da empresa indisponível.', 503)
                if action == 'company-preview':
                    if set(body) != {'expected_version', 'review_hash', 'source_key', 'facts'}:
                        raise Erro('Revisão da empresa inválida.')
                    result = reviewer.preview(ident, body['source_key'], body['facts'], actor,
                        expected_version=version, review_hash=body['review_hash'])
                else:
                    preview = reviewer.repo.get_state('hubspot_preview', body.get('preview_id'))
                    if not preview or preview.get('journey_id') != ident:
                        raise Erro('Prévia de outra jornada ou inexistente.', 404)
                    operation = reviewer.execute if action == 'company-execute' else reviewer.reconcile
                    result = operation(body, actor)
                return self.responder(200, result)
            if action.startswith('hubspot-'):
                importer = getattr(self.server, 'thunder_new_journey_import', None)
                if importer is None:
                    raise Erro('Cadastro HubSpot indisponível.', 503)
                if action == 'hubspot-preview':
                    if set(body) != {'expected_version', 'review_hash', 'data'}:
                        raise Erro('Prévia HubSpot inválida.')
                    result = importer.preview(ident, body['data'], actor,
                        expected_version=version, review_hash=body['review_hash'])
                else:
                    required = {'preview_id', 'expected_version', 'preview_hash'}
                    if action == 'hubspot-execute':
                        required.add('confirm')
                    if set(body) != required:
                        raise Erro('Confirme a prévia exata do cadastro.')
                    preview = importer.repo.get_state('hubspot_preview', body['preview_id'])
                    if not preview or preview.get('journey_id') != ident:
                        raise Erro('Prévia de outra jornada ou inexistente.', 404)
                    operation = importer.execute if action == 'hubspot-execute' else importer.reconcile
                    result = operation(body, actor)
                return self.responder(200, result)
            if action == 'source':
                mode = body.get('mode')
                fields = {'csv': {'mode', 'csv_text'},
                          'apollo_people': {'mode', 'filters', 'page'},
                          'apollo_contacts': {'mode', 'query', 'page'}}.get(mode)
                if fields is None or set(body) != required | fields:
                    raise Erro('Fonte de jornada inválida.')
                result = wizard.source(ident, {k: v for k, v in body.items() if k != 'expected_version'},
                                       actor, expected_version=version)
            elif action == 'qualify':
                if (set(body) != required | {'source_key', 'facts', 'review_hash'}
                        or not isinstance(body['source_key'], str)
                        or not isinstance(body['review_hash'], str)
                        or not re.fullmatch(r'[a-f0-9]{64}', body['review_hash'])):
                    raise Erro('Qualificação inválida.')
                result = wizard.qualify(ident, body['source_key'], body['facts'], actor,
                                        expected_version=version, review_hash=body['review_hash'])
            elif action == 'sequences':
                if set(body) != required | {'query'}:
                    raise Erro('Consulta de sequências inválida.')
                result = wizard.sequences(ident, body['query'], actor, expected_version=version)
            elif action == 'apollo-prompts':
                if set(body) != required | {'apollo_sequence_id', 'apollo_sequence_query'}:
                    raise Erro('Seleção de prompts inválida.')
                result = wizard.select_apollo_prompts(ident, body['apollo_sequence_id'],
                    body['apollo_sequence_query'], actor, expected_version=version)
            elif action == 'sequence':
                selection = {'apollo_sequence_id', 'apollo_sequence_query'}
                if set(body) not in (required | {'steps'}, required | {'steps'} | selection):
                    raise Erro('Sequência inválida.')
                args = {key: body[key] for key in selection if key in body}
                result = wizard.revise_sequence(ident, body['steps'], actor, expected_version=version, **args)
            elif action in {'approve-step', 'export', 'export-preview'}:
                fields = required | {'review_hash'} | ({'step_id'} if action == 'approve-step' else set())
                if (set(body) != fields or not isinstance(body.get('review_hash'), str)
                        or not re.fullmatch(r'[a-f0-9]{64}', body['review_hash'])):
                    raise Erro('Revisão de jornada inválida.')
                args = {'expected_version': version, 'review_hash': body['review_hash']}
                result = (wizard.approve_step(ident, body['step_id'], actor, **args)
                          if action == 'approve-step' else wizard.export_preview(ident, actor, **args)
                          if action == 'export-preview' else wizard.export(ident, actor, **args))
            else:
                if set(body) != required:
                    raise Erro('Consulta de jornada inválida.')
                operation = wizard.check if action == 'check' else wizard.reconcile
                result = operation(ident, actor, expected_version=version)
            return self.responder(200, result)
        except Erro:
            raise
        except RepositoryConflict:
            raise Erro('A jornada mudou. Atualize antes de continuar.', 409) from None
        except (ContractError, ValueError) as error:
            status = getattr(error, 'status', 409)
            if status not in {400, 404, 409, 503}:
                status = 409
            public_errors = {
                'company_qualification_explicit_review_required': 'Confirme a revisão conjunta de ICP, cliente e exclusões.',
                'company_qualification_preview_not_current': 'A revisão expirou ou já foi processada. Atualize as checagens antes de preparar outra prévia.',
                'company_qualification_identity_changed': 'O vínculo da empresa mudou no HubSpot. Refaça a checagem antes de revisar.',
                'journey_apollo_inputs_unverified': 'A leitura completa dos prompts, variáveis e destinatários do Apollo não foi comprovada.',
                'import_writes_disabled': 'O cadastro está desligado nos controles do servidor. A prévia não gravou nada.',
                'import_preview_conflict': 'A prévia mudou. Prepare e confira novamente antes de confirmar.',
                'import_exact_review_required': 'Confirme o hash e a versão exatos da prévia.',
                'journey_apollo_permission_denied': 'A chave do Apollo não permite esta consulta. Confira o escopo de leitura indicado na etapa.',
                'journey_apollo_credential_unavailable': 'A credencial privada do Apollo não está disponível para o painel.',
                'journey_apollo_credential_rejected': 'O Apollo recusou a credencial configurada.',
                'journey_apollo_rate_limited': 'O Apollo limitou as consultas. Nenhum reenvio ou tentativa paga foi realizado.',
                'journey_apollo_timeout': 'O Apollo não respondeu a tempo. A consulta não foi repetida automaticamente.',
                'journey_sheet_incomplete': 'A ficha está incompleta. A pesquisa continua disponível, mas o texto não pode ser aprovado.',
                'journey_sheet_changed': 'A ficha mudou. Atualize a checagem e aprove novamente o texto exato.',
                'journey_checks_changed': 'As evidências mudaram. Refaça a checagem antes de aprovar ou exportar.',
                'journey_no_eligible_rows': 'Nenhum contato passou por todas as checagens. Confira os motivos por pessoa.',
                'journey_review_conflict': 'A revisão mudou. Atualize antes de aprovar ou exportar.',
                'journey_qualification_invalid': 'Confirme a empresa vinculada, os fatos, a fonte pública e a data da qualificação.',
                'journey_identity_unverified': 'Refaça as checagens para confirmar o vínculo desta pessoa antes de consultar o histórico.',
                'journey_history_unavailable': 'O histórico desta pessoa está indisponível. As outras consultas continuam disponíveis.',
                'journey_apollo_text_unverified': 'O texto do Apollo não está comprovadamente igual ao aprovado. Confira os templates e o conteúdo dinâmico.',
            }
            raise Erro(public_errors.get(getattr(error, 'code', ''),
                'Não foi possível concluir: confira os dados, as fontes e a revisão atual.'), status) from None

    def thunder_api(self, rota):
        service = getattr(self.server, 'thunder_service', None)
        if service is None:
            raise Erro('Thunder OS indisponível', 503)
        if rota.path == '/api/os/v1/new-journey' or rota.path.startswith('/api/os/v1/new-journey/'):
            return self.new_journey_api(rota)
        journey_step_action = re.fullmatch(r'/api/os/v1/journey/steps/(review|approve|revise|discard)', rota.path)
        if not journey_step_action and (rota.path == '/api/os/v1/journey' or rota.path.startswith('/api/os/v1/journey/')):
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar a Jornada.', 403)
            if self.command != 'GET':
                raise Erro('Jornada permite somente leitura nesta fase.', 405)
            query = parse_qs(rota.query, keep_blank_values=True)
            if (set(query) - {'campaign'} or len(query.get('campaign', [])) > 1
                    or ('campaign' in query and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', query['campaign'][0]))):
                raise Erro('Filtro de campanha inválido.')
            campaign = query.get('campaign', [None])[0]
            journey = getattr(self.server, 'thunder_journey_readonly', None)
            accounts = getattr(self.server, 'thunder_account_cadence_readonly', None)
            if journey is None or accounts is None:
                raise Erro('Leitura da Jornada indisponível.', 503)
            from thunder_os.today_readonly import TodayError
            try:
                if rota.path == '/api/os/v1/journey':
                    result = journey.snapshot(campaign=campaign)
                elif rota.path == '/api/os/v1/journey/accounts':
                    result = journey.account_list(campaign=campaign)
                elif match := re.fullmatch(r'/api/os/v1/journey/details/([A-Za-z0-9:_-]{1,160})', rota.path):
                    result = journey.detail(match.group(1), campaign=campaign)
                elif match := re.fullmatch(r'/api/os/v1/journey/accounts/([^/]{1,480})', rota.path):
                    account_id = unquote(match.group(1))
                    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}', account_id):
                        raise Erro('Identificador de conta inválido.', 404)
                    result = journey.account_detail(account_id, campaign=campaign)
                else:
                    raise Erro('Item da Jornada não encontrado.', 404)
            except TodayError as error:
                status = error.status if error.status in {400, 404, 409, 503} else 503
                raise Erro('A fonte mudou ou não está disponível; atualize a consulta.', status) from error
            except (KeyError, ValueError) as error:
                raise Erro('Item indisponível nesta campanha; atualize a consulta.', 404) from error
            return self.responder(200, result)
        if rota.path == '/api/os/v1/today-work':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar Hoje.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Hoje permite somente consulta sem parametros.', 405)
            today = getattr(self.server, 'thunder_today_readonly', None)
            if today is None:
                raise Erro('Leitura de Hoje indisponivel.', 503)
            return self.responder(200, today.snapshot())
        preparation = re.fullmatch(r'/api/os/v1/campaigns/([A-Za-z0-9_-]{1,100})/preparation', rota.path)
        if preparation:
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar a preparação.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Preparação permite somente leitura sem parâmetros.', 405)
            reader = getattr(self.server, 'thunder_cadence_preparation', None)
            if reader is None:
                raise Erro('Leitura da preparação indisponível.', 503)
            from thunder_os.approvals import ApprovalError
            try:
                return self.responder(200, reader.snapshot(preparation.group(1)))
            except (ApprovalError, ValueError):
                raise Erro('Campanha ou preparação indisponível.', 409) from None
        if rota.path in {'/api/os/v1/hermes/capabilities','/api/os/v1/hermes/contacts','/api/os/v1/campaign-scoreboard'}:
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar o Hermes.',403)
            if self.command != 'GET' or (rota.query and rota.path != '/api/os/v1/campaign-scoreboard'):
                raise Erro('Esta rota permite somente consulta.',405)
            verifier=getattr(self.server,'thunder_panel_verifier',None)
            if rota.path.endswith('/capabilities'):
                return self.responder(200,verifier.capabilities() if verifier else {'verify':False,'change_route':False,'send':False})
            if rota.path.endswith('/contacts'):
                if verifier is None:raise Erro('Leitura de contatos indisponível.',503)
                return self.responder(200,verifier.contacts())
            scoreboard=getattr(self.server,'thunder_campaign_scoreboard',None)
            if scoreboard is None:raise Erro('Placar indisponível.',503)
            query = parse_qs(rota.query, keep_blank_values=True)
            if query:
                if (set(query) != {'hunter_cursor'} or len(query['hunter_cursor']) != 1
                    or not re.fullmatch(r'[A-Za-z0-9_-]{32}', query['hunter_cursor'][0])):
                    raise Erro('Continuação inválida.',400)
                try:
                    return self.responder(200,scoreboard.snapshot(hunter_cursor=query['hunter_cursor'][0]))
                except ValueError:
                    raise Erro('Consulta expirou ou mudou. Use Atualizar placar para recomeçar.',409) from None
            try:
                return self.responder(200,scoreboard.snapshot())
            except ValueError:
                raise Erro('Consulta temporariamente indisponível. Continue a consulta aberta ou aguarde.',409) from None
        if rota.path in {'/api/os/v1/hermes/change-route','/api/os/v1/hermes/send'}:
            if self.thunder_proxy_actor() != 'ygor':raise Erro('Somente Ygor pode solicitar ações.',403)
            raise Erro('Ações e envios estão desligados nesta rodada.',503)
        if rota.path == '/api/os/v1/campaign-sources':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar fontes.',403)
            if self.command != 'GET' or rota.query:
                raise Erro('Fontes permitem somente consulta.',405)
            sources=getattr(self.server,'thunder_campaign_sources',None)
            if sources is None:
                raise Erro('Fontes indisponíveis.',503)
            return self.responder(200,sources.snapshot())
        if rota.path == '/api/os/v1/linkedin-preparation':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar o executor.',403)
            if self.command != 'GET' or rota.query:
                raise Erro('Executor LinkedIn permite somente consulta.',405)
            prepared=getattr(self.server,'thunder_linkedin_preparation',None)
            if prepared is None:
                raise Erro('Preparação LinkedIn indisponível.',503)
            return self.responder(200,prepared.snapshot())
        if rota.path == '/api/os/v1/reply-control':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar respostas.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Respostas permitem consulta nesta rota.', 405)
            control = getattr(self.server, 'thunder_reply_control', None)
            if control is None:
                raise Erro('Controle de respostas indisponível.', 503)
            return self.responder(200, control.snapshot())
        external_email_base = '/api/os/v1/integrations/external-email'
        if rota.path == '/api/os/v1/integrations/instantly/reader':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar integrações.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Leitor permite apenas consulta.', 405)
            reader = getattr(self.server, 'thunder_instantly_reader', None)
            if reader is None:
                raise Erro('Leitor Instantly indisponível.', 503)
            return self.responder(200, reader.snapshot())
        instantly_event_path = '/api/os/v1/integrations/instantly/events'
        if rota.path == instantly_event_path:
            if self.command != 'POST' or rota.query:
                raise Erro('Evento inválido.', 405)
            receiver = getattr(self.server, 'thunder_instantly_capture', None)
            if receiver is None:
                return self.responder(410, {'status':'disabled','persisted':False,
                    'provider_verified':False,'email_logged':False,'writes':0})
            denied = receiver.authenticate(self.headers)
            if denied is not None:
                self.close_connection = True
                return self.responder(denied['http_status'], denied)
            result = receiver.capture(self.corpo())
            return self.responder(result['http_status'], result)
        if rota.path == external_email_base + '/status':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar integrações.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Disponibilidade permite apenas consulta.', 405)
            from thunder_os.apollo_readonly import contract_status as apollo_status
            from thunder_os.instantly_webhook import contract_status as instantly_status
            receiver = getattr(self.server, 'thunder_instantly_capture', None)
            capture_enabled = receiver is not None and receiver.enabled is True
            reader = getattr(self.server, 'thunder_instantly_reader', None)
            reader_state = reader.snapshot() if reader is not None else {'enabled':False,'status':'disabled'}
            return self.responder(200, {'schema_version':1,'read_only':True,
                'automatic_reader_enabled':False,'crm_import_enabled':False,
                'webhook_enabled':capture_enabled,'webhook_mode':'capture_only' if capture_enabled else 'disabled',
                'apollo':apollo_status(),'instantly':{**instantly_status(),
                    'capture_enabled':capture_enabled,'writer_enabled':False,
                    'enabled':reader_state['enabled'],'status':reader_state['status'],
                    'configuration_pending':not reader_state['enabled'] or reader_state.get('error_code') in (
                        'configuration_pending','credential_unavailable'),
                    'provider_verified':bool(reader_state.get('last_success_at')),
                    'automatic_reader_enabled':reader_state['enabled'],
                    'reader_status':reader_state['status']}})
        membership_base = '/api/os/v1/campaign-membership/'
        if rota.path.startswith(membership_base):
            actor = self.thunder_proxy_actor()
            if actor != 'ygor':
                raise Erro('Somente Ygor pode consultar vínculos.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Vínculos permitem apenas consulta nesta versão.', 405)
            match = re.fullmatch(re.escape(membership_base) + r'([1-9][0-9]{0,29})/([A-Za-z0-9_-]{1,100})', rota.path)
            if not match:
                raise Erro('Contato ou campanha inválidos.', 400)
            console = getattr(self.server, 'thunder_campaign_membership', None)
            if console is None:
                raise Erro('Consulta de vínculo indisponível.', 503)
            try:
                result = console.inspect(match.group(1), match.group(2))
            except Exception:
                raise Erro('Não foi possível verificar o vínculo no HubSpot.', 503) from None
            return self.responder(200, result)
        if rota.path == '/api/os/v1/campaign-sheets':
            if self.thunder_proxy_actor() != 'ygor':
                raise Erro('Somente Ygor pode consultar as fichas.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Ficha da campanha permite somente consulta.', 405)
            campaigns = service.campaign_registry.list_campaigns()
            return self.responder(200, {'schema_version': 1, 'read_only': True,
                'campaigns': [service.campaign_sheet_view(row['id']) for row in campaigns]})
        cadence_base = '/api/os/v1/cadencias'
        if rota.path == cadence_base or rota.path.startswith(cadence_base + '/'):
            actor = self.thunder_proxy_actor()
            if actor != 'ygor':
                raise Erro('Somente Ygor pode consultar cadências.', 403)
            if self.command != 'GET' or rota.query:
                raise Erro('Cadências permite apenas consulta.', 405)
            console = getattr(self.server, 'thunder_cadence_readonly', None)
            if console is None:
                raise Erro('Leitura das cadências indisponível.', 503)
            contact = re.fullmatch(re.escape(cadence_base) + r'/contacts/([1-9][0-9]{0,29})', rota.path)
            if rota.path != cadence_base and not contact:
                raise Erro('Contato inválido.', 400)
            try:
                result = console.snapshot() if not contact else console.timeline(contact.group(1))
            except LookupError:
                raise Erro('Contato não encontrado nas cadências.', 404) from None
            except Exception:
                raise Erro('Não foi possível consultar as cadências no HubSpot.', 503) from None
            if result.get('ok') is False:
                raise Erro('Não foi possível consultar as cadências no HubSpot.', 503)
            return self.responder(200, result)
        if self.command == 'GET' and rota.path == '/api/os/v1/agenttrail' and not rota.query:
            actor = self.agenttrail_ingress_actor()
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode consultar atividade dos agentes.')
            from thunder_os.agenttrail_observer import panel_state
            return self.responder(200, panel_state())
        if self.command == 'GET' and rota.path == '/api/os/v1/connections':
            actor = self.thunder_proxy_actor()
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode consultar conexões.')
            try:
                params = parse_qs(rota.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
            except ValueError:
                raise Erro('Consulta de conexões inválida') from None
            if set(params) - {'view', 'offset', 'limit'} or any(len(values) != 1 for values in params.values()):
                raise Erro('Consulta de conexões inválida')
            view = params.get('view', ['actions'])[0]
            raw_offset, raw_limit = params.get('offset', ['0'])[0], params.get('limit', ['100'])[0]
            if not re.fullmatch(r'\d{1,8}', raw_offset) or not re.fullmatch(r'\d{1,3}', raw_limit):
                raise Erro('Consulta de conexões inválida')
            return self.responder(200, connection_panel_projection(
                service, view=view, offset=int(raw_offset), limit=int(raw_limit)))
        if rota.query:
            raise Erro('Rota Thunder OS inválida', 405)
        if self.command == 'GET' and rota.path == '/api/os/v1/panel-capabilities':
            actor = self.thunder_proxy_actor()
            blocked = (actor != 'ygor' or not external_effects_enabled() or restricted_mode()
                       or os.environ.get('THUNDER_OS_COPILOT_MODE', '') == 'human_approval')
            return self.responder(200, {'legacy_mutations': not blocked,
                'reason': 'Este modo exige revisão e confirmação pelo fluxo de cada canal.' if blocked else ''})
        if self.command == 'GET' and rota.path == '/api/os/v1/items':
            actor = self.thunder_proxy_actor()
            if actor != 'ygor':
                raise Erro('Somente Ygor pode consultar estes cartões.', 403)
            return self.responder(200, self.server.banco.listar_itens())
        if self.command == 'GET' and rota.path == '/api/os/v1/session':
            actor = self.thunder_proxy_actor()
            nonce = secrets.token_urlsafe(32)
            with NONCE_LOCK:
                issued = self.prune_nonces()
                while len(issued) >= NONCE_LIMIT:
                    issued.pop(next(iter(issued)))
                issued[nonce] = (actor, time.monotonic() + NONCE_TTL_SECONDS)
            return self.responder(200, {'actor': actor, 'nonce': nonce})
        if self.command == 'GET' and rota.path in {
                '/api/os/v1/integrations/status', '/api/os/v1/integrations/drafts'}:
            self.thunder_proxy_actor()
            views = getattr(self.server, 'thunder_integration_views', None)
            if views is None:
                views = IntegrationViews(service.repository)
            result = views.status() if rota.path.endswith('/status') else views.drafts()
            return self.responder(200, result)
        if self.command == 'GET' and rota.path == '/api/os/v1/today':
            self.thunder_proxy_actor()
            return self.responder(200, {**service.today(), 'external_effects_enabled': external_effects_enabled()})
        connectsafely_base = '/api/os/v1/integrations/connectsafely'
        if self.command == 'GET' and rota.path.startswith(connectsafely_base + '/'):
            actor = self.thunder_proxy_actor()
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode consultar esta integração.')
            if rota.path == connectsafely_base + '/local-inbox':
                from thunder_os.connectsafely_local_inbox import local_inbox
                return self.responder(200, local_inbox(service.repository))
            if rota.path == connectsafely_base + '/monitor':
                from thunder_os.connectsafely_monitor import status as monitor_status
                return self.responder(200, monitor_status(service.repository))
            app = getattr(self.server, 'thunder_connectsafely_app', None)
            if app is None:
                if rota.path == connectsafely_base + '/status':
                    return self.responder(200, {'status': 'blocked', 'configured': False,
                        'enqueue_enabled': False, 'simulation': False,
                        'blockers': ['application_not_configured']})
                raise Erro('ConnectSafely não configurado', 503)
            if rota.path == connectsafely_base + '/status':
                return self.responder(200, app.status(actor))
            if rota.path == connectsafely_base + '/inbox':
                return self.responder(200, app.inbox(actor))
            if rota.path == connectsafely_base + '/previews':
                return self.responder(200, app.listpreviews(actor))
            cs_preview = re.fullmatch(re.escape(connectsafely_base) + r'/previews/([A-Za-z0-9_-]{1,100})', rota.path)
            if cs_preview:
                return self.responder(200, app.preview(cs_preview.group(1), actor))
            raise Erro('Consulta ConnectSafely inválida', 404)
        seqd_base = '/api/os/v1/integrations/seqd'
        if self.command == 'GET' and rota.path.startswith(seqd_base + '/'):
            actor = self.thunder_proxy_actor()
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode consultar esta caixa.')
            console = getattr(self.server, 'thunder_seqd_console', None)
            if console is None:
                raise Erro('E-mail não configurado', 503)
            if rota.path == seqd_base + '/status':
                return self.responder(200, canary_view(service.repository,console.status(actor)))
            if rota.path == seqd_base + '/messages':
                return self.responder(200, console.messages(actor, limit=10))
            seqd_thread = re.fullmatch(re.escape(seqd_base) + r'/threads/([a-f0-9]{1,64})', rota.path)
            if seqd_thread:
                return self.responder(200, console.conversation(seqd_thread.group(1), actor))
            seqd_message = re.fullmatch(re.escape(seqd_base) + r'/messages/([A-Za-z0-9_-]{1,200})', rota.path)
            if seqd_message:
                return self.responder(200, console.read(seqd_message.group(1), actor))
            seqd_preview = re.fullmatch(re.escape(seqd_base) + r'/previews/([A-Za-z0-9_-]{1,100})', rota.path)
            if seqd_preview:
                return self.responder(200, canary_view(service.repository,console.read_preview(seqd_preview.group(1), actor),seqd_preview.group(1)))
            raise Erro('Consulta de e-mail inválida', 404)
        if self.command == 'GET' and rota.path == '/api/os/v1/integrations/hubspot/status':
            actor = self.thunder_proxy_actor()
            console = getattr(self.server, 'thunder_hubspot_console', None)
            if console is None:
                raise Erro('HubSpot não configurado', 503)
            result = console.status(actor)
            views = getattr(self.server, 'thunder_integration_views', None)
            if views is not None:
                views.observations['hubspot'] = ProbeObservation(dt.datetime.now(dt.timezone.utc),
                    'verified_read' if result.get('read_verified') else 'unavailable')
            return self.responder(200, result)
        crm_preview = re.fullmatch(r'/api/os/v1/integrations/hubspot/previews/([A-Za-z0-9_-]{1,100})', rota.path)
        if self.command == 'GET' and crm_preview:
            actor = self.thunder_proxy_actor()
            console = getattr(self.server, 'thunder_hubspot_console', None)
            if console is None:
                raise Erro('HubSpot não configurado', 503)
            return self.responder(200, console.read_preview(crm_preview.group(1), actor))
        match_context = re.fullmatch(r'/api/os/v1/accounts/([a-zA-Z0-9._-]{1,200})/context', rota.path)
        if self.command == 'GET' and match_context:
            self.thunder_proxy_actor()
            return self.responder(200, service.canonical_context(match_context.group(1)))
        match_effect = re.fullmatch(r'/api/os/v1/effects/([a-zA-Z0-9_-]{1,100})', rota.path)
        if self.command == 'GET' and match_effect:
            self.thunder_proxy_actor()
            effect = service.repository.effect(match_effect.group(1))
            if not effect:
                raise Erro('Efeito não encontrado', 404)
            return self.responder(200, effect)
        if self.command != 'POST':
            raise Erro('Rota Thunder OS inválida', 405)
        actor = self.thunder_actor()
        body = self.corpo()
        if rota.path.startswith('/api/os/v1/integrations/thunder-email/'):
            if actor != 'ygor' or rota.query or not isinstance(body, dict):
                raise Erro('Operação Thunder email não autorizada.', 403)
            cycle = getattr(self.server, 'thunder_email_cycle', None)
            if cycle is None:
                raise Erro('Thunder email não configurado.', 503)
            action = rota.path.rsplit('/', 1)[-1]
            fields = {'receipt-preview': {'receipt'}, 'email-execute': {'plan'},
                      'reply-preview': {'receipt', 'thread_id', 'message_id'},
                      'reply-ingest': {'receipt', 'thread_id', 'message_id', 'review'},
                      'reconcile-preview': {'cadence_id','step_id'},
                      'reconcile-confirm': {'cadence_id','step_id','review_hash'}}
            if action not in fields or set(body) != fields[action]:
                raise Erro('Contrato Thunder email inválido.')
            if action.startswith('reconcile-'):
                if any(not isinstance(body[k],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',body[k]) for k in ('cadence_id','step_id')):
                    raise Erro('Vínculo da reconciliação inválido.')
                if action == 'reconcile-preview':
                    return self.responder(200,cycle.preview_reconciliation(body['cadence_id'],body['step_id']))
                return self.responder(200,cycle.confirm_reconciliation(body['cadence_id'],body['step_id'],body['review_hash']))
            if action == 'email-execute':
                if not external_effects_enabled() or restricted_mode():
                    raise Erro('Escrita CRM desabilitada.', 403)
                return self.responder(200, cycle.log_email(body['plan']))
            if not isinstance(body['receipt'], str) or not re.fullmatch(r'gmail:[a-f0-9]{1,64}', body['receipt']):
                raise Erro('Recibo Gmail inválido.')
            if action == 'receipt-preview':
                return self.responder(200, cycle.prepare_email(body['receipt']))
            if any(not isinstance(body[key], str) or not re.fullmatch(r'[a-f0-9]{1,64}', body[key]) for key in ('thread_id', 'message_id')):
                raise Erro('Identidade Gmail inválida.')
            args = (body['receipt'], body['thread_id'], body['message_id'])
            if action == 'reply-preview':
                return self.responder(200, cycle.preview_reply(*args))
            return self.responder(200, cycle.receive_reply(*args, review=body['review']))
        if journey_step_action:
            if actor != 'ygor':
                raise Erro('Somente Ygor pode decidir sobre um toque.', 403)
            action = journey_step_action.group(1)
            base_fields = {'cadence_id', 'step_id'}
            fields = {'review': base_fields,
                'approve': base_fields | {'expected_version', 'review_hash', 'execution_confirmation'},
                'revise': base_fields | {'expected_version', 'review_hash', 'content', 'subject'},
                'discard': base_fields | {'expected_version', 'review_hash', 'reason'}}[action]
            if (rota.query or not isinstance(body, dict) or set(body) != fields
                    or any(not isinstance(body.get(key), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:@-]{0,159}', body[key])
                           for key in base_fields)):
                raise Erro('Revisão do toque inválida.')
            cadence = service.repository.get_state('cadence', body['cadence_id'])
            campaign_id = (cadence or {}).get('execution_context', {}).get('campaign_id', '')
            # This surface never imports Markdown, guesses identity or operates Mexico.
            if not cadence or not (campaign_id.startswith('BR-') or campaign_id == 'CHATTIE-01'):
                raise Erro('Toque não pertence a uma campanha Brasil desta Jornada.', 404)
            if action == 'review':
                return self.responder(200, service.review_step(body['cadence_id'], body['step_id'], actor))
            if (type(body.get('expected_version')) is not int
                    or not isinstance(body.get('review_hash'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', body['review_hash'])):
                raise Erro('Versão da revisão inválida.')
            if action == 'approve':
                result = service.approve_cadence(body['cadence_id'], actor,
                    expected_version=body['expected_version'], review_hash=body['review_hash'],
                    execution_confirmation=body['execution_confirmation'], step_ids=[body['step_id']])
                return self.responder(200, {'applied': True, 'sent': False, 'receipt': None,
                    'status': 'approved' if result.get('status') == 'active' else result.get('status'),
                    'approval': result,
                    'cadence': service.repository.get_state('cadence', body['cadence_id'])})
            if action == 'discard':
                result = service.discard_step(body['cadence_id'], body['step_id'], actor,
                    expected_version=body['expected_version'], review_hash=body['review_hash'], reason=body['reason'])
                return self.responder(200, {'applied': True, 'sent': False, 'cadence': result})
            if (cadence['version'] != body['expected_version']
                    or service.cadence_review_hash(cadence) != body['review_hash']):
                raise RepositoryConflict('stale cadence review')
            if (not isinstance(body['content'], str) or not body['content'].strip()
                    or len(body['content']) > 30000
                    or body['subject'] is not None and (not isinstance(body['subject'], str)
                        or len(body['subject']) > 1000)):
                raise Erro('Texto do toque inválido.')
            steps = [dict(step) for step in cadence['steps']]
            selected = next((step for step in steps if step['id'] == body['step_id']), None)
            if selected is None:
                raise Erro('Toque não encontrado.', 404)
            if selected['channel'] != 'email' and body['subject'] is not None:
                raise Erro('Este canal não aceita assunto.')
            selected['content'] = body['content']
            if selected['channel'] == 'email':
                selected['subject'] = body['subject']
            result = service.revise_cadence(body['cadence_id'], steps, body['expected_version'], actor)
            return self.responder(200, {'applied': True, 'sent': False, 'cadence': result})
        if rota.path == '/api/os/v1/today-work/draft':
            if actor != 'ygor':
                raise Erro('Somente Ygor pode pedir rascunhos.', 403)
            today = getattr(self.server, 'thunder_today_readonly', None)
            if today is None:
                raise Erro('Leitura de Hoje indisponivel.', 503)
            from thunder_os.today_readonly import TodayError
            try:
                result = today.draft(body)
            except TodayError as error:
                return self.responder(error.status, {'read_only': True, 'applied': False,
                    'code': error.code, 'erro': 'Rascunho indisponivel; confira o contexto ou tente novamente.'})
            return self.responder(200, result)
        if rota.path == '/api/os/v1/hermes/verify':
            if actor != 'ygor':raise Erro('Somente Ygor pode consultar o Hermes.',403)
            if not isinstance(body,dict) or set(body)!={'contact_id','question'} or rota.query:
                raise Erro('Consulta inválida.')
            verifier=getattr(self.server,'thunder_panel_verifier',None)
            gateway=getattr(self.server,'thunder_panel_gateway',None)
            if verifier is None or gateway is None:raise Erro('Hermes indisponível neste painel.',503)
            from thunder_os.panel_verification import VerificationError
            from thunder_os.agent_gateway import EnvelopeError
            try:
                result=gateway.verify({'actor':actor,'capability':'verify',**body},verifier.verify)
            except EnvelopeError:
                raise Erro('Selecione um contato e escreva uma pergunta curta.') from None
            except VerificationError as error:
                code=str(error)
                messages={'auth_expired':'O acesso do Hermes expirou. É preciso reconectar, sem alterar campanhas.',
                    'auth_unavailable':'A autenticação do Hermes não está disponível.',
                    'contact_not_confirmed':'Contato não confirmado nesta consulta de campanhas.',
                    'verification_busy':'Há uma consulta em andamento.',
                    'invalid_verification_answer':'O Hermes retornou uma resposta não validada. Nenhuma ação foi aplicada.'}
                return self.responder(429 if code=='verification_busy' else 409 if code=='contact_not_confirmed' else 503,
                    {'erro':messages.get(code,'Não foi possível consultar o Hermes. Nenhuma ação foi aplicada.'),
                     'code':code,'read_only':True,'applied':False})
            return self.responder(200,result)
        if rota.path == '/api/os/v1/reply-control/manual':
            if actor != 'ygor':
                raise Erro('Somente Ygor pode marcar uma resposta.', 403)
            fields = {'request_id','contact_id','campaign_id','channel','content'}
            if not isinstance(body,dict) or set(body) != fields or rota.query:
                raise Erro('Registro de resposta inválido.')
            if (not isinstance(body['request_id'],str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',body['request_id'])
                    or not isinstance(body['contact_id'],str) or not re.fullmatch(r'[1-9][0-9]{0,29}',body['contact_id'])
                    or not isinstance(body['campaign_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',body['campaign_id'])
                    or not isinstance(body['channel'],str) or body['channel'] not in {'email','linkedin','whatsapp','phone','chattie'}
                    or not isinstance(body['content'],str) or not 1 <= len(body['content'].strip()) <= 4000):
                raise Erro('Contato, campanha, canal ou texto inválidos.')
            reader = getattr(self.server,'thunder_cadence_readonly',None)
            control = getattr(self.server,'thunder_reply_control',None)
            if reader is None or control is None:
                raise Erro('Leitura de identidade indisponível.',503)
            snapshot = reader.snapshot()
            known = any(c.get('id') == body['campaign_id'] and any(
                body['contact_id'] in r.get('contact_ids',[]) for r in c.get('tasks',[])+c.get('activities',[]))
                for c in snapshot.get('campaigns',[]))
            if snapshot.get('ok') is False or not known:
                raise Erro('Contato não confirmado nessa campanha por ID.',409)
            event = {'id':'manual-'+body['request_id'],'channel':body['channel'],
                'content':body['content'].strip(),'received_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                'hubspot_contact_id':body['contact_id'],'campaign_id':body['campaign_id'],
                'human_reply':True,'identity_verified':True,
                'source':{'provider':'manual','account_id':'ygor','provider_message_id':body['request_id']}}
            try:
                return self.responder(200,control.ingest(event,actor))
            except ValueError:
                raise Erro('Resposta não pôde ser registrada; confira o vínculo e tente consultar.',409) from None
        if rota.path == '/api/os/v1/integrations/instantly/simulate':
            if actor != 'ygor':
                raise Erro('Somente Ygor pode simular eventos.', 403)
            if not isinstance(body,dict) or set(body) != {'payload'} or not isinstance(body['payload'],dict):
                raise Erro('Payload de simulação inválido.')
            from thunder_os.instantly_webhook import dry_run
            return self.responder(200,dry_run(payload=body['payload']))
        discard = re.fullmatch(r'/api/os/v1/items/([A-Za-z0-9_-]{1,100})/discard', rota.path)
        if discard:
            if body:
                raise Erro('Descarte não aceita alterações de conteúdo.')
            return self.responder(200, self.server.banco.descartar_pendente(discard.group(1), actor))
        if restricted_mode() and (
                rota.path in {'/api/os/v1/likes','/api/os/v1/control/reconcile'}
                or re.fullmatch(r'/api/os/v1/cadences/[^/]+/dispatch',rota.path)
                or re.fullmatch(r'/api/os/v1/effects/[^/]+/retry',rota.path)
                or re.fullmatch(r'/api/os/v1/integrations/(?:hubspot|connectsafely)/(?:execute|reconcile)',rota.path)
                or (rota.path=='/api/os/v1/meetings' and 'hubspot_effect' in body)):
            raise Erro('Modo canário: somente o e-mail exato aprovado pode produzir efeito externo.',503)
        if not external_effects_enabled() and (
                rota.path in {'/api/os/v1/likes', '/api/os/v1/control/reconcile'}
                or re.fullmatch(r'/api/os/v1/integrations/(?:seqd|hubspot|connectsafely)/(?:execute|reconcile)', rota.path)
                or re.fullmatch(r'/api/os/v1/cadences/[^/]+/dispatch', rota.path)
                or re.fullmatch(r'/api/os/v1/effects/[^/]+/retry', rota.path)
                or (rota.path == '/api/os/v1/meetings' and 'hubspot_effect' in body)):
            raise Erro('Modo sem efeitos externos: envio, reconciliação e escrita no CRM estão desativados.', 503)
        if os.environ.get('THUNDER_OS_COPILOT_MODE','') == 'human_approval' and (
                rota.path in {'/api/os/v1/likes','/api/os/v1/control/reconcile'}
                or re.fullmatch(r'/api/os/v1/cadences/[^/]+/dispatch',rota.path)
                or re.fullmatch(r'/api/os/v1/effects/[^/]+/retry',rota.path)
                or (rota.path == '/api/os/v1/meetings' and 'hubspot_effect' in body)):
            raise Erro('Copiloto: somente as integrações aprovadas individualmente podem executar.',503)
        if rota.path in {seqd_base+'/execute', '/api/os/v1/integrations/hubspot/execute'} and service.global_paused():
            raise Erro('Efeitos externos pausados globalmente.', 503)
        campaign_review_route = re.fullmatch(r'/api/os/v1/integrations/(connectsafely|seqd|hubspot)/campaign-review', rota.path)
        if campaign_review_route:
            target = getattr(self.server, {'connectsafely':'thunder_connectsafely_app',
                'seqd':'thunder_seqd_console','hubspot':'thunder_hubspot_console'}[campaign_review_route.group(1)], None)
            if target is None: raise Erro('Integração não configurada', 503)
            return self.responder(200, target.campaign_review(body, actor))
        if rota.path == connectsafely_base + '/resolve-target':
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode consultar esta integração.')
            app = getattr(self.server, 'thunder_connectsafely_app', None)
            if getattr(app, 'direct_target_resolution', False):
                resolver = getattr(app, 'resolve_target', None)
            else:
                resolver = getattr(self.server, 'thunder_connectsafely_read_resolver', None)
                if resolver is None:resolver = getattr(app, 'resolve_target', None) if app is not None else None
            if not callable(resolver):
                return self.responder(200, {'resolved':False,'status':'blocked','read_only':True,
                    'actions_available':False,'error_code':'target_evidence_unavailable'})
            return self.responder(200, resolver(body))
        if rota.path in {connectsafely_base + '/preview', connectsafely_base + '/execute', connectsafely_base + '/reconcile', connectsafely_base + '/recover'}:
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode aprovar esta operação.')
            app = getattr(self.server, 'thunder_connectsafely_app', None)
            if app is None:
                raise Erro('ConnectSafely não configurado', 503)
            if rota.path.endswith('/execute') and (set(body) != {'preview_id', 'expected_version', 'review_hash', 'execution_confirmation'}
                    or type(body.get('expected_version')) is not int
                    or not isinstance(body.get('preview_id'), str)
                    or not isinstance(body.get('review_hash'), str)):
                raise Erro('Aprovação exige a versão e o conteúdo revisados', 400)
            operation = app.prepare if rota.path.endswith('/preview') else app.enqueue if rota.path.endswith('/execute') else app.recover if rota.path.endswith('/recover') else app.reconcile
            result=operation(body, actor)
            if rota.path.endswith('/execute') and isinstance(getattr(app,'execution_scope',None),str):
                # Individual approval drives only the exact newly approved job.
                # No queue scan or background consumer is started by this path.
                app.process_one(job_id=result['job_id'])
                observed=app.preview(result['preview_id'],actor)['job']
                result={**result,'state':observed['state']}
            return self.responder(200, result)
        if rota.path in {seqd_base + '/preview', seqd_base + '/execute', seqd_base + '/reconcile'}:
            if actor != 'ygor':
                raise AuthorizationError('Somente Ygor pode aprovar esta operação.')
            console = getattr(self.server, 'thunder_seqd_console', None)
            if console is None:
                raise Erro('E-mail não configurado', 503)
            if rota.path.endswith('/execute'):
                if set(body) != {'preview_id', 'expected_version', 'content_hash', 'execution_confirmation'}:
                    raise Erro('Aprovação exige a versão e o conteúdo revisados', 400)
                # thunder_actor consumed and verified this human nonce above.
                body = {**body, 'confirm_nonce': self.headers.get('X-Thunder-Nonce', '')}
                if restricted_mode():
                    console.campaign_gate().approve(body, actor)
                    canary=EmailCanary(service.repository).claim({k:v for k,v in body.items() if k != 'execution_confirmation'},actor)
                    result=console.execute(body,actor,dispatch_guard=canary.before_dispatch,dispatch_expires_at=canary.policy['expires_at'])
                else:
                    result = console.execute(body, actor)
            else:
                if restricted_mode() and rota.path.endswith('/reconcile'):
                    EmailCanary(service.repository).reconcile_scope(body,actor)
                result = (console.prepare if rota.path.endswith('/preview') else console.reconcile)(body, actor)
                if rota.path.endswith('/preview'):
                    result = canary_view(service.repository,result,result.get('id'))
            return self.responder(200, result)
        if rota.path in {'/api/os/v1/integrations/hubspot/preview', '/api/os/v1/integrations/hubspot/execute', '/api/os/v1/integrations/hubspot/reconcile'}:
            console = getattr(self.server, 'thunder_hubspot_console', None)
            if console is None:
                raise Erro('HubSpot não configurado', 503)
            operation = console.prepare if rota.path.endswith('/preview') else console.reconcile if rota.path.endswith('/reconcile') else console.execute
            result = operation(body, actor)
            return self.responder(200, result)
        if rota.path == '/api/os/v1/control/reconcile':
            if actor != 'ygor':
                raise AuthorizationError('human authority required for reconciliation')
            if body != {'confirm': True}:
                raise Erro('Confirme explicitamente a reconciliação')
            legacy_enabled = legacy_aceites_materialization_enabled()
            if legacy_enabled:
                self.server.banco.materializar_aceites()
            self.server.banco.listar('itens', reconciliar=True)
            return self.responder(200, {'status': 'reconciled',
                'legacy_aceites_materialization': 'executed' if legacy_enabled else 'skipped'})
        if rota.path == '/api/os/v1/work':
            if set(body) - {'objective', 'context', 'idempotency_key'} or not {'objective', 'idempotency_key'} <= set(body):
                raise Erro('Pedido Thunder OS inválido')
            item = service.request_work(
                {'objective': body['objective'], 'context': body.get('context', {})}, actor, body['idempotency_key'])
            return self.responder(201, as_payload(item))
        if rota.path == '/api/os/v1/agent/control':
            return self.responder(200, service.control_agent(body, actor))
        cancel_agent = re.fullmatch(r'/api/os/v1/work/([a-zA-Z0-9_-]{1,100})/cancel', rota.path)
        if cancel_agent and body == {}:
            return self.responder(200, service.cancel_agent_work(cancel_agent.group(1), actor))
        if rota.path == '/api/os/v1/research':
            return self.responder(201, service.submit_research(body, actor))
        if rota.path == '/api/os/v1/copy':
            return self.responder(201, service.request_copy(body, actor))
        if rota.path == '/api/os/v1/paid-queries':
            return self.responder(201, service.request_paid_query(body, actor))
        if rota.path == '/api/os/v1/capability-authorizations':
            return self.responder(201, service.authorize_capabilities(body, actor))
        if rota.path == '/api/os/v1/delegations/hermes':
            return self.responder(200, service.configure_hermes_delegation(body, actor))
        if rota.path == '/api/os/v1/likes':
            return self.responder(202, service.dispatch_like(body, actor))
        if rota.path == '/api/os/v1/identities/resolve':
            return self.responder(201, service.resolve_identity(body, actor))
        if rota.path == '/api/os/v1/control/pause' and set(body) == {'paused'}:
            return self.responder(200, service.set_global_pause(body['paused'], actor))
        if rota.path == '/api/os/v1/meetings':
            return self.responder(201, service.record_meeting(body, actor))
        if rota.path == '/api/os/v1/campaigns' and set(body) == {'config', 'expected_version'}:
            return self.responder(200, service.configure_campaign(
                body['config'], actor, expected_version=body['expected_version']))
        match_campaign = re.fullmatch(r'/api/os/v1/campaigns/([a-zA-Z0-9_-]{1,100})/revoke', rota.path)
        if match_campaign and set(body) == {'expected_version'}:
            return self.responder(200, service.revoke_campaign(
                match_campaign.group(1), actor, expected_version=body['expected_version']))
        if rota.path == '/api/os/v1/cadences':
            return self.responder(201, service.create_cadence(body, actor))
        match_cadence = re.fullmatch(
            r'/api/os/v1/cadences/([a-zA-Z0-9_-]{1,100})/(approve|dispatch)', rota.path)
        if match_cadence:
            cadence_id, action = match_cadence.groups()
            if (action == 'approve' and set(body) == {'expected_version', 'review_hash', 'execution_confirmation'}
                    and type(body['expected_version']) is int and isinstance(body['review_hash'], str)):
                return self.responder(200, service.approve_cadence(
                    cadence_id, actor, expected_version=body['expected_version'], review_hash=body['review_hash'],
                    execution_confirmation=body['execution_confirmation']))
            if action == 'dispatch' and set(body) == {'step_id'}:
                return self.responder(202, service.dispatch_step(cadence_id, str(body['step_id']), actor))
            raise Erro('Comando de cadência inválido')
        match_revision = re.fullmatch(r'/api/os/v1/cadences/([a-zA-Z0-9_-]{1,100})/revise', rota.path)
        if match_revision and set(body) == {'version', 'steps'} and type(body['version']) is int and isinstance(body['steps'], list):
            return self.responder(200, service.revise_cadence(match_revision.group(1), body['steps'], body['version'], actor))
        if rota.path == '/api/os/v1/replies':
            return self.responder(201, service.record_reply(body, actor))
        match_classify = re.fullmatch(r'/api/os/v1/replies/([a-zA-Z0-9_-]{1,100})/classify', rota.path)
        if match_classify and set(body) == {'classification', 'confidence', 'justification'}:
            return self.responder(200, service.classify_reply(
                match_classify.group(1), str(body['classification']), float(body['confidence']),
                str(body['justification']), actor))
        match_resolve = re.fullmatch(r'/api/os/v1/replies/([a-zA-Z0-9_-]{1,100})/resolve', rota.path)
        if match_resolve:
            return self.responder(200, service.resolve_reply_identity(match_resolve.group(1), body, actor))
        match_retry = re.fullmatch(r'/api/os/v1/effects/([a-zA-Z0-9_-]{1,100})/retry', rota.path)
        if match_retry and body == {}:
            return self.responder(200, service.retry_effect(match_retry.group(1), actor))
        if rota.path == '/api/os/v1/learning':
            return self.responder(201, service.propose_learning(body, actor))
        match = re.fullmatch(r'/api/os/v1/work/([a-zA-Z0-9_-]{1,100})/(claim|result|fail)', rota.path)
        if not match:
            raise Erro('Rota Thunder OS não encontrada', 404)
        item_id, action = match.groups()
        if action == 'claim' and set(body) == {'version'} and type(body['version']) is int:
            return self.responder(200, as_payload(service.claim(item_id, body['version'], actor)))
        if (action == 'result' and set(body) == {'version', 'result'}
                and type(body['version']) is int and isinstance(body['result'], dict)):
            return self.responder(200, as_payload(service.record_job_result(
                item_id, body['version'], body['result'], actor)))
        if action == 'fail' and set(body) == {'version', 'reason'} and type(body['version']) is int:
            return self.responder(200, as_payload(service.fail_work(
                item_id, body['version'], str(body['reason']), actor)))
        raise Erro('Comando Thunder OS inválido')

    def executar(self):
        try:
            copiloto = os.environ.get('THUNDER_OS_COPILOT_MODE','') == 'human_approval'
            bloqueado = (self.command not in {'GET', 'HEAD'} and (not external_effects_enabled() or restricted_mode() or copiloto)
                    and not urlsplit(self.path).path.startswith('/api/os/v1/'))
            # Copiloto com aprovação humana: o fluxo legado só pode CRIAR card pendente (sem efeito externo).
            # Aprovar, editar, enviar, regras e perguntas continuam bloqueados; modo somente leitura segue intacto.
            self._so_card_pendente = bloqueado and copiloto and external_effects_enabled() and not restricted_mode()
            if bloqueado and not (self._so_card_pendente and self.command == 'POST' and urlsplit(self.path).path == '/api/itens'):
                raise Erro('Modo consulta: comandos e efeitos externos estão desativados nesta instalação.', 503)
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or not re.fullmatch(r'(painel\.r3xdigital\.tech|localhost|127\.0\.0\.1)(?::[0-9]{1,5})?', hosts[0], re.I):
                raise Erro('Host não permitido')
            caminho = self.path
            banco = self.server.banco
            rota = urlsplit(caminho)
            if rota.path.startswith('/api/os/v1/'):
                return self.thunder_api(rota)
            if rota.path == '/api/sugestoes' or rota.path.startswith('/api/sugestoes/'):
                if self.command == 'GET':
                    if caminho != '/api/sugestoes':
                        raise Erro('Consulta de sugestões não aceita parâmetros')
                    return self.responder(200, banco.sugestoes())
                if self.command == 'PATCH' and rota.path.startswith('/api/sugestoes/'):
                    if rota.query or rota.path.count('/') != 3:
                        raise Erro('Identificador de sugestão inválido')
                    return self.responder(200, banco.decidir_sugestao(
                        unquote(rota.path.rsplit('/', 1)[1]), self.corpo()))
                raise Erro('Sugestões permitem GET ou PATCH', 405)
            if urlsplit(caminho).path == '/api/abm':
                if self.command != 'GET':
                    raise Erro('ABM permite somente consulta GET', 405)
                if caminho != '/api/abm':
                    raise Erro('Consulta ABM não aceita parâmetros', 400)
                return self.responder(200, abm_status())
            if self.command == 'GET':
                if caminho == '/saude':
                    return self.responder(200, {
                        'status': 'ok', 'source_sha': getattr(self.server, 'source_sha', 'unknown'),
                        'auth_required': True, 'pid': os.getpid(),
                        'external_effects_enabled': external_effects_enabled(),
                    })
                if caminho == '/':
                    return self.responder(200, painel.gerar('servidor'), 'text/html; charset=utf-8')
                if caminho == '/novo':
                    # Painel unificado: /novo continua valendo para links antigos e abre o mesmo painel de /.
                    return self.responder(200, painel.gerar('servidor'), 'text/html; charset=utf-8')
                if caminho in ('/api/itens', '/api/regras'):
                    headers = {}
                    dados = banco.listar_itens() if caminho == '/api/itens' else banco.listar('regras')
                    return self.responder(200, dados, headers=headers)
                if caminho == '/api/respostas':
                    return self.responder(200, banco.respostas())
                if caminho == '/api/quentes':
                    return self.responder(200, banco.quentes())
                rota = urlsplit(caminho)
                if rota.path.startswith('/api/conta/'):
                    slug = unquote(rota.path[len('/api/conta/'):])
                    query = parse_qs(rota.query, keep_blank_values=True)
                    if set(query) - {'pessoa_chave'} or len(query.get('pessoa_chave', [''])) != 1:
                        raise Erro('Consulta da conta inválida')
                    return self.responder(200, banco.ficha_conta(slug, query.get('pessoa_chave', [''])[0]))
            elif self.command in ('POST', 'PATCH'):
                corpo = self.corpo()
                if self.command == 'POST' and caminho == '/api/perguntar':
                    return self.responder(200, banco.perguntar(corpo))
                partes = caminho.split('/')
                if self.command == 'PATCH' and len(partes) == 4 and partes[:3] == ['', 'api', 'respostas']:
                    return self.responder(200, banco.classificar_resposta(unquote(partes[3]), corpo))
                if len(partes) in (3, 4) and partes[:2] == ['', 'api'] and partes[2] in COLECOES:
                    if self.command == 'POST' and len(partes) == 3:
                        if getattr(self, '_so_card_pendente', False) and (partes[2] != 'itens' or not isinstance(corpo, dict)
                                or corpo.get('status', 'pendente') not in ('pendente', 'copiar')):
                            raise Erro('Modo consulta: só é permitido criar card pendente.', 503)
                        return self.responder(201, banco.salvar(partes[2], corpo, criar=True))
                    if self.command == 'PATCH' and len(partes) == 4:
                        return self.responder(200, banco.salvar(partes[2], corpo, identificador(unquote(partes[3]))))
            raise Erro('Rota não encontrada', 404)
        except Erro as e:
            self.responder(e.status, {'erro': str(e)})
        except toques.ErroBanco:
            self.responder(503, {'erro': 'Histórico indisponível. Confira a conexão e as tabelas do banco de toques.'})
        except (StaleVersionError, RepositoryConflict) as e:
            self.responder(409, {'erro': str(e)})
        except CanaryError:
            self.responder(503, {'erro':'Canário bloqueado: revisar política, escopo, prazo e tentativa anterior; não reenviar automaticamente.'})
        except AuthorizationError as e:
            self.responder(403, {'erro': str(e)})
        except (ConnectSafelyConsoleError, ConnectSafelyProviderError):
            self.responder(400, {'erro': 'Operação ConnectSafely bloqueada ou inválida. Consulte a disponibilidade e a revisão atual.'})
        except SEQDError as e:
            invalid = str(e).startswith(('invalid_', 'exact_review_', 'preview_not_'))
            self.responder(400 if invalid else 503, {'erro': 'Não foi possível concluir a operação de e-mail. Confira a revisão ou a conexão; nenhum reenvio automático será feito.'})
        except HubSpotHTTPError:
            self.responder(503, {'erro': 'HubSpot indisponível; nenhuma repetição automática foi autorizada.'})
        except HubSpotContractError as e:
            self.responder(400, {'erro': str(e)})
        except ContractError as e:
            self.responder(400, {'erro': str(e)})
        except Exception:
            self.responder(500, {'erro': 'Falha interna; confira banco e arquivos do painel'})

    do_GET = executar
    do_POST = executar
    do_PATCH = executar
    do_PUT = executar
    do_DELETE = executar
    do_OPTIONS = executar


def configure_connectsafely_server(server, repository, factory=None):
    """Attach an injected composition or the explicitly pinned panel config.

    Without either input the application stays disconnected. Panel configuration
    requires an individual scope, so only the confirmed job can be dispatched.
    No queue consumer, credential discovery or provider call runs at startup.
    """
    from thunder_os.connectsafely_application import ConnectSafelyApplication
    from thunder_os.connectsafely_composition import ConnectSafelyComposition
    if factory is not None and not callable(factory):
        raise ValueError('explicit_connectsafely_factory_required')
    if factory is None:
        config_path = os.environ.get('THUNDER_OS_CONNECTSAFELY_CONFIG', '')
        config_sha256 = os.environ.get('THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256', '')
        if config_path or config_sha256:
            from thunder_os.connectsafely_live import build_panel_composition
            factory = lambda repo: build_panel_composition(
                repo, config_path=config_path, config_sha256=config_sha256)
    composition = (factory(repository) if factory is not None
                   else ConnectSafelyComposition(ConnectSafelyApplication(repository)))
    if (type(composition) is not ConnectSafelyComposition
            or type(composition.application) is not ConnectSafelyApplication
            or composition.application.repo is not repository):
        raise ValueError('invalid_connectsafely_composition')
    composition.attach_server(server)
    server.thunder_connectsafely_composition = composition
    for console in (composition.application.direct, composition.application.social):
        if console is not None:
            console.campaign_sheet_store = getattr(getattr(server, 'thunder_service', None), 'knowledge_store', None)
    from thunder_os.connectsafely_read_targets import build_read_resolver
    try:server.thunder_connectsafely_read_resolver = build_read_resolver(repository)
    except Exception:server.thunder_connectsafely_read_resolver = None
    return composition


def main(args=None, *, connectsafely_factory=None, connectsafely_runtime=None):
    args = sys.argv[1:] if args is None else args
    try:
        if not args or args[0] not in ('importar', 'exportar', 'upsert', 'servir'):
            raise Erro('Uso: painel_servidor.py importar arquivo.json | exportar | upsert | servir')
        if len(args) != (2 if args[0] == 'importar' else 1):
            raise Erro('Argumentos inválidos')
        runtime = connectsafely_runtime
        banco = Banco(RAIZ if runtime is None else runtime['panel_root'],
                      os.environ.get('PAINEL_DB') if runtime is None else runtime['repository'],
                      initialize=runtime is None)
        if args[0] == 'importar':
            itens = carregar(Path(args[1]).read_text(encoding='utf-8'))
            if not isinstance(itens, list):
                raise Erro('Importação exige lista de itens')
            for it in itens:
                banco.salvar('itens', objeto(it), importar=True)
            print(json.dumps({'importados': len(itens)}))
        elif args[0] == 'upsert':
            entrada = sys.stdin.buffer.read(LIMITE + 1) if hasattr(sys.stdin, 'buffer') else sys.stdin.read(LIMITE + 1)
            if len(entrada) > LIMITE:
                raise Erro('Corpo excede 200 KB', 413)
            print(json.dumps(banco.salvar('itens', objeto(carregar(entrada)), upsert=True), ensure_ascii=False))
        elif args[0] == 'exportar':
            print(json.dumps(banco.listar('itens', reconciliar=False), ensure_ascii=False))
        else:
            porta = int(os.environ.get('PAINEL_PORTA', '8787') if runtime is None else runtime['web_port'])
            if not (0 if runtime is not None else 1) <= porta <= 65535:
                raise Erro('PAINEL_PORTA inválida')
            servidor = ThreadingHTTPServer(('127.0.0.1', porta), Handler)
            servidor.banco = banco
            ledger = None
            if runtime is None and external_effects_enabled() and not restricted_mode():
                try:
                    from thunder_os.touch_ledger import TouchLedger
                    ledger = TouchLedger()
                    # Schema provisioning is an explicit operator action, never web startup.
                except Exception:
                    ledger = None
            if runtime is None:repository = Repository(banco.arquivo)
            else:
                from thunder_os.connectsafely_runtime import ExistingRepository
                repository = ExistingRepository(banco.arquivo)
            servidor.thunder_service = ThunderService(repository, ledger,
                knowledge_store=KnowledgeStore(RAIZ if runtime is None else runtime['panel_root']),
                effect_readers=configured_email_readers(repository, read_enabled=(
                    runtime is None and os.environ.get('THUNDER_OS_SEQD_READ_ENABLED', '') == 'true')))
            if runtime is None:
                from thunder_os.instantly_webhook import (InstantlyCapture,
                    INSTANTLY_CAPTURE_SECRET_FILE, load_capture_secret)
                servidor.thunder_instantly_capture = InstantlyCapture(repository,
                    load_capture_secret, enabled=Path(INSTANTLY_CAPTURE_SECRET_FILE).is_file())
                from thunder_os.instantly_reader import InstantlyReader
                servidor.thunder_instantly_reader = InstantlyReader(repository)
                servidor.thunder_instantly_reader.start()
            from thunder_os.reply_control import ReplyControl
            from thunder_os.campaign_registry import CampaignRegistry
            servidor.thunder_reply_control = ReplyControl(repository, CampaignRegistry(repository),
                external_pause_enabled=False)
            from thunder_os.thunder_email_runtime import configured_cycle
            servidor.thunder_email_cycle = configured_cycle(repository)
            from thunder_os.linkedin_preparation import LinkedInPreparation
            servidor.thunder_linkedin_preparation=LinkedInPreparation(CampaignRegistry(repository))
            from thunder_os.campaign_sources import CampaignSources
            servidor.thunder_campaign_sources=CampaignSources(repository)
            configure_connectsafely_server(servidor, servidor.thunder_service.repository, connectsafely_factory)
            seqd_read = os.environ.get('THUNDER_OS_SEQD_READ_ENABLED', '') == 'true'
            seqd_send = external_effects_enabled() and os.environ.get('THUNDER_OS_SEQD_SEND_ENABLED', '') == 'true'
            servidor.thunder_seqd_console = SEQDConsole(servidor.thunder_service.repository,
                read_enabled=seqd_read, send_enabled=seqd_send,
                campaign_sheet_store=servidor.thunder_service.knowledge_store,
                dispatch_authorized=lambda: external_effects_enabled() and (restricted_mode() or os.environ.get('THUNDER_OS_COPILOT_MODE','') == 'human_approval'))
            crm_read = os.environ.get('THUNDER_OS_HUBSPOT_READ_ENABLED', '') == 'true'
            if crm_read:
                from thunder_os.campaign_membership import CampaignMembership
                from thunder_os.campaign_membership_hubspot import CampaignMembershipHubSpot
                from thunder_os.cadence_readonly import CadenceReadonlyConsole, environment_activity_bindings
                from thunder_os.campaign_registry import CampaignRegistry
                servidor.thunder_cadence_readonly = CadenceReadonlyConsole(systemd_hubspot_credential,
                    campaign_registry=CampaignRegistry(servidor.thunder_service.repository),
                    activity_bindings_loader=environment_activity_bindings)
                membership_port = CampaignMembershipHubSpot(mutation_enabled=False)
                servidor.thunder_campaign_membership = CampaignMembership(
                    servidor.thunder_service.repository, membership_port,
                    writer=membership_port, mutation_enabled=False)
                if runtime is None:
                    from thunder_os.panel_verification import PanelVerification,HermesVerifyProcess
                    from thunder_os.agent_gateway import AgentGateway
                    from thunder_os.campaign_scoreboard import CampaignScoreboard
                    servidor.thunder_panel_verifier=PanelVerification(servidor.thunder_cadence_readonly,HermesVerifyProcess.configured())
                    servidor.thunder_panel_gateway=AgentGateway(servidor.thunder_service,RAIZ)
                    from thunder_os.hunter_scoreboard import HunterScoreboard
                    scoreboard_reader=CadenceReadonlyConsole(systemd_hubspot_credential,
                        campaign_registry=CampaignRegistry(repository),
                        activity_bindings_loader=environment_activity_bindings,
                        max_pages=5, max_requests=60, budget_seconds=8)
                    servidor.thunder_campaign_scoreboard=CampaignScoreboard(scoreboard_reader,repository,
                        hunter=HunterScoreboard(scoreboard_reader.adapter, repository))
            crm_write = external_effects_enabled() and not restricted_mode() and os.environ.get('THUNDER_OS_HUBSPOT_WRITE_ENABLED', '') == 'true'
            copilot = external_effects_enabled() and not restricted_mode() and os.environ.get('THUNDER_OS_COPILOT_MODE', '') == 'human_approval'
            servidor.thunder_hubspot_console = HubSpotConsole(servidor.thunder_service.repository,
                read_enabled=crm_read, write_enabled=crm_write,
                dispatch_authorized=lambda: external_effects_enabled() and not restricted_mode() and os.environ.get('THUNDER_OS_COPILOT_MODE','') == 'human_approval')
            servidor.thunder_integration_views = IntegrationViews(servidor.thunder_service.repository,
                runtime={'hubspot': IntegrationRuntime(read_configured=crm_read, write_configured=crm_write,
                    write_authorized=crm_write, external_effects_enabled=crm_write),
                    'seqd_email': IntegrationRuntime(read_configured=seqd_read, write_configured=seqd_send,
                        write_authorized=seqd_send, external_effects_enabled=seqd_send, approval_mode_selected=copilot)})
            from thunder_os.today_readonly import TodayReadonly
            from thunder_os.cadence_readonly import HubSpotReadonlyAdapter
            from thunder_os.hubspot_executor import HubSpotHTTP
            servidor.thunder_today_readonly = TodayReadonly(servidor.thunder_service,
                instantly=getattr(servidor, 'thunder_instantly_reader', None),
                seqd=servidor.thunder_seqd_console,
                hubspot=HubSpotReadonlyAdapter(systemd_hubspot_credential,
                    max_pages=20, max_requests=40, budget_seconds=15) if crm_read else None,
                deals=HubSpotHTTP(systemd_hubspot_credential, timeout=4) if crm_read else None,
                runner=getattr(getattr(servidor, 'thunder_panel_verifier', None), 'runner', None))
            from thunder_os.account_cadence_readonly import AccountCadenceReadonly
            from thunder_os.journey_readonly import JourneyReadonly
            servidor.thunder_account_cadence_readonly = AccountCadenceReadonly(
                RAIZ, repo=servidor.thunder_today_readonly.repo)
            servidor.thunder_journey_readonly = JourneyReadonly(servidor.thunder_today_readonly,
                cadence_reader=getattr(servidor, 'thunder_cadence_readonly', None),
                accounts=servidor.thunder_account_cadence_readonly)
            from thunder_os.apollo_prospect_readonly import ApolloProspectReadonly
            from thunder_os.apollo_sequence_readonly import ApolloSequenceReadonly
            from thunder_os.instantly_reader import private_text
            from thunder_os.commercial_account import CommercialAccountReadonly
            from thunder_os.new_journey import NewJourney
            from thunder_os.new_journey_checks import NewJourneyChecks
            from thunder_os.new_journey_membership import CrossCampaignReadOnly, InstantlyMembershipReadOnly
            from thunder_os.new_journey_refusal import RegisteredRefusalRead
            from thunder_os.external_email_import import APOLLO_TEAM
            apollo_prospects = ApolloProspectReadonly(
                credential_loader=lambda: private_text('/root/.config/thunder/credentials/apollo_api_key.txt'),
                enabled=runtime is None, timeout=8)
            servidor.thunder_new_journey = NewJourney(servidor.thunder_service,
                commercial_accounts=CommercialAccountReadonly(servidor.thunder_account_cadence_readonly),
                apollo=apollo_prospects if runtime is None else None,
                template_reader=ApolloSequenceReadonly(
                    lambda: private_text('/root/.config/thunder/credentials/apollo_api_key.txt'))
                    if runtime is None else None,
                checker=NewJourneyChecks(servidor.thunder_service,
                    hubspot=HubSpotHTTP(systemd_hubspot_credential, timeout=4)
                    if runtime is None and crm_read else None,
                    membership=CrossCampaignReadOnly(apollo_prospects,
                        InstantlyMembershipReadOnly(), apollo_account_id=APOLLO_TEAM)
                    if runtime is None else None,
                    refusals=RegisteredRefusalRead() if runtime is None else None))
            from thunder_os.contact_history import ContactHistoryReadonly
            servidor.thunder_new_journey.history_reader = ContactHistoryReadonly(
                servidor.thunder_new_journey.checker.hubspot,
                getattr(servidor, 'thunder_cadence_readonly', None),
                seqd=servidor.thunder_seqd_console,
                repo=servidor.thunder_today_readonly.repo)
            from thunder_os.cadence_preparation import CadencePreparationReadonly
            from thunder_os.instantly_sequence_readonly import InstantlySequenceReadonly
            servidor.thunder_cadence_preparation = CadencePreparationReadonly(
                servidor.thunder_service, journeys=servidor.thunder_new_journey,
                accounts=servidor.thunder_account_cadence_readonly,
                instantly=InstantlySequenceReadonly() if runtime is None else None)
            from thunder_os.new_journey_import import NewJourneyImport
            servidor.thunder_new_journey_import = NewJourneyImport(
                servidor.thunder_new_journey, servidor.thunder_hubspot_console)
            from thunder_os.company_qualification import CompanyQualification
            servidor.thunder_company_qualification = CompanyQualification(
                servidor.thunder_new_journey, servidor.thunder_hubspot_console)
            servidor.thunder_origin = os.environ.get(
                'THUNDER_OS_ORIGIN', 'https://painel.r3xdigital.tech')
            if runtime is not None:servidor.thunder_origin = runtime['public_origin']
            servidor.thunder_nonces = {}
            servidor.thunder_hermes_credential = os.environ.get('THUNDER_HERMES_CREDENTIAL', '')
            servidor.source_sha = release_source_sha()
            if runtime is not None:
                print(json.dumps({'role':'connectsafely-web','port':servidor.server_port,'config_sha256':runtime['_digest']}),flush=True)
            try:
                servidor.serve_forever()
            finally:
                reader = getattr(servidor, 'thunder_instantly_reader', None)
                if reader is not None:
                    reader.stop()
        return 0
    except Erro as e:
        print(json.dumps({'erro': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({'erro': 'Falha na configuração, banco ou arquivo de entrada'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
