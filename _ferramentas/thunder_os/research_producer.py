"""Bounded, credential-free public research collection for Thunder OS.

The collector only performs public HTTPS GETs. It does not use a browser, login,
paid API, model, CRM, or outbound provider. Extracted text remains untrusted data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import queue
import re
import socket
import ssl
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

from .research import SOURCE_AREAS


MAX_RESPONSE_BYTES = 512_000
MAX_FACTS = 40
MAX_MODEL_FACTS = 12
MAX_SEEDS_PER_AREA = 6
MAX_REQUESTS = 24
REQUEST_TIMEOUT = 8.0
MAX_COLLECTION_SECONDS = 90.0
DNS_TIMEOUT = 3.0
MAX_REDIRECTS = 3
_PUBLIC_SOCIAL_HOSTS = {"linkedin.com", "facebook.com", "instagram.com", "youtube.com"}
_JOB_HOST_MARKERS = {"gupy.io", "vagas.com.br", "indeed.com", "infojobs.com.br"}
_PAGE_TOPIC = re.compile(r"sobre|empresa|produto|solu|quem|institucional|not[ií]cia|news|blog|lan[cç]|evento|carreir|vaga|trabalhe|jobs?", re.I)
_NEWS_TOPIC = re.compile(r"not[ií]cia|news|blog|lan[cç]|evento|imprensa|release", re.I)
_HIRING_TOPIC = re.compile(r"carreir|vaga|trabalhe|jobs?|career|gupy", re.I)
_GENERIC_AUTH = re.compile(r"(sign in|log in|entre na sua conta|fa[cç]a login|entrar na conta|join linkedin)", re.I)


class ResearchCollectionError(ValueError):
    """Safe, non-secret producer failure code."""


@dataclass(frozen=True)
class ResearchRequest:
    account_id: str
    company_name: str
    website_url: str
    sector: str = ""
    public_urls: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    account_context: Mapping[str, str] = field(default_factory=dict)
    decision_maker_name: str = ""


@dataclass(frozen=True)
class FetchResponse:
    status: int
    url: str
    headers: Mapping[str, str]
    body: bytes
    observed_at: date
    cached: bool = False


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").encode("idna").decode("ascii").lower().rstrip(".")


def _normalize_https_url(url: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise ResearchCollectionError("invalid_public_url")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ResearchCollectionError("public_https_url_required")
    host = _host(url)
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".localhost", ".local", ".internal")):
        raise ResearchCollectionError("non_public_host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ResearchCollectionError("non_public_host")
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def _validate_public_url(url: str) -> str:
    safe_url = _normalize_https_url(url)
    host = _host(safe_url)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None:
        resolved = _resolve_addresses(host, DNS_TIMEOUT)
        if not resolved or any(not ipaddress.ip_address(value).is_global for value in resolved):
            raise ResearchCollectionError("non_public_host")
    return safe_url


def _resolve_addresses(host: str, timeout: float) -> tuple[str, ...]:
    """Resolve with a deadline; only globally routable addresses may be used."""
    try:
        address = ipaddress.ip_address(host)
        return (str(address),) if address.is_global else ()
    except ValueError:
        pass
    result: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def resolve():
        try:
            values = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            result.put(("ok", tuple(dict.fromkeys(item[4][0] for item in values))))
        except Exception:
            result.put(("error", None))

    threading.Thread(target=resolve, name="thunder-public-dns", daemon=True).start()
    try:
        status, addresses = result.get(timeout=max(0.01, timeout))
    except queue.Empty:
        raise ResearchCollectionError("dns_timeout") from None
    if status != "ok" or not addresses:
        raise ResearchCollectionError("public_host_unresolved")
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ResearchCollectionError("non_public_host")
    except ResearchCollectionError:
        raise
    except ValueError:
        raise ResearchCollectionError("public_host_unresolved") from None
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to the validated DNS answer while retaining SNI."""
    def __init__(self, host: str, addresses: tuple[str, ...], *, timeout: float):
        super().__init__(host, 443, timeout=timeout, context=ssl.create_default_context())
        self._public_addresses = addresses

    def connect(self):
        last_error = None
        for address in self._public_addresses:
            try:
                sock = socket.create_connection((address, self.port), self.timeout)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as exc:
                last_error = exc
        if last_error:
            raise last_error
        raise OSError("no validated public address")


