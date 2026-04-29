#!/usr/bin/env python3
"""SEO audit agent with Google Search Console, URL Inspection, and PageSpeed Insights."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

USER_AGENT = "SEOAuditAgent/3.0 (+https://example.com)"
TIMEOUT_SECONDS = 20
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]


@dataclass
class PageAudit:
    url: str
    final_url: str
    status_code: int | None
    title: str
    meta_description: str
    h1: str
    h2s: str
    canonical: str
    robots_meta: str
    word_count: int
    internal_links_count: int
    internal_links: str
    external_links_count: int
    external_links: str
    structured_data: str
    images_missing_alt_count: int
    images_missing_alt: str
    indexability: str
    error: str
    gsc_clicks: float
    gsc_impressions: float
    gsc_ctr: float
    gsc_position: float
    gsc_top_queries: str
    indexed_status: str
    coverage_state: str
    google_selected_canonical: str
    user_declared_canonical: str
    robots_state: str
    last_crawl_time: str
    inspection_note: str
    performance_score: float
    lcp: str
    inp: str
    cls: str
    mobile_performance_issues: str
    desktop_performance_issues: str
    opportunities_diagnostics: str
    psi_note: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an SEO audit from sitemap.xml")
    parser.add_argument("--sitemap-url", default="", help="Absolute URL to sitemap.xml or sitemap index")
    parser.add_argument("--sitemap", default="", help="Alias for --sitemap-url")
    parser.add_argument("--output-csv", default="", help="Optional explicit CSV output path")
    parser.add_argument("--output-report", default="", help="Optional explicit Markdown report path")
    parser.add_argument(
        "--output",
        default="",
        help="Output folder (example: reports). If set, files are auto-created inside it.",
    )
    parser.add_argument("--credentials-file", default="gsc_credentials.json")
    parser.add_argument("--oauth-client-file", default="", help="OAuth client JSON for interactive Google login")
    parser.add_argument(
        "--oauth-manual",
        action="store_true",
        help="Use manual OAuth flow by pasting redirected localhost URL (Codespaces-friendly).",
    )
    parser.add_argument("--site-url", default="", help="Search Console property URL")
    parser.add_argument("--start-date", default=(date.today() - timedelta(days=28)).isoformat())
    parser.add_argument("--end-date", default=(date.today() - timedelta(days=1)).isoformat())
    parser.add_argument("--inspection-limit", type=int, default=100)
    parser.add_argument(
        "--pagespeed-api-key",
        default="",
        help="Google PageSpeed Insights API key (optional but required for PSI data)",
    )
    parser.add_argument("--openai-api-key", default="", help="OpenAI API key for AI analysis section")
    parser.add_argument("--openai-model", default="gpt-4.1", help="OpenAI model for AI analysis")
    return parser.parse_args()


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_url_for_match(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def extract_domain(url: str) -> str:
    return urlparse(url).netloc.lower()


def request_text(url: str, session: requests.Session) -> str:
    response = session.get(url, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


def parse_sitemap_xml(xml_text: str) -> tuple[list[str], list[str]]:
    root = ET.fromstring(xml_text)
    tag = root.tag.split("}")[-1]
    if tag == "urlset":
        return [loc.text.strip() for loc in root.findall(".//{*}url/{*}loc") if loc.text], []
    if tag == "sitemapindex":
        return [], [loc.text.strip() for loc in root.findall(".//{*}sitemap/{*}loc") if loc.text]
    raise ValueError("Unsupported sitemap format. Expected <urlset> or <sitemapindex>.")


def discover_urls_from_sitemap(sitemap_url: str, session: requests.Session) -> list[str]:
    discovered_urls: set[str] = set()
    queue = [sitemap_url]
    visited: set[str] = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        urls, sitemaps = parse_sitemap_xml(request_text(current, session))
        discovered_urls.update(urls)
        queue.extend([s for s in sitemaps if s not in visited])
    return sorted(discovered_urls)


def extract_internal_links(soup: BeautifulSoup, final_url: str) -> list[str]:
    domain = extract_domain(final_url)
    internal: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(final_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in {"http", "https"} and extract_domain(absolute) == domain:
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            if parsed.query:
                normalized += f"?{parsed.query}"
            internal.add(normalized)
    return sorted(internal)


def extract_external_links(soup: BeautifulSoup, final_url: str) -> list[str]:
    domain = extract_domain(final_url)
    external: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = urljoin(final_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in {"http", "https"} and extract_domain(absolute) != domain:
            normalized = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
            if parsed.query:
                normalized += f"?{parsed.query}"
            external.add(normalized)
    return sorted(external)


def extract_structured_data(soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    type_pattern = re.compile(r'"@type"\s*:\s*"([^"]+)"', re.I)
    for script in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        content = (script.string or script.get_text() or "").strip()
        if not content:
            continue
        for token in type_pattern.findall(content):
            found.append(token)
        if not type_pattern.findall(content):
            found.append("JSON-LD present (type_unparsed)")
    return found[:10]


def extract_images_missing_alt(soup: BeautifulSoup, final_url: str) -> list[str]:
    missing: list[str] = []
    for image in soup.find_all("img"):
        alt = image.get("alt")
        if alt is None or not str(alt).strip():
            src = image.get("src", "").strip()
            missing.append(urljoin(final_url, src) if src else "(missing-src)")
    return missing[:30]


def derive_indexability(status_code: int | None, robots_meta: str) -> str:
    if status_code != 200:
        return "not_indexable_status"
    if "noindex" in robots_meta.lower():
        return "not_indexable_noindex"
    return "indexable"


def extract_word_count(soup: BeautifulSoup) -> int:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    words = re.findall(r"\b\w+\b", soup.get_text(" ", strip=True))
    return len(words)


def audit_page(url: str, session: requests.Session) -> PageAudit:
    base = dict(
        url=url,
        final_url="",
        status_code=None,
        title="",
        meta_description="",
        h1="",
        h2s="",
        canonical="",
        robots_meta="",
        word_count=0,
        internal_links_count=0,
        internal_links="",
        external_links_count=0,
        external_links="",
        structured_data="",
        images_missing_alt_count=0,
        images_missing_alt="",
        indexability="unconfirmed",
        error="",
        gsc_clicks=0.0,
        gsc_impressions=0.0,
        gsc_ctr=0.0,
        gsc_position=0.0,
        gsc_top_queries="",
        indexed_status="unavailable",
        coverage_state="unavailable",
        google_selected_canonical="",
        user_declared_canonical="",
        robots_state="unavailable",
        last_crawl_time="",
        inspection_note="not_requested",
        performance_score=0.0,
        lcp="unavailable",
        inp="unavailable",
        cls="unavailable",
        mobile_performance_issues="",
        desktop_performance_issues="",
        opportunities_diagnostics="",
        psi_note="not_requested",
    )
    try:
        response = session.get(url, timeout=TIMEOUT_SECONDS, allow_redirects=True)
        base["final_url"] = response.url
        base["status_code"] = response.status_code
        ctype = response.headers.get("Content-Type", "")
        if "text/html" not in ctype:
            base["error"] = f"Skipped non-HTML content type: {ctype}"
            return PageAudit(**base)

        soup = BeautifulSoup(response.text, "html.parser")
        title_tag = soup.find("title")
        meta_desc_tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
        h1_tag = soup.find("h1")
        h2_tags = soup.find_all("h2")
        canonical_tag = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
        robots_tag = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
        internal_links = extract_internal_links(soup, response.url)
        external_links = extract_external_links(soup, response.url)
        structured_data_items = extract_structured_data(soup)
        images_missing_alt = extract_images_missing_alt(soup, response.url)
        robots_meta = robots_tag.get("content", "").strip() if robots_tag else ""

        base.update(
            title=normalize_whitespace(title_tag.get_text()) if title_tag else "",
            meta_description=normalize_whitespace(meta_desc_tag.get("content", "")) if meta_desc_tag else "",
            h1=normalize_whitespace(h1_tag.get_text()) if h1_tag else "",
            h2s=" | ".join(normalize_whitespace(t.get_text()) for t in h2_tags if t.get_text(strip=True)),
            canonical=canonical_tag.get("href", "").strip() if canonical_tag else "",
            robots_meta=robots_meta,
            word_count=extract_word_count(soup),
            internal_links_count=len(internal_links),
            internal_links=" | ".join(internal_links),
            external_links_count=len(external_links),
            external_links=" | ".join(external_links),
            structured_data=" | ".join(structured_data_items),
            images_missing_alt_count=len(images_missing_alt),
            images_missing_alt=" | ".join(images_missing_alt),
            indexability=derive_indexability(response.status_code, robots_meta),
        )
        return PageAudit(**base)
    except Exception as exc:  # noqa: BLE001
        base["error"] = str(exc)
        return PageAudit(**base)


def run_manual_oauth_flow(oauth_client_file: str) -> Credentials:
    flow = InstalledAppFlow.from_client_secrets_file(oauth_client_file, SCOPES)
    flow.redirect_uri = "http://localhost:8080/"
    auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
    print("\nManual OAuth mode enabled (--oauth-manual).")
    print("Open this URL in your browser to authorize access:")
    print(auth_url)
    print("\nAfter Google redirects, copy the full localhost URL from your browser address bar.")
    redirected_url = input("Paste the full localhost URL here: ").strip()
    parsed = urlparse(redirected_url)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    if not code:
        raise ValueError("Authorization code not found in pasted URL.")
    flow.fetch_token(code=code)
    return flow.credentials


def get_search_console_service(
    credentials_file: str = "",
    oauth_client_file: str = "",
    token_file: str = "token.json",
    oauth_manual: bool = False,
):
    if oauth_client_file:
        creds = None
        token_path = Path(token_file)
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if oauth_manual:
                    creds = run_manual_oauth_flow(oauth_client_file)
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(oauth_client_file, SCOPES)
                    creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        return build("searchconsole", "v1", credentials=creds, cache_discovery=False)

    creds = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def fetch_gsc_page_metrics(service, site_url: str, start_date: str, end_date: str) -> tuple[dict[str, dict[str, float]], dict[str, list[str]]]:
    page_metrics: dict[str, dict[str, float]] = {}
    page_queries: dict[str, list[tuple[str, float]]] = defaultdict(list)

    page_rows = service.searchanalytics().query(
        siteUrl=site_url,
        body={"startDate": start_date, "endDate": end_date, "dimensions": ["page"], "rowLimit": 25000},
    ).execute().get("rows", [])
    for row in page_rows:
        page = row.get("keys", [""])[0]
        page_metrics[normalize_url_for_match(page)] = {
            "clicks": float(row.get("clicks", 0)),
            "impressions": float(row.get("impressions", 0)),
            "ctr": float(row.get("ctr", 0)),
            "position": float(row.get("position", 0)),
        }

    pq_rows = service.searchanalytics().query(
        siteUrl=site_url,
        body={"startDate": start_date, "endDate": end_date, "dimensions": ["page", "query"], "rowLimit": 25000},
    ).execute().get("rows", [])
    for row in pq_rows:
        keys = row.get("keys", ["", ""])
        if len(keys) < 2:
            continue
        page_queries[normalize_url_for_match(keys[0])].append((keys[1], float(row.get("clicks", 0))))

    top_queries = {page: [q for q, _ in sorted(items, key=lambda x: x[1], reverse=True)[:5]] for page, items in page_queries.items()}
    return page_metrics, top_queries


def inspection_priority(audit: PageAudit) -> float:
    score = (audit.gsc_clicks * 10) + audit.gsc_impressions
    if audit.status_code and audit.status_code != 200:
        score += 200
    if not audit.canonical:
        score += 100
    if "noindex" in audit.robots_meta.lower():
        score += 100
    return score


def inspect_urls(service, site_url: str, audits: list[PageAudit], inspection_limit: int) -> tuple[int, int]:
    inspectable = sorted(audits, key=inspection_priority, reverse=True)
    selected = inspectable[: max(0, inspection_limit)]
    skipped = max(0, len(inspectable) - len(selected))

    for audit in audits:
        audit.inspection_note = "not_selected_due_to_quota"

    for audit in selected:
        body = {"inspectionUrl": audit.final_url or audit.url, "siteUrl": site_url, "languageCode": "en-US"}
        try:
            info = service.urlInspection().index().inspect(body=body).execute().get("inspectionResult", {}).get("indexStatusResult", {})
            verdict = info.get("verdict", "UNKNOWN")
            coverage = info.get("coverageState", "")
            robots_txt = info.get("robotsTxtState", "")
            indexing_state = info.get("indexingState", "")
            audit.indexed_status = "indexed" if verdict.upper() == "PASS" else "not_indexed"
            audit.coverage_state = coverage or indexing_state or "unavailable"
            audit.google_selected_canonical = info.get("googleCanonical", "")
            audit.user_declared_canonical = info.get("userCanonical", "")
            audit.robots_state = robots_txt or indexing_state or "unavailable"
            audit.last_crawl_time = info.get("lastCrawlTime", "")
            audit.inspection_note = "inspected"
        except Exception as exc:  # noqa: BLE001
            audit.inspection_note = f"inspection_error: {exc}"
            audit.indexed_status = "unavailable"
            audit.coverage_state = "unavailable"
            audit.robots_state = "unavailable"
    return len(selected), skipped


def enrich_with_gsc(
    audits: list[PageAudit],
    credentials_file: str,
    oauth_client_file: str,
    site_url: str,
    start_date: str,
    end_date: str,
    inspection_limit: int,
    oauth_manual: bool,
) -> dict[str, str]:
    summary = {"gsc_enabled": "no", "gsc_error": "", "inspection_selected": "0", "inspection_skipped": "0"}
    if not site_url:
        summary["gsc_error"] = "GSC skipped: missing --site-url."
        return summary

    if oauth_client_file:
        if not Path(oauth_client_file).exists():
            summary["gsc_error"] = "GSC skipped: --oauth-client-file not found."
            return summary
    elif not credentials_file or not Path(credentials_file).exists():
        summary["gsc_error"] = "GSC skipped: missing --credentials-file or provide --oauth-client-file."
        return summary

    try:
        service = get_search_console_service(
            credentials_file=credentials_file,
            oauth_client_file=oauth_client_file,
            oauth_manual=oauth_manual,
        )
        page_metrics, top_queries = fetch_gsc_page_metrics(service, site_url, start_date, end_date)
        for audit in audits:
            key = normalize_url_for_match(audit.final_url or audit.url)
            metrics = page_metrics.get(key)
            if metrics:
                audit.gsc_clicks = metrics["clicks"]
                audit.gsc_impressions = metrics["impressions"]
                audit.gsc_ctr = metrics["ctr"]
                audit.gsc_position = metrics["position"]
            if key in top_queries:
                audit.gsc_top_queries = " | ".join(top_queries[key])

        selected, skipped = inspect_urls(service, site_url, audits, inspection_limit)
        summary.update(gsc_enabled="yes", inspection_selected=str(selected), inspection_skipped=str(skipped))
        return summary
    except Exception as exc:  # noqa: BLE001
        summary["gsc_error"] = f"GSC integration failed: {exc}"
        return summary


def extract_issue_items(audits_blob: dict, group: str) -> list[str]:
    result: list[str] = []
    for audit_id, details in audits_blob.items():
        if details.get("details", {}).get("type") != group:
            continue
        score = details.get("score")
        if isinstance(score, (int, float)) and score < 0.9:
            title = details.get("title", audit_id)
            display_value = details.get("displayValue", "")
            result.append(f"{title} ({display_value})".strip())
    return result[:10]


def extract_performance_issues(audits_blob: dict) -> list[str]:
    issues: list[str] = []
    checks = [
        ("largest-contentful-paint", "LCP"),
        ("interaction-to-next-paint", "INP"),
        ("cumulative-layout-shift", "CLS"),
    ]
    for audit_key, label in checks:
        details = audits_blob.get(audit_key, {})
        score = details.get("score")
        display_value = details.get("displayValue", "unavailable")
        if isinstance(score, (int, float)) and score < 0.9:
            issues.append(f"{label} needs improvement ({display_value})")
    return issues


def fetch_pagespeed_strategy(url: str, api_key: str, strategy: str, session: requests.Session) -> dict:
    endpoint = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
    params = {
        "url": url,
        "strategy": strategy,
        "category": "performance",
        "key": api_key,
    }
    resp = session.get(endpoint, params=params, timeout=TIMEOUT_SECONDS)
    resp.raise_for_status()
    return resp.json()


def enrich_with_pagespeed(audits: list[PageAudit], api_key: str, session: requests.Session) -> dict[str, str]:
    summary = {"psi_enabled": "no", "psi_error": ""}
    if not api_key:
        summary["psi_error"] = "PageSpeed skipped: missing --pagespeed-api-key."
        return summary

    summary["psi_enabled"] = "yes"
    for audit in audits:
        target = audit.final_url or audit.url
        try:
            mobile = fetch_pagespeed_strategy(target, api_key, "mobile", session)
            desktop = fetch_pagespeed_strategy(target, api_key, "desktop", session)

            m_lh = mobile.get("lighthouseResult", {})
            d_lh = desktop.get("lighthouseResult", {})
            m_audits = m_lh.get("audits", {})
            d_audits = d_lh.get("audits", {})

            perf = m_lh.get("categories", {}).get("performance", {}).get("score")
            audit.performance_score = round(float(perf) * 100, 1) if perf is not None else 0.0
            audit.lcp = str(m_audits.get("largest-contentful-paint", {}).get("displayValue", "unavailable"))
            audit.inp = str(m_audits.get("interaction-to-next-paint", {}).get("displayValue", "unavailable"))
            audit.cls = str(m_audits.get("cumulative-layout-shift", {}).get("displayValue", "unavailable"))

            mobile_issues = extract_performance_issues(m_audits)
            desktop_issues = extract_performance_issues(d_audits)
            mobile_od = extract_issue_items(m_audits, "opportunity") + extract_issue_items(m_audits, "diagnostic")
            desktop_od = extract_issue_items(d_audits, "opportunity") + extract_issue_items(d_audits, "diagnostic")
            opportunities = [f"mobile: {x}" for x in mobile_od[:5]] + [f"desktop: {x}" for x in desktop_od[:5]]

            audit.mobile_performance_issues = " | ".join(mobile_issues)
            audit.desktop_performance_issues = " | ".join(desktop_issues)
            audit.opportunities_diagnostics = " | ".join(opportunities)
            audit.psi_note = "collected"
        except Exception as exc:  # noqa: BLE001
            audit.psi_note = f"psi_error: {exc}"
    return summary


def build_ai_dataset(audits: list[PageAudit], max_rows: int = 120) -> list[dict]:
    ranked = sorted(
        audits,
        key=lambda a: (
            a.indexed_status != "not_indexed",
            -(a.gsc_clicks + a.gsc_impressions),
            a.performance_score,
        ),
    )
    trimmed = ranked[:max_rows]
    rows: list[dict] = []
    for a in trimmed:
        rows.append(
            {
                "url": a.final_url or a.url,
                "status_code": a.status_code,
                "title": a.title,
                "meta_description": a.meta_description,
                "h1": a.h1,
                "canonical": a.canonical,
                "robots_meta": a.robots_meta,
                "indexability": a.indexability,
                "gsc_clicks": a.gsc_clicks,
                "gsc_impressions": a.gsc_impressions,
                "gsc_ctr": a.gsc_ctr,
                "gsc_position": a.gsc_position,
                "gsc_top_queries": a.gsc_top_queries,
                "indexed_status": a.indexed_status,
                "coverage_state": a.coverage_state,
                "google_selected_canonical": a.google_selected_canonical,
                "user_declared_canonical": a.user_declared_canonical,
                "robots_state": a.robots_state,
                "last_crawl_time": a.last_crawl_time,
                "performance_score": a.performance_score,
                "lcp": a.lcp,
                "inp": a.inp,
                "cls": a.cls,
                "mobile_performance_issues": a.mobile_performance_issues,
                "desktop_performance_issues": a.desktop_performance_issues,
                "opportunities_diagnostics": a.opportunities_diagnostics,
                "external_links_count": a.external_links_count,
                "external_links": a.external_links,
                "structured_data": a.structured_data,
                "images_missing_alt_count": a.images_missing_alt_count,
                "images_missing_alt": a.images_missing_alt,
                "crawl_error": a.error,
            }
        )
    return rows


def generate_ai_analysis(
    audits: list[PageAudit],
    openai_api_key: str,
    openai_model: str,
    session: requests.Session,
) -> dict[str, str]:
    summary = {"ai_enabled": "no", "ai_error": "", "ai_analysis_md": ""}
    if not openai_api_key:
        summary["ai_error"] = "AI analysis skipped: missing --openai-api-key."
        return summary

    payload_rows = build_ai_dataset(audits)
    prompt = (
        "You are an SEO analyst. Use ONLY the provided evidence table. "
        "Do not provide generic advice. Every claim must cite specific URL-level evidence from the dataset "
        "(crawl, GSC, URL inspection, and PageSpeed). If evidence is insufficient, explicitly say 'unconfirmed' "
        "and list exact extra data needed. GA4 data is not available unless explicitly present.\n\n"
        "Generate markdown with these sections:\n"
        "1) Indexation diagnosis\n"
        "2) Metadata and keyword map\n"
        "3) Traffic performance diagnosis\n"
        "4) Reasons pages are underperforming\n"
        "5) Page-specific solutions\n"
        "6) Conversion improvement ideas\n"
        "7) New content and SEO opportunities\n"
        "8) AEO/GEO recommendations\n"
        "9) 30/60/90-day SEO roadmap\n\n"
        "For each recommendation include: impacted URLs, supporting evidence fields, impact level (High/Medium/Low), "
        "effort level (High/Medium/Low), and owner-friendly implementation wording.\n"
        "If proof is missing, explicitly label the finding as unconfirmed and request exact missing data.\n\n"
        f"Dataset JSON:\n{json.dumps(payload_rows, ensure_ascii=False)}"
    )
    body = {
        "model": openai_model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "Return concise, structured markdown only."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
        "max_output_tokens": 3000,
    }
    try:
        resp = session.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=TIMEOUT_SECONDS * 3,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("output_text", "").strip()
        if not text:
            summary["ai_error"] = "AI analysis returned empty output."
            return summary
        summary["ai_enabled"] = "yes"
        summary["ai_analysis_md"] = text
        return summary
    except Exception as exc:  # noqa: BLE001
        summary["ai_error"] = f"AI analysis failed: {exc}"
        return summary


def save_csv(rows: Iterable[PageAudit], output_csv: Path) -> None:
    fields = [
        "url",
        "final_url",
        "status_code",
        "title",
        "meta_description",
        "h1",
        "h2s",
        "canonical",
        "robots_meta",
        "indexability",
        "word_count",
        "internal_links_count",
        "internal_links",
        "external_links_count",
        "external_links",
        "structured_data",
        "images_missing_alt_count",
        "images_missing_alt",
        "gsc_clicks",
        "gsc_impressions",
        "gsc_ctr",
        "gsc_position",
        "gsc_top_queries",
        "indexed_status",
        "coverage_state",
        "google_selected_canonical",
        "user_declared_canonical",
        "robots_state",
        "last_crawl_time",
        "performance_score",
        "lcp",
        "inp",
        "cls",
        "mobile_performance_issues",
        "desktop_performance_issues",
        "opportunities_diagnostics",
        "inspection_note",
        "psi_note",
        "error",
    ]
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.__dict__)


def evidence_and_fix(a: PageAudit) -> tuple[str, str]:
    evidence: list[str] = []
    fixes: list[str] = []

    if a.error:
        evidence.append(f"crawl_error={a.error}")
        fixes.append("unconfirmed: resolve crawl error and rerun with successful fetch")
        return "; ".join(evidence), "; ".join(fixes)

    if a.status_code and a.status_code != 200:
        evidence.append(f"status_code={a.status_code}")
        fixes.append("fix URL status to 200 or remove URL from sitemap")
    if "BLOCKED" in a.robots_state.upper() or "BLOCKED" in a.coverage_state.upper():
        evidence.append(f"robots_state={a.robots_state}")
        fixes.append("allow Googlebot in robots.txt for this URL path")
    if "NOINDEX" in a.coverage_state.upper() or "NOINDEX" in a.robots_state.upper() or "noindex" in a.robots_meta.lower():
        evidence.append(f"noindex_signal robots_meta={a.robots_meta} coverage_state={a.coverage_state}")
        fixes.append("remove noindex only if this page should appear in search")
    if a.google_selected_canonical and a.user_declared_canonical and a.google_selected_canonical != a.user_declared_canonical:
        evidence.append(f"canonical_mismatch google={a.google_selected_canonical} declared={a.user_declared_canonical}")
        fixes.append("align internal links/content signals with preferred canonical URL")
    if a.indexed_status == "not_indexed" and not evidence:
        evidence.append(f"coverage_state={a.coverage_state or 'unavailable'} robots_state={a.robots_state}")
        fixes.append("unconfirmed: collect server logs and live URL test details for root cause")
    if not evidence:
        evidence.append("no_negative_indexation_evidence_detected")
        fixes.append("no action needed based on current evidence")

    return "; ".join(evidence), "; ".join(fixes)


def build_markdown_report(
    audits: list[PageAudit],
    sitemap_url: str,
    output_csv: str,
    gsc_summary: dict[str, str],
    psi_summary: dict[str, str],
    ai_summary: dict[str, str],
) -> str:
    total = len(audits)
    successful = sum(1 for a in audits if not a.error)
    indexed = sum(1 for a in audits if a.indexed_status == "indexed")
    not_indexed = sum(1 for a in audits if a.indexed_status == "not_indexed")
    inconclusive = sum(1 for a in audits if a.indexed_status == "unavailable")
    status_counts = Counter(str(a.status_code) for a in audits if a.status_code is not None)
    psi_pages = [a for a in audits if a.psi_note == "collected"]
    avg_perf = round(sum(a.performance_score for a in psi_pages) / len(psi_pages), 1) if psi_pages else 0.0

    lines = [
        "# SEO Audit Report",
        "",
        f"- **Generated (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
        f"- **Sitemap:** {sitemap_url}",
        f"- **Pages audited:** {total}",
        f"- **Successful crawls:** {successful}",
        f"- **CSV results file:** `{output_csv}`",
        f"- **GSC integration enabled:** {gsc_summary.get('gsc_enabled', 'no')}",
        f"- **PageSpeed integration enabled:** {psi_summary.get('psi_enabled', 'no')}",
        f"- **AI analysis enabled:** {ai_summary.get('ai_enabled', 'no')}",
    ]
    if gsc_summary.get("gsc_error"):
        lines.append(f"- **GSC warning:** {gsc_summary['gsc_error']}")
    if psi_summary.get("psi_error"):
        lines.append(f"- **PageSpeed warning:** {psi_summary['psi_error']}")
    if ai_summary.get("ai_error"):
        lines.append(f"- **AI warning:** {ai_summary['ai_error']}")

    lines.extend(
        [
            "",
            "## Indexation Snapshot (evidence-based)",
            "",
            f"- Indexed pages: **{indexed}**",
            f"- Not indexed pages: **{not_indexed}**",
            f"- Inconclusive pages (no inspection/GSC proof): **{inconclusive}**",
            f"- URL Inspection processed: **{gsc_summary.get('inspection_selected', '0')}**",
            f"- URL Inspection skipped due to quota handling: **{gsc_summary.get('inspection_skipped', '0')}**",
            "",
            "Quota note: URLs were prioritized by search impact (clicks/impressions) plus technical risk signals.",
            "",
            "## PageSpeed Snapshot",
            "",
            f"- Pages with PageSpeed data: **{len(psi_pages)}**",
            f"- Average mobile performance score: **{avg_perf}**",
            "",
        ]
    )

    if status_counts:
        lines.append("## HTTP Status Distribution")
        for code, count in sorted(status_counts.items()):
            lines.append(f"- `{code}`: {count}")
        lines.append("")

    lines.extend(["## Page-Specific Indexation + Performance Evidence", ""])
    for a in sorted(audits, key=lambda x: (x.indexed_status != "not_indexed", -x.gsc_impressions)):
        evidence, fixes = evidence_and_fix(a)
        lines.extend(
            [
                f"### {a.final_url or a.url}",
                f"- Indexed status: **{a.indexed_status}**",
                f"- Coverage/indexing state: `{a.coverage_state}`",
                f"- Robots/indexing state: `{a.robots_state}`",
                f"- Google-selected canonical: `{a.google_selected_canonical or 'n/a'}`",
                f"- User-declared canonical: `{a.user_declared_canonical or a.canonical or 'n/a'}`",
                f"- Last crawl time: `{a.last_crawl_time or 'unavailable'}`",
                f"- Top queries: `{a.gsc_top_queries or 'unavailable'}`",
                f"- Performance score (mobile): `{a.performance_score}`",
                f"- LCP: `{a.lcp}` | INP: `{a.inp}` | CLS: `{a.cls}`",
                f"- Indexability: `{a.indexability}`",
                f"- Mobile issues: `{a.mobile_performance_issues or 'none_detected_or_unavailable'}`",
                f"- Desktop issues: `{a.desktop_performance_issues or 'none_detected_or_unavailable'}`",
                f"- Opportunities/diagnostics: `{a.opportunities_diagnostics or 'none_detected_or_unavailable'}`",
                f"- External links count: `{a.external_links_count}`",
                f"- Structured data: `{a.structured_data or 'none_detected'}`",
                f"- Images missing alt: `{a.images_missing_alt_count}`",
                f"- Evidence: {evidence}",
                f"- Recommended fix (evidence-tied): {fixes}",
                f"- Inspection note: `{a.inspection_note}` | PSI note: `{a.psi_note}`",
                "",
            ]
        )

    if ai_summary.get("ai_analysis_md"):
        lines.extend(["## AI Evidence-Based Analysis", "", ai_summary["ai_analysis_md"], ""])

    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    sitemap_url = args.sitemap_url or args.sitemap
    if not sitemap_url:
        print("Missing sitemap argument. Use --sitemap-url or --sitemap.", file=sys.stderr)
        return 1

    try:
        urls = discover_urls_from_sitemap(sitemap_url, session)
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to parse sitemap: {exc}", file=sys.stderr)
        return 1
    if not urls:
        print("No URLs found in sitemap.", file=sys.stderr)
        return 1

    audits: list[PageAudit] = []
    for idx, url in enumerate(urls, 1):
        print(f"[{idx}/{len(urls)}] Crawling {url}")
        audits.append(audit_page(url, session))

    gsc_summary = enrich_with_gsc(
        audits,
        credentials_file=args.credentials_file,
        oauth_client_file=args.oauth_client_file,
        site_url=args.site_url,
        start_date=args.start_date,
        end_date=args.end_date,
        inspection_limit=args.inspection_limit,
        oauth_manual=args.oauth_manual,
    )
    psi_summary = enrich_with_pagespeed(audits, args.pagespeed_api_key, session)
    ai_summary = generate_ai_analysis(audits, args.openai_api_key, args.openai_model, session)

    if args.output:
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        output_csv = output_dir / f"seo_audit_results_{stamp}.csv"
        output_report = output_dir / f"seo_audit_report_{stamp}.md"
    else:
        output_csv = Path(args.output_csv or "seo_audit_results.csv")
        output_report = Path(args.output_report or "seo_audit_report.md")
        output_csv.parent.mkdir(parents=True, exist_ok=True)
        output_report.parent.mkdir(parents=True, exist_ok=True)

    save_csv(audits, output_csv)
    output_report.write_text(
        build_markdown_report(audits, sitemap_url, str(output_csv), gsc_summary, psi_summary, ai_summary),
        encoding="utf-8",
    )

    print(f"Done. CSV: {output_csv} | Report: {output_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
