"""Application service and sole workflow state writer."""

from __future__ import annotations

from dataclasses import replace
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import re
import unicodedata
from typing import Any, Mapping
from uuid import uuid4

from .db import Repository, RepositoryConflict
from .models import ContractError, WorkItem, WorkStatus, as_payload, payload_hash, transition
from .approvals import (ApprovalService, ApprovalError, _step_hash, execution_context,
                        execution_request, confirm_execution, validate_execution)
from .campaign_registry import CampaignRegistry
from .learning import LearningEngine
from .research import validate_artifact, research_is_current
from .knowledge import KnowledgeStore, KnowledgeError, validate_catalog
from .cadence import effective_due
from .touch_ledger import TouchLedger, TouchBlocked


class AuthorizationError(ContractError):
    """Authenticated actor lacks the required human or delegated authority."""


class ThunderService:
    HERMES_SCOPES = {"followup", "like", "hubspot_write"}
    SAFE_RETRY_ERRORS = {
        "provider_rejected", "email_rejected", "hubspot_rejected", "channel_not_configured",
    }

    def __init__(self, repository: Repository, ledger: TouchLedger | None = None, hubspot: Any = None, *,
                 knowledge_store: KnowledgeStore | None = None, approved_catalog: Mapping[str, Any] | None = None, campaign_context_loader=None,
                 effect_readers: Mapping[str, Any] | None = None):
        self.repository = repository
        # Trusted composition reader, never a request/provider/model field.
        if campaign_context_loader is not None:
            raise ApprovalError("external_campaign_loader_unsupported")
        self.campaign_registry = CampaignRegistry(repository)
        self.ledger = ledger
        self.hubspot = hubspot
        self.knowledge_store = knowledge_store
        self._approved_catalog = dict(approved_catalog) if approved_catalog is not None else None
        # Trusted composition only: result payloads never provide a reader or credentials.
        self._effect_readers = dict(effect_readers or {})

    @staticmethod
    def _identity_key(value):
        text = str(value or '').lower().strip()
        destination = TouchLedger.identity_destination(text)
        return destination if destination else ('canonical', text)

    @staticmethod
    def _identity_records_match(records):
        if (not isinstance(records, list) or len(records) < 2
                or any(not isinstance(row, Mapping) for row in records)):
            return False
        domains = {str(row.get('domain') or '').lower().strip() for row in records}
        people = {str(row.get('person_key') or '').lower().strip() for row in records}
        if '' in domains or '' in people or len(domains) != 1 or len(people) != 1:
            return False
        if any(row.get('cnpj') is not None for row in records):
            cnpjs = [re.sub(r'[.\s/-]', '', str(row.get('cnpj') or '')) for row in records]
            if len(set(cnpjs)) != 1 or any(not re.fullmatch(r'[0-9]{14}', value) for value in cnpjs):
                return False
        return True

    def identity_execution_error(self, cadence):
        account = str(cadence.get('account_id') or '').lower().strip()
        required = {self._identity_key(cadence.get('person_key'))}
        required.update(self._identity_key(step.get('recipient')) for step in cadence.get('steps', []))
        rows = self.repository.list_state('identity')
        confirmed = set()
        contacts = ({str(cadence['hubspot_contact_id'])} if cadence.get('hubspot_contact_id') is not None else set())
        for row in rows:
            records = row.get('records', [])
            keys = {self._identity_key(row.get('canonical_person'))}
            keys.update(self._identity_key(record.get('person_key'))
                        for record in records if isinstance(record, Mapping))
            keys.update(self._identity_key('hubspot:' + str(record['hubspot_contact_id']))
                        for record in records if isinstance(record, Mapping) and record.get('hubspot_contact_id') is not None)
            matched = keys & required
            if not matched:
                continue
            if (row.get('actor') != 'ygor' or row.get('status') != 'canonical'
                    or not self._identity_records_match(records)
                    or row.get('canonical_account') != account
                    or any(self._identity_key(record.get('person_key')) != self._identity_key(row.get('canonical_person'))
                           for record in records)
                    or any(str(record.get('domain') or '').lower().strip() != account for record in records)):
                return 'identity_resolution_required'
            contacts.update(str(record['hubspot_contact_id']) for record in records
                            if record.get('hubspot_contact_id') is not None)
            confirmed.update(matched)
        if not required <= confirmed or len(contacts) > 1 or (len(required) > 1 and not contacts):
            return 'identity_resolution_required'
        return None

    def canonical_catalog(self) -> dict[str, Any]:
        try:
            if self.knowledge_store is not None:
                return self.knowledge_store.load_catalog()
            return validate_catalog(self._approved_catalog)
        except (KnowledgeError, TypeError) as exc:
            raise ContractError("canonical catalog not provisioned or invalid") from exc

    def current_research(self, account_id: str) -> dict[str, Any]:
        research = self.repository.get_state("research", account_id)
        if not research_is_current(research):
            raise ContractError("complete current canonical research required")
        catalog = self.canonical_catalog()
        if research.get("catalog_hash") != catalog["content_hash"]:
            raise ContractError("canonical catalog changed; review research")
        return research

    @staticmethod
    def cadence_review_hash(cadence: Mapping[str, Any]) -> str:
        return payload_hash({
            "id": cadence["id"], "version": cadence["version"],
            "account_id": cadence["account_id"], "person_key": cadence["person_key"],
            "objective": cadence["objective"], "research_hash": cadence.get("research_hash"),
            "execution_context": cadence.get("execution_context"),
            **({"context_sources": cadence["context_sources"]} if "context_sources" in cadence else {}),
            **({"discarded_step_ids": [s["id"] for s in cadence["steps"] if s.get("discarded")]}
               if any(s.get("discarded") for s in cadence["steps"]) else {}),
            "steps": [ThunderService._approval_step(step) for step in cadence["steps"]],
        })

    def _validate_context_sources(self, cadence, *, required=False):
        sources = cadence.get("context_sources", [])
        if required and not sources:
            raise ApprovalError("context_sources_required")
        if not isinstance(sources, list) or len(sources) > 32:
            raise ApprovalError("context_sources_invalid")
        seen = set()
        for source in sources:
            if (not isinstance(source, Mapping) or set(source) != {"path", "sha256"}
                    or not isinstance(source["path"], str) or not source["path"]
                    or not isinstance(source["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"])):
                raise ApprovalError("context_sources_invalid")
            relative = PurePosixPath(source["path"])
            if (relative.is_absolute() or ".." in relative.parts or "\\" in source["path"]
                    or str(relative) != source["path"] or source["path"] in seen):
                raise ApprovalError("context_sources_invalid")
            seen.add(source["path"])
            if self.knowledge_store is None:
                raise ApprovalError("context_sources_unavailable")
            path = self.knowledge_store.root
            try:
                if path.is_symlink():
                    raise ApprovalError("context_sources_invalid")
                for part in relative.parts:
                    path = path / part
                    if path.is_symlink():
                        raise ApprovalError("context_sources_invalid")
                snapshot = self.knowledge_store.read(source["path"])
            except (KnowledgeError, OSError, ValueError) as exc:
                raise ApprovalError("context_sources_unavailable") from exc
            if not snapshot.validated:
                raise ApprovalError("context_source_not_validated")
            if snapshot.content_hash != source["sha256"]:
                raise ApprovalError("context_source_changed")
        return payload_hash(sources)

    def _selected_steps(self, cadence, step_ids=None):
        steps = cadence["steps"]
        sent = {e["payload"].get("step", {}).get("id") for e in self.repository.effects()
                if e["payload"].get("cadence_id") == cadence["id"] and e["state"] == "sent"}
        available = {s["id"] for s in steps if s["id"] not in sent and not s.get("discarded")}
        if step_ids is not None:
            if (not isinstance(step_ids, list) or not step_ids
                    or any(not isinstance(ident, str) or not ident for ident in step_ids)
                    or len(set(step_ids)) != len(step_ids) or not set(step_ids) <= available):
                raise ApprovalError("invalid_selected_steps")
            available = set(step_ids)
        selected = [s for s in steps if s["id"] in available]
        if not selected:
            raise ApprovalError("cadence has no future steps to approve")
        return selected

    def _check_prior_step_authority(self, cadence, step_ids):
        if step_ids is None:
            return
        prior = self.repository.get_state("approval", cadence["id"])
        future = {s["id"] for s in self._selected_steps(cadence)}
        if (prior and prior.get("status") == "active"
                and (set(prior.get("step_hashes", {})) & future) - set(step_ids)):
            # One existing pack/confirmation per cadence: never replace another
            # future grant implicitly or manufacture a combined human envelope.
            raise ApprovalError("prior_approval_review_required")

    def campaign_sheet_view(self, campaign_id):
        from .campaign_sheet import sheet_view
        try:
            sheet = self.campaign_registry.sheet(campaign_id)
        except (ApprovalError, ValueError, TypeError):
            sheet = None
        return sheet_view(campaign_id, sheet, getattr(self, 'campaign_sheet_store', self.knowledge_store))

    def require_campaign_sheet(self, campaign_id):
        from .campaign_sheet import require_sheet
        return require_sheet(campaign_id, self.campaign_registry.sheet(campaign_id),
            getattr(self, 'campaign_sheet_store', self.knowledge_store))

    def configure_campaign(self, config, actor, *, expected_version):
        return self.campaign_registry.register(config, actor, expected_version)

    def revoke_campaign(self, campaign_id, actor, *, expected_version):
        return self.campaign_registry.revoke(campaign_id, actor, expected_version)

    def _campaign_context(self, cadence):
        context = execution_context(cadence.get("execution_context"))
        try:
            current = execution_context(self.campaign_registry.load(context["campaign_id"]))
        except Exception as exc:
            raise ApprovalError("campaign_context_unavailable") from exc
        if current != context or current["platform"] != "thunder_os":
            raise ApprovalError("campaign_context_changed")
        self.require_campaign_sheet(context['campaign_id'])
        return current

    def campaign_execution_review(self, cadence, *, approval=None, step_ids=None):
        self._validate_context_sources(cadence)
        context = self._campaign_context(cadence)
        if any(s["channel"] not in context["providers"] for s in cadence["steps"]):
            raise ApprovalError("campaign_provider_mismatch")
        hashes = {s["id"]: _step_hash(self._approval_step(s)) for s in cadence["steps"]}
        if approval is not None:
            if step_ids is not None:
                raise ApprovalError("invalid_selected_steps")
            approved = approval.get("step_hashes", {})
            if not approved or any(hashes.get(k) != v for k, v in approved.items()):
                raise ApprovalError("campaign_confirmation_scope_changed")
            if any(s.get("discarded") and s["id"] in approved for s in cadence["steps"]):
                raise ApprovalError("step_discarded")
            hashes = {k: hashes[k] for k in approved}
            version = approval["cadence_version"]
            if cadence["version"] != version + 1:
                raise ApprovalError("campaign_confirmation_scope_changed")
        else:
            selected = self._selected_steps(cadence, step_ids)
            hashes = {s["id"]: hashes[s["id"]] for s in selected}
            version = cadence["version"]
        control = self.repository.get_state("control", "global") or {}
        return execution_request(context, cadence["id"], version, hashes, control.get("version", 0))

    def campaign_review_view(self, cadence):
        context = cadence.get('execution_context') or {}
        view = self.campaign_sheet_view(context.get('campaign_id', ''))
        stale = any(row['status'] == 'hash_mismatch' for row in view['fields'])
        try:
            current = self.campaign_registry.load(context.get('campaign_id', ''))
            stale = stale or context.get('configuration_hash') != current['configuration_hash']
        except ApprovalError:
            pass
        sheet_meta = {'campaign_sheet_status': 'stale' if stale else view['status'],
            'campaign_sheet_version': view['version'],
            'campaign_sheet_label': 'versão anterior' if stale else
                'ficha incompleta' if view['status'] != 'complete' else 'Ficha da campanha verificada'}
        try:
            request = self.campaign_execution_review(cadence)
        except ApprovalError as exc:
            question = ("A ficha mudou. Prepare uma nova revisão e aprove novamente."
                if stale else "Complete e confira as fontes da ficha antes de aprovar o texto."
                if str(exc) == 'campaign_sheet_incomplete' else
                "É no Chattie, Thunder OS ou Instantly? Identifique a campanha e suas regras antes de executar.")
            return {**sheet_meta, "execution_confirmation": None, "execution_question": question}
        context = request["context"]
        question = (f"Confirma executar no Thunder OS a campanha {context['campaign_id']} "
                    f"(versão {context['campaign_version']}, regras {context['rules_version']}), "
                    f"somente o lote {cadence['id']} e os destinatários/textos exibidos?")
        return {**sheet_meta, "execution_confirmation": request, "execution_question": question}

    def validate_campaign_execution(self, cadence, approval, *, channel=None, provider=None):
        context_hash = self._validate_context_sources(cadence)
        if (cadence.get("context_sources") or "context_hash" in approval) and approval.get("context_hash") != context_hash:
            raise ApprovalError("context_source_changed")
        expected = self.campaign_execution_review(cadence, approval=approval)
        validate_execution(approval.get("execution_confirmation"), expected, datetime.now(timezone.utc))
        if channel is not None and (not provider or expected["context"]["providers"].get(channel) != provider):
            raise ApprovalError("campaign_provider_mismatch")
        return expected

    def global_paused(self) -> bool:
        control = self.repository.get_state("control", "global")
        return bool(control and control.get("paused"))

    def set_global_pause(self, paused: bool, actor: str) -> dict[str, Any]:
        if type(paused) is not bool or actor != "ygor":
            raise ContractError("global pause requires Ygor")
        return self.repository.put_state("control", "global", {"paused": paused, "actor": actor})

    def configure_hermes_delegation(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required for delegation")
        if set(data) != {"scopes", "daily_limit", "enabled"}:
            raise ContractError("invalid Hermes delegation")
        scopes = data["scopes"]
        limit = data["daily_limit"]
        if (not isinstance(scopes, list) or not set(scopes) <= self.HERMES_SCOPES
                or not scopes or type(limit) is not int or limit < 1 or limit > 100
                or type(data["enabled"]) is not bool):
            raise ContractError("invalid Hermes delegation")
        return self.repository.put_state("delegation", "hermes", {
            "scopes": sorted(set(scopes)), "daily_limit": limit,
            "enabled": data["enabled"], "actor": actor,
            "audit_actor": "hermes (delegado)",
        })

    def _authorize_effect_actor(
        self, actor: str, action: str, cadence: Mapping[str, Any],
        step: Mapping[str, Any], approval: Mapping[str, Any],
    ) -> str:
        if actor == "ygor":
            return actor
        if actor != "hermes":
            raise AuthorizationError("actor cannot authorize external effects")
        delegation = self.repository.get_state("delegation", "hermes")
        if (not delegation or not delegation.get("enabled")
                or action not in delegation.get("scopes", [])):
            raise AuthorizationError("Hermes delegation is disabled or out of scope")
        if action != "followup" or int(step.get("order", 0)) <= 1:
            raise AuthorizationError("first touch and new people require human authority")
        if approval.get("actor") != "ygor":
            raise AuthorizationError("human-approved cadence required")
        try:
            self.current_research(str(cadence.get("account_id", "")))
        except ContractError as exc:
            raise AuthorizationError("complete canonical research required for delegated dispatch") from exc
        return "hermes (delegado)"

    def _consume_delegated_use(self, action: str, reference_id: str, *, day: str | None = None) -> None:
        day = day or datetime.now(timezone.utc).date().isoformat()
        try:
            self.repository.reserve_delegated_action("hermes", action, day)
        except RepositoryConflict as exc:
            raise AuthorizationError(str(exc)) from exc
        self.repository.append_state("delegation_audit", f"{reference_id}:{uuid4().hex}", {
            "actor": "hermes (delegado)", "action": action, "reference_id": reference_id,
        })

    def _enqueue_authorized_effect(self, effect_key: str, payload: dict[str, Any], actor: str, action: str, reference_id: str) -> dict[str, Any]:
        # Quota and durable intent commit together. Exact replays consume no quota.
        with self.repository.transaction():
            existing = next((job for job in self.repository.effects() if job["effect_key"] == effect_key), None)
            if existing:
                previous = existing["payload"]
                old_scope, new_scope = dict(previous), dict(payload)
                if action == "like":
                    old_scope["step"] = {k: v for k, v in previous["step"].items() if k != "due_at"}
                    new_scope["step"] = {k: v for k, v in payload["step"].items() if k != "due_at"}
                if old_scope != new_scope:
                    raise RepositoryConflict("effect key payload mismatch")
                return existing
            authorization_day = datetime.now(timezone.utc).date().isoformat()
            if actor == "hermes":
                self._consume_delegated_use(action, reference_id, day=authorization_day)
            if action == "like":
                delegation = self.repository.get_state("delegation", "hermes") if actor == "hermes" else None
                self.repository.append_state("effect_authorization", effect_key, {
                    "action": "like", "actor": payload["actor"], "payload_hash": payload_hash(payload),
                    "authorized_by": "ygor" if actor == "ygor" else delegation.get("actor"),
                    "delegation_version": delegation["version"] if delegation else None,
                    "day": authorization_day,
                })
            return self.repository.enqueue_effect(effect_key, payload)

    def _authorize_delegated_scope(self, actor: str, action: str) -> str:
        if actor == "ygor":
            return actor
        delegation = self.repository.get_state("delegation", "hermes") if actor == "hermes" else None
        if (not delegation or not delegation.get("enabled")
                or action not in delegation.get("scopes", [])):
            raise AuthorizationError("human or delegated Hermes authority required")
        return "hermes (delegado)"

    def request_work(
        self, command: Mapping[str, Any], actor: str, idempotency_key: str
    ) -> WorkItem:
        # WorkItem permits 200 characters: reserve 65 for ':' plus SHA-256.
        # SQLite length() stops at NUL; reject controls before any transaction.
        if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                or len(idempotency_key) > 135
                or any(unicodedata.category(char).startswith("C") for char in idempotency_key)):
            raise ContractError("invalid idempotency key")
        # Serialize exact replay, base-key conflict and job creation together.
        # The base key is compared literally, never interpreted as a LIKE pattern.
        with self.repository.transaction():
            return self._request_work_locked(command, actor, idempotency_key)

    def _request_work_locked(
        self, command: Mapping[str, Any], actor: str, idempotency_key: str
    ) -> WorkItem:
        allowed = {"objective", "context"}
        if set(command) - allowed or not isinstance(command.get("objective"), str):
            raise ContractError("invalid work request")
        request_fingerprint = payload_hash({"actor": actor, **dict(command)})
        stored_key = f"{idempotency_key}:{request_fingerprint}"
        existing_exact = self.repository.get_by_idempotency_key(stored_key)
        if existing_exact:
            return existing_exact
        with self.repository._connect() as connection:
            mismatch = connection.execute(
                "SELECT 1 FROM thunder_work_items WHERE substr(idempotency_key,1,length(idempotency_key)-?) = ? LIMIT 1",
                (len(request_fingerprint) + 1, idempotency_key),
            ).fetchone()
        if mismatch:
            raise ContractError("idempotency key payload mismatch")
        item = WorkItem(
            id=f"work-{uuid4().hex}",
            objective=command["objective"].strip(),
            actor=actor,
            idempotency_key=stored_key,
            context=dict(command.get("context", {})),
            logical_owner=actor,
        )
        if re.search(r"\b(enviar|publicar|gastar|pagar|deletar|excluir|aprovar)\b", item.objective, re.I):
            item = replace(
                item, status=WorkStatus.NEEDS_HUMAN,
                next_action="Ygor decide alvo, conteúdo, canal e custo antes do efeito",
            )
            return self.repository.save_with_event(item, 0, "authority_decision_required")
        return self.repository.save_with_event(item, 0, "work_requested", "agent_job")

    def _all_created_events(self) -> list[dict[str, Any]]:
        with self.repository._connect() as con:
            rows = con.execute("SELECT payload FROM thunder_events WHERE type='work_requested'").fetchall()
        import json
        return [{"payload": json.loads(row["payload"])} for row in rows]

    def claim(self, item_id: str, expected_version: int, actor: str) -> WorkItem:
        item = self._item(item_id)
        updated = transition(item, expected_version, WorkStatus.IN_PROGRESS)
        updated = replace(updated, actor=actor, next_action="aguardar resultado")
        return self.repository.save_with_event(updated, expected_version, "work_started")

    def command(
        self, item_id: str, expected_version: int, command: str, actor: str
    ) -> WorkItem:
        if command in {"approve", "send", "spend", "succeed"}:
            raise ContractError("authority or evidence required")
        raise ContractError("unsupported command")

    def fail_work(self, item_id: str, expected_version: int, reason: str, actor: str) -> WorkItem:
        if not reason.strip():
            raise ContractError("failure reason required")
        item = self._item(item_id)
        updated = transition(item, expected_version, WorkStatus.FAILED)
        updated = replace(updated, actor=actor, result={"error_code": "agent_failed"}, next_action=f"revisar falha: {reason}")
        return self.repository.save_with_event(updated, expected_version, "work_failed")

    def record_job_result(
        self,
        item_id: str,
        expected_version: int,
        result: Mapping[str, Any],
        actor: str,
    ) -> WorkItem:
        if "verification" in result:
            raise ContractError("verification metadata is server-owned")
        evidence = result.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ContractError("result evidence required")
        with self.repository.transaction():
            item = self._item(item_id)
            failure = result.get("dependency_failure")
            verification = self._validate_effect_result(item, result) if not failure else None
            target = WorkStatus.NEEDS_HUMAN if failure else WorkStatus.SUCCEEDED
            updated = transition(item, expected_version, target)
            next_action = f"revisar dependência: {failure}" if failure else "resultado disponível"
            stored_result = {**dict(result), **({'verification': verification} if verification else {})}
            updated = replace(updated, actor=actor, result=stored_result, next_action=next_action)
            return self.repository.save_with_event(
                updated,
                expected_version,
                "work_needs_human" if failure else "work_succeeded",
            )

    @staticmethod
    def _receipt_conflict(cadence, effect):
        step_id = effect['payload'].get('step', {}).get('id')
        step = next((step for step in cadence.get('steps', []) if step.get('id') == step_id), {})
        return (effect.get('state') == 'sent'
                and step.get('status') in {'failed', 'uncertain', 'needs_reconciliation'})

    def _validate_effect_result(self, item, result):
        operational = (item.context.get('effect_id') is not None or 'effect_id' in result
                       or result.get('status') in {'sent', 'verified', 'executed', 'confirmed', 'delivered'}
                       or any(key in result for key in ('receipt_ref', 'provider_receipt', 'readback')))
        if not operational:
            return
        effect_id = item.context.get('effect_id')
        if not isinstance(effect_id, str) or result.get('effect_id') != effect_id:
            raise ContractError('effect result binding required')
        effect = self.repository.effect(effect_id)
        if not effect or effect.get('state') != 'sent' or not effect.get('receipt_ref'):
            raise ContractError('effect receipt not confirmed')
        payload = effect['payload']
        step = payload.get('step', {})
        cadence_id, channel = payload.get('cadence_id'), step.get('channel')
        if (not cadence_id or item.context.get('cadence_id') != cadence_id
                or result.get('cadence_id') != cadence_id or item.context.get('channel') != channel
                or result.get('channel') != channel or result.get('receipt_ref') != effect['receipt_ref']
                or result.get('status') not in {'sent', 'verified'}):
            raise ContractError('effect result scope mismatch')
        cadence = self.repository.get_state('cadence', cadence_id)
        if not cadence or self._receipt_conflict(cadence, effect):
            raise ContractError('effect receipt conflicts with cadence')
        for declared in (item.context, result):
            if (('account_id' in declared and declared['account_id'] != cadence['account_id'])
                    or ('person_key' in declared and declared['person_key'] != cadence['person_key'])
                    or ('recipient' in declared and declared['recipient'] != step.get('recipient'))):
                raise ContractError('effect result identity mismatch')
        expected = {'target': step.get('recipient'), 'content': step.get('content'), 'effect_id': step.get('id')}
        if channel == 'email':
            expected['subject'] = step.get('subject')
        if result.get('readback') != expected:
            raise ContractError('effect readback required')
        reader = self._effect_readers.get(channel)
        if reader is None or not callable(getattr(reader, 'readback', None)):
            raise ContractError('effect readback unavailable')
        try:
            binding = self.campaign_registry.binding(cadence['execution_context']['campaign_id'])
            provider = cadence['execution_context']['providers'].get(channel)
            if (reader.provider_name != provider or not reader.account_id
                    or binding['provider_accounts'].get(channel) != reader.account_id):
                raise ContractError('effect readback account mismatch')
            observed = reader.readback(effect['receipt_ref'])
        except ContractError:
            raise
        except Exception as exc:
            raise ContractError('effect readback unavailable') from exc
        if observed != expected:
            raise ContractError('effect readback mismatch')
        if (self.repository.effect(effect_id) != effect
                or self.repository.get_state('cadence', cadence_id) != cadence
                or self.campaign_registry.binding(cadence['execution_context']['campaign_id']) != binding):
            raise ContractError('effect changed during readback')
        return {'source': 'configured_provider_readback', 'effect_id': effect_id,
                'receipt_ref': effect['receipt_ref'], 'channel': channel,
                'provider': provider, 'account_id': reader.account_id,
                'scope': 'message_in_sender_account_not_delivery'}

    def _item(self, item_id: str) -> WorkItem:
        item = self.repository.get_work(item_id)
        if not item:
            raise ContractError("work item not found")
        return item

    def submit_research(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        dossier = data.get("dossier")
        if set(data) != {"dossier"} or not isinstance(dossier, Mapping):
            raise ContractError("invalid research command; client approval catalogs are forbidden")
        catalog = self.canonical_catalog()
        artifact = validate_artifact(dossier, set(catalog["capabilities"]), set(catalog["proofs"]))
        account_id = str(dossier["account"].get("domain", "")).lower().strip()
        if not account_id:
            raise ContractError("canonical account domain required")
        # A fresh collection timestamp or an empty fact list must never imply that
        # research is ready. validate_artifact already applies the full evidence gate.
        status = "validated" if artifact.recommendation == "abordar" else "insufficient"
        stored = self.repository.put_state(
            "research", account_id,
            {**asdict(artifact), "actor": actor, "status": status,
             "knowledge_source": "thunder/catalog.json", "catalog_hash": catalog["content_hash"],
             "catalog_version": catalog["version"]},
        )
        self.repository.append_state(
            "research_history", f"{account_id}:v{stored['version']}",
            {**stored, "source_dates": [{"source": fact["source"], "published_at": fact["published_at"],
                "collected_at": fact["observed_at"]} for fact in dossier["facts"]]},
        )
        return stored

    def create_cadence(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        required = {"id", "account_id", "person_key", "objective", "steps"}
        if set(data) - (required | {"execution_context", "context_sources"}) or not required <= set(data) or not isinstance(data.get("steps"), list) or not data["steps"]:
            raise ContractError("invalid cadence")
        self._validate_context_sources(data)
        if "execution_context" in data:
            execution_context(data["execution_context"])
        research = self.current_research(str(data["account_id"]))
        expected_order = 1
        normalized = []
        base_due = datetime.now(timezone.utc)
        for raw in data["steps"]:
            if not isinstance(raw, Mapping):
                raise ContractError("invalid cadence step")
            required_step = {"id", "recipient", "channel", "content", "order", "interval_hours"}
            allowed = required_step | {"subject"}
            if set(raw) - allowed or not required_step <= set(raw) or raw["order"] != expected_order:
                raise ContractError("invalid cadence step")
            if raw["channel"] == "email" and not isinstance(raw.get("subject"), str):
                raise ContractError("email subject required")
            if self.ledger:
                try:
                    self.ledger.validate_identity(raw)
                except (ValueError, KeyError) as exc:
                    raise ContractError("recipient identity unresolved") from exc
            if type(raw["interval_hours"]) is not int or not 0 <= raw["interval_hours"] <= 87600:
                raise ContractError("invalid cadence interval")
            if not isinstance(raw["id"], str) or not raw["id"].strip() or any(s["id"] == raw["id"] for s in normalized):
                raise ContractError("duplicate or invalid cadence step id")
            due_at = base_due + timedelta(hours=raw["interval_hours"])
            normalized.append({**dict(raw), "status": "draft", "due_at": due_at.isoformat(),
                               "predecessor_id": normalized[-1]["id"] if normalized else None})
            base_due = due_at
            expected_order += 1
        unavailable = [step["channel"] for step in normalized if step["channel"] not in {"linkedin", "email"}]
        return self.repository.put_states_atomic([(
            "cadence", str(data["id"]),
            {**dict(data), "steps": normalized, "actor": actor, "status": "ready_for_review",
             "source_context": "; ".join(
                 f"{fact['source']} · publicado em {fact['published_at'] or 'data ausente'} · coleta {fact['observed_at']}"
                 for fact in research["facts"]
             ),
             "research_sources": [
                 {"source": fact["source"], "published_at": fact["published_at"],
                  "collected_at": fact["observed_at"]} for fact in research["facts"]
             ],
             "thunder_knowledge_source": research["knowledge_source"],
             "research_hash": payload_hash({"version": research["version"], "facts": research["facts"]}),
             "consequence": "autoriza somente os toques exatos deste pacote", "cost": "sem custo de provider nesta configuração",
             **({"status": "needs_channel_decision", "unavailable_channels": unavailable,
                 "alternative": {"channel": "email", "status": "proposal_only", "requires_approval": True}}
                if unavailable else {})}, 0,
        )])[0]

    @staticmethod
    def _approval_step(step: Mapping[str, Any]) -> dict[str, Any]:
        fields = {"id", "recipient", "channel", "content", "order", "interval_hours", "subject"}
        return {key: value for key, value in step.items() if key in fields}

    def review_step(self, cadence_id: str, step_id: str, actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required to review a cadence")
        cadence = self.repository.get_state("cadence", cadence_id)
        if not cadence:
            raise ContractError("cadence not found")
        step = next((s for s in cadence["steps"] if s["id"] == step_id), None)
        if not step:
            raise ContractError("cadence step not found")
        blockers = []
        confirmation = None
        try:
            self._validate_context_sources(cadence, required=True)
            if cadence.get("status") not in {"ready_for_review", "approved"}:
                raise ApprovalError("cadence is paused or awaiting review")
            if self.global_paused():
                raise ApprovalError("global pause active")
            selected = self._selected_steps(cadence, [step_id])
            research = self.current_research(str(cadence["account_id"]))
            if cadence.get("research_hash") != payload_hash({"version": research["version"], "facts": research["facts"]}):
                raise ApprovalError("research changed; prepare a new cadence review")
            identity_error = self.identity_execution_error(cadence)
            if identity_error:
                raise ApprovalError(identity_error)
            self._check_prior_step_authority(cadence, [step_id])
            if self._approval_preflight(selected, require_ledger=True):
                raise ApprovalError("duplicate_touch_review_required")
            confirmation = self.campaign_execution_review(cadence, step_ids=[step_id])
        except (ContractError, TouchBlocked) as exc:
            blocker = str(exc)
            if (blocker == "campaign_context_changed"
                    and (cadence.get("execution_context") or {}).get("platform") != "thunder_os"):
                blocker = "external_campaign_executor_not_supported"
            blockers.append(blocker)
        return {"cadence_id": cadence_id, "step_id": step_id,
                "expected_version": cadence["version"], "review_hash": self.cadence_review_hash(cadence),
                "execution_confirmation": confirmation, "content": step["content"],
                "subject": step.get("subject"), "context_hash": payload_hash(cadence.get("context_sources", [])),
                "can_approve": not blockers, "blockers": blockers}

    def _approval_preflight(self, steps, *, require_ledger=False):
        duplicates = []
        if require_ledger and self.ledger is None:
            raise ApprovalError("touch_ledger_unavailable")
        if self.ledger:
            for step in steps:
                scoped = self._approval_step(step)
                evidence = self.ledger.preflight(_step_hash(scoped), scoped)
                if require_ledger and evidence.get("blocked"):
                    raise ApprovalError(str(evidence["blocked"]))
                if evidence.get("duplicate"):
                    duplicates.append(evidence)
        return duplicates

    def approve_cadence(self, cadence_id: str, actor: str, *, expected_version: int, review_hash: str, execution_confirmation=None, step_ids=None) -> dict[str, Any]:
        with self.repository.transaction():
            return self._approve_cadence(cadence_id, actor, expected_version=expected_version,
                review_hash=review_hash, execution_confirmation=execution_confirmation, step_ids=step_ids)

    def _approve_cadence(self, cadence_id, actor, *, expected_version, review_hash, execution_confirmation, step_ids):
        if actor != "ygor":
            raise AuthorizationError("human authority required to approve a cadence")
        cadence = self.repository.get_state("cadence", cadence_id)
        if not cadence:
            raise ContractError("cadence not found")
        if type(expected_version) is not int or cadence["version"] != expected_version or self.cadence_review_hash(cadence) != review_hash:
            raise RepositoryConflict("stale cadence review")
        if step_ids is not None:
            self._validate_context_sources(cadence, required=True)
        research = self.current_research(str(cadence["account_id"]))
        if cadence.get("research_hash") != payload_hash({"version": research["version"], "facts": research["facts"]}):
            raise RepositoryConflict("research changed; prepare a new cadence review")
        if cadence.get("status") not in {"ready_for_review", "approved"}:
            raise ContractError("cadence requires a channel decision")
        now = datetime.now(timezone.utc)
        if self.global_paused():
            raise ContractError("global pause active")
        selected = self._selected_steps(cadence, step_ids)
        self._check_prior_step_authority(cadence, step_ids)
        request = self.campaign_execution_review(cadence, step_ids=step_ids)
        confirmation = confirm_execution(execution_confirmation, request, actor, now + timedelta(days=14), now)
        identity_error = self.identity_execution_error(cadence)
        if identity_error:
            raise ContractError(identity_error)
        sent_ids = {effect["payload"].get("step", {}).get("id") for effect in self.repository.effects()
                    if effect["payload"].get("cadence_id") == cadence_id and effect["state"] == "sent"}
        scoped_steps = [self._approval_step(step) for step in selected]
        if not scoped_steps:
            raise ContractError("cadence has no future steps to approve")
        duplicate_evidence = self._approval_preflight(scoped_steps, require_ledger=step_ids is not None)
        if duplicate_evidence:
            return self.repository.put_state("approval", cadence_id, {
                "actor": actor, "status": "needs_reconciliation", "duplicate_evidence": duplicate_evidence,
                "next_action": "revisar toque já reservado ou executado",
            })
        pack = ApprovalService(self.global_paused).approve_pack(
            cadence_id, cadence["version"], scoped_steps, actor,
            now + timedelta(days=14), now,
        )
        approval_payload = {
            "actor": actor, "cadence_version": pack.cadence_version,
            "step_ids": list(pack.step_ids), "step_hashes": dict(pack.step_hashes),
            "expires_at": pack.expires_at.isoformat(), "status": pack.status,
            "review_hash": review_hash, "execution_confirmation": confirmation,
            **({"context_hash": self._validate_context_sources(cadence)} if "context_sources" in cadence else {}),
        }
        cadence_payload = {k: v for k, v in cadence.items() if k not in {"id", "version", "updated_at"}}
        cadence_payload.update(status="approved", steps=[
            {**step, "status": "sent" if step["id"] in sent_ids else
                "approved" if step["id"] in pack.step_ids else step.get("status", "draft")}
            for step in cadence["steps"]])
        return self.repository.approve_cadence_atomic(
            cadence_id, expected_version, review_hash, approval_payload, cadence_payload,
            review_hasher=self.cadence_review_hash, research_version=research["version"],
        )

    def discard_step(self, cadence_id: str, step_id: str, actor: str, *, expected_version: int,
                     review_hash: str, reason: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required to discard a step")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise ContractError("discard reason required")
        with self.repository.transaction():
            cadence = self.repository.get_state("cadence", cadence_id)
            if (not cadence or type(expected_version) is not int or cadence["version"] != expected_version
                    or self.cadence_review_hash(cadence) != review_hash):
                raise RepositoryConflict("stale cadence review")
            self._selected_steps(cadence, [step_id])
            if cadence.get("status") not in {"ready_for_review", "approved"}:
                raise ContractError("paused or blocked cadence cannot be changed by discard")
            approval = self.repository.get_state("approval", cadence_id)
            updates = []
            if approval:
                updates.append(("approval", cadence_id, {**approval, "status": "invalidated",
                    "invalidated_step_ids": list(approval.get("step_ids", [])),
                    "next_action": "review required after step discard"}, approval["version"]))
            updates.append(("cadence", cadence_id, {**cadence, "status": "ready_for_review",
                "steps": [{**s, "discarded": True, "discard_reason": reason.strip()} if s["id"] == step_id
                          else s for s in cadence["steps"]], "actor": actor}, expected_version))
            return self.repository.put_states_atomic(updates)[-1]

    def dispatch_step(self, cadence_id: str, step_id: str, actor: str) -> dict[str, Any]:
        cadence = self.repository.get_state("cadence", cadence_id)
        approval = self.repository.get_state("approval", cadence_id)
        if not cadence or not approval or approval.get("status") != "active":
            raise ContractError("active approval required")
        if self.global_paused() or cadence.get("status") != "approved":
            raise ContractError("cadence is paused or awaiting review")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(str(approval["expires_at"])):
            self.repository.put_state("approval", cadence_id, {**{k: v for k, v in approval.items() if k not in {"id", "version", "updated_at"}}, "status": "expired", "next_action": "revisar pacote expirado"})
            self.repository.put_state("cadence", cadence_id, {
                **{k: v for k, v in cadence.items() if k not in {"id", "version", "updated_at"}},
                "status": "ready_for_review", "review_reason": "autorização expirada",
            })
            raise ContractError("approval expired; review required")
        self.validate_campaign_execution(cadence, approval)
        identity_error = self.identity_execution_error(cadence)
        if identity_error:
            raise ContractError(identity_error)
        step = next((value for value in cadence["steps"] if value["id"] == step_id), None)
        if not step:
            raise ContractError("cadence step not found")
        effect_key = approval["step_hashes"].get(step_id)
        if not effect_key:
            raise ContractError("step is not approved")
        research = self.current_research(str(cadence["account_id"]))
        if cadence.get("research_hash") != payload_hash({"version": research["version"], "facts": research["facts"]}):
            raise ContractError("research changed; review cadence before dispatch")
        due_at = effective_due(step, self.repository.effects(), cadence_id)
        if due_at is None or datetime.now(timezone.utc) < due_at:
            raise ContractError("step is not due or predecessor confirmation time is missing")
        if step.get("predecessor_id"):
            predecessor = next((effect for effect in self.repository.effects()
                                if effect["payload"].get("cadence_id") == cadence_id
                                and effect["payload"].get("step", {}).get("id") == step["predecessor_id"]), None)
            if not predecessor or predecessor["state"] != "sent":
                raise ContractError("predecessor is not confirmed")
        if step.get("dependency_status") in {"failed", "uncertain", "blocked"}:
            raise ContractError("dependency blocks step")
        if self.ledger:
            self.ledger.preflight(effect_key, step, raise_blocked=True)
        audit_actor = self._authorize_effect_actor(actor, "followup", cadence, step, approval)
        job = self._enqueue_authorized_effect(
            effect_key, {"cadence_id": cadence_id, "step": step, "approval_hash": effect_key,
                         "actor": audit_actor, "execution_confirmation_hash": payload_hash(approval["execution_confirmation"]),
                         **({"delegated_action": "followup"} if actor == "hermes" else {})},
            actor, "followup", f"{cadence_id}:{step_id}",
        )
        return {**job, "payload": job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])}

    def dispatch_like(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        raise ApprovalError("campaign_confirmation_integration_required")
        if set(data) != {"id", "person_key", "post_url"} or not all(
            isinstance(data.get(field), str) and data[field].strip()
            for field in ("id", "person_key", "post_url")
        ):
            raise ContractError("invalid delegated like")
        if self.global_paused():
            raise ContractError("global pause active")
        audit_actor = self._authorize_delegated_scope(actor, "like")
        step = {
            "id": str(data["id"]), "recipient": str(data["person_key"]),
            "post_url": str(data["post_url"]), "channel": "linkedin", "action": "like",
            "content": "like", "order": 1, "interval_hours": 0,
            "due_at": datetime.now(timezone.utc).isoformat(), "predecessor_id": None,
        }
        try:
            if self.ledger:
                self.ledger.validate_identity(step)
            effect_key = payload_hash({"action": "like", "person_key": step["recipient"], "post_url": step["post_url"]})
            if self.ledger:
                self.ledger.preflight(effect_key, step, raise_blocked=True)
        except (ValueError, KeyError) as exc:
            raise ContractError("invalid like identity or post") from exc
        job = self._enqueue_authorized_effect(effect_key, {
            "step": step, "approval_hash": effect_key, "actor": audit_actor,
            "delegated_action": "like",
        }, actor, "like", f"like:{data['id']}")
        return {**job, "payload": job["payload"] if isinstance(job["payload"], dict) else __import__("json").loads(job["payload"])}

    def revise_cadence(self, cadence_id: str, steps: list[Mapping[str, Any]], expected_version: int, actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required to revise a cadence")
        with self.repository.transaction():
            return self._revise_cadence(cadence_id, steps, expected_version, actor)

    def _revise_cadence(self, cadence_id, steps, expected_version, actor):
        cadence = self.repository.get_state("cadence", cadence_id)
        if not cadence or cadence["version"] != expected_version:
            raise ContractError("stale cadence version")
        if not isinstance(steps, list) or not steps or any(not isinstance(step, Mapping) for step in steps):
            raise ContractError("invalid revised steps")
        old_steps = {step["id"]: step for step in cadence["steps"]}
        ids = [step.get("id") for step in steps]
        if len(ids) != len(set(ids)) or set(ids) != set(old_steps):
            raise ContractError("revision must preserve unique step ids")
        normalized = []
        for order, raw in enumerate(steps, 1):
            scoped = self._approval_step(raw)
            _step_hash(scoped)
            if raw.get("order") != order or type(raw.get("interval_hours")) is not int or not 0 <= raw["interval_hours"] <= 87600:
                raise ContractError("invalid revised step order or interval")
            original = old_steps[raw["id"]]
            if raw.get("recipient") != original["recipient"]:
                raise ContractError("revision must preserve recipients")
            normalized.append({**original, **scoped, "predecessor_id": steps[order - 2]["id"] if order > 1 else None})
        old = {key: _step_hash(self._approval_step(value)) for key, value in old_steps.items()}
        new = {step["id"]: _step_hash(self._approval_step(step)) for step in normalized}
        changed = [step_id for step_id in old if new.get(step_id) != old[step_id]]
        research = self.current_research(str(cadence["account_id"]))
        research_hash = payload_hash({"version": research["version"], "facts": research["facts"]})
        research_changed = cadence.get("research_hash") != research_hash
        if not changed and not research_changed:
            return cadence
        if cadence.get("status") not in {"ready_for_review", "approved"}:
            raise ContractError("paused or blocked cadence cannot be resumed by revision")
        sent = {effect["payload"].get("step", {}).get("id") for effect in self.repository.effects()
                if effect["payload"].get("cadence_id") == cadence_id and effect["state"] == "sent"}
        if sent.intersection(changed):
            raise ContractError("confirmed steps are immutable")
        affected = [step["id"] for step in normalized if step["id"] not in sent] if research_changed else changed
        normalized = [{**step, "status": "sent" if step["id"] in sent else "draft" if step["id"] in affected else step.get("status", "draft")}
                      for step in normalized]
        approval = self.repository.get_state("approval", cadence_id)
        updates = []
        if approval:
            updates.append(("approval", cadence_id, {
                **{k: v for k, v in approval.items() if k not in {"id", "version", "updated_at"}},
                "status": "partially_invalidated", "invalidated_step_ids": affected,
                "next_action": "revisar passos futuros e pesquisa atualizada",
            }, approval["version"]))
        updates.append(("cadence", cadence_id, {
            **{k: v for k, v in cadence.items() if k not in {"id", "version", "updated_at"}},
            "steps": normalized, "status": "ready_for_review",
            "review_reason": "pesquisa atualizada" if research_changed else "escopo futuro alterado", "actor": actor,
            "research_hash": research_hash,
            "source_context": "; ".join(
                f"{fact['source']} · publicado em {fact['published_at'] or 'data ausente'} · coleta {fact['observed_at']}"
                for fact in research["facts"]
            ),
            "research_sources": [{"source": fact["source"], "published_at": fact["published_at"],
                "collected_at": fact["observed_at"]} for fact in research["facts"]],
            "thunder_knowledge_source": research["knowledge_source"],
        }, expected_version))
        return self.repository.put_states_atomic(updates)[-1]

    def _reply_cadence(self, data):
        cadence = self.repository.get_state('cadence', str(data.get('cadence_id', '')))
        if not cadence:
            raise ContractError('cadence not found')
        account = data.get('account_id', data.get('account_slug'))
        person = cadence.get('person_key') or (
            'hubspot:' + str(cadence['hubspot_contact_id']) if cadence.get('hubspot_contact_id') else None)
        if (not account or cadence.get('account_id') != account
                or (data.get('account_slug') is not None and data['account_slug'] != account)
                or person != data.get('person_key')):
            raise ContractError('reply identity mismatch')
        if data.get('control_source') == 'reply_control':
            if (data.get('identity_confirmed') is not True or data.get('human_reply') is not True
                    or not data.get('hubspot_contact_id')
                    or cadence['id'] not in data.get('paused_cadence_ids', [])
                    or cadence.get('reply_id') != data.get('id')):
                raise ContractError('reply identity unresolved')
        if (data.get('hubspot_contact_id') and cadence.get('hubspot_contact_id')
                and data['hubspot_contact_id'] != cadence['hubspot_contact_id']):
            raise ContractError('reply identity conflict')
        return cadence

    def _reply_cadences(self, cadence, people):
        return [row for row in self.repository.list_state('cadence')
                if row['id'] == cadence['id'] or (
                    row.get('person_key') in people
                    and row.get('account_id') == cadence['account_id']
                    and row.get('hubspot_contact_id') in (None, cadence.get('hubspot_contact_id')))]

    def _reply_suppression_identities(self, data, cadence):
        rows = [row for row in self.repository.list_state('identity')
                if row.get('status') == 'canonical' and row.get('actor') == 'ygor']
        def contacts(row):
            records = row.get('records')
            if not isinstance(records, list) or not all(isinstance(r, Mapping) for r in records):
                raise ContractError('reply identity conflict')
            return {r.get('hubspot_contact_id') for r in records if r.get('hubspot_contact_id') is not None}
        contact = data.get('hubspot_contact_id') or cadence.get('hubspot_contact_id')
        person = str(data['person_key']).lower().strip()
        seeds = [row for row in rows if row.get('canonical_person') == person
                 or (contact is not None and contact in contacts(row))]
        if not seeds:
            return set(), set()
        ids = {ident for row in seeds for ident in contacts(row)} | ({contact} if contact else set())
        if len(ids) > 1:
            raise ContractError('reply identity conflict')
        selected = [row for row in rows if row in seeds or bool(contacts(row) & ids)]
        people = {row.get('canonical_person') for row in selected}
        if (person not in people and person not in {contact, 'hubspot:' + str(contact)}):
            raise ContractError('reply identity mismatch')
        # Only Ygor-confirmed identity rows establish destinations; drafts never do.
        for row in rows:
            if row.get('canonical_person') in people:
                records = row.get('records', [])
                domains = {str(r.get('domain') or '').lower().strip() for r in records}
                keys = {str(r.get('person_key') or '').lower().strip() for r in records}
                if (row.get('canonical_account') != cadence['account_id']
                        or domains != {cadence['account_id']} or keys != {row.get('canonical_person')}
                        or contacts(row) != ids):
                    raise ContractError('reply identity conflict')
        identities = {destination for person in people
                      if (destination := TouchLedger.identity_destination(person)) is not None}
        return identities, people

    @staticmethod
    def _reply_recipient(data, cadences):
        recipients = {step['recipient'] for row in cadences if row['id'] == data['cadence_id']
                      for step in row['steps'] if step.get('channel') == data['channel']
                      and isinstance(step.get('recipient'), str) and step['recipient']}
        if len(recipients) != 1:
            raise ContractError('reply channel identity mismatch')
        return next(iter(recipients))

    def record_reply(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        base = {"id", "channel", "content"}
        allowed = base | {"cadence_id", "account_id", "person_key", "classification", "confidence", "justification", "preceding_touch_id"}
        if set(data) - allowed or not base <= set(data):
            raise ContractError("invalid reply")
        correlated = all(data.get(field) for field in ("cadence_id", "account_id", "person_key", "preceding_touch_id"))
        if not correlated:
            return self.repository.put_state("reply", str(data["id"]), {
                **dict(data), "actor": actor, "state": "identity_resolution",
                "classification_history": [], "next_action": "resolver identidade",
            })
        classification = data.get("classification")
        if classification not in {"positive", "objection", "negative", "opt_out", "risk", "unrelated"}:
            raise ContractError("valid classification required")
        cadence = self._reply_cadence(data)
        negative = classification in {'negative', 'opt_out', 'risk'}
        identities, people = self._reply_suppression_identities(data, cadence)
        cadences = self._reply_cadences(cadence, people)
        recipient = self._reply_recipient(data, [cadence])
        if negative and identities and TouchLedger.identity_destination(recipient) not in identities:
            raise ContractError('reply destination unconfirmed')
        if self.ledger:
            self.ledger.record_reply(
                str(data["channel"]), recipient, str(classification),
                str(data["content"]), str(data["id"]), str(data["preceding_touch_id"]),
                identities=identities,
            )
        from .reply_control import _CadenceBridge
        next_action = "decidir próximo passo comercial" if classification == "positive" else "contato suprimido" if classification in {"negative", "opt_out", "risk"} else "revisar resposta"
        history = [{"classification": classification, "confidence": data.get("confidence", 1.0),
                    "justification": data.get("justification", "classificação informada"), "actor": actor}]
        with self.repository.transaction():
            pending = [row for row in cadences if row.get('status') in {
                'draft', 'ready_for_review', 'approved', 'active', 'blocked',
                'needs_channel_decision', 'paused_reply'}]
            _CadenceBridge(self.repository, pending).pause_for_reply(str(data['person_key']), str(data['id']))
            self.repository.put_state('operator_alert', str(data['id']), {
                'reply_id': str(data['id']), 'active': True, 'priority': 'high', 'owner': 'ygor',
                'reason': 'external_pause_off', 'code': 'external_pause_off',
                'title': 'Revisar resposta e parada dos canais',
                'message': 'Resposta registrada. Pausa externa nao confirmada; revisar canais.',
                'next_action': 'Ygor: revisar a conversa e decidir o proximo passo comercial.',
                'external_pauses_enabled': False,
            })
            return self.repository.put_state(
                "reply", str(data["id"]), {**dict(data), "actor": actor, "state": "classified",
                                            "classification_history": history, "next_action": next_action}
            )

    def classify_reply(self, reply_id: str, classification: str, confidence: float, justification: str, actor: str) -> dict[str, Any]:
        reply = self.repository.get_state("reply", reply_id)
        if not reply or reply.get("state") == "identity_resolution":
            raise ContractError("reply identity unresolved")
        if classification not in {"positive", "objection", "negative", "opt_out", "risk", "unrelated"} or not 0 <= confidence <= 1 or not justification.strip():
            raise ContractError("invalid classification")
        history = list(reply.get("classification_history", []))
        history.append({"classification": classification, "confidence": confidence,
                        "justification": justification, "actor": actor})
        next_action = "decidir próximo passo comercial" if classification == "positive" else "contato suprimido" if classification in {"negative", "opt_out", "risk"} else "revisar resposta"
        if classification in {"negative", "opt_out", "risk"} and self.ledger:
            cadence = self._reply_cadence(reply)
            identities, _people = self._reply_suppression_identities(reply, cadence)
            channel = str(reply['channel'])
            if reply.get('control_source') == 'reply_control':
                if not identities:
                    raise ContractError('reply identity unconfirmed')
                channel, recipient = min(identities)
            else:
                recipient = self._reply_recipient(reply, [cadence])
            if identities and TouchLedger.identity_destination(recipient) not in identities:
                raise ContractError('reply destination unconfirmed')
            if not identities:
                if not reply.get('preceding_touch_id'):
                    raise ContractError('reply identity unconfirmed')
                self.ledger.record_reply(str(reply['channel']), recipient, classification,
                    str(reply['content']), reply_id, str(reply['preceding_touch_id']))
            else:
                self.ledger.suppress(channel, recipient, classification, identities=identities)
        return self.repository.put_state("reply", reply_id, {
            **{k: v for k, v in reply.items() if k not in {"id", "version", "updated_at"}},
            "classification": classification, "confidence": confidence, "justification": justification,
            "classification_history": history, "actor": actor, "next_action": next_action,
            "state": "classified",
        })

    def resolve_reply_identity(self, reply_id: str, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if set(data) != {"account_id", "person_key", "cadence_id", "preceding_touch_id"}:
            raise ContractError("invalid identity resolution")
        reply = self.repository.get_state("reply", reply_id)
        if not reply or reply.get("state") != "identity_resolution":
            raise ContractError("reply is not awaiting identity")
        cadence = self.repository.get_state("cadence", str(data["cadence_id"]))
        if not cadence:
            raise ContractError("cadence not found")
        sent_step_ids = {
            effect["payload"]["step"]["id"] for effect in self.repository.effects()
            if effect["state"] == "sent" and effect["payload"].get("cadence_id") == data["cadence_id"]
        }
        self.repository.put_state("cadence", str(data["cadence_id"]), {
            **{k: v for k, v in cadence.items() if k not in {"id", "version", "updated_at"}},
            "steps": [dict(step) if step.get("id") in sent_step_ids else {**step, "status": "skipped_reply"}
                      for step in cadence["steps"]],
            "status": "paused_reply", "pause_reason": "resposta recebida após resolver identidade",
        })
        return self.repository.put_state("reply", reply_id, {
            **{k: v for k, v in reply.items() if k not in {"id", "version", "updated_at"}},
            **dict(data), "actor": actor, "state": "needs_classification",
            "next_action": "classificar resposta",
        })

    def request_copy(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        required = {"account_id", "capability_ids", "proof_ids", "final_copy"}
        if set(data) != required:
            raise ContractError("invalid copy request")
        research = self.repository.get_state("research", str(data["account_id"]))
        if (not research or set(data["capability_ids"]) - set(research["capability_ids"])
                or set(data["proof_ids"]) - set(research["proof_ids"])):
            raise ContractError("approved research evidence required for copy")
        missing = list(research.get("missing", []))
        catalog = self.canonical_catalog()
        complete = (research.get("status") == "validated"
                    and research.get("recommendation") == "abordar"
                    and research_is_current(research)
                    and research.get("catalog_hash") == catalog["content_hash"])
        if not complete and not missing:
            missing.append("pesquisa vencida ou catálogo alterado")
        if actor != "ygor" and not complete:
            raise AuthorizationError("complete canonical research required for agent copy")
        status = "ready_for_review" if complete else "draft_missing_evidence"
        return self.repository.put_state("copy", str(data["account_id"]), {
            **dict(data), "actor": actor, "status": status, "missing": missing,
            "warning": None if complete else f"faltam: {', '.join(missing)}",
            "delegated_dispatch_allowed": complete,
        })

    def request_paid_query(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if set(data) != {"id", "target", "purpose", "cost", "approved"} or data["approved"] is not False:
            raise ContractError("paid query requires preview before approval")
        return self.repository.put_state("paid_query", str(data["id"]), {
            **dict(data), "actor": actor, "status": "awaiting_specific_authorization",
            "next_action": "Ygor autoriza esta consulta e este custo",
        })

    def authorize_capabilities(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required to authorize capabilities")
        if set(data) != {"id", "capabilities"} or not isinstance(data["capabilities"], list):
            raise ContractError("invalid capability authorization")
        channels = [item.get("channel") for item in data["capabilities"] if isinstance(item, Mapping)]
        if set(channels) != {"linkedin", "email", "whatsapp", "hubspot"} or len(channels) != 4:
            raise ContractError("four independent capabilities required")
        grants = [{"channel": item["channel"], "hash": payload_hash(dict(item)), "status": "authorized"}
                  for item in data["capabilities"]]
        return self.repository.put_state("capability_authorization", str(data["id"]), {
            "actor": actor, "grants": grants, "independent": True,
        })

    def canonical_context(self, account_id: str) -> dict[str, Any]:
        research = self.repository.get_state("research", account_id)
        if not research:
            raise ContractError("account not found")
        cadences = [value for value in self.repository.list_state("cadence") if value.get("account_id") == account_id]
        effects = [value for value in self.repository.effects() if value["payload"].get("cadence_id") in {c["id"] for c in cadences}]
        replies = [value for value in self.repository.list_state("reply") if value.get("account_id") == account_id]
        return {
            "account_id": account_id, "account": research["account"], "people": sorted({c["person_key"] for c in cadences}),
            "channels": sorted({step["channel"] for cadence in cadences for step in cadence.get("steps", [])}),
            "signals": research["favorable_facts"], "sources": research["facts"],
            "last_touch": effects[-1] if effects else None, "history": effects + replies,
            "blocks": [effect["error_code"] for effect in effects if effect.get("error_code")],
            "canonical": True, "history_authority": "touch_ledger_and_reply_state",
            "history_copied": False,
        }

    def resolve_identity(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if set(data) != {"id", "records"} or not isinstance(data["records"], list) or len(data["records"]) < 2:
            raise ContractError("identity records required")
        if any(not isinstance(record, Mapping) for record in data["records"]):
            raise ContractError("invalid identity record")
        domains = {str(record.get("domain") or "").lower().strip() for record in data["records"]} - {""}
        people = {str(record.get("person_key") or "").lower().strip() for record in data["records"]} - {""}
        resolved = self._identity_records_match(data['records'])
        return self.repository.put_state("identity", str(data["id"]), {
            "records": [dict(record) for record in data["records"]], "actor": actor,
            "status": "canonical" if resolved else "identity_resolution",
            "canonical_account": next(iter(domains)) if resolved else None,
            "canonical_person": next(iter(people)) if resolved else None,
            "next_action": "usar ficha canônica" if resolved else "resolver identidade",
        })

    def record_meeting(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        required = {"id", "account_id", "person_key", "occurred_at", "hubspot_effect"}
        if set(data) != required:
            raise ContractError("invalid meeting")
        audit_actor = self._authorize_delegated_scope(actor, "hubspot_write")
        if actor == "hermes" and self.hubspot is not None:
            self._consume_delegated_use("hubspot_write", f"meeting:{data['id']}")
        pending = self.repository.put_state("meeting", str(data["id"]), {
            **dict(data), "actor": audit_actor, "status": "mirror_pending", "next_action": "confirmar readback no HubSpot",
        })
        if self.hubspot is None:
            return pending
        raise ApprovalError("campaign_confirmation_integration_required")
        result = self.hubspot.mirror(data["hubspot_effect"])
        return self.repository.put_state("meeting", str(data["id"]), {
            **dict(data), "actor": audit_actor,
            "status": "mirrored" if result.status == "confirmed" else "mirror_pending",
            "receipt_ref": result.receipt_ref, "next_action": "histórico atualizado" if result.status == "confirmed" else "reconciliar HubSpot",
        })

    def _retry_scope(self, effect_id):
        effect = self.repository.effect(effect_id)
        if not effect:
            raise ContractError("effect not found")
        if effect["state"] != "failed" or effect["error_code"] not in self.SAFE_RETRY_ERRORS:
            raise RepositoryConflict("effect is not safely retryable")
        cadence = self.repository.get_state('cadence', effect['payload'].get('cadence_id', ''))
        if not cadence or cadence.get('status') not in {'approved', 'ready_for_review'} or self.global_paused():
            raise ContractError('cadence is paused or awaiting review')
        identity_error = self.identity_execution_error(cadence)
        if identity_error:
            raise ContractError(identity_error)
        research = self.current_research(str(cadence['account_id']))
        if cadence.get('research_hash') != payload_hash({'version': research['version'], 'facts': research['facts']}):
            raise ContractError('research changed; review cadence before dispatch')
        step = next((step for step in cadence.get('steps', [])
                     if step.get('id') == effect['payload'].get('step', {}).get('id')), None)
        if (step is None or _step_hash(self._approval_step(step)) != effect['effect_key']
                or effect['payload'].get('approval_hash') != effect['effect_key']):
            raise ApprovalError('campaign_confirmation_scope_changed')
        context = self._campaign_context(cadence)
        if step['channel'] not in context['providers']:
            raise ApprovalError('campaign_provider_mismatch')
        due = effective_due(step, self.repository.effects(), cadence['id'])
        if due is None or due > datetime.now(timezone.utc):
            raise ContractError('step is not due or predecessor confirmation time is missing')
        control = self.repository.get_state('control', 'global') or {}
        request = execution_request(context, cadence['id'], cadence['version'],
                                    {step['id']: effect['effect_key']}, control.get('version', 0))
        return effect, cadence, step, research, request

    def retry_execution_review(self, effect_id):
        """Read-only preview using the existing exact execution confirmation contract."""
        return self._retry_scope(effect_id)[4]

    def retry_effect(self, effect_id: str, actor: str, *, execution_confirmation=None) -> dict[str, Any]:
        if actor != 'ygor':
            raise ContractError('manual retry requires Ygor')
        with self.repository.transaction():
            effect, cadence, step, research, request = self._retry_scope(effect_id)
            if execution_confirmation is None:
                raise ApprovalError('campaign_restart_confirmation_required')
            now = datetime.now(timezone.utc)
            confirmation = confirm_execution(execution_confirmation, request, actor, now + timedelta(days=14), now)
            previous = self.repository.get_state('approval', cadence['id'])
            if (not previous or previous.get('execution_confirmation', {}).get('request') == request
                    or not self.ledger):
                raise ApprovalError('campaign_restart_confirmation_required')
            preflight = self.ledger.preflight(effect['effect_key'], step, raise_blocked=True)
            if preflight.get('duplicate') and preflight['duplicate']['status'] != 'failed':
                raise RepositoryConflict('effect is not safely retryable')
            pack = ApprovalService(self.global_paused).approve_pack(
                cadence['id'], cadence['version'], [self._approval_step(step)], actor,
                now + timedelta(days=14), now)
            review_hash = self.cadence_review_hash(cadence)
            approval_payload = {'actor': actor, 'cadence_version': pack.cadence_version,
                'step_ids': list(pack.step_ids), 'step_hashes': dict(pack.step_hashes),
                'expires_at': pack.expires_at.isoformat(), 'status': pack.status,
                'review_hash': review_hash, 'execution_confirmation': confirmation}
            cadence_payload = {key: value for key, value in cadence.items()
                               if key not in {'id', 'version', 'updated_at'}}
            cadence_payload.update(status='approved', steps=[
                {**value, 'status': 'approved' if value['id'] == step['id']
                 else ('draft' if value.get('status') == 'approved' else value.get('status', 'draft'))}
                for value in cadence['steps']])
            self.repository.approve_cadence_atomic(
                cadence['id'], cadence['version'], review_hash, approval_payload, cadence_payload,
                review_hasher=self.cadence_review_hash, research_version=research['version'])
            self.ledger.release_failed_for_retry(effect['effect_key'])
            return self.repository.requeue_confirmed_effect(effect, payload_hash(confirmation))

    def propose_learning(self, data: Mapping[str, Any], actor: str) -> dict[str, Any]:
        required = {"records", "hypothesis", "population", "review_at"}
        if set(data) != required:
            raise ContractError("invalid learning command")
        result = LearningEngine(10, 0.2).analyze(
            data["records"], str(data["hypothesis"]), str(data["population"]), actor, str(data["review_at"])
        )
        payload = asdict(result.recommendation) if result.recommendation else {"observation": dict(result.observation)}
        return self.repository.put_state("learning", "latest", payload)

    def control_agent(self, command: Mapping[str, Any], actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required")
        if set(command) != {"action", "expected_version"} or command["action"] not in {"pause", "resume", "reset_circuit"} or type(command["expected_version"]) is not int:
            raise ContractError("invalid agent control")
        with self.repository.transaction():
            current = self.repository.get_state("agent_control", "global") or {}
            if current.get("version", 0) != command["expected_version"]:
                raise RepositoryConflict("stale agent control")
            if command["action"] == "resume" and not current.get("enabled", False):
                raise ContractError("agent runtime not configured")
            payload = {k:v for k,v in current.items() if k not in {"id", "version", "updated_at"}}
            payload.update(actor=actor)
            if command["action"] in {"pause", "resume"}:
                payload["paused"] = command["action"] == "pause"
            else:
                payload["circuit_reset_version"] = payload.get("circuit_reset_version", 0) + 1
            return self.repository.put_state("agent_control", "global", payload)

    def cancel_agent_work(self, item_id: str, actor: str) -> dict[str, Any]:
        if actor != "ygor":
            raise AuthorizationError("human authority required")
        with self.repository.transaction():
            item = self._item(item_id)
            if item.status not in {WorkStatus.REQUESTED, WorkStatus.IN_PROGRESS}:
                raise ContractError("work is already terminal")
            return self.repository.put_state("agent_cancel", item_id, {"actor": actor, "cancelled": True})

    def today(self) -> dict[str, Any]:
        values: dict[str, list[dict[str, Any]]] = {}
        partial_errors = []
        readers = {
            "work": lambda: [as_payload(item) for item in self.repository.list_work(limit=100)],
            "research": lambda: self.repository.list_state("research"),
            "cadences": lambda: self.repository.list_state("cadence"),
            "effects": self.repository.effects,
            "replies": lambda: self.repository.list_state("reply"),
            "learning": lambda: self.repository.list_state("learning"),
            "paid_queries": lambda: self.repository.list_state("paid_query"),
            "copies": lambda: self.repository.list_state("copy"),
            "meetings": lambda: self.repository.list_state("meeting"),
            "agent_controls": lambda: [self.repository.get_state("agent_control", "global") or {"enabled": False, "paused": True, "version": 0}],
            "agent_usage": lambda: self.repository.list_state("agent_usage"),
            "operator_alerts": lambda: self.repository.list_state("operator_alert"),
            "agent_circuits": lambda: self.repository.list_state("agent_circuit"),
        }
        for name, reader in readers.items():
            try:
                values[name] = reader()
            except Exception:
                values[name] = []
                partial_errors.append({"source": name, "guidance": f"{name} indisponível; tente novamente"})
        effect_by_step = {
            (effect["payload"].get("cadence_id"), effect["payload"].get("step", {}).get("id")): effect
            for effect in values["effects"]
        }
        values["cadences"] = [{
            **cadence, "review_hash": self.cadence_review_hash(cadence),
            **self.campaign_review_view(cadence),
            "steps": [{**step,
                       "effective_due_at": (due.isoformat() if (due := effective_due(step, values["effects"], cadence["id"])) is not None else None),
                       **({"status": ('needs_reconciliation' if self._receipt_conflict(cadence, effect_by_step[(cadence['id'], step['id'])])
                                      else effect_by_step[(cadence["id"], step["id"])]["state"]),
                           **({'recorded_step_status': step['status'], 'error_code': 'receipt_card_conflict'}
                              if self._receipt_conflict(cadence, effect_by_step[(cadence['id'], step['id'])]) else {}),
                           "receipt_ref": effect_by_step[(cadence["id"], step["id"])].get("receipt_ref")}
                          if (cadence["id"], step.get("id")) in effect_by_step else {})}
                      for step in cadence.get("steps", [])],
        } for cadence in values["cadences"]]
        priorities = []
        for reply in values["replies"]:
            priorities.append({"priority": 100, "reason": "Resultado precisa de decisão",
                               "next_action": reply["next_action"], "account": reply.get("account_id")})
        for cadence in values["cadences"]:
            if cadence.get("status") == "ready_for_review":
                priorities.append({"priority": 90, "reason": cadence.get("review_reason", "Cadência pronta para revisão"),
                                   "next_action": "revisar pacote de contatos", "account": cadence["account_id"]})
            for step in cadence.get("steps", []):
                due_at = effective_due(step, values["effects"], cadence["id"])
                if step.get("status") == "approved" and due_at is not None and due_at <= datetime.now(timezone.utc):
                    priorities.append({"priority": 80, "reason": "Contato está na data combinada",
                                       "next_action": "executar toque autorizado", "account": cadence["account_id"]})
        priorities.sort(key=lambda item: (-item["priority"], str(item.get("account"))))
        return {
            "status": "partial" if partial_errors else "ok", **values,
            "priorities": priorities, "partial_errors": partial_errors,
        }
