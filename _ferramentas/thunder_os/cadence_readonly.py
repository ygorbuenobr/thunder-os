"""Read-only HubSpot cadence and recorded evidence projection; no writer.

Inject hubspot_console.systemd_hubspot_credential in the authorized server.
CRM v3 tasks/search, properties and batch/read; v4 contact associations. The
sequence fields were confirmed in portal metadata by the coordinator (2026-10-21).
They identify sequence-created tasks even after a human renames their subject.
No sequence/enrollment write endpoint or automatic retry exists here.

Public contract: snapshot -> campaigns[{id, metrics, tasks}], today, unlinked;
timeline(contact_id, campaign=None) -> events. Both include ok, collected_at,
coverage{complete, partial, limitations, sources}, errors and target identity.
Coverage is of these bounded reads, not of all provider/channel history.
Text is plain text (not HTML-safe markup); consumers must use textContent/escape.
CRM logs and completed tasks are not delivery receipts or human approval.
"""
from copy import deepcopy
from datetime import datetime, timezone
from html.parser import HTMLParser
import http.client
import json
import os
import re
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .approvals import ApprovalError
from .cadence_evidence import pipe_title, task_evidence
from . import hubspot_read_budget

PORTAL_ID = "51412240"
OWNER_ID = "92901049"
SEQUENCE_ID = "316669709"
CAMPAIGN_ID = "BR-CADENCIA-V1"
TASK_PROPERTIES = (
    "hs_task_subject", "hs_task_body", "hs_task_status", "hs_timestamp",
    "hubspot_owner_id", "hs_task_sequence_id",
    "hs_task_sequence_step_enrollment_contact_id",
    "hs_task_sequence_step_enrollment_id", "hs_task_sequence_step_order",
    "hs_task_sequence_enrollment_active",
)
LINK_PROPERTIES = set(TASK_PROPERTIES[5:8])
ACTIVITY_PROPERTIES = {
    "emails": ("hs_timestamp", "hs_email_subject", "hs_email_text", "hs_email_html",
               "hs_email_direction", "hs_email_status", "hs_campaign_guid", "hs_engagement_source_id"),
    "meetings": ("hs_timestamp", "hs_meeting_title", "hs_meeting_body", "hs_meeting_outcome",
                 "hs_campaign_guid", "hs_engagement_source_id"),
    "calls": ("hs_timestamp", "hs_call_title", "hs_call_body", "hs_call_status"),
    "notes": ("hs_timestamp", "hs_note_body"),
    "communications": ("hs_timestamp", "hs_communication_body", "hs_communication_channel_type"),
}
LIMITATIONS = [
    "completed_status_is_not_delivery_receipt",
    "crm_recorded_confirmation_is_not_delivery_proof",
    "approval_state_unknown",
    "contact_current_campaign_is_not_historical_membership",
    "unreplied_cross_channel_contract_unavailable",
    "search_is_eventually_consistent_and_excludes_archived_records",
]
TITLE_PATTERN = re.compile(r"^\[([A-Za-z0-9_-]{1,80})\](?:\[(?:YGOR|LOA)\](?=\[))?\[([^\]\r\n]{1,80})\]\s*(.*?)\s*\|\s*([^|\r\n]+)$")
TOUCHES = {"linkedin: convite": ("LI0", "linkedin"),
           "linkedin: msg 1": ("LI1", "linkedin"),
           "linkedin: msg 2": ("LI2", "linkedin"),
           "linkedin: msg 3": ("LI3", "linkedin"),
           "whatsapp 1": ("WA1", "whatsapp")}


class CadenceReadonlyError(ValueError):
    """Stable code only, never provider body, credentials or transport details."""

    def __init__(self, code, status=None):
        self.code, self.status = code, status
        super().__init__(code)


def _id(value):
    if isinstance(value, bool):
        return None
    value = str(value) if isinstance(value, (str, int)) else ""
    return value if re.fullmatch(r"[1-9][0-9]{0,29}", value) else None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object"}:
            self.hidden += 1
        elif tag in {"br", "p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "li"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value):
    """Preserve full CRM text within the bounded HTTP response; never render HTML."""
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value)
    result = "".join(parser.parts)
    result = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", result)
    result = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", result)
    result = re.sub(r"\bpat-[A-Za-z0-9_-]+", "[REDACTED]", result)
    result = re.sub(r"(?i)((?:access_token|api_key|token|password)\s*[=:]\s*)[^\s&]+", r"\1[REDACTED]", result)
    return result.strip()