class BoundedHTTPSFetcher:
    """Small in-memory response cache and strict, bounded public HTTPS GET client."""
    def __init__(self, *, timeout: float = REQUEST_TIMEOUT, max_bytes: int = MAX_RESPONSE_BYTES,
                 cache_ttl_seconds: int = 21_600, clock: Callable[[], datetime] | None = None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.cache_ttl_seconds = cache_ttl_seconds
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.cache: dict[str, tuple[datetime, FetchResponse]] = {}
        # Redirects are returned to the producer and requested one hop at a
        # time, so each hop consumes the request and host-delay budget.

    def fetch(self, url: str, *, timeout: float | None = None) -> FetchResponse:
        safe_url = _normalize_https_url(url)
        now = self.clock()
        cached = self.cache.get(safe_url)
        if cached and (now - cached[0]).total_seconds() <= self.cache_ttl_seconds:
            response = cached[1]
            return FetchResponse(response.status, response.url, response.headers,
                                 response.body, response.observed_at, True)
        parsed = urlsplit(safe_url)
        request_path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request_timeout = max(0.001, min(self.timeout, timeout if timeout is not None else self.timeout))
        request_deadline = time.monotonic() + request_timeout

        def remaining_request_time() -> float:
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                raise ResearchCollectionError("request_timeout")
            return remaining

        try:
            host = _host(safe_url)
            try:
                address = ipaddress.ip_address(host)
                addresses = (str(address),) if address.is_global else ()
            except ValueError:
                addresses = _resolve_addresses(host, min(DNS_TIMEOUT, remaining_request_time()))
            if not addresses:
                raise ResearchCollectionError("non_public_host")
            connection = _PinnedHTTPSConnection(host, addresses, timeout=remaining_request_time())
            try:
                if connection.sock:
                    connection.sock.settimeout(remaining_request_time())
                connection.request("GET", request_path, headers={
                    "Host": parsed.netloc,
                    "User-Agent": "ThunderOS-PublicResearch/1.0",
                    "Accept": "text/html,application/xhtml+xml,application/rss+xml,application/atom+xml,application/xml;q=0.9,*/*;q=0.1",
                    "Connection": "close",
                })
                if connection.sock:
                    connection.sock.settimeout(remaining_request_time())
                response = connection.getresponse()
                chunks: list[bytes] = []
                received = 0
                while True:
                    if connection.sock:
                        connection.sock.settimeout(remaining_request_time())
                    chunk = response.read1(min(64 * 1024, self.max_bytes + 1 - received))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise ResearchCollectionError("response_too_large")
                body = b"".join(chunks)
                result = FetchResponse(int(response.status), safe_url,
                    {key.lower(): value for key, value in response.getheaders()}, body, now.date())
            finally:
                connection.close()
        except http.client.HTTPException:
            raise ResearchCollectionError("request_failed") from None
        except ResearchCollectionError:
            raise
        except (TimeoutError, socket.timeout):
            raise ResearchCollectionError("request_timeout") from None
        except OSError:
            raise ResearchCollectionError("request_failed") from None
        self.cache[safe_url] = (now, result)
        return result


class _HTMLMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.description = ""
        self.published_at: str | None = None
        self.links: list[tuple[str, str]] = []
        self._in_title = False
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._times: list[str] = []
        self.paragraphs: list[str] = []
        self._in_paragraph = False
        self._skip_depth = 0
        self._capture_jsonld = False
        self._jsonld_parts: list[str] = []
        self._heading_count = 0
        self._pre_heading_dates: list[tuple[str, tuple[int, ...]]] = []
        self.article_paragraphs: list[str] = []
        self._containers: list[tuple[str, int]] = []
        self._container_sequence = 0
        self._heading_scope: tuple[int, ...] = ()
        self._heading_date_scope: tuple[int, ...] = ()

    def _scope(self):
        return tuple(number for _, number in self._containers)

    def _chrome(self):
        return any(tag in {'header', 'footer', 'nav', 'aside'} for tag, _ in self._containers)

    def _date_scope(self):
        semantic = tuple(number for tag, number in self._containers if tag in {'article', 'main', 'section'})
        return semantic or self._scope()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag in {'div', 'article', 'main', 'section', 'header', 'footer', 'nav', 'aside'}:
            self._container_sequence += 1
            self._containers.append((tag, self._container_sequence))
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
            if tag == "script" and values.get("type", "").lower().split(";", 1)[0].strip() == "application/ld+json":
                self._capture_jsonld = True
                self._jsonld_parts = []
        elif tag == "h1" and not self._skip_depth and not self._chrome():
            self._heading_count += 1
            self._heading_scope = self._scope()
            self._heading_date_scope = self._date_scope()
        elif tag == "p" and not self._skip_depth and not self._chrome():
            self._in_paragraph = True
            self._paragraph_parts = []
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            content = values.get("content", "").strip()
            if name in {"description", "og:description", "twitter:description"} and content and not self.description:
                self.description = content
            if name in {"article:published_time", "datepublished", "date", "pubdate"} and content and not self.published_at:
                self.published_at = _date_from_text(content)
        elif tag == "time" and not self._chrome():
            value = values.get("datetime", "").strip()
            if value:
                self._times.append(value)
        elif tag == "a":
            self._anchor_href = values.get("href") or None
            self._anchor_text = []

    def handle_endtag(self, tag: str):
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            if tag == "script" and self._capture_jsonld:
                self._read_jsonld_dates("".join(self._jsonld_parts))
                self._capture_jsonld = False
                self._jsonld_parts = []
            self._skip_depth -= 1
        elif tag == "p" and self._in_paragraph:
            text = _clean_text(" ".join(self._paragraph_parts))
            if len(text) >= 45 and text not in self.paragraphs:
                self.paragraphs.append(text)
                if (self._heading_count == 1 and not self._chrome()
                        and self._scope()[:len(self._heading_scope)] == self._heading_scope):
                    self.article_paragraphs.append(text)
            self._in_paragraph = False
            self._paragraph_parts = []
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._anchor_href:
            text = " ".join(" ".join(self._anchor_text).split())
            if text:
                self.links.append((self._anchor_href, text[:240]))
            self._anchor_href = None
            self._anchor_text = []
        for index in range(len(self._containers) - 1, -1, -1):
            if self._containers[index][0] == tag:
                del self._containers[index:]
                break

    def handle_data(self, data: str):
        value = " ".join(data.split())
        if not value:
            return
        if self._capture_jsonld:
            self._jsonld_parts.append(data)
        if self._skip_depth:
            return
        if (not self._heading_count and not self._in_title and not self._anchor_href and not self._chrome()
                and re.fullmatch(r"\d{1,2}\s+de\s+[A-Za-zçÇ]+\s+de\s+\d{4}", value)):
            parsed_date = _date_from_text(value)
            if parsed_date:
                self._pre_heading_dates.append((parsed_date, self._date_scope()))
        if self._in_title:
            self.title_parts.append(value)
        if self._anchor_href:
            self._anchor_text.append(value)
        if self._in_paragraph and not self._chrome():
            self._paragraph_parts.append(value)

    def _read_jsonld_dates(self, raw: str):
        if len(raw.encode("utf-8", errors="replace")) > 128_000:
            return
        try:
            value = json.loads(raw.strip())
        except (json.JSONDecodeError, RecursionError):
            return
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                candidate = item.get("datePublished")
                if isinstance(candidate, str) and not self.published_at:
                    self.published_at = _date_from_text(candidate)
                stack.extend(child for child in item.values() if isinstance(child, (dict, list)))
            elif isinstance(item, list):
                stack.extend(child for child in item if isinstance(child, (dict, list)))

    @property
    def title(self) -> str:
        return " ".join(" ".join(self.title_parts).split())[:500]

    @property
    def statements(self) -> list[str]:
        values = []
        if self.title:
            values.append(self.title)
        if self.description:
            values.append(self.description)
        paragraphs = self.article_paragraphs if self._heading_count == 1 and self.article_paragraphs else self.paragraphs
        values.extend(paragraphs[:4])
        return list(dict.fromkeys(_clean_text(value) for value in values if _clean_text(value)))

    @property
    def page_date(self) -> str | None:
        explicit = self.published_at or next((value for item in self._times if (value := _date_from_text(item))), None)
        if explicit:
            return explicit
        if self._heading_count == 1 and len(self._pre_heading_dates) == 1:
            value, scope = self._pre_heading_dates[0]
            if scope == self._heading_date_scope:
                return value
        return None


def _date_from_text(value: str) -> str | None:
    if not value:
        return None
    months = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro")
    portuguese = re.fullmatch(r"\s*(\d{1,2})\s+de\s+([A-Za-zçÇ]+)\s+de\s+(\d{4})\s*", value, re.I)
    if portuguese and portuguese[2].lower() in months:
        try:
            return date(int(portuguese[3]), months.index(portuguese[2].lower()) + 1, int(portuguese[1])).isoformat()
        except ValueError:
            return None
    match = re.search(r"\b([0-3]?\d)/([01]?\d)/(\d{4})\b", value)
    if match:
        try:
            return date(int(match.group(3)), int(match.group(2)), int(match.group(1))).isoformat()
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            return None


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())[:700]


