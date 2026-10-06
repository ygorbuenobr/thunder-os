"""Resolve exact LinkedIn-to-account links from bounded brain account fichas."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
from urllib.parse import urlsplit

from .connection_intake import ConnectionIntakeError, profile_key


LINK = re.compile(r"https://(?:www\.)?linkedin\.com/in/[^\s|)\]]+", re.I)
FIT_SECTOR = re.compile(r"\b(?:ind[uú]stri\w*|distribui[cç][aã]o|distribuidor\w*)\b", re.I)
DOMAIN = re.compile(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}\Z")


def company_name_key(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200 or any(ord(c) < 32 for c in value):
        raise ConnectionIntakeError("company_name_invalid")
    return " ".join(value.split()).lower()


def _section(content: str, heading: str) -> str:
    match = re.search(rf"(?ims)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)", content)
    return match.group(1) if match else ""


def _account_domain(content: str) -> str | None:
    match = re.search(r"(?im)^\s*-?\s*Site:\s*(https://\S+)", content)
    if not match:
        return None
    try:
        parsed = urlsplit(match.group(1).rstrip(".,;)>"))
    except ValueError:
        return None
    try:
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or port not in (None, 443) or not DOMAIN.fullmatch(host)):
        return None
    return host.removeprefix("www.")


def load_connection_catalog(brain: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return exact profile mapping and *candidate* fit evidence refs.

    A sector signal only permits bounded research; it is not a validated fit,
    verified revenue, intent, or permission to create an approval card.
    """
    root = Path(brain).resolve()
    accounts = root / "contas"
    if not accounts.is_dir() or accounts.is_symlink():
        raise ConnectionIntakeError("account_catalog_unavailable")
    mapping: dict[str, str] = {}
    fit: dict[str, str] = {}
    for path in sorted(accounts.glob("*/ficha.md")):
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > 100_000:
                continue
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            continue
        domain = _account_domain(content)
        if domain is None:
            continue
        relative = path.relative_to(root).as_posix()
        provenance = f"brain:{relative}#sha256:{sha256(raw).hexdigest()}"
        sector = re.search(r"(?im)^\s*-\s*(?:Setor|Segmento|Ind[uú]stria)(?:\s*/\s*porte)?\s*:\s*([^\n]+)", content)
        if sector and FIT_SECTOR.search(sector.group(1)):
            fit[domain] = provenance
        for heading in ("Decisores", "Decisor", "Decisora", "Decisor e solicitante"):
            for match in LINK.finditer(_section(content, heading)):
                try:
                    key = profile_key(match.group(0).rstrip(".,;>"))
                except ConnectionIntakeError:
                    continue
                existing = mapping.get(key)
                if existing is not None and existing != domain:
                    raise ConnectionIntakeError("connection_account_ambiguous")
                mapping[key] = domain
    return mapping, fit


def load_company_catalog(brain: str | Path) -> dict[str, tuple[str, str]]:
    """Index only unique, explicit account titles with a validated site.

    A name match is evidence for research triage, never an outreach identity.
    """
    root = Path(brain).resolve()
    accounts = root / "contas"
    if not accounts.is_dir() or accounts.is_symlink():
        raise ConnectionIntakeError("account_catalog_unavailable")
    result: dict[str, tuple[str, str]] = {}
    ambiguous: set[str] = set()
    for path in sorted(accounts.glob("*/ficha.md")):
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > 100_000:
                continue
            content = raw.decode("utf-8")
        except (OSError, UnicodeError):
            continue
        domain = _account_domain(content)
        title = re.search(r"(?m)^#\s+([^\n]+)$", content)
        if domain is None or title is None:
            continue
        try:
            name = company_name_key(title.group(1))
        except ConnectionIntakeError:
            continue
        if not name or "|" in name:
            continue
        ref = f"brain:{path.relative_to(root).as_posix()}#sha256:{sha256(raw).hexdigest()}"
        if name in result:
            ambiguous.add(name)
        else:
            result[name] = (domain, ref)
    return {name: item for name, item in result.items() if name not in ambiguous}