def _date(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    try:
        if str(value).isdigit():
            result = datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        else:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                return None
        return result.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def project_task(record, *, associated_contact_ids=None, association_complete=None,
                 enrollment_contact_available=True):
    """Pure projection; title labels never establish enrollment/contact identity."""
    if not isinstance(record, dict) or record.get("archived") is True:
        return None
    props = record.get("properties")
    if not isinstance(props, dict) or _id(props.get("hubspot_owner_id")) != OWNER_ID:
        return None
    task_id = _id(record.get("id"))
    if not task_id:
        return None
    title = plain_text(props.get("hs_task_subject"))
    pattern = TITLE_PATTERN.fullmatch(title)
    pipe = pipe_title(title)
    sequence = _id(props.get("hs_task_sequence_id"))
    enrollment = _id(props.get("hs_task_sequence_step_enrollment_id"))
    proven_sequence = sequence == SEQUENCE_ID and enrollment is not None
    if proven_sequence:
        campaign = CAMPAIGN_ID
    elif pattern:
        campaign = pattern[1]
    elif pipe:
        campaign = pipe['campaign']
    else:
        return None
    touch, channel = TOUCHES.get(title.casefold(), (None, None))
    company = None
    if pipe:
        touch, company, channel = pipe['touch'], pipe['company'], pipe['channel']
    if pattern:
        touch, company = pattern[2], pattern[3] or None
        channel = {"linkedin": "linkedin", "whatsapp": "whatsapp", "email": "email",
                   "e-mail": "email"}.get(pattern[4].strip().casefold())
    contact = _id(props.get("hs_task_sequence_step_enrollment_contact_id")) if enrollment else None
    contacts = [contact] if contact else []
    link_status = "linked" if contacts else "sem_vinculo"
    if proven_sequence and not enrollment_contact_available:
        contacts, link_status = [], "unverified"
    # Sequence tasks must use the enrollment contact; no generic fallback there.
    if not proven_sequence and (pattern or pipe):
        contacts = sorted({_id(value) for value in (associated_contact_ids or []) if _id(value)})
        if association_complete is None:
            association_complete = associated_contact_ids is not None
        link_status = ("linked" if contacts else "sem_vinculo") if association_complete else "unverified"
    status = plain_text(props.get("hs_task_status")) or "UNKNOWN"
    result = {"id": task_id, "title": title, "campaign": campaign,
            "contact_ids": contacts, "contact_name": None,
            "company": company, "link_status": link_status,
            "channel": channel, "touch": touch, "status": status,
            "due_at": _date(props.get("hs_timestamp")), "receipt": None,
            "approval_status": "unknown", "body": plain_text(props.get("hs_task_body")),
            "sequence_id": sequence, "enrollment_id": enrollment,
            "source": "hubspot_task", "source_kind": "sequence" if proven_sequence else "title",
            "completed": status == "COMPLETED"}
    result.update(task_evidence(props.get('hs_task_body'), result, pipe if not proven_sequence else None))
    if result['reason']:
        result['reason'] = plain_text(result['reason'])
    return result


def _coverage():
    return {"complete": True, "partial": False, "limitations": [], "sources": {}}


def _partial(coverage, reason):
    coverage["complete"], coverage["partial"] = False, True
    if reason not in coverage["limitations"]:
        coverage["limitations"].append(reason)


def validate_activity_bindings(value):
    """Legacy configuration parser; the current-contact reader never uses it."""
    if not isinstance(value, list) or len(value) > 30:
        raise CadenceReadonlyError('activity_bindings_invalid')
    seen_campaigns, seen_sources = set(), set()
    for row in value:
        if (not isinstance(row, dict) or set(row) != {'campaign_id', 'provider', 'campaign_guid', 'source_id'}
                or row.get('provider') not in {'apollo', 'instantly'}
                or any(not isinstance(row.get(k), str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', row[k])
                       for k in ('campaign_id', 'campaign_guid', 'source_id'))):
            raise CadenceReadonlyError('activity_bindings_invalid')
        pair = row['source_id'], row['campaign_guid']
        if row['campaign_id'] in seen_campaigns or pair in seen_sources:
            raise CadenceReadonlyError('activity_bindings_ambiguous')
        seen_campaigns.add(row['campaign_id'])
        seen_sources.add(pair)
    return deepcopy(value)


def environment_activity_bindings():
    """Compatibility with the existing server import; no attribution authority."""
    raw = os.environ.get('THUNDER_OS_CADENCE_ACTIVITY_BINDINGS', '[]')
    if len(raw) > 10000:
        raise CadenceReadonlyError('activity_bindings_invalid')
    try:
        value = json.loads(raw)
    except ValueError:
        raise CadenceReadonlyError('activity_bindings_invalid') from None
    return validate_activity_bindings(value)


def project_snapshot(records, *, collected_at, coverage=None, associations=None, associations_complete=None,
                     registered_campaigns=()):
    """Pure deterministic projection, including visibly unknown metrics."""
    associations = associations or {}
    if associations_complete is None:
        associations_complete = set(associations)
    tasks = {task["id"]: task for row in records if (task := project_task(
        row, associated_contact_ids=associations.get(str(row.get("id"))),
        association_complete=str(row.get("id")) in associations_complete,
        enrollment_contact_available="hs_task_sequence_step_enrollment_contact_id" not in
            (coverage or {}).get("sources", {}).get("task_schema", {}).get("missing", [])))}
    ordered = sorted(tasks.values(), key=lambda t: (t["due_at"] or "~", t["id"]))
    now = datetime.fromisoformat(collected_at.replace("Z", "+00:00"))
    today = now.astimezone(ZoneInfo("America/Sao_Paulo")).date()
    campaigns, planned = [], []
    registered = {row['id']: row for row in registered_campaigns}
    sources = (coverage or {}).get('sources', {})
    task_coverage_complete = all(sources.get(key, {}).get('complete') is True
                                 for key in ('tasks', 'task_schema'))
    # Only exact campaign IDs join. A registry ID is not a prefix or alias rule.
    for campaign in sorted({task["campaign"] for task in ordered} | registered.keys()):
        rows = [task for task in ordered if task["campaign"] == campaign]
        pending = [task for task in rows if task["status"] in {"NOT_STARTED", "IN_PROGRESS", "WAITING", "DEFERRED"}]
        overdue = [task for task in pending if task["due_at"] and
                   datetime.fromisoformat(task["due_at"].replace("Z", "+00:00")) < now]
        metrics = {"contacts": len({cid for task in rows for cid in task["contact_ids"]}),
                   "tasks": len(rows), "completed_with_receipt": None,
                   "completed_status": sum(task["completed"] for task in rows),
                   "pending": len(pending), "overdue": len(overdue),
                   "replies": None, "meetings": None,
                   "unknown_status": sum(task["status"] == "UNKNOWN" for task in rows)}
        task_counts = ('contacts', 'tasks', 'completed_status', 'pending', 'overdue', 'unknown_status')
        for key in task_counts:
            metrics['observed_' + key] = metrics[key]
        receipt_ids = {(task['receipt']['provider'], task['receipt']['message_id'])
                       for task in rows if task.get('receipt')}
        metrics['observed_completed_with_receipt'] = len(receipt_ids)
        receipt_coverage_complete = task_coverage_complete and not any(
            task['link_status'] == 'unverified' or task['outcome'] == 'unverified' for task in rows)
        metrics['completed_with_receipt'] = len(receipt_ids) if receipt_ids and receipt_coverage_complete else None
        for key, outcome in [('uncertain', 'uncertain_no_retry'), ('blocked', 'blocked'),
                             ('completed_without_receipt', 'completed_without_receipt')]:
            count = sum(task.get('outcome') == outcome for task in rows)
            metrics['observed_' + key] = count
            metrics[key] = count if task_coverage_complete else None
        metrics.update(external_sent=None, observed_external_sent=0)
        if any(task["link_status"] == "unverified" for task in rows):
            metrics["contacts"] = None
        task_state = 'observed' if rows else 'empty' if task_coverage_complete else 'unknown'
        if not task_coverage_complete:
            for key in task_counts:
                metrics[key] = None
        campaigns.append({"id": campaign, "metrics": metrics, "tasks": rows,
                          "registry": deepcopy(registered.get(campaign)),
                          "task_state": task_state, "task_coverage_complete": task_coverage_complete,
                          "activities": [], "external_activity_state": "not_configured",
                          "metric_availability": {"completed_with_receipt": "recorded_receipts" if metrics['completed_with_receipt'] is not None else "receipt_coverage_unavailable",
                              "contacts": "task_read_incomplete" if not task_coverage_complete else
                                  "association_read_incomplete" if metrics["contacts"] is None else "observed_task_contacts",
                              "replies": "campaign_attribution_unavailable",
                              "meetings": "campaign_attribution_unavailable"}})
        planned.extend(task for task in pending if task["due_at"] and
                       datetime.fromisoformat(task["due_at"].replace("Z", "+00:00")).astimezone(
                           ZoneInfo("America/Sao_Paulo")).date() == today)
    result_coverage = deepcopy(coverage) if coverage is not None else _coverage()
    result_coverage["limitations"] = list(dict.fromkeys(result_coverage["limitations"] + LIMITATIONS))
    return {"ok": True, "portal_id": PORTAL_ID, "owner_id": OWNER_ID,
            "collected_at": collected_at, "coverage": result_coverage, "errors": [],
            "campaigns": campaigns, "today": {"planned": planned, "awaiting_approval": None, "unreplied": None},
            "unlinked": [task for task in ordered if task["link_status"] == "sem_vinculo"],
            "unverified": [task for task in ordered if task["link_status"] == "unverified"],
            "contact_memberships": [], "contact_conflicts": [], "unattributed_activities": []}


def _https(method, path, body, headers, timeout):
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        raise CadenceReadonlyError("tls_override_blocked")
    connection = http.client.HTTPSConnection("api.hubapi.com", timeout=timeout,
                                            context=ssl.create_default_context())
    try:
        connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = connection.getresponse()
        raw = response.read(2_000_001)
        return hubspot_read_budget.HubSpotReadResponse(response.status, raw,
            {'Retry-After': response.getheader('Retry-After')})
    finally:
        connection.close()


class HubSpotReadonlyAdapter:
    """Fixed origin and read-only allowlist. One attempt; next refresh may retry.

    Injected transport has signature (method, path, body, headers, timeout) and
    returns (status, JSON bytes[, headers]). No token is cached, logged or exposed in output.
    """
    def __init__(self, credential_loader, *, request=None, monotonic=time.monotonic,
                 max_pages=100, max_requests=150, budget_seconds=45, sleep=time.sleep, read_budget=None):
        self.loader, self.request = credential_loader, request or _https
        self.monotonic = monotonic
        self.max_pages = max(1, min(int(max_pages), 100))
        self.max_requests = max(1, min(int(max_requests), 150))
        self.budget_seconds = max(1, min(float(budget_seconds), 60))
        self.sleep = sleep
        self.read_budget = read_budget if read_budget is not None else hubspot_read_budget.SHARED_READ_BUDGET

    def session(self):
        return _ReadSession(self)


class _ReadSession:
    def __init__(self, adapter):
        self.adapter = adapter
        self.deadline = adapter.monotonic() + adapter.budget_seconds
        self.requests = 0

    def call(self, method, path, body=None):
        kinds = "emails|meetings|calls|notes|communications"
        allowed = (method == "GET" and (path == "/account-info/v3/details" or
                   path == "/crm/v3/owners/" + OWNER_ID or
                   re.fullmatch(r"/crm/v3/properties/(?:tasks|contacts|" + kinds + r")", path) or
                   re.fullmatch(r"/crm/v4/objects/contacts/[1-9][0-9]{0,29}/associations/(?:" + kinds + r")(?:\?[^#]*)?", path) or
                   re.fullmatch(r"/crm/v4/objects/tasks/[1-9][0-9]{0,29}/associations/contacts(?:\?[^#]*)?", path)))
        allowed = allowed or (method == "GET" and (
            path == "/crm/v3/pipelines/deals" or
            re.fullmatch(r"/crm/v3/objects/(?:deals|meetings)\?[^#]+", path) or
            re.fullmatch(r"/crm/v3/objects/contacts/[1-9][0-9]{0,29}\?properties=hubspot_owner_id(?:&associations=deals)?", path)))
        allowed = allowed or (method == "POST" and (path in {"/crm/v3/objects/deals/search", "/crm/v3/objects/deals/batch/read", "/crm/v3/objects/tasks/search", "/crm/v3/objects/emails/search", "/crm/v3/objects/meetings/search"} or
                  path in {"/crm/v4/associations/tasks/contacts/batch/read",
                           "/crm/v4/associations/emails/contacts/batch/read"} or
                  re.fullmatch(r"/crm/v3/objects/(?:contacts|" + kinds + r")/batch/read", path)))
        if not allowed:
            raise CadenceReadonlyError("readonly_endpoint_blocked")
        remaining = self.deadline - self.adapter.monotonic()
        if remaining <= 0 or self.requests >= self.adapter.max_requests:
            raise CadenceReadonlyError("read_budget_exhausted")
        self.requests += 1
        try:
            token = self.adapter.loader()
        except Exception:
            raise CadenceReadonlyError("credential_unavailable") from None
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise CadenceReadonlyError("credential_unavailable")
        remaining = self.deadline - self.adapter.monotonic()
        if remaining <= 0:
            raise CadenceReadonlyError('read_budget_exhausted')
        try:
            status, raw = self.adapter.read_budget.call(token, method, path, body,
                {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                min(8, remaining), self.adapter.request)
        except hubspot_read_budget.HubSpotReadBudgetError as exc:
            raise CadenceReadonlyError(exc.code, exc.status) from None
        except Exception:
            raise CadenceReadonlyError("provider_read_failed") from None
        if self.adapter.monotonic() >= self.deadline:
            raise CadenceReadonlyError("read_budget_exhausted")
        if type(status) is not int or not 200 <= status < 300:
            raise CadenceReadonlyError("provider_http_error", status if type(status) is int else None)
        try:
            if not isinstance(raw, bytes) or len(raw) > 2_000_000:
                raise ValueError()
            # Also redact the exact scoped credential if a provider ever echoes it.
            result = json.loads(raw.decode().replace(token, "[REDACTED]"))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except Exception:
            raise CadenceReadonlyError("provider_invalid_response") from None

    def identity(self):
        account = self.call("GET", "/account-info/v3/details")
        if _id(account.get("portalId")) != PORTAL_ID:
            raise CadenceReadonlyError("portal_mismatch")
        owner = self.call("GET", "/crm/v3/owners/" + OWNER_ID)
        if _id(owner.get("id")) != OWNER_ID or owner.get("archived") is not False:
            raise CadenceReadonlyError("owner_mismatch")

    def properties(self, kind):
        if not hasattr(self, "_schemas"):
            self._schemas = {}
        if kind in self._schemas:
            return self._schemas[kind]
        result = self.call("GET", "/crm/v3/properties/" + kind)
        if not hubspot_read_budget._schema_response(json.dumps(result).encode()):
            raise CadenceReadonlyError("provider_invalid_schema")
        names = {row["name"] for row in result["results"] if isinstance(row, dict)
                and isinstance(row.get("name"), str) and row.get("archived") is not True}
        self._schemas[kind] = names
        return names

    def task_contacts(self, task_ids, coverage):
        return self.object_contacts('tasks', task_ids, coverage)

    def object_contacts(self, kind, object_ids, coverage):
        """V4 batch read (100 inputs), with per-result after cursors, no links.

        https://developers.hubspot.com/changelog/bug-fix-batch-read-limit-for-hubspot-associations-api
        Provider limit is 1,000 inputs; using 100 bounds response size as well.
        """
        source = 'task_contacts' if kind == 'tasks' else 'email_contacts'
        pending = [{"id": id_} for id_ in sorted(set(object_ids))]
        contacts = {id_: set() for id_ in object_ids}
        cursors, resolved, incomplete = {}, set(), set()
        expected = set()
        info = {"complete": not pending, "pages": 0, "records": 0, "requested": len(pending)}
        coverage["sources"][source] = info
        try:
            while pending and info["pages"] < self.adapter.max_pages:
                inputs, pending = pending[:100], pending[100:]
                expected = {item["id"] for item in inputs}
                result = self.call("POST", f"/crm/v4/associations/{kind}/contacts/batch/read", {"inputs": inputs})
                info["pages"] += 1
                batch_complete = result.get("status") == "COMPLETE" and not result.get("errors") and not result.get("numErrors", 0)
                if not batch_complete:
                    incomplete.update(expected)
                    _partial(coverage, source + ":batch_incomplete")
                rows, found = result.get("results"), set()
                if not isinstance(rows, list):
                    raise CadenceReadonlyError("provider_invalid_batch")
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("from"), dict):
                        raise CadenceReadonlyError("provider_invalid_association")
                    id_ = _id(row["from"].get("id"))
                    if id_ not in expected or id_ in found or not isinstance(row.get("to"), list):
                        raise CadenceReadonlyError("provider_invalid_association")
                    found.add(id_)
                    for link in row["to"]:
                        contact = _id(link.get("toObjectId")) if isinstance(link, dict) else None
                        if not contact:
                            raise CadenceReadonlyError("provider_invalid_association")
                        contacts[id_].add(contact)
                    paging = row.get("paging", {})
                    if not isinstance(paging, dict) or not isinstance(paging.get("next", {}), dict):
                        raise CadenceReadonlyError("provider_invalid_paging")
                    after = paging.get("next", {}).get("after")
                    if after is None:
                        if paging.get("next"):
                            raise CadenceReadonlyError("provider_invalid_paging")
                        if batch_complete and id_ not in incomplete:
                            resolved.add(id_)
                    else:
                        if not isinstance(after, (str, int)) or isinstance(after, bool):
                            raise CadenceReadonlyError("provider_invalid_cursor")
                        after = str(after)
                        seen = cursors.setdefault(id_, set())
                        if not after or len(after) > 512 or after in seen:
                            raise CadenceReadonlyError("pagination_cycle_or_invalid_cursor")
                        seen.add(after)
                        pending.append({"id": id_, "after": after})
                if found != expected:
                    _partial(coverage, source + ":batch_records_missing")
            if pending:
                _partial(coverage, source + ":page_limit")
        except CadenceReadonlyError as exc:
            resolved.difference_update(expected)
            _partial(coverage, source + ":" + exc.code)
            info["error"] = exc.code
        info["records"] = len(resolved)
        info["complete"] = len(resolved) == len(contacts) and not any(
            reason.startswith(source + ":") for reason in coverage["limitations"])
        return {id_: sorted(values) for id_, values in contacts.items()}, resolved

    def contact_campaigns(self, contact_ids, coverage):
        """Current CRM properties by proved ID; no autofill, history or writer."""
        rows, resolved, ids = {}, set(), sorted(set(contact_ids))
        source = 'contact_campaigns'
        info = {'complete': not ids, 'records': 0, 'requested': len(ids)}
        coverage['sources'][source] = info
        try:
            available = self.properties('contacts')
            missing = {'campanha_ativa', 'executor'} - available
            coverage['sources']['contact_campaign_schema'] = {
                'complete': not missing, 'missing': sorted(missing)}
            if missing:
                _partial(coverage, source + ':configuration_pending')
            if 'campanha_ativa' not in available:
                raise CadenceReadonlyError('campaign_property_unavailable')
            for offset in range(0, len(ids), 100):
                expected = set(ids[offset:offset + 100])
                result = self.call('POST', '/crm/v3/objects/contacts/batch/read', {
                    'properties': sorted({'campanha_ativa', 'executor'} & available),
                    'inputs': [{'id': ident} for ident in sorted(expected)]})
                complete = (result.get('status') == 'COMPLETE' and not result.get('errors')
                            and not result.get('numErrors', 0))
                if not complete:
                    _partial(coverage, source + ':batch_incomplete')
                if not isinstance(result.get('results'), list):
                    raise CadenceReadonlyError('provider_invalid_batch')
                found, batch_rows = set(), {}
                for row in result['results']:
                    ident = _id(row.get('id')) if isinstance(row, dict) else None
                    if ident not in expected or not isinstance(row.get('properties'), dict):
                        raise CadenceReadonlyError('provider_invalid_record')
                    if ident in found:
                        raise CadenceReadonlyError('duplicate_contact_record')
                    found.add(ident)
                    if complete and row.get('archived') is not True:
                        batch_rows[ident] = row
                rows.update(batch_rows)
                resolved.update(batch_rows)
                if not expected <= resolved:
                    _partial(coverage, source + ':batch_records_missing')
        except CadenceReadonlyError as exc:
            _partial(coverage, source + ':' + exc.code)
            info['error'] = exc.code
        info['records'] = len(resolved)
        info['complete'] = len(resolved) == len(ids) and not any(
            reason.startswith(source + ':') for reason in coverage['limitations'])
        return rows, resolved

    def contact_names(self, contact_ids, coverage):
        names, ids = {}, sorted(set(contact_ids))
        info = {"complete": not ids, "records": 0, "requested": len(ids)}
        coverage["sources"]["contact_names"] = info
        try:
            for offset in range(0, len(ids), 100):
                expected = set(ids[offset:offset + 100])
                result = self.call("POST", "/crm/v3/objects/contacts/batch/read", {
                    "properties": ["firstname", "lastname"], "inputs": [{"id": id_} for id_ in sorted(expected)]})
                if result.get("status") != "COMPLETE" or result.get("errors") or result.get("numErrors", 0):
                    _partial(coverage, "contact_names:batch_incomplete")
                if not isinstance(result.get("results"), list):
                    raise CadenceReadonlyError("provider_invalid_batch")
                for row in result["results"]:
                    if not isinstance(row, dict) or _id(row.get("id")) not in expected or not isinstance(row.get("properties"), dict):
                        raise CadenceReadonlyError("provider_invalid_record")
                    if row.get("archived") is not True:
                        names[str(row["id"])] = " ".join(filter(None, (
                            plain_text(row["properties"].get("firstname")),
                            plain_text(row["properties"].get("lastname"))))) or None
                if not expected <= names.keys():
                    _partial(coverage, "contact_names:batch_records_missing")
        except CadenceReadonlyError as exc:
            _partial(coverage, "contact_names:" + exc.code)
            info["error"] = exc.code
        info["records"] = len(names)
        info["complete"] = len(names) == len(ids) and not any(
            reason.startswith("contact_names:") for reason in coverage["limitations"])
        return names

    def pages(self, method, path, body, coverage, source, *, key="id"):
        rows, seen_ids, seen_cursors, cursor = [], set(), set(), None
        info = {"complete": False, "pages": 0, "records": 0}
        coverage["sources"][source] = info
        try:
            for _ in range(self.adapter.max_pages):
                payload = deepcopy(body)
                target = path
                if method == "GET":
                    target += "?" + urlencode({"limit": 100, **({"after": cursor} if cursor is not None else {})})
                elif cursor is not None:
                    payload["after"] = cursor
                result = self.call(method, target, payload)
                info["pages"] += 1
                items = result.get("results")
                if not isinstance(items, list):
                    raise CadenceReadonlyError("provider_invalid_page")
                for row in items:
                    identifier = _id(row.get(key)) if isinstance(row, dict) else None
                    if not identifier:
                        raise CadenceReadonlyError("provider_invalid_record")
                    if identifier not in seen_ids:
                        seen_ids.add(identifier)
                        rows.append(row)
                info["records"] = len(rows)
                paging = result.get("paging", {})
                if not isinstance(paging, dict) or not isinstance(paging.get("next", {}), dict):
                    raise CadenceReadonlyError("provider_invalid_paging")
                following = paging.get("next", {}).get("after")
                if following is None:
                    if paging.get("next"):
                        raise CadenceReadonlyError("provider_invalid_paging")
                    if "total" in result and (type(result["total"]) is not int or result["total"] != len(rows)):
                        raise CadenceReadonlyError("provider_total_mismatch")
                    info["complete"] = True
                    break
                if not isinstance(following, (str, int)) or isinstance(following, bool):
                    raise CadenceReadonlyError("provider_invalid_cursor")
                cursor = str(following)
                if not cursor or len(cursor) > 512 or cursor in seen_cursors:
                    raise CadenceReadonlyError("pagination_cycle_or_invalid_cursor")
                seen_cursors.add(cursor)
                if method == 'POST' and path.endswith('/search'):
                    if not cursor.isascii() or not cursor.isdigit() or int(cursor) > 10000:
                        raise CadenceReadonlyError('provider_invalid_cursor')
                    cursor = int(cursor)
            if not info["complete"]:
                _partial(coverage, source + ":page_limit")
        except CadenceReadonlyError as exc:
            info["error"] = exc.code
            if exc.status is not None:
                info["http_status"] = exc.status
            _partial(coverage, source + ":" + exc.code)
        return rows


class CadenceReadonlyConsole:
    def __init__(self, credential_loader, *, request=None, clock=time.time,
                 monotonic=time.monotonic, max_pages=100, max_requests=150, budget_seconds=45,
                 sleep=time.sleep, campaign_registry=None, activity_bindings=None, activity_bindings_loader=None,
                 read_budget=None):
        self.adapter = HubSpotReadonlyAdapter(credential_loader, request=request, monotonic=monotonic,
            max_pages=max_pages, max_requests=max_requests, budget_seconds=budget_seconds, sleep=sleep,
            read_budget=read_budget)
        self.clock, self.monotonic = clock, monotonic
        self.campaign_registry = campaign_registry
        # Retain the server constructor contract; legacy event bindings are ignored.
        self.activity_bindings = activity_bindings
        self.activity_bindings_loader = activity_bindings_loader
        self._lock, self._cache = threading.RLock(), {}

    def _cached(self, key, build):
        with self._lock:
            now = self.monotonic()
            self._cache = {k: v for k, v in self._cache.items() if v[0] > now}
            if key in self._cache:
                return deepcopy(self._cache[key][1])
            value = build()
            if len(self._cache) >= 64:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (now + 60, deepcopy(value))
            return value

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat().replace("+00:00", "Z")

    def _tasks(self, session, coverage, contact_id=None):
        available = session.properties("tasks")
        missing = set(TASK_PROPERTIES[:5]) | LINK_PROPERTIES
        missing -= available
        coverage["sources"]["task_schema"] = {"complete": not missing, "missing": sorted(missing)}
        if missing:
            _partial(coverage, "task_schema_missing:" + ",".join(sorted(missing)))
        filters = [{"propertyName": "hubspot_owner_id", "operator": "EQ", "value": OWNER_ID}]
        if "hubspot_owner_id" not in available:
            raise CadenceReadonlyError("task_owner_schema_unavailable")
        if contact_id:
            if "hs_task_sequence_step_enrollment_contact_id" not in available:
                raise CadenceReadonlyError("task_contact_schema_unavailable")
            filters.append({"propertyName": "hs_task_sequence_step_enrollment_contact_id", "operator": "EQ", "value": contact_id})
        body = {"filterGroups": [{"filters": filters}], "properties": sorted(set(TASK_PROPERTIES) & available), "limit": 200}
        return session.pages("POST", "/crm/v3/objects/tasks/search", body, coverage, "tasks")

    def snapshot(self):
        return self._cached(("snapshot",), self._snapshot)

    def _snapshot(self):
        coverage, associations = _coverage(), {}
        registered = []
        if self.campaign_registry is not None:
            try:
                registered = self.campaign_registry.list_campaigns()
                coverage['sources']['campaign_registry'] = {'complete': True, 'records': len(registered)}
            except (ApprovalError, sqlite3.Error):
                coverage['sources']['campaign_registry'] = {'complete': False, 'error': 'unavailable'}
                _partial(coverage, 'campaign_registry:unavailable')
        session = self.adapter.session()
        try:
            session.identity()
        except CadenceReadonlyError as exc:
            if self.campaign_registry is None:
                raise
            coverage['sources']['identity'] = {'complete': False, 'error': exc.code}
            coverage['sources']['tasks'] = {'complete': False, 'error': 'identity_unverified'}
            _partial(coverage, 'identity:' + exc.code)
            return project_snapshot([], collected_at=self._now(), coverage=coverage, registered_campaigns=registered)
        try:
            records = self._tasks(session, coverage)
        except CadenceReadonlyError as exc:
            if self.campaign_registry is None:
                raise
            records = []
            coverage['sources']['tasks'] = {'complete': False, 'records': 0, 'error': exc.code}
            _partial(coverage, 'tasks:' + exc.code)
        if self.campaign_registry is None and not records and coverage["sources"].get("tasks", {}).get("error"):
            raise CadenceReadonlyError("task_collection_unavailable")
        bracket_ids = [task["id"] for record in records if
                       (task := project_task(record)) and task["source_kind"] == "title"]
        associations, resolved = session.task_contacts(bracket_ids, coverage)
        result = project_snapshot(records, collected_at=self._now(), coverage=coverage,
                                  associations=associations, associations_complete=resolved,
                                  registered_campaigns=registered)
        contacts = sorted({cid for campaign in result["campaigns"] for task in campaign["tasks"] for cid in task["contact_ids"]})
        names = session.contact_names(contacts, result["coverage"])
        for group in result["campaigns"]:
            for task in group["tasks"]:
                task["contacts"] = [{"id": cid, "name": names.get(cid)} for cid in task["contact_ids"]]
                task["contact_names"] = {cid: names.get(cid) for cid in task["contact_ids"]}
                task["contact_name"] = names.get(task["contact_ids"][0]) if len(task["contact_ids"]) == 1 else None
        self._external_activities(session, result)
        result["activity_collection"] = {"mode": "bounded_email_search_and_on_demand_timeline",
            "population_sampled": False, "campaign_attribution": "contact_current_campaign",
            "historical_membership_verified": False, "delivery_verified": False}
        return result

    def _external_activities(self, session, snapshot):
        coverage = snapshot['coverage']
        groups = {group['id']: group for group in snapshot['campaigns']
                  if (group.get('registry') or {}).get('active') is True
                  and group['registry'].get('platform') in {'apollo', 'instantly'}}
        if not groups:
            return
        task_contact_ids = {campaign: {cid for task in group['tasks'] if task['link_status'] == 'linked'
                                      for cid in task['contact_ids']} for campaign, group in groups.items()}
        for group in groups.values():
            group['external_activity_state'] = 'unknown'
            group['metrics'].update(contacts=None, observed_contacts=len(task_contact_ids[group['id']]))
            group['metric_availability']['contacts'] = 'contact_read_incomplete'
        source = 'external_emails'
        try:
            schema = session.properties('contacts')
            missing = {'campanha_ativa', 'executor'} - schema
            coverage['sources']['contact_campaign_schema'] = {
                'complete': not missing, 'missing': sorted(missing)}
            if missing:
                _partial(coverage, 'contact_campaigns:configuration_pending')
            configuration_pending = bool(missing)
            if 'campanha_ativa' not in schema:
                raise CadenceReadonlyError('configuration_pending')
            available = session.properties('emails')
            required = {'hs_email_status', 'hs_email_direction', 'hs_timestamp'}
            if not required <= available:
                raise CadenceReadonlyError('activity_classification_schema_unavailable')
            rows = session.pages('POST', '/crm/v3/objects/emails/search', {
                'filterGroups': [], 'limit': 100,
                'properties': sorted(set(ACTIVITY_PROPERTIES['emails']) & available)}, coverage, source)
            links, resolved = session.object_contacts('emails', [str(row['id']) for row in rows], coverage)
            task_contacts = {cid for group in snapshot['campaigns'] for task in group['tasks']
                             for cid in task['contact_ids']}
            contacts, proved = session.contact_campaigns(
                task_contacts | {cid for ids in links.values() for cid in ids}, coverage)
            registry = {group['id']: group['registry'] for group in snapshot['campaigns'] if group.get('registry')}
            memberships = {}
            for cid, record in contacts.items():
                props = record['properties']
                campaign = props.get('campanha_ativa')
                registered = registry.get(campaign) if isinstance(campaign, str) else None
                known = registered is not None and registered.get('active') is True
                without_campaign = 'campanha_ativa' in props and (campaign is None or campaign == '')
                memberships[cid] = {'id': cid, 'campaign': campaign if known else None,
                    'status': 'without_campaign' if without_campaign else
                        'linked' if known else 'campaign_missing_or_unknown',
                    'executor': plain_text(props.get('executor')) or None,
                    'platform': registered.get('platform') if known else None}
                member = memberships[cid]
                if member['executor'] and member['platform'] and member['executor'] != member['platform']:
                    snapshot['contact_conflicts'].append({'contact_id': cid, 'campaign': member['campaign'],
                        'executor': member['executor'], 'platform': member['platform'],
                        'reason': 'executor_differs_from_registry_platform'})
            snapshot['contact_memberships'] = list(memberships.values())
            for group in snapshot['campaigns']:
                for task in group['tasks']:
                    if task['link_status'] != 'linked':
                        continue
                    for cid in task['contact_ids']:
                        current = memberships.get(cid, {}).get('campaign')
                        if current and current != task['campaign']:
                            snapshot['contact_conflicts'].append({'contact_id': cid, 'campaign': current,
                                'task_campaign': task['campaign'], 'task_id': task['id'],
                                'reason': 'task_campaign_differs_from_current_campaign'})
            for row in rows:
                ident = str(row['id'])
                props = row.get('properties')
                if not isinstance(props, dict) or row.get('archived') is True:
                    _partial(coverage, source + ':invalid_or_archived_record')
                    continue
                event = project_activity('emails', row)
                ids = links.get(ident, [])
                event.update(contact_ids=ids, provider=None, origin_provider=None,
                    origin_identifiers={key: plain_text(props.get(key)) or None
                        for key in ('hs_campaign_guid', 'hs_engagement_source_id')},
                    sent_evidence=False, response_evidence=False, crm_recorded_sent=False,
                    historical_membership_verified=False, delivery_verified=False)
                direction, status = props.get('hs_email_direction'), props.get('hs_email_status')
                classified = (isinstance(direction, str)
                    and direction in {'EMAIL', 'INCOMING_EMAIL', 'FORWARDED_EMAIL', 'DRAFT_EMAIL'}
                    and (direction == 'INCOMING_EMAIL' or isinstance(status, str) and status in {
                        'SENT', 'BOUNCED', 'FAILED', 'SCHEDULED', 'SENDING', 'DRAFT'})
                    and event['occurred_at'] is not None)
                sent = classified and status == 'SENT' and direction == 'EMAIL'
                event.update(sent_evidence=sent, crm_recorded_sent=sent,
                             sent_evidence_kind='crm_recorded_status')
                reason = None
                if ident not in resolved:
                    reason = 'contact_association_incomplete'
                elif not ids:
                    reason = 'contact_id_unavailable'
                elif not set(ids) <= proved:
                    reason = 'contact_read_incomplete'
                elif any(memberships[cid]['status'] == 'campaign_missing_or_unknown' for cid in ids):
                    reason = 'contact_campaign_unavailable'
                elif len({memberships[cid]['campaign'] for cid in ids}) != 1:
                    reason = 'contact_campaign_conflict'
                elif memberships[ids[0]]['status'] == 'without_campaign':
                    reason = 'contact_without_campaign'
                else:
                    campaign = memberships[ids[0]]['campaign']
                    if campaign not in groups:
                        reason = 'contact_campaign_out_of_scope'
                if reason:
                    event['attribution_reason'] = reason
                    snapshot['unattributed_activities'].append(event)
                    if reason not in {'contact_without_campaign', 'contact_campaign_out_of_scope'}:
                        _partial(coverage, source + ':' + reason)
                    continue
                if not classified:
                    _partial(coverage, source + ':classification_unavailable')
                group = groups[campaign]
                event.update(campaign=campaign, provider=group['registry']['platform'],
                    provider_semantics='registry_platform_not_origin',
                    attribution='contact_current_campaign')
                group['activities'].append(event)
            complete = all(coverage['sources'].get(key, {}).get('complete') is True for key in
                ('external_emails', 'email_contacts', 'contact_campaigns', 'contact_campaign_schema'))
            complete = complete and not any(reason.startswith(source + ':') for reason in coverage['limitations'])
            for campaign, group in groups.items():
                observed = sum(event['crm_recorded_sent'] for event in group['activities'])
                group['metrics'].update(observed_external_sent=observed,
                                        external_sent=observed if complete else None)
                observed_contacts = len(task_contact_ids[campaign] | {
                    cid for event in group['activities'] for cid in event['contact_ids']})
                contacts_complete = complete and group['task_coverage_complete'] and not any(
                    task['link_status'] == 'unverified' for task in group['tasks'])
                group['metrics'].update(contacts=observed_contacts if contacts_complete else None,
                                        observed_contacts=observed_contacts)
                group['metric_availability']['contacts'] = (
                    'observed_task_and_email_contacts' if contacts_complete else 'contact_read_incomplete')
                group['metric_availability']['external_sent'] = ('configuration_pending' if configuration_pending else
                    'crm_recorded_sent_not_delivery' if complete else 'activity_read_incomplete')
                group['external_activity_state'] = ('configuration_pending' if configuration_pending else
                    'complete' if complete else 'partial')
                coverage['sources'][source + ':' + campaign] = {'complete': complete,
                    'records': len(group['activities']), 'attribution': 'contact_current_campaign'}
        except CadenceReadonlyError as exc:
            _partial(coverage, source + ':' + exc.code)
            coverage['sources'][source] = {'complete': False, 'error': exc.code}
            for campaign, group in groups.items():
                group['external_activity_state'] = 'configuration_pending' if exc.code == 'configuration_pending' else 'partial'
                group['metric_availability']['external_sent'] = exc.code
                coverage['sources'][source + ':' + campaign] = {'complete': False, 'error': exc.code}

    def timeline(self, contact_id, campaign=None):
        if not isinstance(contact_id, str) or _id(contact_id) is None:
            raise CadenceReadonlyError("invalid_contact_id")
        if campaign is not None and (not isinstance(campaign, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", campaign)):
            raise CadenceReadonlyError("invalid_campaign")
        snapshot = self.snapshot()
        return self._cached(("timeline", contact_id, campaign, snapshot["collected_at"]),
                            lambda: self._timeline(contact_id, campaign, snapshot))

    def _timeline(self, contact_id, campaign, snapshot):
        coverage, events, errors = deepcopy(snapshot["coverage"]), [], []
        result = {"ok": True, "portal_id": PORTAL_ID, "owner_id": OWNER_ID,
                  "contact_id": contact_id, "campaign": campaign, "collected_at": self._now(),
                  "events": events, "coverage": coverage, "errors": errors}
        tasks = [task for group in snapshot["campaigns"] for task in group["tasks"] if
                 contact_id in task["contact_ids"] and (campaign is None or task["campaign"] == campaign)]
        linked_activities = {event['id']: event for group in snapshot['campaigns']
            for event in group.get('activities', []) if contact_id in event.get('contact_ids', [])
            and (campaign is None or event['campaign'] == campaign)}
        if not tasks and not linked_activities:
            task_sources = {key: value for key, value in coverage["sources"].items()
                            if key in {"tasks", "task_contacts", "task_schema", "external_emails",
                                       "email_contacts", "contact_campaigns", "contact_campaign_schema"}}
            if not task_sources or not all(source["complete"] for source in task_sources.values()):
                raise CadenceReadonlyError("contact_membership_unverified")
            raise LookupError("contact_not_in_observed_cadence")
        result["contact_name"] = next((contact["name"] for task in tasks for contact in task.get("contacts", [])
                                       if contact["id"] == contact_id and contact["name"]), None)
        events.extend({"id": "tasks:" + task["id"], "type": "task", "occurred_at": task["due_at"],
                "timestamp_semantics": "due_at", "text": task["body"], "subject": task["title"],
                "receipt": task['receipt'], "outcome": task['outcome'], "reason": task['reason'],
                "status": task["status"], "source": "hubspot_task",
                "campaign": task["campaign"], "approval_status": "unknown"} for task in tasks)
        session = self.adapter.session()
        session.identity()
        context = self._cached(("activities", contact_id), lambda: self._activities(session, contact_id))
        events.extend(context["events"])
        observed_ids = {event['id'] for event in events}
        events.extend(deepcopy(event) for ident, event in linked_activities.items() if ident not in observed_ids)
        for event in events:
            if event['id'] in linked_activities:
                event.update(deepcopy(linked_activities[event['id']]))
        for source, info in context["coverage"]["sources"].items():
            coverage["sources"]["activities:" + source] = info
        for reason in context["coverage"]["limitations"]:
            _partial(coverage, reason)
        errors.extend(context["errors"])
        events.sort(key=lambda event: (event["occurred_at"] or "", event["id"]))
        coverage["limitations"].extend(["unbound_activities_are_contact_context_only",
            "crm_activity_logs_are_not_external_delivery_receipts", "approval_state_unknown"])
        result["metrics"] = context["metrics"]
        return result

    def _activities(self, session, contact_id):
        coverage, events, errors = _coverage(), [], []
        for kind, wanted in ACTIVITY_PROPERTIES.items():
                try:
                    available = session.properties(kind)
                    required = set(wanted) - {'hs_campaign_guid', 'hs_engagement_source_id'}
                    if required - available:
                        _partial(coverage, kind + ":properties_unavailable")
                    links = session.pages("GET", f"/crm/v4/objects/contacts/{contact_id}/associations/{kind}",
                        None, coverage, kind, key="toObjectId")
                    for offset in range(0, len(links), 100):
                        ids = {_id(row["toObjectId"]) for row in links[offset:offset + 100]}
                        batch = session.call("POST", f"/crm/v3/objects/{kind}/batch/read",
                            {"properties": sorted(set(wanted) & available), "inputs": [{"id": id_} for id_ in sorted(ids)]})
                        if batch.get("status") != "COMPLETE" or batch.get("errors") or batch.get("numErrors", 0):
                            _partial(coverage, kind + ":batch_incomplete")
                        rows = batch.get("results")
                        if not isinstance(rows, list):
                            raise CadenceReadonlyError("provider_invalid_batch")
                        found = set()
                        for row in rows:
                            if not isinstance(row, dict) or _id(row.get("id")) not in ids or not isinstance(row.get("properties"), dict):
                                raise CadenceReadonlyError("provider_invalid_record")
                            if row.get("archived") is True or row["id"] in found:
                                continue
                            found.add(str(row["id"]))
                            events.append(project_activity(kind, row))
                        if found != ids:
                            _partial(coverage, kind + ":batch_records_missing")
                except CadenceReadonlyError as exc:
                    _partial(coverage, kind + ":" + exc.code)
                    errors.append({"source": kind, "code": exc.code})
        replies = sum(event.get("response_evidence", False) for event in events)
        meetings = sum(event["type"] == "meeting" for event in events)
        def available(kind):
            return coverage["sources"].get(kind, {}).get("complete", False) and not any(
                reason.startswith(kind + ":") for reason in coverage["limitations"])
        metrics = {"replies": replies if available("emails") else None,
                   "meetings": meetings if available("meetings") else None,
                   "observed_replies": replies, "observed_meetings": meetings,
                   "scope": "observed_contact_context", "complete": coverage["complete"]}
        return {"events": events, "coverage": coverage, "errors": errors, "metrics": metrics}


def project_activity(kind, record):
    props = record["properties"]
    names = {"emails": ("email", "hs_email_subject", "hs_email_text", "hs_email_status"),
             "meetings": ("meeting", "hs_meeting_title", "hs_meeting_body", "hs_meeting_outcome"),
             "calls": ("call", "hs_call_title", "hs_call_body", "hs_call_status"),
             "notes": ("note", "", "hs_note_body", ""),
             "communications": ("communication", "", "hs_communication_body", "")}
    event_type, title, body, status = names[kind]
    text = props.get(body) or (props.get("hs_email_html") if kind == "emails" else "")
    return {"id": kind + ":" + str(record["id"]), "type": event_type,
            "occurred_at": _date(props.get("hs_timestamp")), "text": plain_text(text),
            "subject": plain_text(props.get(title)), "receipt": None,
            "status": plain_text(props.get(status)) or "UNKNOWN", "source": "hubspot_" + event_type,
            "campaign": None, "attribution": "contact_context",
            "response_evidence": kind == "emails" and props.get("hs_email_direction") == "INCOMING_EMAIL"}