def _published_label_date(statement: str) -> str | None:
    match = re.search(r"(?i)\b(?:publicad[oa]s?|published|updated|atualizad[oa]s?)\s+(?:em|on)\s+([^;|]+)", statement)
    return _date_from_text(match.group(1)) if match else None


def _collection_page(url: str) -> bool:
    path = urlsplit(url).path.rstrip("/").lower()
    return path in {"/blog", "/news", "/noticias", "/noticia", "/imprensa", "/novidades", "/carreiras", "/jobs"}


def _query_url(query: str) -> str:
    params = {"q": query, "hl": "pt-BR", "gl": "BR", "ceid": "BR:pt-419"}
    return "https://news.google.com/rss/search?" + urlencode(params)


def _parse_rss(body: bytes) -> tuple[bool, list[dict[str, str | None]]]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return False, []
    items = root.findall(".//item")
    if not items and root.tag.lower().endswith("feed"):
        items = root.findall("{*}entry")
    if root.tag.lower() != "rss" and not root.tag.lower().endswith("feed"):
        return False, []
    records = []
    for item in items[:MAX_FACTS]:
        title = _clean_text(item.findtext("title") or item.findtext("{*}title") or "")
        link = (item.findtext("link") or item.findtext("{*}link") or "").strip()
        if not link:
            link_node = item.find("{*}link")
            link = (link_node.attrib.get("href", "") if link_node is not None else "").strip()
        published = _date_from_text(item.findtext("pubDate") or item.findtext("{*}published") or item.findtext("{*}updated") or "")
        source_node = item.find("source")
        if source_node is None:
            source_node = item.find("{*}source")
        publisher = _clean_text(source_node.text or "") if source_node is not None else ""
        if title and link:
            records.append({"title": title, "url": link, "published_at": published, "publisher": publisher})
    return True, records


def _strip_url_trailing(url: str) -> str:
    return url.rstrip(".,;:)]}")


def _same_site(url: str, root_host: str) -> bool:
    try:
        host = _host(url)
    except (UnicodeError, ValueError):
        return False
    return host == root_host or host.endswith("." + root_host)


def _company_tokens(name: str) -> set[str]:
    ignored = {"grupo", "indústria", "industria", "comércio", "comercio", "brasil", "ltda", "s/a", "sa"}
    return {token for token in re.findall(r"[a-z0-9]+", name.casefold()) if len(token) >= 4 and token not in ignored}


def _name_match(title: str, company_name: str) -> bool:
    tokens = _company_tokens(company_name)
    if not tokens:
        return False
    found = _company_tokens(title)
    return bool(tokens & found) and (len(tokens) == 1 or len(tokens & found) >= min(2, len(tokens)))


def _domain_matches(expected: str, actual: str) -> bool:
    return actual == expected or actual.endswith("." + expected)


def _section(text: str, heading: str) -> str:
    pattern = re.compile(rf"(?ims)^##\s+{re.escape(heading)}\s*$\n(.*?)(?=^##\s+|\Z)")
    match = pattern.search(text)
    return match.group(1) if match else ""


def _urls(text: str) -> list[str]:
    return [_strip_url_trailing(value) for value in re.findall(r"https://[^\s<>\]\[()]+", text)]


