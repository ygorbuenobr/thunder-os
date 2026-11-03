"""Read-only support for the panel's "Autorizar LinkedIn" screen.

Nothing here records an authorization, sends a message or calls a provider write
route. The screen records each authorization through the existing endpoints,
after an explicit click on the exact text shown here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import http.client
import json
import os
import re
import ssl
from typing import Any, Mapping
from urllib.parse import quote, urlencode

from .connectsafely_console import ConnectSafelyConsole, ConnectSafelyError
from .hermes_linkedin_cadence import STEP_CONTRACT, approval_payload_hash, plan_hash
from .models import payload_hash

PORTAL_ID = "51412240"
OWNER_ID = "92901049"
CONTACT_ID = re.compile(r"[1-9][0-9]{0,29}")
CONTACT_PROPERTIES = ("firstname", "lastname", "company", "linkedin_url", "hs_linkedin_url", "cad_status")
TASK_PROPERTIES = ("hs_task_subject", "hs_task_body", "hs_task_status", "hs_timestamp", "hubspot_owner_id")
MAX_APPROVAL_HOURS = 168


class AuthorizationReadError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise AuthorizationReadError(code)


def contact_id(value: Any) -> str:
    require(isinstance(value, str) and CONTACT_ID.fullmatch(value), "contact_id_invalid")
    return value


def configured_sender(environ=os.environ) -> dict[str, Any]:
    """Sender identity from the pinned direct runtime config `live` section."""
    from .connectsafely_runtime import load_config
    path, digest = environ.get("THUNDER_OS_CONNECTSAFELY_CONFIG", ""), environ.get("THUNDER_OS_CONNECTSAFELY_CONFIG_SHA256", "")
    require(path and re.fullmatch(r"[0-9a-f]{64}", digest or ""), "sender_config_unavailable")
    try:
        cfg = load_config(path)
    except (OSError, ValueError, KeyError, TypeError):
        raise AuthorizationReadError("sender_config_unavailable") from None
    require(cfg["_digest"] == digest, "sender_config_changed")
    live = cfg.get("live")
    require(isinstance(live, dict) and isinstance(live.get("account_id"), str)
            and isinstance(live.get("public_id"), str), "sender_config_unavailable")
    canary = live.get("canary_readers") if isinstance(live.get("canary_readers"), dict) else {}
    verified = sorted(str(row.get("hubspot_contact_id")) for row in canary.get("contacts", [])
                      if isinstance(row, dict) and "linkedin" in (row.get("channels") or []))
    return {"account_id": live["account_id"], "public_id": live["public_id"],
            "profile_url": "https://www.linkedin.com/in/" + live["public_id"] + "/",
            "source": "direct_runtime_live", "identity_verifier_contacts": verified}


def validation_console(repository, sender: Mapping[str, Any]) -> ConnectSafelyConsole:
    """Plan validation only: no client, ledger or write capability."""
    return ConnectSafelyConsole(repository, None, None, account_id=sender["account_id"],
                                account_public_id=sender["public_id"], writes_enabled=False)


def validation_application(repository, sender: Mapping[str, Any]):
    from .connectsafely_application import ConnectSafelyApplication
    return ConnectSafelyApplication(repository, direct_console=validation_console(repository, sender))


def _https(method, path, body, headers, timeout):
    if any(os.environ.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        raise AuthorizationReadError("hubspot_tls_override_blocked")
    connection = http.client.HTTPSConnection("api.hubapi.com", 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        require(len(raw) <= 1_000_000, "hubspot_response_too_large")
        return response.status, raw
    finally:
        connection.close()


class HubSpotCadenceReader:
    """Search and GET only; this class has no route that changes HubSpot."""

    def __init__(self, credential_loader, *, request=None, timeout=20):
        require(callable(credential_loader), "hubspot_credential_unavailable")
        self.credential_loader, self.request, self.timeout = credential_loader, request or _https, timeout

    def _call(self, method, path, body=None):
        allowed = (method == "GET" and (path == "/account-info/v3/details" or re.fullmatch(
            r"/crm/v3/objects/contacts/[1-9][0-9]{0,29}\?properties=[A-Za-z0-9_%,]+", path))
            or method == "POST" and path == "/crm/v3/objects/tasks/search")
        require(allowed, "hubspot_endpoint_blocked")
        try:
            status, raw = self.request(method, path, body, {
                "Authorization": "Bearer " + self.credential_loader(), "Content-Type": "application/json"}, self.timeout)
            value = json.loads(raw)
        except AuthorizationReadError:
            raise
        except Exception:
            raise AuthorizationReadError("hubspot_unavailable") from None
        require(status == 200 and isinstance(value, dict), "hubspot_unavailable")
        return value

    def _portal(self):
        require(str(self._call("GET", "/account-info/v3/details").get("portalId")) == PORTAL_ID,
                "hubspot_portal_mismatch")

    def contact(self, ident: str) -> dict[str, Any]:
        ident = contact_id(ident)
        self._portal()
        row = self._call("GET", f"/crm/v3/objects/contacts/{quote(ident, safe='')}?" +
                         urlencode({"properties": ",".join(CONTACT_PROPERTIES)}))
        props = row.get("properties")
        require(str(row.get("id")) == ident and isinstance(props, dict), "hubspot_contact_mismatch")
        name = " ".join(props.get(key) or "" for key in ("firstname", "lastname")).strip()
        return {"id": ident, "name": name or None, "company": props.get("company") or None,
                "linkedin_url": props.get("linkedin_url") or props.get("hs_linkedin_url") or None,
                "cad_status": props.get("cad_status") or None}

    def open_tasks(self, ident: str) -> dict[str, Any]:
        ident = contact_id(ident)
        self._portal()
        query = {"filterGroups": [{"filters": [
            {"propertyName": "associations.contact", "operator": "EQ", "value": ident},
            {"propertyName": "hs_task_status", "operator": "EQ", "value": "NOT_STARTED"},
            {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": OWNER_ID},
            {"propertyName": "hs_task_subject", "operator": "EQ", "value": subject},
        ]} for subject, _action in STEP_CONTRACT.values()],
            "properties": list(TASK_PROPERTIES), "sorts": ["hs_timestamp"], "limit": 20}
        response = self._call("POST", "/crm/v3/objects/tasks/search", query)
        rows = response.get("results")
        require(isinstance(rows, list) and len(rows) <= 20, "hubspot_unavailable")
        expected = {subject: marker for marker, (subject, _action) in STEP_CONTRACT.items()}
        tasks = []
        for row in rows:
            props = row.get("properties") if isinstance(row, dict) else None
            require(isinstance(props, dict) and str(row.get("id", "")).isdigit(), "hubspot_unavailable")
            subject, body = props.get("hs_task_subject"), props.get("hs_task_body")
            tasks.append({"id": str(row["id"]), "subject": subject, "body": body,
                          "status": props.get("hs_task_status"), "due_at": props.get("hs_timestamp"),
                          "marker": expected.get(subject),
                          "contract_ok": expected.get(subject) is not None and body == expected.get(subject)})
        return {"read_only": True, "contact_id": ident, "complete": not response.get("paging"),
                "tasks": tasks}


def campaign_options(registry, sender: Mapping[str, Any]) -> list[dict[str, Any]]:
    options = []
    for row in registry.list_campaigns():
        try:
            binding = registry.binding(row["id"])
        except Exception:
            continue
        if binding["providers"].get("linkedin") is None:
            continue
        reason = None
        if not row.get("active"):
            reason = "campaign_inactive"
        elif row.get("platform") != "thunder_os":
            reason = "campaign_platform_not_thunder_os"
        elif binding["providers"].get("linkedin") != "connectsafely":
            reason = "campaign_provider_not_connectsafely"
        elif binding["provider_accounts"].get("linkedin") != sender.get("account_id"):
            reason = "campaign_account_mismatch"
        options.append({"id": row["id"], "platform": row.get("platform"), "eligible": reason is None,
                        "reason": reason})
    return options


def _public(state: Mapping[str, Any] | None, fields) -> dict[str, Any] | None:
    return {key: state.get(key) for key in fields if key in state} if state else None


def authorization_state(repository, *, contact: str, sender: Mapping[str, Any] | None,
                        sender_error: str | None, app, campaigns: list, clock) -> dict[str, Any]:
    contact = contact_id(contact)
    delegation = repository.get_state("delegation", "hermes")
    agent = repository.get_state("agent_control", "global") or {"enabled": False, "paused": True, "version": 0}
    control = repository.get_state("control", "global") or {}
    approvals = [a for a in repository.list_state("linkedin_cadence_approval")
                 if a.get("hubspot_contact_id") == contact]
    bindings = [b for b in repository.list_state("linkedin_cadence_task_binding") if b.get("contact_id") == contact]
    tasks = []
    for binding in bindings:
        task_id = binding.get("task_id")
        tasks.append({
            "task_id": task_id, "binding": _public(binding, ("task_id", "approval_id", "approval_version",
                                                              "review_hash", "marker", "cadence_id", "actor", "version")),
            "campaign_confirmation": _public(repository.get_state("linkedin_cadence_human_confirmation", task_id),
                                             ("preview_id", "campaign_approval_id", "review_hash", "actor")),
            "execution": _public(repository.get_state("linkedin_cadence_task", task_id),
                                 ("status", "job_id", "receipt_ref", "error_code", "marker")),
            "alert": _public(repository.get_state("linkedin_cadence_alert", task_id), ("status", "error_code")),
            "previews": len([p for p in repository.list_state("linkedin_cadence_preview_binding")
                             if p.get("task_id") == task_id]),
        })
    now = clock()
    blockers = []
    if sender is None:
        blockers.append(sender_error or "sender_config_unavailable")
    elif contact not in sender.get("identity_verifier_contacts", []):
        blockers.append("executor_identity_verifier_missing")
    if app is None or getattr(app, "direct", None) is None:
        blockers.append("connectsafely_console_unavailable")
    if not delegation or delegation.get("enabled") is not True or "linkedin_cadence" not in delegation.get("scopes", []):
        blockers.append("delegation_off")
    if agent.get("enabled") is not True or agent.get("paused") is not False:
        blockers.append("agent_paused")
    if control.get("paused") is True:
        blockers.append("global_pause")
    if not any(option["eligible"] for option in campaigns):
        blockers.append("campaign_unavailable")
    return {
        "read_only": True, "contact_id": contact, "observed_at": now,
        "sender": {key: sender[key] for key in ("account_id", "public_id", "profile_url", "source")} if sender else None,
        "delegation": _public(delegation, ("scopes", "daily_limit", "enabled", "actor", "version", "updated_at")),
        "agent_control": _public(agent, ("enabled", "paused", "actor", "version", "updated_at")),
        "global_pause": control.get("paused") is True,
        "approvals": [{**_public(a, ("id", "version", "cadence_id", "expires_at", "status", "approved_by",
                                     "revoked_by", "review_hash")),
                       "expired": _expired(a.get("expires_at"), now),
                       "steps": [{"marker": s.get("marker"), "subject": s.get("subject"),
                                  "action": s.get("plan", {}).get("action"),
                                  "recipient_public_id": s.get("plan", {}).get("recipient_public_id"),
                                  "recipient_profile_urn": s.get("plan", {}).get("recipient_profile_urn"),
                                  "account_public_id": s.get("plan", {}).get("account_public_id"),
                                  "content": s.get("plan", {}).get("content")} for s in a.get("steps", [])]}
                      for a in approvals],
        "tasks": tasks, "campaigns": campaigns, "blockers": blockers,
    }


def _expired(value, now) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.tzinfo is None or parsed.timestamp() <= now
    except ValueError:
        return True


def draft_approval(body: Any, *, sender: Mapping[str, Any], resolver, hubspot, repository, clock) -> dict[str, Any]:
    """Build the exact `linkedin-cadence-approvals` body; nothing is persisted here.

    The recipient URN comes from the bounded ConnectSafely profile read; the sender
    comes from the pinned runtime config. The approval endpoint revalidates both.
    """
    require(isinstance(body, dict) and set(body) == {"hubspot_contact_id", "profile", "texts", "valid_hours"},
            "draft_invalid")
    contact = contact_id(body["hubspot_contact_id"])
    texts, hours = body["texts"], body["valid_hours"]
    require(isinstance(texts, dict) and set(texts) == set(STEP_CONTRACT)
            and all(isinstance(value, str) for value in texts.values()), "draft_texts_invalid")
    require(type(hours) is int and 1 <= hours <= MAX_APPROVAL_HOURS, "draft_validity_invalid")
    profile = body["profile"]
    require(isinstance(profile, str) and 0 < len(profile) <= 2048, "draft_profile_invalid")
    if not profile.startswith("https://"):
        profile = "https://www.linkedin.com/in/" + profile.strip().strip("/") + "/"
    require(callable(resolver), "profile_reader_unavailable")
    resolved = resolver({"action": "dm", "profile_url": profile})
    require(isinstance(resolved, dict), "profile_reader_unavailable")
    if resolved.get("resolved") is not True:
        raise AuthorizationReadError(str(resolved.get("error_code") or "profile_target_unverified"))
    target = resolved.get("target") or {}
    require(target.get("account_id") == sender["account_id"]
            and target.get("account_public_id") == sender["public_id"], "sender_identity_mismatch")
    console = validation_console(repository, sender)
    steps = []
    for marker, (subject, action) in sorted(STEP_CONTRACT.items()):
        plan = {"account_id": sender["account_id"], "account_public_id": sender["public_id"], "action": action,
                "recipient_public_id": target.get("recipient_public_id"),
                "recipient_profile_urn": target.get("recipient_profile_urn"),
                "channel": "linkedin", "messaging_channel": "linkedin_inbox", "content": texts[marker]}
        try:
            # The executor's preview stores this same normalized plan, so its
            # plan_hash matches the approved step (conversation_urn included).
            plan = console._plan(plan)
        except ConnectSafelyError as exc:
            raise AuthorizationReadError(marker + ":" + str(exc)) from None
        steps.append({"marker": marker, "subject": subject, "plan": plan})
    expires = (datetime.fromtimestamp(clock(), timezone.utc) + timedelta(hours=hours)).replace(microsecond=0)
    expires_at = expires.isoformat().replace("+00:00", "Z")
    fingerprint = payload_hash({"contact": contact, "steps": steps})[:12]
    approval = {"id": "li-" + contact + "-" + payload_hash({"f": fingerprint, "e": expires_at})[:12],
                "hubspot_contact_id": contact, "cadence_id": "li-" + contact + "-" + fingerprint,
                "expires_at": expires_at, "steps": steps}
    try:
        crm = hubspot.contact(contact) if hubspot is not None else None
        crm_error = None if crm is not None else "hubspot_reader_unavailable"
    except AuthorizationReadError as exc:
        crm, crm_error = None, str(exc)
    return {"read_only": True, "persisted": False, "approval": approval,
            "sender": {key: sender[key] for key in ("account_id", "public_id", "profile_url")},
            "recipient": {"public_id": target["recipient_public_id"], "profile_urn": target["recipient_profile_urn"],
                          "profile_url": "https://www.linkedin.com/in/" + target["recipient_public_id"] + "/",
                          "label": (resolved.get("preview") or {}).get("label"),
                          "urn_source": "connectsafely_profile_read"},
            "hubspot_contact": crm, "hubspot_error": crm_error,
            "plan_hashes": {step["marker"]: plan_hash(step["plan"]) for step in steps},
            "approval_hash_preview": approval_payload_hash({**approval, "steps": [
                {**step, "plan_hash": plan_hash(step["plan"])} for step in steps]})}
