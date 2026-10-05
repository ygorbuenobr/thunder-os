"""Operator-only audit of the fixed legacy panel batch; no LinkedIn dispatch."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from .connection_reconcile_apply import (
    ReconciliationBlocked, _batch, _manifest, create_legacy_manifest,
    reconcile_legacy_batch,
)
from .connection_reconcile_runtime import _card_public_slug
from .connection_reconcile_runtime import audit_cards
from .connection_runtime import load_config, _protected_file
from .connectsafely_social_client import ConnectSafelySocialClient, post_identity
from .connectsafely_social_transport import ConnectSafelySocialHTTPS
from .connectsafely_client import ConnectSafelyClient


def bindings_for_batch(cards, account_id, account_public_id, owner):
    bindings = {}
    for card_id, _, card in cards:
        if card.get("tipo") == "dm":
            try:
                slug = _card_public_slug(card)
            except ValueError:
                continue
            bindings[card_id] = {
                "card_id": card_id, "channel": "linkedin_inbox",
                "account_id": account_id, "account_public_id": account_public_id,
                "owner_profile_urn": owner, "recipient_public_id": slug,
                "recipient_profile_url": card["perfil_url"],
            }
        elif card.get("tipo") == "comentario":
            try:
                post = post_identity(card.get("post_url"))
            except ValueError:
                continue
            bindings[card_id] = {
                "card_id": card_id, "channel": "linkedin_comment",
                "account_id": account_id, "account_public_id": account_public_id,
                "owner_profile_urn": owner, "post_urn": post,
            }
    return bindings


def owner_profile_urn(value):
    owner_id = ConnectSafelyClient._profile_identity(value)
    if owner_id is None:
        raise ReconciliationBlocked("owner_identity_unconfigured")
    return "urn:li:fsd_profile:" + owner_id


def canonical_panel_lock(panel):
    profile_root = panel.parent.parent
    brain = profile_root / "brain"
    if (panel.name != "painel.db" or panel.parent.name != "painel-dados"
            or not brain.is_dir() or brain.is_symlink()):
        raise ReconciliationBlocked("panel_lock_authority_unverified")
    return brain / "_log" / ".painel.lock"


def audit_chunk(cards, bindings, client, *, offset, limit):
    if (type(offset) is not int or type(limit) is not int or offset < 0
            or not 1 <= limit <= 10 or offset + limit > len(cards)):
        raise ReconciliationBlocked("audit_chunk_invalid")
    selected = cards[offset:offset + limit]
    result = audit_cards([card for _, _, card in selected],
                         {key: bindings[key] for key, _, _ in selected if key in bindings},
                         client)
    if len(result["items"]) != len(selected):
        raise ReconciliationBlocked("audit_chunk_incomplete")
    return {"scope_offset": offset, "scope_limit": limit,
            "scope_count": len(selected), "manifest_complete": True,
            "audit_complete": False, "decisions_provisional": True,
            "read_only": True, "items": result["items"], "applied": []}


def consolidate_audit_chunks(paths, entries, source_sha, manifest_sha):
    if not paths or len(paths) > len(entries):
        raise ReconciliationBlocked("audit_chunks_invalid")
    indexed = {}
    receipt_hashes = []
    for path in paths:
        info = path.lstat()
        if (not path.is_file() or path.is_symlink() or info.st_mode & 0o077
                or info.st_uid not in {0, os.geteuid()}):
            raise ReconciliationBlocked("audit_chunk_receipt_invalid")
        raw = path.read_bytes()
        receipt = json.loads(raw)
        report = receipt.get("report")
        if (receipt.get("mode") != "audit" or receipt.get("source_sha") != source_sha
                or receipt.get("manifest_sha256") != manifest_sha
                or not isinstance(report, dict) or report.get("audit_complete") is not False
                or report.get("decisions_provisional") is not True):
            raise ReconciliationBlocked("audit_chunk_receipt_invalid")
        offset, count = report.get("scope_offset"), report.get("scope_count")
        if (type(offset) is not int or type(count) is not int or count < 1
                or offset < 0 or offset + count > len(entries)
                or not isinstance(report.get("items"), list)
                or len(report["items"]) != count):
            raise ReconciliationBlocked("audit_chunk_receipt_invalid")
        for position, item in enumerate(report["items"], offset):
            if (position in indexed or not isinstance(item, dict)
                    or item.get("card_id") != entries[position]["id"]):
                raise ReconciliationBlocked("audit_chunk_overlap_or_mismatch")
            if item.get("decision") == "close" and not isinstance(item.get("evidence"), dict):
                raise ReconciliationBlocked("audit_chunk_receipt_invalid")
            indexed[position] = dict(item)
        receipt_hashes.append(sha256(raw).hexdigest())
    if set(indexed) != set(range(len(entries))):
        raise ReconciliationBlocked("audit_chunks_incomplete")
    items = [indexed[position] for position in range(len(entries))]
    owners = Counter((item.get("evidence", {}).get("account_sha256"),
                      item.get("evidence", {}).get("reference_sha256"))
                     for item in items if item.get("decision") == "close")
    for item in items:
        evidence = item.get("evidence")
        if (item.get("decision") == "close" and isinstance(evidence, dict)
                and owners[(evidence.get("account_sha256"),
                            evidence.get("reference_sha256"))] > 1):
            item.pop("evidence", None)
            item.pop("proposed_status", None)
            item.update(decision="inconclusive", reason="shared_receipt_ambiguous")
    return {"manifest_complete": True, "audit_complete": True,
            "read_only": True, "items": items, "receipt_sha256": receipt_hashes,
            "applied": []}


def _write_receipt(path, payload):
    if not path.parent.is_dir() or path.exists() or path.is_symlink():
        raise ReconciliationBlocked("receipt_target_invalid")
    encoded = (json.dumps(payload, sort_keys=True, ensure_ascii=True,
                          separators=(",", ":"), allow_nan=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_directory(path.parent)


def _sync_directory(path):
    directory = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _finish_receipt(path, payload):
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write((json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":"), allow_nan=False) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    _sync_directory(path.parent)


def _recover_receipt(path, panel, entries, source_sha, manifest_sha):
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ReconciliationBlocked("receipt_journal_invalid") from None
    if (journal.get("source_sha") != source_sha
            or journal.get("manifest_sha256") != manifest_sha):
        raise ReconciliationBlocked("receipt_journal_mismatch")
    if journal.get("mode") == "apply" and isinstance(journal.get("report"), dict):
        _sync_directory(path.parent)
        return {"status": "existing_final_verified", "receipt": str(path),
                "items": len(journal["report"].get("items", []))}
    if journal.get("status") != "started":
        raise ReconciliationBlocked("receipt_journal_mismatch")
    cards = _batch(panel, entries)
    items = []
    for card_id, _, card in cards:
        evidence = card.get("reconciliacao_externa")
        if isinstance(evidence, dict) and evidence.get("source") == "connectsafely_readback":
            decision = evidence.get("decision")
            expected = "enviado" if decision == "close" else "descartado"
            if decision not in {"close", "superseded"} or card.get("status") != expected:
                raise ReconciliationBlocked("receipt_recovery_state_invalid")
            items.append({"card_id": card_id, "status": expected,
                          "decision": decision, "evidence": evidence})
        else:
            items.append({"card_id": card_id, "status": card.get("status"),
                          "decision": "unverified"})
    payload = {"status": "recovered_from_panel", "source_sha": source_sha,
               "manifest_sha256": manifest_sha,
               "recovered_at": datetime.now(timezone.utc).isoformat(), "items": items}
    _finish_receipt(path, payload)
    return {"status": "recovered", "receipt": str(path), "items": len(items),
            "verified_changes": sum(item["decision"] != "unverified" for item in items)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "panel-db", "panel-lock", "manifest", "receipt"):
        parser.add_argument("--" + name, type=Path, required=name != "receipt")
    parser.add_argument("--create-manifest", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--recover-receipt", action="store_true")
    parser.add_argument("--audit-offset", type=int)
    parser.add_argument("--audit-limit", type=int)
    parser.add_argument("--consolidate-receipts", type=Path, nargs="+")
    args = parser.parse_args(argv)
    if sum((args.create_manifest, args.apply, args.recover_receipt,
            args.consolidate_receipts is not None)) > 1:
        parser.error("manifest creation, apply and recovery are separate operations")
    if ((args.audit_offset is None) != (args.audit_limit is None)
            or args.apply and args.audit_offset is not None
            or args.create_manifest and args.audit_offset is not None
            or args.recover_receipt and args.audit_offset is not None
            or args.consolidate_receipts is not None and args.audit_offset is not None):
        parser.error("chunk options are only for read-only audit")
    try:
        config = load_config(args.config)
        panel = args.panel_db.resolve(strict=True)
        if panel != Path(config["repository"]).resolve(strict=True):
            raise ReconciliationBlocked("panel_repository_mismatch")
        if args.apply and args.panel_lock.resolve(strict=False) != canonical_panel_lock(panel):
            raise ReconciliationBlocked("panel_lock_mismatch")
        if args.create_manifest:
            digest = create_legacy_manifest(panel, args.manifest)
            print(json.dumps({"status": "manifest_created", "sha256": digest,
                              "count": 41}, sort_keys=True))
            return 0
        if args.receipt is None:
            raise ReconciliationBlocked("receipt_target_required")
        if not args.recover_receipt and (args.receipt.exists() or args.receipt.is_symlink()):
            raise ReconciliationBlocked("receipt_target_invalid")
        owner = owner_profile_urn(config.get("self_profile_id"))
        credential_path = Path(config["api_credential_file"])

        def credential():
            _protected_file(credential_path)
            return credential_path.read_text(encoding="utf-8").strip()

        transport = ConnectSafelySocialHTTPS(config["account_id"], credential)
        client = ConnectSafelySocialClient(config["account_id"], config["account_public_id"],
                                            request=transport, self_profile_id=owner)
        entries = _manifest(args.manifest)
        cards = _batch(panel, entries)
        source_manifest = Path(__file__).resolve().parents[2] / "thunder-os-manifest.json"
        source_sha = json.loads(source_manifest.read_text(encoding="utf-8"))["source_sha"]
        if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
            raise ReconciliationBlocked("source_revision_unverified")
        manifest_sha = sha256(args.manifest.read_bytes()).hexdigest()
        if args.consolidate_receipts is not None:
            report = consolidate_audit_chunks(args.consolidate_receipts, entries,
                                              source_sha, manifest_sha)
            _write_receipt(args.receipt.absolute(), {
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "mode": "consolidated_audit", "source_sha": source_sha,
                "manifest_sha256": manifest_sha, "report": report})
            print(json.dumps({"status": "ok", "audit_complete": True,
                              "items": len(report["items"]),
                              "receipt": str(args.receipt.absolute())}, sort_keys=True))
            return 0
        if args.recover_receipt:
            print(json.dumps(_recover_receipt(args.receipt.absolute(), panel, entries,
                                              source_sha, manifest_sha), sort_keys=True))
            return 0
        bindings = bindings_for_batch(cards, config["account_id"],
                                      config["account_public_id"], owner)
        receipt_path = args.receipt.absolute()
        if args.apply:
            _write_receipt(receipt_path, {"status": "started", "source_sha": source_sha,
                                          "manifest_sha256": manifest_sha,
                                          "started_at": datetime.now(timezone.utc).isoformat()})
        try:
            report = (audit_chunk(cards, bindings, client,
                                  offset=args.audit_offset, limit=args.audit_limit)
                      if args.audit_offset is not None else
                      reconcile_legacy_batch(panel, args.panel_lock, args.manifest,
                                             bindings, client, apply=args.apply))
        except BaseException:
            if args.apply:
                print(json.dumps({"status": "uncertain", "receipt": str(receipt_path),
                                  "recovery": "--recover-receipt"}), file=sys.stderr)
            raise
        payload = {"observed_at": datetime.now(timezone.utc).isoformat(),
                   "mode": "apply" if args.apply else "audit",
                   "source_sha": source_sha,
                   "manifest_sha256": manifest_sha,
                   "report": report}
        try:
            if args.apply:
                _finish_receipt(receipt_path, payload)
            else:
                _write_receipt(receipt_path, payload)
        except OSError:
            if args.apply:
                print(json.dumps({"status": "uncertain", "receipt": str(receipt_path),
                                  "recovery": "--recover-receipt"}), file=sys.stderr)
            raise
        counts = dict(Counter(item["decision"] for item in report["items"]))
        print(json.dumps({"status": "ok", "mode": payload["mode"],
                          "baseline_complete": report.get("baseline_complete", False),
                          "audit_complete": report.get("audit_complete",
                                                       report.get("baseline_complete", False)),
                          "decisions": counts, "applied": len(report["applied"]),
                          "receipt": str(args.receipt.absolute())}, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, ReconciliationBlocked):
        print(json.dumps({"status": "blocked", "error_code": "reconciliation_blocked"}),
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