def research_request_from_brain(brain: str, account_id: str) -> ResearchRequest:
    """Resolve one existing account by its exact official site; reject ambiguity."""
    root = __import__("pathlib").Path(brain).resolve()
    target = account_id.strip().lower().rstrip(".")
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", target) or "." not in target:
        raise ResearchCollectionError("canonical_account_required")
    accounts = root / "contas"
    if not accounts.is_dir() or accounts.is_symlink():
        raise ResearchCollectionError("account_repository_unavailable")
    matches: list[tuple[str, str, str]] = []
    for path in sorted(accounts.glob("*/ficha.md")):
        if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(root):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if len(content.encode("utf-8")) > 100_000:
            continue
        site_match = re.search(r"(?im)^\s*-?\s*Site:\s*(https://\S+)", content)
        title_match = re.search(r"(?m)^#\s+(.+?)\s*$", content)
        if not site_match or not title_match:
            continue
        site = _strip_url_trailing(site_match.group(1))
        try:
            site_host = _host(_normalize_https_url(site))
        except ResearchCollectionError:
            continue
        if _domain_matches(target, site_host):
            matches.append((path.relative_to(root).as_posix(), content, site))
    if len(matches) != 1:
        raise ResearchCollectionError("account_identity_missing" if not matches else "account_identity_ambiguous")
    relative, content, website = matches[0]
    # Exclude operational-history sections from both source selection and model
    # context. They are not public evidence and may contain CRM/contact history.
    content = re.split(r"(?im)^##\s+Hist[oó]rico operacional\s*$", content, maxsplit=1)[0]
    name = re.search(r"(?m)^#\s+(.+?)\s*$", content).group(1).split("|", 1)[0].strip()
    sector_match = re.search(r"(?im)^\s*-\s*(?:Setor|Segmento|Indústria|Industria)(?:\s*/\s*porte)?\s*:\s*([^\n]+)", content)
    sector = sector_match.group(1).split(". ", 1)[0].strip() if sector_match else ""
    if not sector:
        industry = re.search(r"(?i)\bind[uú]stria\s+brasileira\s+de\s+([^,.;]+)", content)
        sector = industry.group(1).split(" e pela", 1)[0].strip() if industry else ""
    all_urls = _urls(content)
    site_host = _host(website)
    public: dict[str, list[str]] = {area: [] for area in SOURCE_AREAS}
    public["company_site"].append(website)
    decision_text = _section(content, "Decisores") or _section(content, "Decisor") or _section(content, "Decisora") or _section(content, "Decisor e solicitante")
    decision_maker_name = ""
    for line in decision_text.splitlines():
        if "linkedin.com/in/" not in line.casefold():
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells and cells[0].casefold() not in {"nome", "name", "---", ""}:
            decision_maker_name = _clean_text(cells[0])[:120]
            break
    sector_text = _section(content, "Momento do setor")
    for url in _urls(decision_text):
        if "linkedin.com/in/" in url.lower():
            public["decision_maker"].append(url)
    for url in _urls(sector_text):
        if url != website:
            public["sector"].append(url)
    for url in all_urls:
        host = _host(url)
        path_part = urlsplit(url).path.lower()
        if any(host == social or host.endswith("." + social) for social in _PUBLIC_SOCIAL_HOSTS):
            if "linkedin.com/in/" in url.lower():
                continue
            public["company_social"].append(url)
        elif any(marker in host for marker in _JOB_HOST_MARKERS) or _HIRING_TOPIC.search(path_part):
            public["hiring"].append(url)
        elif _same_site(url, site_host) and _NEWS_TOPIC.search(path_part):
            public["company_news"].append(url)
        elif url != website and url not in public["sector"] and url not in public["decision_maker"]:
            public["other_public_source"].append(url)
    public["company_social"] = list(dict.fromkeys(public["company_social"]))
    public["company_news"] = list(dict.fromkeys(public["company_news"]))
    public["hiring"] = list(dict.fromkeys(public["hiring"]))
    public["decision_maker"] = list(dict.fromkeys(public["decision_maker"]))
    public["sector"] = list(dict.fromkeys(public["sector"]))
    public["other_public_source"] = list(dict.fromkeys(public["other_public_source"]))
    context = {"name": name, "domain": target, "sector": sector or "Não verificado",
               "website_url": website, "account_source_file": relative}
    return ResearchRequest(target, name, website, sector, {key: tuple(value) for key, value in public.items()}, context,
                           decision_maker_name)


