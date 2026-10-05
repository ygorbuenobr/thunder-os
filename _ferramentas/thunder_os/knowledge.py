"""Path-safe access to canonical Thunder knowledge."""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping


class KnowledgeError(ValueError):
    pass


class KnowledgeConflict(KnowledgeError):
    pass


@dataclass(frozen=True)
class KnowledgeSnapshot:
    provenance: str
    content: str
    content_hash: str
    validated: bool
    issue: str | None = None


class KnowledgeStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _path(self, relative: str, *, must_exist: bool = True) -> Path:
        if not isinstance(relative, str) or relative.startswith("/") or ".." in Path(relative).parts:
            raise KnowledgeError("outside brain root")
        path = self.root / relative
        try:
            resolved = path.resolve(strict=must_exist)
        except FileNotFoundError:
            resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(self.root) or (path.exists() and path.is_symlink()):
            raise KnowledgeError("outside brain root")
        return resolved

    def read(self, relative: str, *, required: bool = True) -> KnowledgeSnapshot:
        path = self._path(relative, must_exist=False)
        if not path.is_file():
            if required:
                raise KnowledgeError("knowledge missing")
            return KnowledgeSnapshot(relative, "", self._hash(""), False, "missing")
        content = path.read_text(encoding="utf-8")
        validated = re.search(r"(?m)^status:\s*validado\s*$", content) is not None
        return KnowledgeSnapshot(
            relative,
            content,
            self._hash(content),
            validated,
            None if validated else "not_validated",
        )

    def promote_validated_facts(
        self,
        slug: str,
        facts: list[Mapping[str, Any]],
        source_hash: str,
    ) -> KnowledgeSnapshot:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", slug):
            raise KnowledgeError("invalid account slug")
        if not facts:
            raise KnowledgeError("facts required")
        normalized = []
        for fact in facts:
            if set(fact) != {"fact", "source", "date", "status"}:
                raise KnowledgeError("invalid fact fields")
            if (fact["status"] != "validado" or not isinstance(fact["fact"], str)
                    or not fact["fact"].strip() or not re.fullmatch(r"https?://\S+", str(fact["source"]))
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(fact["date"]))):
                raise KnowledgeError("fact is not validated")
            normalized.append(dict(fact))
        path = self._path(f"contas/{slug}/ficha.md")
        with path.open("r+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            current = stream.read()
            if self._hash(current) != source_hash:
                conflict = self._path(
                    f"_reconciliation/{slug}-{self._hash(json.dumps(normalized, sort_keys=True))[:12]}.json",
                    must_exist=False,
                )
                conflict.parent.mkdir(parents=True, exist_ok=True)
                conflict.write_text(json.dumps({
                    "status": "needs_reconciliation", "slug": slug,
                    "current": {"content": current, "hash": self._hash(current)},
                    "incoming": {"facts": normalized, "source_hash": source_hash},
                }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
                raise KnowledgeConflict("source hash mismatch")
            lines = [
                f"- {fact['fact']} | fonte: {fact['source']} | data: {fact['date']} | status: validado"
                for fact in normalized
            ]
            updated = current.rstrip() + "\n\n## Fatos validados\n" + "\n".join(lines) + "\n"
            stream.seek(0)
            stream.write(updated)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
        return KnowledgeSnapshot(
            f"contas/{slug}/ficha.md", updated, self._hash(updated), True
        )

    def load_catalog(self) -> dict[str, Any]:
        path = self._path("thunder/catalog.json")
        try:
            return validate_catalog(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as exc:
            raise KnowledgeError("canonical catalog not provisioned or invalid") from exc

    def load_thunder_context(self) -> dict[str, KnowledgeSnapshot]:
        return {
            relative: self.read(relative, required=False)
            for relative in ("thunder/empresa.md", "thunder/icp.md", "thunder/provas.md")
        }


def validate_catalog(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Only trusted server configuration may call this catalog parser."""
    required = {"version", "status", "capabilities", "proofs"}
    if not isinstance(catalog, Mapping) or set(catalog) != required:
        raise KnowledgeError("canonical catalog not provisioned")
    if catalog["status"] != "validado" or not isinstance(catalog["version"], str) or not catalog["version"].strip():
        raise KnowledgeError("canonical catalog must be validated and versioned")
    for field in ("capabilities", "proofs"):
        if not isinstance(catalog[field], list) or any(not isinstance(v, str) or not v.strip() for v in catalog[field]):
            raise KnowledgeError("invalid canonical catalog ids")
    return {**dict(catalog), "content_hash": hashlib.sha256(json.dumps(dict(catalog), sort_keys=True).encode()).hexdigest()}
