"""Narrow HubSpot task port for the Hermes LinkedIn cadence runner."""

from __future__ import annotations

import http.client
import json
import os
import re
import ssl
from urllib.parse import quote, urlencode

from .hermes_linkedin_cadence import STEP_CONTRACT


class HubSpotTaskError(RuntimeError):
    pass


def environment_token(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", name):
        raise HubSpotTaskError("hubspot_token_reference_invalid")
    value = os.environ.get(name)
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise HubSpotTaskError("hubspot_credential_unavailable")
    return value


def _request(method, path, body, headers, timeout):
    if any(os.environ.get(name) for name in ("SSL_CERT_FILE", "SSL_CERT_DIR")):
        raise HubSpotTaskError("hubspot_tls_override_blocked")
    connection = http.client.HTTPSConnection("api.hubapi.com", 443, timeout=timeout,
                                             context=ssl.create_default_context())
    try:
        connection.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise HubSpotTaskError("hubspot_response_too_large")
        return response.status, raw
    finally:
        connection.close()


class HubSpotTasks:
    PROPERTIES = ("hs_task_subject", "hs_task_body", "hs_task_status", "hs_timestamp",
                  "hs_task_type", "hubspot_owner_id")

    def __init__(self, portal_id: str, owner_id: str, token_env: str, *, request=None, timeout=20):
        if not re.fullmatch(r"\d{1,20}", str(portal_id)) or not re.fullmatch(r"\d{1,20}", str(owner_id)):
            raise ValueError("hubspot_identity_invalid")
        if not isinstance(timeout, int) or not 5 <= timeout <= 30:
            raise ValueError("hubspot_timeout_invalid")
        self.portal_id, self.owner_id, self.token_env = str(portal_id), str(owner_id), token_env
        self.request, self.timeout, self._token, self._portal_checked = request or _request, timeout, None, False

    def _call(self, method, path, body=None):
        allowed = (
            method == "GET" and (path == "/account-info/v3/details" or bool(re.fullmatch(
                r"/crm/v3/objects/(?:tasks|contacts)/\d+\?(?:properties=[A-Za-z0-9_,%]+&associations=contacts|properties=[A-Za-z0-9_,%]+)", path)))
            or method == "POST" and path == "/crm/v3/objects/tasks/search"
            or method == "PATCH" and bool(re.fullmatch(r"/crm/v3/objects/(?:tasks|contacts)/\d+", path))
        )
        if not allowed:
            raise HubSpotTaskError("hubspot_endpoint_blocked")
        if self._token is None:
            self._token = environment_token(self.token_env)
        try:
            status, raw = self.request(method, path, body, {
                "Authorization": "Bearer " + self._token, "Content-Type": "application/json",
            }, self.timeout)
            if not isinstance(status, int) or not 200 <= status < 300:
                raise HubSpotTaskError("hubspot_http_error")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise HubSpotTaskError("hubspot_invalid_response")
            return value
        except HubSpotTaskError:
            raise
        except Exception as exc:
            raise HubSpotTaskError("hubspot_request_uncertain") from exc

    def _portal(self):
        if not self._portal_checked:
            if str(self._call("GET", "/account-info/v3/details").get("portalId")) != self.portal_id:
                raise HubSpotTaskError("hubspot_portal_mismatch")
            self._portal_checked = True

    def due(self, limit=20):
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("hubspot_task_limit_invalid")
        self._portal()
        response = self._call("POST", "/crm/v3/objects/tasks/search", {
            "filterGroups": [{"filters": [
                {"propertyName": "hs_task_status", "operator": "EQ", "value": "NOT_STARTED"},
                {"propertyName": "hubspot_owner_id", "operator": "EQ", "value": self.owner_id},
                {"propertyName": "hs_task_subject", "operator": "EQ", "value": subject},
            ]} for subject, _action in STEP_CONTRACT.values()],
            "properties": list(self.PROPERTIES), "sorts": ["hs_timestamp"], "limit": limit,
        })
        rows = response.get("results")
        if not isinstance(rows, list) or len(rows) > limit:
            raise HubSpotTaskError("hubspot_invalid_response")
        return [str(row["id"]) for row in rows if isinstance(row, dict) and str(row.get("id", "")).isdigit()]

    def get(self, task_id):
        if not isinstance(task_id, str) or not task_id.isdigit():
            raise ValueError("hubspot_task_id_invalid")
        self._portal()
        query = urlencode({"properties": ",".join(self.PROPERTIES), "associations": "contacts"})
        row = self._call("GET", f"/crm/v3/objects/tasks/{quote(task_id, safe='')}?{query}")
        properties = row.get("properties")
        contacts = row.get("associations", {}).get("contacts", {}).get("results", [])
        if (str(row.get("id")) != task_id or not isinstance(properties, dict) or
                str(properties.get("hubspot_owner_id")) != self.owner_id or not isinstance(contacts, list)):
            raise HubSpotTaskError("hubspot_task_scope_mismatch")
        contact_ids = [str(item.get("id")) for item in contacts if isinstance(item, dict) and str(item.get("id", "")).isdigit()]
        contact = self.contact(contact_ids[0]) if len(contact_ids) == 1 else {}
        return {"id": task_id, "status": properties.get("hs_task_status"),
                "due_at": properties.get("hs_timestamp"), "subject": properties.get("hs_task_subject"),
                "body": properties.get("hs_task_body"), "contact_ids": contact_ids,
                "contact_status": contact.get("cad_status"), "cadence_id": contact.get("cadence_id")}

    def contact(self, contact_id):
        if not isinstance(contact_id, str) or not contact_id.isdigit():
            raise ValueError("hubspot_contact_id_invalid")
        self._portal()
        query = urlencode({"properties": "cad_status,hs_latest_sequence_enrolled"})
        row = self._call("GET", f"/crm/v3/objects/contacts/{quote(contact_id, safe='')}?{query}")
        properties = row.get("properties")
        if str(row.get("id")) != contact_id or not isinstance(properties, dict):
            raise HubSpotTaskError("hubspot_contact_scope_mismatch")
        status = properties.get("cad_status")
        cadence_id = properties.get("hs_latest_sequence_enrolled")
        if status not in {"rascunho", "aprovado", "em_cadencia", "respondeu", "encerrado", "incerto"}:
            raise HubSpotTaskError("hubspot_cadence_status_invalid")
        if not isinstance(cadence_id, str) or not cadence_id.isdigit():
            raise HubSpotTaskError("hubspot_cadence_id_invalid")
        return {"cad_status": status, "cadence_id": cadence_id}

    def mark_uncertain(self, contact_id, expected_status):
        if expected_status not in {"aprovado", "em_cadencia"}:
            raise HubSpotTaskError("hubspot_cadence_status_changed")
        current = self.contact(contact_id)["cad_status"]
        if current == "incerto":
            return {"id": contact_id, "cad_status": "incerto"}
        if current != expected_status:
            raise HubSpotTaskError("hubspot_cadence_status_changed")
        self._call("PATCH", f"/crm/v3/objects/contacts/{quote(contact_id, safe='')}",
                   {"properties": {"cad_status": "incerto"}})
        if self.contact(contact_id)["cad_status"] != "incerto":
            raise HubSpotTaskError("hubspot_uncertain_status_unconfirmed")
        return {"id": contact_id, "cad_status": "incerto"}

    def complete(self, task_id, receipt_ref, expected_task):
        if not isinstance(receipt_ref, str) or not receipt_ref.startswith("connectsafely:"):
            raise ValueError("provider_receipt_required")
        if (not isinstance(expected_task, dict)
                or set(expected_task)!={"subject","body","contact_ids","content","sent_at"}
                or not isinstance(expected_task["contact_ids"],list)
                or not all(isinstance(expected_task[key],str) and expected_task[key]
                           for key in ("content","sent_at"))):
            raise ValueError("expected_task_contract_required")
        current = self.get(task_id)
        note = (expected_task["body"] + "\n\n[Thunder OS] Enviado por Hermes via ConnectSafely; "
                "horario=" + expected_task["sent_at"] + "; provider_id=" + receipt_ref
                + "; texto=" + expected_task["content"])
        if (current["subject"] != expected_task["subject"] or current["contact_ids"] != expected_task["contact_ids"]
                or current["body"] not in {expected_task["body"], note}):
            raise HubSpotTaskError("hubspot_task_changed_after_dispatch")
        if current["status"] == "COMPLETED":
            return {"id": task_id, "status": "COMPLETED"}
        self._call("PATCH", f"/crm/v3/objects/tasks/{quote(task_id, safe='')}",
                   {"properties": {"hs_task_status": "COMPLETED", "hs_task_body": note}})
        confirmed = self.get(task_id)
        if confirmed["status"] != "COMPLETED":
            raise HubSpotTaskError("hubspot_completion_unconfirmed")
        return {"id": task_id, "status": "COMPLETED"}