def select_model_evidence(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound model output while retaining source diversity and dated evidence."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for fact in facts:
        groups.setdefault((fact["source_area"], fact["source"]), []).append(fact)
    for rows in groups.values():
        rows.sort(key=lambda row: (not bool(row.get("published_at")), -len(row["statement"])))
    priority = {area: index for index, area in enumerate((
        "company_news", "company_site", "decision_maker", "hiring", "company_social", "sector"))}
    buckets = sorted(groups.values(), key=lambda rows: (
        not any(row.get("published_at") for row in rows), priority.get(rows[0]["source_area"], 10), rows[0]["source"]))
    selected = []
    for offset in range(max((len(rows) for rows in buckets), default=0)):
        for rows in buckets:
            if offset < len(rows):
                selected.append(rows[offset])
                if len(selected) == MAX_MODEL_FACTS:
                    return selected
    return selected


class PublicResearchProducer:
    """Plan and collect one bounded public research packet for one account."""
    def __init__(self, *, fetcher: Any | None = None, max_requests: int = MAX_REQUESTS,
                 timeout: float = REQUEST_TIMEOUT, max_pages_per_area: int = MAX_SEEDS_PER_AREA,
                 max_collection_seconds: float = MAX_COLLECTION_SECONDS,
                 clock: Callable[[], datetime] | None = None, sleeper: Callable[[float], None] | None = None,
                 min_host_interval: float = 0.15):
        self._owns_fetcher = fetcher is None
        self.fetcher = fetcher or BoundedHTTPSFetcher(timeout=timeout)
        self.timeout = timeout
        self.max_collection_seconds = max(1.0, min(float(max_collection_seconds), MAX_COLLECTION_SECONDS))
        self.max_requests = max(1, min(int(max_requests), MAX_REQUESTS))
        self.max_pages_per_area = max(1, min(int(max_pages_per_area), MAX_SEEDS_PER_AREA))
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper or time.sleep
        self.min_host_interval = max(0.0, min_host_interval)
        self.request_count = 0
        self.last_host_at: dict[str, float] = {}
        self.robots: dict[str, tuple[RobotFileParser | None, str, bool]] = {}
        self._fetch_cache: dict[str, tuple[FetchResponse | None, str]] = {}
        self._area_statuses: dict[str, list[str]] = {}
        self._area_details: dict[str, list[tuple[str, str]]] = {}
        self._area_checks: dict[str, list[date]] = {}
        self._actual_requests: list[str] = []
        self._fetch_attempts: dict[str, tuple[str, ...]] = {}
        self._deadline = 0.0
        self._monotonic = time.monotonic

    def collect_from_brain(self, brain: str, account_id: str) -> dict[str, Any]:
        return self.collect(research_request_from_brain(brain, account_id))

    def _fetch_raw(self, url: str) -> FetchResponse:
        if self.request_count >= self.max_requests:
            raise ResearchCollectionError("request_budget_exhausted")
        remaining_total = self._deadline - self._monotonic()
        if remaining_total <= 0:
            raise ResearchCollectionError("collection_deadline_exceeded")
        safe = _normalize_https_url(url)
        host = _host(safe)
        now = self._monotonic()
        remaining = self.min_host_interval - (now - self.last_host_at.get(host, -1e9))
        if remaining > 0:
            self.sleeper(remaining)
        remaining_total = self._deadline - self._monotonic()
        if remaining_total <= 0:
            raise ResearchCollectionError("collection_deadline_exceeded")
        self.last_host_at[host] = self._monotonic()
        self.request_count += 1
        self._actual_requests.append(safe)
        if self._owns_fetcher:
            return self.fetcher.fetch(safe, timeout=min(self.timeout, remaining_total))
        return self.fetcher.fetch(safe)

    def _robots_allowed(self, url: str) -> tuple[bool, str]:
        host = _host(url)
        policy = self.robots.get(host)
        if policy is None:
            robots_url = f"https://{host}/robots.txt"
            try:
                response = self._fetch_raw(robots_url)
            except ResearchCollectionError as exc:
                policy = (None, f"robots check unavailable: {exc}", False)
            else:
                if response.status == 404:
                    policy = (None, "robots.txt returned HTTP 404; no disallow rule returned", True)
                elif response.status != 200:
                    policy = (None, f"robots.txt returned HTTP {response.status}", False)
                else:
                    parser = RobotFileParser(robots_url)
                    parser.parse(response.body.decode("utf-8", errors="replace").splitlines())
                    policy = (parser, "robots.txt policy loaded", True)
            self.robots[host] = policy
        parser, detail, available = policy
        if not available:
            return False, detail
        if parser is None:
            return True, detail
        allowed = parser.can_fetch("ThunderOS-PublicResearch/1.0", url)
        return allowed, "robots.txt permits request" if allowed else "robots.txt disallows this path"

    def _fetch(self, url: str, *, respect_robots: bool = True) -> tuple[FetchResponse | None, str]:
        if url in self._fetch_cache:
            self._fetch_attempts.setdefault(url, ())
            return self._fetch_cache[url]
        current = _normalize_https_url(url)
        attempts: list[str] = []
        response = None
        detail = ""
        for redirect_count in range(MAX_REDIRECTS + 1):
            if respect_robots:
                allowed, robot_detail = self._robots_allowed(current)
                if not allowed:
                    detail = robot_detail
                    break
            request_mark = self.request_count
            try:
                response = self._fetch_raw(current)
            except ResearchCollectionError as exc:
                detail = str(exc)
                if self.request_count > request_mark:
                    attempts.append(current)
                break
            attempts.append(current)
            if response.status not in {301, 302, 303, 307, 308}:
                break
            location = response.headers.get("location", "").strip()
            if not location:
                detail = "redirect response did not include a location"
                break
            if redirect_count >= MAX_REDIRECTS:
                detail = "redirect limit reached"
                break
            try:
                current = _normalize_https_url(urljoin(current, location))
            except ResearchCollectionError as exc:
                detail = f"redirect destination rejected: {exc}"
                break
        if response is not None and response.cached and not attempts:
            # The collector consulted a cached public response; coverage keeps
            # its original observation date and says that no fresh GET occurred.
            attempts.append(_normalize_https_url(url))
        self._fetch_attempts[url] = tuple(attempts)
        if response is None:
            result = (None, detail or "public source request did not complete")
            self._fetch_cache[url] = result
            return result
        if response.status in {401, 403, 429}:
            detail = f"HTTP {response.status}: public access was not available"
        elif response.status >= 500:
            detail = f"HTTP {response.status}: source unavailable"
        elif response.status < 200 or response.status >= 300:
            detail = detail or f"HTTP {response.status}: source returned a non-success response"
        else:
            detail = "public GET succeeded"
        if response.cached:
            detail += f"; cached response collected {response.observed_at.isoformat()}"
        result = (response, detail)
        self._fetch_cache[url] = result
        return result

    @staticmethod
    def _html(response: FetchResponse) -> _HTMLMetadata | None:
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
            return None
        parser = _HTMLMetadata()
        try:
            parser.feed(response.body.decode("utf-8", errors="replace"))
        except (ValueError, RecursionError):
            return None
        return parser

    def _coverage_init(self, area: str, query: str, planned_urls: list[str]) -> dict[str, Any]:
        return {"area": area, "status": "not_provided" if not planned_urls else "planned",
                "query": query, "checked_at": None, "planned_urls": list(dict.fromkeys(planned_urls)),
                "attempted_urls": [], "discovered_urls": [], "source_urls": [],
                "details": "No public source request has been made." if planned_urls else "No seed or public endpoint was provided to this collector."}

    @staticmethod
    def _append_fact(facts: list[dict[str, Any]], *, area: str, source: str, statement: str,
                     published_at: str | None, observed_at: date) -> bool:
        statement = _clean_text(statement)
        if not statement or not source or any(fact["source"] == source and fact["statement"] == statement for fact in facts):
            return False
        facts.append({"source": source, "statement": statement, "source_area": area,
                      "published_at": published_at, "observed_at": observed_at.isoformat()})
        return True

    def _record_page(self, area: str, url: str, *, planned: dict[str, Any], facts: list[dict[str, Any]],
                     observed_at: date, ad_library: bool = False) -> str:
        def finish(status: str) -> str:
            self._area_statuses[area].append(status)
            self._area_details[area].append((status, planned["details"]))
            return status

        if url not in planned["planned_urls"]:
            planned["planned_urls"].append(url)
        if len(planned["attempted_urls"]) >= self.max_pages_per_area:
            return finish("planned")
        try:
            safe = _normalize_https_url(url)
        except ResearchCollectionError as exc:
            planned["details"] = str(exc)
            return finish("planned")
        response, detail = self._fetch(safe)
        attempts = self._fetch_attempts.get(safe, ())
        for attempted in attempts:
            if attempted and attempted not in planned["attempted_urls"]:
                planned["attempted_urls"].append(attempted)
        if response is None:
            planned["details"] = detail
            if attempts:
                self._area_checks[area].append(self.clock().date())
            if detail.startswith("robots") or detail in {"request_budget_exhausted", "collection_deadline_exceeded"}:
                return finish("planned")
            return finish("not_accessible" if detail in {
                "public_host_unresolved", "non_public_host", "public_https_url_required", "dns_timeout",
                "request_timeout", "request_failed"} else "attempted")
        self._area_checks[area].append(response.observed_at)
        if response.status in {401, 403, 429} or response.status >= 500:
            planned["details"] = detail
            return finish("not_accessible")
        if response.status < 200 or response.status >= 300:
            planned["details"] = detail
            return finish("attempted")
        parsed = self._html(response)
        if parsed is None:
            planned["details"] = "A public response was received, but no supported HTML record could be extracted."
            return finish("attempted")
        visible = " ".join(parsed.statements)
        if _GENERIC_AUTH.search(visible):
            planned["details"] = "The page returned a login or access prompt, not public account evidence."
            return finish("not_accessible")
        if ad_library:
            planned["details"] = "The public page responded, but the collector found no stable, parsable ad record; no ad result was inferred."
            return finish("attempted")
        added = False
        for statement in parsed.statements[:5]:
            if not _GENERIC_AUTH.search(statement):
                published_at = parsed.published_at or _published_label_date(statement)
                if published_at is None and not _collection_page(response.url):
                    published_at = parsed.page_date
                added |= self._append_fact(facts, area=area, source=response.url, statement=statement,
                                           published_at=published_at, observed_at=response.observed_at)
        if added:
            if response.url not in planned["source_urls"]:
                planned["source_urls"].append(response.url)
            planned["details"] = "Public page metadata captured; the page text is untrusted and must be interpreted with its source."
            if response.cached:
                planned["details"] += f" Cached observation date: {response.observed_at.isoformat()}."
            return finish("checked_with_evidence")
        planned["details"] = "A public response was received, but no dated title or description could be verified."
        return finish("attempted")

    def _record_rss(self, area: str, url: str, *, planned: dict[str, Any],
                    facts: list[dict[str, Any]], company_name: str | None, observed_at: date,
                    match_company: bool) -> str:
        def finish(status: str) -> str:
            self._area_statuses[area].append(status)
            self._area_details[area].append((status, planned["details"]))
            return status

        if url not in planned["planned_urls"]:
            planned["planned_urls"].append(url)
        response, detail = self._fetch(url)
        for attempted in self._fetch_attempts.get(url, ()):
            if attempted and attempted not in planned["attempted_urls"]:
                planned["attempted_urls"].append(attempted)
        if response is None:
            planned["details"] = detail
            if self._fetch_attempts.get(url):
                self._area_checks[area].append(self.clock().date())
            if detail.startswith("robots") or detail in {"request_budget_exhausted", "collection_deadline_exceeded"}:
                return finish("planned")
            return finish("not_accessible" if detail in {
                "public_host_unresolved", "non_public_host", "public_https_url_required", "dns_timeout",
                "request_timeout", "request_failed"} else "attempted")
        self._area_checks[area].append(response.observed_at)
        if response.status in {401, 403, 429} or response.status >= 500:
            planned["details"] = detail
            return finish("not_accessible")
        if response.status < 200 or response.status >= 300:
            planned["details"] = detail
            return finish("attempted")
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not any(kind in content_type for kind in ("xml", "rss", "atom", "text/plain")):
            planned["details"] = "Search response was not an RSS or Atom document; no result was inferred."
            return finish("attempted")
        valid, records = _parse_rss(response.body)
        if not valid:
            planned["details"] = "Search returned content that the RSS/Atom parser could not verify."
            return finish("attempted")
        kept = 0
        for record in records:
            if match_company and company_name and not _name_match(str(record["title"]), company_name):
                continue
            link = str(record["url"])
            try:
                safe_link = _normalize_https_url(link)
            except ResearchCollectionError:
                continue
            if safe_link not in planned["discovered_urls"]:
                planned["discovered_urls"].append(safe_link)
            # Search feed text is only a lead. Open the publisher URL and ground
            # facts in its own page metadata; never promote a search snippet.
            before = len(facts)
            self._record_page(area, safe_link, planned=planned, facts=facts,
                              observed_at=response.observed_at)
            if len(facts) > before:
                kept += 1
            if kept >= 2:
                break
        if kept:
            planned["details"] = f"Public RSS search completed; {kept} publisher page(s) were opened and verified."
            return finish("checked_with_evidence")
        if records:
            planned["details"] = "Public RSS search returned leads, but no publisher page produced verifiable metadata."
            return finish("attempted")
            planned["details"] = "Public RSS search completed with no matching source records."
            if response.cached:
                planned["details"] += f" This is cached coverage from {response.observed_at.isoformat()}, not a fresh source check."
        return finish("checked_no_result")

    def _finish_coverage(self, rows: dict[str, dict[str, Any]], facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for area, row in rows.items():
            for field in ("planned_urls", "attempted_urls", "discovered_urls", "source_urls"):
                row[field] = list(dict.fromkeys(row[field]))
            fact_urls = sorted({fact["source"] for fact in facts if fact["source_area"] == area})
            row["source_urls"] = fact_urls
            statuses = self._area_statuses.get(area, [])
            if fact_urls:
                row["status"] = "checked_with_evidence"
                partial_details = list(dict.fromkeys(
                    detail for status, detail in self._area_details.get(area, [])
                    if status != "checked_with_evidence" and detail))
                if partial_details:
                    row["details"] = "Verified public evidence was captured; other planned source checks were incomplete: " + "; ".join(partial_details[:3])
            elif not row["planned_urls"] and not row["attempted_urls"]:
                row["status"] = "not_provided"
                row["details"] = "No public URL or supported unauthenticated endpoint was available to this collector."
            elif statuses and all(status == "checked_no_result" for status in statuses):
                row["status"] = "checked_no_result"
            elif statuses and all(status == "not_accessible" for status in statuses):
                row["status"] = "not_accessible"
            elif row["attempted_urls"]:
                row["status"] = "attempted"
                if not row["details"]:
                    row["details"] = "A public source was attempted, but the collector could not establish a complete result."
            elif row["planned_urls"]:
                row["status"] = "planned"
            else:
                row["status"] = "not_provided"
            if row["status"] in {"checked_with_evidence", "checked_no_result", "not_accessible", "attempted"}:
                checks = self._area_checks.get(area, [])
                row["checked_at"] = max(checks).isoformat() if checks else None
        return [rows[area] for area in sorted(rows)]

    def collect(self, request: ResearchRequest) -> dict[str, Any]:
        if not isinstance(request, ResearchRequest) or not request.company_name.strip():
            raise ResearchCollectionError("company_identity_missing")
        self.request_count = 0
        self.last_host_at.clear()
        self.robots.clear()
        self._fetch_cache.clear()
        self._actual_requests.clear()
        self._area_statuses = {area: [] for area in SOURCE_AREAS}
        self._area_details = {area: [] for area in SOURCE_AREAS}
        self._area_checks = {area: [] for area in SOURCE_AREAS}
        self._fetch_attempts.clear()
        self._deadline = self._monotonic() + self.max_collection_seconds
        domain = request.account_id.lower().strip().rstrip(".")
        try:
            site = _normalize_https_url(request.website_url)
        except ResearchCollectionError as exc:
            raise ResearchCollectionError("account_website_invalid") from exc
        if not _domain_matches(domain, _host(site)):
            raise ResearchCollectionError("account_identity_mismatch")
        today = self.clock().date()
        rows: dict[str, dict[str, Any]] = {}
        facts: list[dict[str, Any]] = []
        queries = {
            "company_site": "Official company website and relevant public pages",
            "company_news": f'"{request.company_name}" (launch OR investment OR expansion OR factory OR acquisition OR event) when:6m',
            "company_social": "Public company social pages explicitly linked by the account record",
            "google_ads": "Google Ads Transparency Center; public advertiser search by official domain",
            "meta_ads": f"Meta Ad Library public search for {request.company_name} in Brazil",
            "linkedin_ads": f"LinkedIn Ad Library public search for {request.company_name}",
            "hiring": f'"{request.company_name}" (vagas OR vaga OR emprego OR carreiras OR Gupy OR SDR OR CRM) when:6m',
            "sector": f'"{request.sector}" Brazil (official industry data OR association) when:6m' if request.sector else "Sector was not present in the canonical account record.",
            "decision_maker": (f'"{request.decision_maker_name}" "{request.company_name}" (career OR appointment OR profile) when:5y'
                               if request.decision_maker_name else "Public decision-maker profile URLs explicitly listed in the account record"),
            "other_public_source": "Other public source URLs explicitly listed in the account record",
        }
        site_host = _host(site)
        google_ads = "https://adstransparency.google.com/?" + urlencode({"region": "BR", "domain": domain})
        meta_ads = "https://www.facebook.com/ads/library/?" + urlencode({"active_status": "all", "ad_type": "ALL", "country": "BR", "q": request.company_name})
        linkedin_ads = "https://www.linkedin.com/ad-library/search/"
        news_url = _query_url(queries["company_news"])
        hiring_url = _query_url(queries["hiring"])
        sector_url = _query_url(queries["sector"]) if request.sector else ""
        decision_url = _query_url(queries["decision_maker"]) if request.decision_maker_name else ""
        direct = {area: list(request.public_urls.get(area, ()))[:MAX_SEEDS_PER_AREA]
                  for area in SOURCE_AREAS}
        direct["company_site"] = [site, *direct["company_site"]]
        rows["company_site"] = self._coverage_init("company_site", queries["company_site"], direct["company_site"])
        rows["company_news"] = self._coverage_init("company_news", queries["company_news"], [news_url, *direct["company_news"]])
        rows["company_social"] = self._coverage_init("company_social", queries["company_social"], direct["company_social"])
        rows["google_ads"] = self._coverage_init("google_ads", queries["google_ads"], [google_ads])
        rows["meta_ads"] = self._coverage_init("meta_ads", queries["meta_ads"], [meta_ads])
        # No stable unauthenticated LinkedIn search endpoint/parameter has been
        # verified here. Keep it visible as unprovided instead of probing a guessed URL.
        rows["linkedin_ads"] = self._coverage_init("linkedin_ads", queries["linkedin_ads"], [])
        rows["hiring"] = self._coverage_init("hiring", queries["hiring"], [hiring_url, *direct["hiring"]])
        rows["sector"] = self._coverage_init("sector", queries["sector"], ([sector_url] if sector_url else []) + direct["sector"])
        rows["decision_maker"] = self._coverage_init("decision_maker", queries["decision_maker"],
            ([decision_url] if decision_url else []) + direct["decision_maker"])
        rows["other_public_source"] = self._coverage_init("other_public_source", queries["other_public_source"], direct["other_public_source"])

        # Company website: fetch the official seed, then only a few same-host
        # pages whose paths identify company, product, newsroom, or careers.
        site_candidates = direct["company_site"][:1]
        for url in site_candidates:
            self._record_page("company_site", url, planned=rows["company_site"], facts=facts,
                              observed_at=today)
        discovered: dict[str, list[str]] = {"company_site": [], "company_news": [], "hiring": [], "company_social": []}
        homepage = site_candidates[0] if site_candidates else site
        # Re-read only the already-fetched homepage from cache; no second
        # network request is made to discover same-site source pages.
        if homepage:
            response, _ = self._fetch(homepage)
            parsed = self._html(response) if response else None
            if parsed:
                for href, label in parsed.links:
                    candidate = _strip_url_trailing(urljoin(homepage, href))
                    try:
                        candidate = _normalize_https_url(candidate)
                        candidate_host = _host(candidate)
                    except (UnicodeError, ValueError):
                        continue
                    if any(candidate_host == social or candidate_host.endswith("." + social)
                           for social in _PUBLIC_SOCIAL_HOSTS) and "linkedin.com/in/" not in candidate.lower():
                        discovered["company_social"].append(candidate)
                        rows["company_social"]["discovered_urls"].append(candidate)
                        continue
                    if not _same_site(candidate, site_host):
                        continue
                    path = urlsplit(candidate).path
                    if not _PAGE_TOPIC.search(path + " " + label):
                        continue
                    area = "company_news" if _NEWS_TOPIC.search(path + " " + label) else "hiring" if _HIRING_TOPIC.search(path + " " + label) else "company_site"
                    discovered[area].append(candidate)
                    rows[area]["discovered_urls"].append(candidate)
        for area in ("company_site", "company_news", "hiring", "company_social"):
            combined = direct[area] + discovered[area]
            article_links = 0
            seen = set(rows[area]["planned_urls"])
            for url in combined:
                if url not in seen and len(rows[area]["attempted_urls"]) < self.max_pages_per_area:
                    rows[area]["planned_urls"].append(url)
                    seen.add(url)
                if url == homepage and area == "company_site":
                    continue
                if len(rows[area]["attempted_urls"]) >= self.max_pages_per_area:
                    break
                self._record_page(area, url, planned=rows[area], facts=facts,
                                  observed_at=today)
                # One bounded hop from a fetched newsroom index to its articles.
                if area == "company_news" and _collection_page(url) and article_links < 2:
                    cached = self._fetch_cache.get(_normalize_https_url(url))
                    page = self._html(cached[0]) if cached and cached[0] else None
                    for href, label in page.links if page else ():
                        try:
                            article = _normalize_https_url(_strip_url_trailing(urljoin(url, href)))
                        except (ValueError, UnicodeError):
                            continue
                        if (not _same_site(article, site_host) or article in combined
                                or _collection_page(article) or not _NEWS_TOPIC.search(urlsplit(article).path)
                                or "/category/" in article or "/tag/" in article):
                            continue
                        combined.append(article)
                        rows[area]["discovered_urls"].append(article)
                        article_links += 1
                        if article_links >= 2:
                            break

        # Public news, hiring and sector search use a bounded unauthenticated RSS
        # feed. Search attempts are recorded independently of returned articles.
        self._record_rss("company_news", news_url,
                         planned=rows["company_news"], facts=facts,
                         company_name=request.company_name, observed_at=today, match_company=True)
        self._record_rss("hiring", hiring_url,
                         planned=rows["hiring"], facts=facts,
                         company_name=request.company_name, observed_at=today, match_company=True)
        if sector_url:
            self._record_rss("sector", sector_url,
                             planned=rows["sector"], facts=facts,
                             company_name=None, observed_at=today, match_company=False)
        if decision_url:
            self._record_rss("decision_maker", decision_url, planned=rows["decision_maker"], facts=facts,
                             company_name=request.decision_maker_name or request.company_name,
                             observed_at=today, match_company=True)
        for area in ("decision_maker", "other_public_source"):
            for url in direct[area][:self.max_pages_per_area]:
                self._record_page(area, url, planned=rows[area], facts=facts, observed_at=today)
        for area, url in (("google_ads", google_ads), ("meta_ads", meta_ads)):
            self._record_page(area, url, planned=rows[area], facts=facts, observed_at=today, ad_library=True)

        for fact in facts:
            if fact["source_area"] not in SOURCE_AREAS:
                raise ResearchCollectionError("producer_source_area_invalid")
            if not fact["published_at"]:
                continue
            try:
                date.fromisoformat(fact["published_at"])
            except ValueError:
                fact["published_at"] = None
        collected = deepcopy(facts)
        collected_coverage = deepcopy(self._finish_coverage(rows, facts))
        facts = select_model_evidence(facts)
        coverage = self._finish_coverage(rows, facts)
        context = dict(request.account_context)
        context.setdefault("name", request.company_name)
        context.setdefault("domain", domain)
        context.setdefault("sector", request.sector or "Não verificado")
        context.setdefault("website_url", site)
        return {"account_id": domain, "account": context, "research_evidence": facts,
                "collected_evidence": collected, "collected_coverage": collected_coverage,
                "source_coverage": coverage, "angle_ids": [], "producer": {
                    "name": "thunder_os.public_research", "version": 2,
                    "collected_records": len(collected), "model_records": len(facts),
                    "collected_at": self.clock().isoformat(), "request_count": self.request_count,
                    "max_requests": self.max_requests, "public_get_only": True,
                    "credentials_used": False, "model_calls": 0,
                }}
