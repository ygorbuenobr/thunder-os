"""F1 offline only: synthetic metadata -> schema proposal and sequence fixture.

No HTTP transport, credential loader, CRM writer, enrollment or scheduler exists.
MODO_SIMULACAO cannot be disabled. Output is not a receipt or authorization.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json

PORTAL_ID = "51412240"
OWNER_ID = "92901049"
GROUP = {"name": "cadencia_thunder", "label": "Cadência Thunder", "displayOrder": -1}
MULTILINE = (
    "cad_email_1", "cad_email_2", "cad_email_3",
    "cad_email_4", "cad_email_4b", "cad_li_0", "cad_li_1", "cad_li_2",
    "cad_li_3", "cad_wa_1", "cad_tese", "cad_fato_fonte",
)
TEXT = ("cad_email_1_assunto", "cad_teste_id")
REUSED_LINKEDIN_PROPERTY = "linkedin_url"
CHOICES = {
    "cad_braco": ("A", "B"),
    "cad_email_4_versao": ("A", "B"),
    "cad_status": ("rascunho", "aprovado", "em_cadencia", "respondeu", "encerrado", "incerto"),
}


class SimulationError(ValueError):
    """Missing identity, incomplete snapshot or incompatible schema."""


def _require(condition, message):
    if not condition:
        raise SimulationError(message)


def properties():
    """Fresh copies of the 17 contact property definitions in spec section 4."""
    result = []
    for name in (*MULTILINE, *CHOICES, *TEXT):
        prop = {"name": name, "label": name, "groupName": GROUP["name"],
                "type": "enumeration" if name in CHOICES else "string",
                "fieldType": "select" if name in CHOICES else "textarea" if name in MULTILINE else "text"}
        if name in CHOICES:
            prop["options"] = [{"label": v, "value": v, "displayOrder": i, "hidden": False}
                               for i, v in enumerate(CHOICES[name])]
        result.append(prop)
    return result


def _index(rows):
    _require(isinstance(rows, list), "snapshot_list_required")
    result = {}
    for row in rows:
        _require(isinstance(row, dict) and isinstance(row.get("name"), str), "invalid_schema_row")
        _require(row["name"] not in result, "duplicate_schema_name")
        result[row["name"]] = row
    return result


def plan_schema(snapshot, *, modo_simulacao=True):
    """Compare a caller-supplied synthetic full snapshot; never apply proposals.

    Archived rows must be included. Conflicts suppress ALL create proposals;
    existing definitions are never patched, deleted or silently recreated.
    The synthetic marker is a provenance assertion, not verified CRM evidence.
    """
    _require(modo_simulacao is True, "real_mode_unavailable")
    _require(isinstance(snapshot, dict) and snapshot.get("synthetic") is True, "synthetic_snapshot_required")
    _require(snapshot.get("portal_id") == PORTAL_ID and snapshot.get("owner_id") == OWNER_ID,
             "target_mismatch")
    _require(snapshot.get("object_type") == "contacts", "contacts_only")
    _require(snapshot.get("complete") is True and snapshot.get("includes_archived") is True,
             "complete_snapshot_including_archived_required")
    groups, current = _index(snapshot.get("groups")), _index(snapshot.get("properties"))
    conflicts, proposals, unchanged = [], [], []
    group = groups.get(GROUP["name"])
    alias = [g for g in groups.values() if g.get("label") == GROUP["label"] and g["name"] != GROUP["name"]]
    if alias:
        conflicts.append({"name": GROUP["name"], "reason": "group_label_collision"})
    if group is None:
        proposals.append({"method": "POST", "path": "/crm/v3/properties/contacts/groups", "body": deepcopy(GROUP)})
    elif group.get("label") != GROUP["label"] or group.get("archived", False) is not False:
        conflicts.append({"name": GROUP["name"], "reason": "group_conflict"})
    for desired in properties():
        name = desired["name"]
        actual = current.get(name)
        if actual is None:
            proposals.append({"method": "POST", "path": "/crm/v3/properties/contacts", "body": desired})
            continue
        fields = ("type", "fieldType", "groupName")
        compatible = all(actual.get(k) == desired[k] for k in fields)
        compatible &= actual.get("archived", False) is False
        compatible &= actual.get("calculated", False) is False
        compatible &= actual.get("hasUniqueValue", False) is False
        compatible &= actual.get("modificationMetadata", {}).get("readOnlyValue", False) is False
        if name in CHOICES:
            opts = actual.get("options", [])
            compatible &= isinstance(opts, list) and all(isinstance(o, dict) for o in opts)
            if compatible:
                values = [o.get("value") for o in opts]
                compatible &= len(values) == len(CHOICES[name]) and set(values) == set(CHOICES[name])
                compatible &= all(o.get("hidden", False) is False for o in opts)
        if compatible:
            unchanged.append(name)
        else:
            conflicts.append({"name": name, "reason": "incompatible_or_archived_property"})
    return {"mode": "MODO_SIMULACAO", "source": "synthetic_fixture", "portal_id": PORTAL_ID,
            "owner_id": OWNER_ID, "status": "blocked" if conflicts else "planned_only",
            "external_effects": 0, "conflicts": conflicts, "unchanged": unchanged,
            "proposals": [] if conflicts else proposals}


def sequence_blueprint():
    """Desired calendar, not a HubSpot enrollment or proof of scheduling."""
    rows = [
        (0, "email", "E-mail 1", "cad_email_1"),
        (1, "task", "LinkedIn: convite", "cad_li_0"),
        (3, "email", "E-mail 2", "cad_email_2"),
        (4, "task", "WhatsApp 1", "cad_wa_1"),
        (5, "task", "LinkedIn: msg 1", "cad_li_1"),
        (8, "email", "E-mail 3", "cad_email_3"),
        (10, "task", "LinkedIn: msg 2", "cad_li_2"),
        (13, "email", "E-mail 4", "cad_email_4"),
        (15, "task", "LinkedIn: msg 3", "cad_li_3"),
    ]
    steps, previous = [], 0
    for day, kind, title, prop in rows:
        step = {"day": day, "delay_business_days": day - previous, "kind": kind, "title": title,
                "body_token": "{{ contact." + prop + " }}" if prop else None}
        if kind == "email":
            step["thread"] = "new" if day == 0 else "reply_to_email_1"
            if day == 0:
                step["subject_token"] = "{{ contact.cad_email_1_assunto }}"
        else:
            step["pause_until_completed"] = False
        steps.append(step)
        previous = day
    return {"name": "Cadência Thunder v1", "timezone": "America/Sao_Paulo",
            "business_days_only": True, "steps": steps, "status": "design_only",
            "linkedin_property": REUSED_LINKEDIN_PROPERTY,
            "unimplemented_dependencies": ["F3: aceite/reagendamento/parada",
                "D10: seleção A/B e verificação de atualização do token antes do D13"]}


def list_sequences_fixture(fixture, *, modo_simulacao=True):
    """Exercise v4 pagination using ONLY in-memory synthetic response pages.

    A user ID is resolved from owner metadata, never guessed from the owner ID.
    Response pagination links are ignored; only opaque `after` cursors are used.
    No network transport is injectable.
    """
    _require(modo_simulacao is True, "real_mode_unavailable")
    _require(isinstance(fixture, dict) and fixture.get("synthetic") is True, "synthetic_fixture_required")
    _require(fixture.get("portal_id") == PORTAL_ID, "target_mismatch")
    owner = fixture.get("owner", {})
    _require(owner.get("id") == OWNER_ID and owner.get("archived") is False, "owner_mismatch")
    user_id = owner.get("userId")
    if type(user_id) is int and user_id > 0:
        user_id = str(user_id)
    _require(isinstance(user_id, str) and user_id.isascii() and user_id.isdigit()
             and int(user_id) > 0, "resolved_user_id_required")
    pages = fixture.get("pages")
    _require(isinstance(pages, dict), "fixture_pages_required")
    cursor, seen, ids, rows, requests = "", set(), set(), [], []
    while True:
        _require(cursor not in seen, "pagination_cycle")
        seen.add(cursor)
        _require(len(seen) <= 100, "pagination_limit")
        page = pages.get(cursor)
        _require(isinstance(page, dict) and page.get("status") == 200, "page_missing_or_api_error")
        query = {"userId": user_id, "limit": 100}
        if cursor:
            query["after"] = cursor
        requests.append({"method": "GET", "path": "/automation/v4/sequences", "query": query})
        body = page.get("body", {})
        _require(isinstance(body.get("results"), list), "invalid_sequence_page")
        for row in body["results"]:
            _require(isinstance(row, dict) and isinstance(row.get("id"), str)
                     and bool(row["id"]) and isinstance(row.get("name"), str)
                     and row.get("userId") == user_id, "invalid_sequence_identity")
            _require(row["id"] not in ids, "duplicate_sequence_id")
            ids.add(row["id"])
            rows.append({key: row[key] for key in ("id", "name", "userId")})
        paging = body.get("paging", {})
        _require(isinstance(paging, dict), "invalid_paging")
        if "next" not in paging:
            break
        nxt = paging["next"]
        _require(isinstance(nxt, dict) and isinstance(nxt.get("after"), str) and nxt["after"], "invalid_cursor")
        cursor = nxt["after"]
    return {"mode": "MODO_SIMULACAO", "source": "synthetic_fixture", "external_effects": 0,
            "status": "fixture_read_only", "results": rows, "requests": requests}


def demo():
    snapshot = {"synthetic": True, "portal_id": PORTAL_ID, "owner_id": OWNER_ID,
                "object_type": "contacts", "complete": True, "includes_archived": True,
                "groups": [], "properties": []}
    fixture = {"synthetic": True, "portal_id": PORTAL_ID,
               "owner": {"id": OWNER_ID, "userId": 123456, "archived": False},
               "pages": {"": {"status": 200, "body": {"results": [
                   {"id": "fixture-sequence-1", "name": "Cadência Thunder v1 (SINTÉTICA)", "userId": "123456"}]}}}}
    return {"schema": plan_schema(snapshot), "sequence": sequence_blueprint(),
            "sequence_read": list_sequences_fixture(fixture)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", required=True, action="store_true", help="emit synthetic F1 JSON only")
    parser.parse_args()
    print(json.dumps(demo(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
