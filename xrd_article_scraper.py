#!/usr/bin/env python3
"""Download a small, reproducible set of open-access XRD-related papers.

The script discovers candidate journal articles with OpenAlex, resolves legal
open-access PDF locations with OpenAlex and optionally Unpaywall, validates and
deduplicates the downloaded PDFs, and writes a JSONL manifest that a downstream
PDF parser can consume.

Example:
    export OPENALEX_API_KEY='...'
    export UNPAYWALL_EMAIL='you@example.edu'

    python xrd_article_scraper.py \
        --output-dir data/xrd_samples \
        --count 10

The resulting PDFs are stored in:
    data/xrd_samples/pdfs/

Optional downstream parser integration:
    python xrd_article_scraper.py \
        --output-dir data/xrd_samples \
        --count 10 \
        --parser-command 'python parse_pdf.py --pdf {pdf} --metadata {metadata}'

`--parser-command` is split with shlex and executed without a shell. Supported
placeholders are: {pdf}, {metadata}, {output_dir}, and {paper_id}.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import quote, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

OPENALEX_WORKS_URL = "https://api.openalex.org/works"
UNPAYWALL_WORK_URL = "https://api.unpaywall.org/v2/{doi}"

DEFAULT_QUERIES = (
    '"x-ray diffraction" crystal structure',
    '"powder x-ray diffraction"',
    '"XRD pattern" materials',
    '"Rietveld refinement" diffraction',
    '"diffraction peaks" phase identification',
)

# Terms are intentionally domain-specific. The score is a ranking heuristic,
# not a scientific classification label.
TITLE_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("x-ray diffraction", 12),
    ("x ray diffraction", 12),
    ("powder x-ray diffraction", 14),
    ("powder diffraction", 10),
    ("xrd pattern", 12),
    ("xrd patterns", 12),
    ("rietveld", 11),
    ("diffraction pattern", 8),
    ("diffraction peaks", 7),
    ("crystal structure", 4),
    ("phase identification", 5),
)

BODY_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("x-ray diffraction", 5),
    ("x ray diffraction", 5),
    ("powder x-ray diffraction", 7),
    ("powder diffraction", 5),
    ("xrd pattern", 6),
    ("xrd patterns", 6),
    ("rietveld", 6),
    ("2 theta", 4),
    ("2θ", 4),
    ("cu k", 3),
    ("bragg", 3),
    ("diffraction peak", 3),
    ("phase purity", 3),
    ("indexed to", 2),
    ("crystal structure", 2),
)

SELECT_FIELDS = ",".join(
    (
        "id",
        "doi",
        "title",
        "display_name",
        "publication_year",
        "publication_date",
        "type",
        "language",
        "cited_by_count",
        "is_retracted",
        "is_paratext",
        "primary_location",
        "locations",
        "best_oa_location",
        "open_access",
        "authorships",
        "abstract_inverted_index",
        "keywords",
        "topics",
        "has_content",
        "content_url",
    )
)


class ScraperError(RuntimeError):
    """Base exception for expected scraper failures."""


class DownloadRejected(ScraperError):
    """Raised when a URL does not return a usable PDF."""


@dataclass(frozen=True)
class PdfCandidate:
    url: str
    provider: str
    license: str | None = None
    version: str | None = None
    host_type: str | None = None
    landing_page_url: str | None = None
    requires_openalex_key: bool = False


@dataclass
class DownloadResult:
    requested_url: str
    resolved_url: str
    provider: str
    output_path: str
    sha256: str
    size_bytes: int
    page_count: int | None
    content_type: str | None
    retrieved_at: str


@dataclass
class PaperRecord:
    paper_id: str
    openalex_id: str
    doi: str | None
    title: str
    authors: list[str]
    publication_year: int | None
    publication_date: str | None
    journal: str | None
    cited_by_count: int
    language: str | None
    relevance_score: int
    matched_query: str
    open_access: dict[str, Any]
    pdf_source: dict[str, Any]
    download: dict[str, Any]
    parser: dict[str, Any] | None = None


@dataclass
class Config:
    openalex_api_key: str
    unpaywall_email: str | None
    output_dir: Path
    target_count: int
    queries: Sequence[str]
    candidates_per_query: int
    min_relevance_score: int
    min_pdf_bytes: int
    max_pdf_bytes: int
    request_timeout: float
    request_interval: float
    allow_openalex_content: bool
    parser_command: str | None
    from_year: int | None
    to_year: int | None
    log_level: str


class RateLimiter:
    """Simple process-local minimum interval limiter."""

    def __init__(self, minimum_interval_seconds: float) -> None:
        self.minimum_interval = max(0.0, minimum_interval_seconds)
        self._last_request_at = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.minimum_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self._last_request_at = time.monotonic()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_http_session() -> requests.Session:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.0,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "XRDArticleScraper/1.0 "
                "(academic open-access corpus collection; requests-python)"
            )
        }
    )
    return session


def normalize_doi(raw_doi: str | None) -> str | None:
    if not raw_doi:
        return None
    doi = raw_doi.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    return doi.lower().strip() or None


def openalex_short_id(openalex_id: str) -> str:
    return openalex_id.rstrip("/").rsplit("/", 1)[-1]


def reconstruct_abstract(inverted_index: Mapping[str, Sequence[int]] | None) -> str:
    if not inverted_index:
        return ""
    positioned_words: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for position in positions:
            positioned_words.append((int(position), word))
    positioned_words.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positioned_words)


def normalize_text(text: str) -> str:
    text = text.lower().replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def weighted_term_score(text: str, weights: Sequence[tuple[str, int]]) -> int:
    normalized = normalize_text(text)
    score = 0
    for term, weight in weights:
        if term in normalized:
            score += weight
    # Standalone XRD gets credit, while avoiding matches inside larger words.
    if re.search(r"\bxrd\b", normalized):
        score += 8
    return score


def relevance_score(work: Mapping[str, Any]) -> int:
    title = str(work.get("title") or work.get("display_name") or "")
    abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
    keywords = " ".join(
        str(item.get("display_name") or "") for item in (work.get("keywords") or [])
    )
    topics = " ".join(
        str(item.get("display_name") or "") for item in (work.get("topics") or [])
    )
    score = weighted_term_score(title, TITLE_WEIGHTS)
    score += weighted_term_score(f"{abstract} {keywords} {topics}", BODY_WEIGHTS)
    return score


def extract_authors(work: Mapping[str, Any]) -> list[str]:
    authors: list[str] = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name") or authorship.get("raw_author_name")
        if name:
            authors.append(str(name))
    return authors


def extract_journal(work: Mapping[str, Any]) -> str | None:
    for location_name in ("primary_location", "best_oa_location"):
        location = work.get(location_name) or {}
        source = location.get("source") or {}
        display_name = source.get("display_name")
        if display_name:
            return str(display_name)
    return None


def work_identity(work: Mapping[str, Any]) -> str:
    doi = normalize_doi(work.get("doi"))
    if doi:
        return f"doi:{doi}"
    openalex_id = str(work.get("id") or "")
    return f"openalex:{openalex_short_id(openalex_id)}"


def safe_slug(text: str, max_length: int = 70) -> str:
    slug = normalize_text(text)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return (slug[:max_length].rstrip("-") or "paper")


def filename_for_work(work: Mapping[str, Any], ordinal: int) -> str:
    openalex_id = openalex_short_id(str(work.get("id") or "unknown"))
    title = str(work.get("title") or work.get("display_name") or "paper")
    return f"{ordinal:02d}_{openalex_id}_{safe_slug(title)}.pdf"


def validate_http_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_json(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_successful_manifest(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logging.warning("Skipping malformed manifest line %s", line_number)
                continue
            output_path = row.get("download", {}).get("output_path")
            if output_path and Path(output_path).is_file():
                rows.append(row)
    return rows


def get_json(
    session: requests.Session,
    limiter: RateLimiter,
    url: str,
    *,
    params: Mapping[str, Any],
    timeout: float,
) -> dict[str, Any]:
    limiter.wait()
    response = session.get(
        url,
        params=params,
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    if response.status_code >= 400:
        snippet = response.text[:500].replace("\n", " ")
        raise ScraperError(
            f"GET {response.url} returned HTTP {response.status_code}: {snippet}"
        )
    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise ScraperError(f"GET {response.url} did not return JSON") from exc
    if not isinstance(payload, dict):
        raise ScraperError(f"GET {response.url} returned an unexpected JSON shape")
    return payload


def build_filter(config: Config) -> str:
    filters = ["type:article", "open_access.is_oa:true"]
    if config.from_year is not None and config.to_year is not None:
        filters.append(f"publication_year:{config.from_year}-{config.to_year}")
    elif config.from_year is not None:
        filters.append(f"publication_year:>{config.from_year - 1}")
    elif config.to_year is not None:
        filters.append(f"publication_year:<{config.to_year + 1}")
    return ",".join(filters)


def discover_candidates(
    session: requests.Session,
    limiter: RateLimiter,
    config: Config,
) -> list[dict[str, Any]]:
    by_identity: dict[str, dict[str, Any]] = {}

    for query in config.queries:
        params = {
            "api_key": config.openalex_api_key,
            "search": query,
            "filter": build_filter(config),
            "sort": "-relevance_score",
            "per_page": min(100, config.candidates_per_query),
            "select": SELECT_FIELDS,
        }
        logging.info("Searching OpenAlex: %s", query)
        payload = get_json(
            session,
            limiter,
            OPENALEX_WORKS_URL,
            params=params,
            timeout=config.request_timeout,
        )

        for work in payload.get("results") or []:
            if not isinstance(work, dict):
                continue
            if work.get("is_retracted") or work.get("is_paratext"):
                continue
            if work.get("type") != "article":
                continue
            score = relevance_score(work)
            if score < config.min_relevance_score:
                continue

            identity = work_identity(work)
            existing = by_identity.get(identity)
            candidate = dict(work)
            candidate["_matched_query"] = query
            candidate["_relevance_score"] = score

            if existing is None or score > int(existing.get("_relevance_score", 0)):
                by_identity[identity] = candidate

    candidates = list(by_identity.values())
    candidates.sort(
        key=lambda work: (
            int(work.get("_relevance_score", 0)),
            int(work.get("cited_by_count") or 0),
        ),
        reverse=True,
    )
    logging.info("Found %d unique XRD candidates", len(candidates))
    return candidates


def location_to_candidate(location: Mapping[str, Any], provider: str) -> PdfCandidate | None:
    url = location.get("pdf_url") or location.get("url_for_pdf")
    if not url:
        generic_url = location.get("url")
        if generic_url and urlparse(str(generic_url)).path.lower().endswith(".pdf"):
            url = generic_url
    if not url or not validate_http_url(str(url)):
        return None
    return PdfCandidate(
        url=str(url),
        provider=provider,
        license=location.get("license"),
        version=(
            location.get("version")
            or ("publishedVersion" if location.get("is_published") else None)
            or ("acceptedVersion" if location.get("is_accepted") else None)
        ),
        host_type=location.get("host_type"),
        landing_page_url=(
            location.get("landing_page_url") or location.get("url_for_landing_page")
        ),
    )


def deduplicate_pdf_candidates(candidates: Iterable[PdfCandidate]) -> list[PdfCandidate]:
    seen: set[str] = set()
    output: list[PdfCandidate] = []
    for candidate in candidates:
        normalized_url = candidate.url.strip()
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        output.append(candidate)
    return output


def get_unpaywall_record(
    session: requests.Session,
    limiter: RateLimiter,
    doi: str,
    email: str,
    timeout: float,
) -> dict[str, Any] | None:
    limiter.wait()
    url = UNPAYWALL_WORK_URL.format(doi=quote(doi, safe="/"))
    response = session.get(
        url,
        params={"email": email},
        timeout=timeout,
        headers={"Accept": "application/json"},
    )
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        logging.warning(
            "Unpaywall lookup failed for %s with HTTP %s", doi, response.status_code
        )
        return None
    try:
        payload = response.json()
    except requests.JSONDecodeError:
        logging.warning("Unpaywall returned non-JSON data for %s", doi)
        return None
    return payload if isinstance(payload, dict) else None


def resolve_pdf_candidates(
    work: Mapping[str, Any],
    session: requests.Session,
    limiter: RateLimiter,
    config: Config,
) -> list[PdfCandidate]:
    candidates: list[PdfCandidate] = []

    for location_name in ("best_oa_location", "primary_location"):
        location = work.get(location_name)
        if isinstance(location, dict):
            candidate = location_to_candidate(location, f"openalex:{location_name}")
            if candidate:
                candidates.append(candidate)

    for location in work.get("locations") or []:
        if isinstance(location, dict) and location.get("is_oa") is not False:
            candidate = location_to_candidate(location, "openalex:location")
            if candidate:
                candidates.append(candidate)

    doi = normalize_doi(work.get("doi"))
    if doi and config.unpaywall_email:
        record = get_unpaywall_record(
            session,
            limiter,
            doi,
            config.unpaywall_email,
            config.request_timeout,
        )
        if record:
            best = record.get("best_oa_location")
            if isinstance(best, dict):
                candidate = location_to_candidate(best, "unpaywall:best_oa_location")
                if candidate:
                    candidates.append(candidate)
            for location in record.get("oa_locations") or []:
                if isinstance(location, dict):
                    candidate = location_to_candidate(location, "unpaywall:oa_location")
                    if candidate:
                        candidates.append(candidate)

    has_cached_pdf = bool((work.get("has_content") or {}).get("pdf"))
    if config.allow_openalex_content and has_cached_pdf:
        work_id = openalex_short_id(str(work.get("id") or ""))
        if work_id:
            candidates.append(
                PdfCandidate(
                    url=f"https://content.openalex.org/works/{work_id}.pdf",
                    provider="openalex:cached_content",
                    requires_openalex_key=True,
                )
            )

    return deduplicate_pdf_candidates(candidates)


def inspect_pdf(path: Path) -> int | None:
    """Open the PDF with PyMuPDF when installed; return page count.

    The magic-byte and size checks are always performed. PyMuPDF validation is
    optional so the downloader can run before the parsing environment is built.
    """
    try:
        import fitz  # type: ignore
    except ImportError:
        return None

    try:
        with fitz.open(path) as document:
            if document.page_count < 1:
                raise DownloadRejected("PDF contains no pages")
            return int(document.page_count)
    except Exception as exc:
        raise DownloadRejected(f"PyMuPDF could not open PDF: {exc}") from exc


def download_pdf(
    session: requests.Session,
    limiter: RateLimiter,
    candidate: PdfCandidate,
    destination: Path,
    config: Config,
) -> DownloadResult:
    params: dict[str, str] | None = None
    if candidate.requires_openalex_key:
        params = {"api_key": config.openalex_api_key}

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_file: Path | None = None

    try:
        limiter.wait()
        with session.get(
            candidate.url,
            params=params,
            timeout=(10.0, config.request_timeout),
            stream=True,
            allow_redirects=True,
            headers={"Accept": "application/pdf, application/octet-stream;q=0.9, */*;q=0.1"},
        ) as response:
            if response.status_code >= 400:
                raise DownloadRejected(
                    f"HTTP {response.status_code} from {response.url}"
                )

            content_length = response.headers.get("Content-Length")
            if content_length and content_length.isdigit():
                declared_size = int(content_length)
                if declared_size > config.max_pdf_bytes:
                    raise DownloadRejected(
                        f"declared file size {declared_size} exceeds limit"
                    )

            hasher = hashlib.sha256()
            total_bytes = 0
            first_bytes = bytearray()

            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=destination.name + ".",
                suffix=".part",
                dir=destination.parent,
                delete=False,
            ) as temporary_handle:
                temporary_file = Path(temporary_handle.name)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > config.max_pdf_bytes:
                        raise DownloadRejected(
                            f"download exceeded {config.max_pdf_bytes} bytes"
                        )
                    if len(first_bytes) < 1024:
                        first_bytes.extend(chunk[: 1024 - len(first_bytes)])
                    hasher.update(chunk)
                    temporary_handle.write(chunk)

            if total_bytes < config.min_pdf_bytes:
                raise DownloadRejected(
                    f"file is implausibly small ({total_bytes} bytes)"
                )
            if b"%PDF-" not in first_bytes:
                content_type = response.headers.get("Content-Type")
                raise DownloadRejected(
                    f"response is not a PDF (Content-Type={content_type!r})"
                )

            page_count = inspect_pdf(temporary_file)
            os.replace(temporary_file, destination)
            temporary_file = None

            return DownloadResult(
                requested_url=candidate.url,
                resolved_url=str(response.url),
                provider=candidate.provider,
                output_path=str(destination.resolve()),
                sha256=hasher.hexdigest(),
                size_bytes=total_bytes,
                page_count=page_count,
                content_type=response.headers.get("Content-Type"),
                retrieved_at=utc_now(),
            )
    except requests.RequestException as exc:
        raise DownloadRejected(f"network error: {exc}") from exc
    finally:
        if temporary_file and temporary_file.exists():
            temporary_file.unlink(missing_ok=True)


def substitute_parser_command(
    command: str,
    *,
    pdf: Path,
    metadata: Path,
    output_dir: Path,
    paper_id: str,
) -> list[str]:
    replacements = {
        "{pdf}": str(pdf.resolve()),
        "{metadata}": str(metadata.resolve()),
        "{output_dir}": str(output_dir.resolve()),
        "{paper_id}": paper_id,
    }
    arguments = shlex.split(command)
    output: list[str] = []
    for argument in arguments:
        for placeholder, value in replacements.items():
            argument = argument.replace(placeholder, value)
        output.append(argument)
    if not output:
        raise ScraperError("parser command is empty")
    return output


def run_parser(
    command_template: str,
    *,
    pdf_path: Path,
    metadata_path: Path,
    output_dir: Path,
    paper_id: str,
    log_dir: Path,
) -> dict[str, Any]:
    command = substitute_parser_command(
        command_template,
        pdf=pdf_path,
        metadata=metadata_path,
        output_dir=output_dir,
        paper_id=paper_id,
    )
    log_dir.mkdir(parents=True, exist_ok=True)
    log_stem = pdf_path.stem
    stdout_path = log_dir / f"{log_stem}.stdout.log"
    stderr_path = log_dir / f"{log_stem}.stderr.log"

    started_at = utc_now()
    try:
        with stdout_path.open("wb") as stdout_handle, stderr_path.open("wb") as stderr_handle:
            completed = subprocess.run(
                command,
                stdout=stdout_handle,
                stderr=stderr_handle,
                check=False,
            )
        return_code: int | None = completed.returncode
        error: str | None = None
    except OSError as exc:
        return_code = None
        error = str(exc)
        stderr_path.write_text(error + "\n", encoding="utf-8")

    status = "success" if return_code == 0 else "failed"
    return {
        "command": command,
        "return_code": return_code,
        "started_at": started_at,
        "finished_at": utc_now(),
        "stdout_log": str(stdout_path.resolve()),
        "stderr_log": str(stderr_path.resolve()),
        "status": status,
        "error": error,
    }


def create_paper_record(
    work: Mapping[str, Any],
    candidate: PdfCandidate,
    result: DownloadResult,
) -> PaperRecord:
    openalex_id = openalex_short_id(str(work.get("id") or ""))
    return PaperRecord(
        paper_id=work_identity(work),
        openalex_id=openalex_id,
        doi=normalize_doi(work.get("doi")),
        title=str(work.get("title") or work.get("display_name") or ""),
        authors=extract_authors(work),
        publication_year=work.get("publication_year"),
        publication_date=work.get("publication_date"),
        journal=extract_journal(work),
        cited_by_count=int(work.get("cited_by_count") or 0),
        language=work.get("language"),
        relevance_score=int(work.get("_relevance_score") or 0),
        matched_query=str(work.get("_matched_query") or ""),
        open_access=dict(work.get("open_access") or {}),
        pdf_source={
            "provider": candidate.provider,
            "requested_url": candidate.url,
            "landing_page_url": candidate.landing_page_url,
            "license": candidate.license,
            "version": candidate.version,
            "host_type": candidate.host_type,
        },
        download=asdict(result),
    )


def acquire(config: Config) -> int:
    output_dir = config.output_dir.resolve()
    pdf_dir = output_dir / "pdfs"
    metadata_dir = output_dir / "metadata"
    parser_log_dir = output_dir / "parser_logs"
    manifest_path = output_dir / "manifest.jsonl"
    attempts_path = output_dir / "attempts.jsonl"
    summary_path = output_dir / "summary.json"

    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    existing = read_successful_manifest(manifest_path)
    successful_identities = {str(row.get("paper_id")) for row in existing}
    known_hashes = {
        str(row.get("download", {}).get("sha256"))
        for row in existing
        if row.get("download", {}).get("sha256")
    }
    success_count = len(existing)

    if success_count >= config.target_count:
        logging.info(
            "Manifest already contains %d valid PDFs; target is %d",
            success_count,
            config.target_count,
        )
        return 0

    session = create_http_session()
    metadata_limiter = RateLimiter(config.request_interval)
    download_limiter = RateLimiter(config.request_interval)

    candidates = discover_candidates(session, metadata_limiter, config)
    attempted_works = 0
    attempted_urls = 0

    for work in candidates:
        if success_count >= config.target_count:
            break

        identity = work_identity(work)
        if identity in successful_identities:
            continue

        attempted_works += 1
        pdf_candidates = resolve_pdf_candidates(
            work, session, metadata_limiter, config
        )
        if not pdf_candidates:
            append_jsonl(
                attempts_path,
                {
                    "timestamp": utc_now(),
                    "paper_id": identity,
                    "openalex_id": openalex_short_id(str(work.get("id") or "")),
                    "title": work.get("title"),
                    "status": "no_pdf_location",
                },
            )
            continue

        downloaded = False
        errors: list[dict[str, str]] = []
        destination = pdf_dir / filename_for_work(work, success_count + 1)

        for pdf_candidate in pdf_candidates:
            attempted_urls += 1
            logging.info(
                "Trying %s for %s",
                pdf_candidate.provider,
                str(work.get("title") or "")[:100],
            )
            try:
                result = download_pdf(
                    session,
                    download_limiter,
                    pdf_candidate,
                    destination,
                    config,
                )
            except DownloadRejected as exc:
                error = {
                    "provider": pdf_candidate.provider,
                    "url": pdf_candidate.url,
                    "error": str(exc),
                }
                errors.append(error)
                logging.warning("Rejected PDF candidate: %s", exc)
                continue

            if result.sha256 in known_hashes:
                Path(result.output_path).unlink(missing_ok=True)
                errors.append(
                    {
                        "provider": pdf_candidate.provider,
                        "url": pdf_candidate.url,
                        "error": "duplicate PDF content",
                    }
                )
                continue

            record = create_paper_record(work, pdf_candidate, result)
            metadata_path = metadata_dir / f"{destination.stem}.json"
            write_json(metadata_path, asdict(record))

            if config.parser_command:
                parser_result = run_parser(
                    config.parser_command,
                    pdf_path=destination,
                    metadata_path=metadata_path,
                    output_dir=output_dir,
                    paper_id=record.paper_id,
                    log_dir=parser_log_dir,
                )
                record.parser = parser_result
                write_json(metadata_path, asdict(record))

            append_jsonl(manifest_path, asdict(record))
            append_jsonl(
                attempts_path,
                {
                    "timestamp": utc_now(),
                    "paper_id": identity,
                    "status": "downloaded",
                    "provider": pdf_candidate.provider,
                    "url": pdf_candidate.url,
                    "output_path": result.output_path,
                },
            )

            successful_identities.add(identity)
            known_hashes.add(result.sha256)
            success_count += 1
            downloaded = True
            logging.info(
                "Downloaded %d/%d: %s",
                success_count,
                config.target_count,
                record.title,
            )
            break

        if not downloaded:
            append_jsonl(
                attempts_path,
                {
                    "timestamp": utc_now(),
                    "paper_id": identity,
                    "openalex_id": openalex_short_id(str(work.get("id") or "")),
                    "title": work.get("title"),
                    "status": "all_pdf_locations_failed",
                    "errors": errors,
                },
            )

    summary = {
        "finished_at": utc_now(),
        "target_count": config.target_count,
        "downloaded_count": success_count,
        "new_candidates_considered": attempted_works,
        "pdf_urls_attempted": attempted_urls,
        "output_dir": str(output_dir),
        "pdf_dir": str(pdf_dir),
        "manifest": str(manifest_path),
        "complete": success_count >= config.target_count,
        "allow_openalex_content": config.allow_openalex_content,
    }
    write_json(summary_path, summary)

    print(json.dumps(summary, indent=2))
    if success_count < config.target_count:
        logging.error(
            "Only obtained %d of %d requested PDFs. See %s for failures.",
            success_count,
            config.target_count,
            attempts_path,
        )
        return 2
    return 0


def parse_args(argv: Sequence[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(
        description=(
            "Discover and download validated open-access XRD-related journal PDFs "
            "using OpenAlex, with optional Unpaywall fallback."
        )
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/xrd_samples"))
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument(
        "--query",
        action="append",
        dest="queries",
        help="OpenAlex search query; repeat for multiple queries",
    )
    parser.add_argument("--candidates-per-query", type=int, default=40)
    parser.add_argument("--min-relevance-score", type=int, default=5)
    parser.add_argument("--from-year", type=int)
    parser.add_argument("--to-year", type=int)
    parser.add_argument("--min-pdf-kb", type=int, default=50)
    parser.add_argument("--max-pdf-mb", type=int, default=100)
    parser.add_argument("--request-timeout", type=float, default=60.0)
    parser.add_argument("--request-interval", type=float, default=0.25)
    parser.add_argument(
        "--allow-openalex-content",
        action="store_true",
        help=(
            "Use OpenAlex cached PDFs as a final fallback. This endpoint is billed "
            "by OpenAlex per downloaded file."
        ),
    )
    parser.add_argument(
        "--parser-command",
        help=(
            "Optional per-PDF parser command. Placeholders: {pdf}, {metadata}, "
            "{output_dir}, {paper_id}. The command is not run through a shell."
        ),
    )
    parser.add_argument(
        "--openalex-api-key",
        default=os.getenv("OPENALEX_API_KEY"),
        help="Defaults to OPENALEX_API_KEY",
    )
    parser.add_argument(
        "--unpaywall-email",
        default=os.getenv("UNPAYWALL_EMAIL"),
        help="Defaults to UNPAYWALL_EMAIL; optional but recommended",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    args = parser.parse_args(argv)

    if not args.openalex_api_key:
        parser.error(
            "OpenAlex requires an API key. Set OPENALEX_API_KEY or pass "
            "--openalex-api-key."
        )
    if args.count < 1:
        parser.error("--count must be at least 1")
    if not 1 <= args.candidates_per_query <= 100:
        parser.error("--candidates-per-query must be between 1 and 100")
    if args.min_pdf_kb < 1 or args.max_pdf_mb < 1:
        parser.error("PDF size limits must be positive")
    if args.from_year and args.to_year and args.from_year > args.to_year:
        parser.error("--from-year cannot be greater than --to-year")

    return Config(
        openalex_api_key=args.openalex_api_key,
        unpaywall_email=args.unpaywall_email,
        output_dir=args.output_dir,
        target_count=args.count,
        queries=tuple(args.queries or DEFAULT_QUERIES),
        candidates_per_query=args.candidates_per_query,
        min_relevance_score=args.min_relevance_score,
        min_pdf_bytes=args.min_pdf_kb * 1024,
        max_pdf_bytes=args.max_pdf_mb * 1024 * 1024,
        request_timeout=args.request_timeout,
        request_interval=args.request_interval,
        allow_openalex_content=args.allow_openalex_content,
        parser_command=args.parser_command,
        from_year=args.from_year,
        to_year=args.to_year,
        log_level=args.log_level,
    )


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return acquire(config)
    except KeyboardInterrupt:
        logging.error("Interrupted")
        return 130
    except ScraperError as exc:
        logging.error("Scraper failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
