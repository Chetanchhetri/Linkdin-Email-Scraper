import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
PHONE_REGEX = r'(?<!\d)(?:\+?\d[\d .()\-]{7,}\d)(?!\d)'
LOCATION_LABELS = ("location", "based in", "located in", "city", "office", "headquarters")
ADDRESS_LABELS = ("address", "office address", "registered office", "hq", "head office")
COMMON_SOCIAL_DOMAINS = ("linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com")

IGNORED_SERP_DOMAINS = (
    "google.", "gstatic.com", "youtube.com", "bing.com", "microsoft.com",
    "duckduckgo.com", "yahoo.com", "schema.org", "wikipedia.org", "w3.org",
    "facebook.com", "twitter.com", "instagram.com", "linkedin.com"
)

NON_PAGE_SCHEMES = ("mailto:", "tel:", "javascript:", "#")

_write_lock = threading.Lock()
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def fetch_serp_urls(query: str, max_results: int = 50) -> list[str]:
    discovered_urls = set()
    print(f"[SEARCH] Executing Search Query: {query}")

    def parse_results(results_list):
        for result in results_list:
            href = result.get("href") or result.get("url") or ""
            if not href.startswith("http"):
                continue
            parsed = urlparse(href)
            domain = parsed.netloc.lower()

            if any(ignored in domain for ignored in IGNORED_SERP_DOMAINS):
                continue

            if parsed.scheme in ("http", "https") and domain:
                base_domain_url = f"{parsed.scheme}://{domain}"
                discovered_urls.add(base_domain_url)

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(query, max_results=max_results))
            parse_results(raw_results)

            if not discovered_urls and '"' in query:
                clean_query = query.replace('"', '')
                print(f"[SEARCH] Retrying with unquoted query: {clean_query}")
                raw_results = list(ddgs.text(clean_query, max_results=max_results))
                parse_results(raw_results)
    except Exception as e:
        print(f"[!] Warning during DDGS search execution: {e}")

    final_urls = sorted(list(discovered_urls))
    print(f"[OK] Discovered {len(final_urls)} target domain(s).\n")
    return final_urls


def _extract_phone(text: str) -> str | None:
    candidates = []
    for match in re.findall(PHONE_REGEX, text or ""):
        digits = re.sub(r"\D", "", match)
        if 10 <= len(digits) <= 15:
            candidates.append(match.strip())
    return candidates[0] if candidates else None


def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str | None:
    if not text:
        return None
    label_pattern = "|".join(re.escape(x) for x in labels)
    m = re.search(rf"(?:{label_pattern})\s*[:\-–]\s*([^|.;\n]{{2,100}})", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _clean_candidate_url(url: str) -> str | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    domain = urlparse(url).netloc.lower()
    if not domain or any(x in domain for x in COMMON_SOCIAL_DOMAINS):
        return None
    return url.split("?")[0].rstrip("/")


def _company_from_profile(headline: str, snippet: str) -> str | None:
    """Best-effort extraction of an employer/company from indexed profile text."""
    text = f"{headline or ''} | {snippet or ''}"
    # Common LinkedIn result format: "Role at Company | LinkedIn"
    patterns = [
        r'\bat\s+([^|•·\n]{2,80})',
        r'\bwith\s+([^|•·\n]{2,80})',
        r'\b@\s*([^|•·\n]{2,80})',
        r'\b(?:Experience|Company)\s*[:\-]\s*([^|•·\n]{2,80})',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            value = re.sub(r'\s+', ' ', m.group(1)).strip(' -–—,.;')
            if value and len(value.split()) <= 12:
                return value
    return None


def _search_public_contact(name: str, role: str, profile_url: str = "", headline: str = "",
                           snippet: str = "", location_hint: str = "") -> dict:
    """Find publicly indexed professional contact details using identity-specific searches.

    This does not log into LinkedIn, bypass access controls, or infer a private contact
    detail. Results are accepted only when they come from publicly indexed pages.
    """
    candidate_websites = []
    found_emails = set()
    found_phones = set()
    locations = []
    addresses = []
    source_urls = []

    company = _company_from_profile(headline, snippet)
    slug = urlparse(profile_url).path.rstrip('/').split('/')[-1] if profile_url else ''
    identity_parts = [f'"{name}"']
    if company:
        identity_parts.append(f'"{company}"')
    elif headline:
        identity_parts.append(f'"{headline[:80]}"')
    identity = ' '.join(identity_parts)

    # Search for the person's identity, not merely the generic job role. This substantially
    # reduces accidental association of one person's phone/email with another person.
    queries = [
        f'{identity} email',
        f'{identity} contact',
        f'{identity} phone',
        f'{identity} website',
        f'{identity} "contact info"',
    ]
    if slug:
        queries += [
            f'"{slug}" email',
            f'"{slug}" contact',
        ]
    if location_hint:
        queries = [q + f' "{location_hint}"' for q in queries[:5]] + queries[5:]

    # Keep only domains that are plausibly professional sources. LinkedIn itself is not
    # crawled; its public search result is used only to identify the person.
    allowed_hint_domains = ('github.com', 'about.me', 'medium.com', 'substack.com')

    try:
        with DDGS() as ddgs:
            for q in queries:
                try:
                    results = list(ddgs.text(q, max_results=10))
                except Exception:
                    continue
                for result in results:
                    title = (result.get('title') or '').strip()
                    body = (result.get('body') or '').strip()
                    href = result.get('href') or result.get('url') or ''
                    combined = f'{title} {body}'
                    lower = combined.lower()

                    # Require the person's name to occur in the result text for contact
                    # data to be considered a match.
                    name_tokens = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z'-]+", name) if len(t) > 1]
                    if name_tokens and not all(token in lower for token in name_tokens[:2]):
                        continue

                    found_emails.update(
                        m.lower().rstrip('.')
                        for m in re.findall(EMAIL_REGEX, combined, re.IGNORECASE)
                    )
                    for match in re.findall(PHONE_REGEX, combined):
                        digits = re.sub(r'\D', '', match)
                        if 10 <= len(digits) <= 15:
                            found_phones.add(re.sub(r'\s+', ' ', match.strip()))

                    loc = _extract_labeled_value(combined, LOCATION_LABELS)
                    addr = _extract_labeled_value(combined, ADDRESS_LABELS)
                    if loc and loc not in locations:
                        locations.append(loc)
                    if addr and addr not in addresses:
                        addresses.append(addr)

                    clean = _clean_candidate_url(href)
                    if clean:
                        host = urlparse(clean).netloc.lower()
                        # Don't treat arbitrary search result pages as the person's website.
                        if host in allowed_hint_domains or (company and company.lower().replace(' ', '') in host.replace('.', '').replace('-', '')):
                            if clean not in candidate_websites:
                                candidate_websites.append(clean)
                        if clean not in source_urls:
                            source_urls.append(clean)
    except Exception as e:
        print(f'[!] Warning during contact enrichment for {name}: {e}')

    # Prefer business/professional email domains over generic mailbox domains when several
    # publicly indexed matches exist. We still return all verified indexed matches.
    return {
        'emails': sorted(found_emails),
        'contact_number': sorted(found_phones)[0] if found_phones else None,
        'contact_numbers': sorted(found_phones),
        'location': locations[0] if locations else None,
        'address': addresses[0] if addresses else None,
        'website': candidate_websites[0] if candidate_websites else None,
        'contact_sources': source_urls[:10],
        'matched_company': company,
    }


def fetch_linkedin_profiles(query: str, max_results: int = 20) -> list[dict]:
    profiles = []
    seen_urls = set()
    linkedin_query = f'site:linkedin.com/in {query}'

    def parse_results(results_list):
        for result in results_list:
            href = result.get("href") or result.get("url") or ""
            if "linkedin.com/in/" not in href.lower():
                continue
            clean_url = href.split("?")[0].rstrip("/")
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)
            raw_title = (result.get("title") or "").strip()
            title_parts = re.split(r"\s[-|]\s", raw_title, maxsplit=1)
            name = title_parts[0].strip() or None
            headline = title_parts[1].strip() if len(title_parts) > 1 else None
            snippet = (result.get("body") or "").strip()
            profiles.append({
                "name": name,
                "headline": headline,
                "profile_url": clean_url,
                "snippet": snippet,
                "emails": sorted(set(m.lower().rstrip(".") for m in re.findall(EMAIL_REGEX, snippet + " " + raw_title, re.IGNORECASE))),
                "contact_number": _extract_phone(snippet),
                "contact_numbers": [],
                "location": _extract_labeled_value(snippet, LOCATION_LABELS),
                "website": None,
                "address": None,
                "matched_company": None,
                "contact_sources": [],
            })

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(linkedin_query, max_results=max_results))
            parse_results(raw_results)
    except Exception as e:
        print(f"[!] Warning during LinkedIn search execution: {e}")

    # Email/contact/website/location discovery is now automatic for every profile.
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(profiles)))) as pool:
        future_map = {
            pool.submit(
                _search_public_contact,
                p["name"],
                query,
                p.get("profile_url") or "",
                p.get("headline") or "",
                p.get("snippet") or "",
                p.get("location") or ""
            ): p
            for p in profiles if p.get("name")
        }
        for future in as_completed(future_map):
            profile = future_map[future]
            try:
                extra = future.result()
                profile["emails"] = sorted(set(profile["emails"]) | set(extra["emails"]))
                profile["contact_number"] = extra["contact_number"] or profile["contact_number"]
                profile["contact_numbers"] = sorted(set(profile.get("contact_numbers", [])) | set(extra.get("contact_numbers", [])))
                if profile["contact_number"] and profile["contact_number"] not in profile["contact_numbers"]:
                    profile["contact_numbers"].append(profile["contact_number"])
                profile["location"] = extra["location"] or profile["location"]
                profile["website"] = extra["website"] or profile.get("website")
                profile["address"] = extra["address"] or profile.get("address")
                profile["matched_company"] = extra.get("matched_company") or profile.get("matched_company")
                profile["contact_sources"] = extra.get("contact_sources", [])
            except Exception as e:
                print(f"[!] Contact enrichment failed: {e}")
    return profiles


def crawl_single_site(base_url: str, max_pages: int = 5) -> dict:
    site_start_time = time.time()
    base_url = base_url.rstrip('/')
    domain = urlparse(base_url).netloc

    visited_urls = set()
    urls_to_visit = [
        base_url,
        f"{base_url}/contact",
        f"{base_url}/contact-us",
        f"{base_url}/about",
        f"{base_url}/about-us",
    ]
    found_emails = set()
    found_phones = set()

    def is_internal_link(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.netloc and parsed.netloc != domain:
            return False
        ignored_extensions = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp',
                              '.pdf', '.css', '.js', '.ico', '.zip', '.mp4')
        return not parsed.path.lower().endswith(ignored_extensions)

    session = requests.Session()
    session.headers.update(HTTP_HEADERS)

    while urls_to_visit and len(visited_urls) < max_pages:
        current_url = urls_to_visit.pop(0)

        if current_url.lower().startswith(NON_PAGE_SCHEMES) or current_url in visited_urls:
            continue

        visited_urls.add(current_url)

        try:
            resp = session.get(current_url, timeout=8, verify=False)
            if resp.status_code != 200 or not resp.text:
                continue

            raw_html = resp.text

            for match in re.findall(EMAIL_REGEX, raw_html, re.IGNORECASE):
                found_emails.add(match.lower().rstrip('.'))

            visible_text = BeautifulSoup(raw_html, 'html.parser').get_text(" ", strip=True)
            for match in re.findall(PHONE_REGEX, visible_text):
                digits = re.sub(r"\D", "", match)
                if 10 <= len(digits) <= 15:
                    found_phones.add(match.strip())

            soup = BeautifulSoup(raw_html, 'html.parser')
            for anchor in soup.find_all('a', href=True):
                href = anchor['href'].strip()

                if href.lower().startswith("mailto:"):
                    addr = href[7:].split("?")[0].strip()
                    if re.match(EMAIL_REGEX, addr, re.IGNORECASE):
                        found_emails.add(addr.lower())
                    continue

                if href.lower().startswith(("tel:", "javascript:", "#")):
                    continue

                full_url = urljoin(current_url, href).split('#')[0].rstrip('/')
                if full_url not in visited_urls and is_internal_link(full_url):
                    if full_url not in urls_to_visit:
                        urls_to_visit.append(full_url)
        except Exception:
            pass

    site_execution_time = round(time.time() - site_start_time, 2)
    sorted_emails = sorted(list(found_emails))

    return {
        "domain": domain,
        "target_url": base_url,
        "execution_time_seconds": site_execution_time,
        "emails_count": len(sorted_emails),
        "emails": sorted_emails,
        "contact_numbers": sorted(found_phones),
        "website": base_url,
        "pages_visited": sorted(list(visited_urls))
    }


def _build_output(query, results, total_execution_time, all_unique_emails, linkedin_profiles=None):
    linkedin_profiles = linkedin_profiles or []
    
    # Merge emails extracted from LinkedIn profile snippets into master email list
    linkedin_emails = {e for profile in linkedin_profiles for e in profile.get("emails", [])}
    combined_unique_emails = sorted(set(all_unique_emails) | linkedin_emails)

    return {
        "query": query,
        "metrics": {
            "total_domains_crawled": len(results),
            "total_unique_emails_found": len(combined_unique_emails),
            "total_linkedin_profiles_found": len(linkedin_profiles),
            "total_execution_time_seconds": total_execution_time,
            "total_execution_time_formatted": f"{int(total_execution_time // 60)}m {round(total_execution_time % 60, 2)}s"
        },
        "all_emails": combined_unique_emails,
        "all_contact_numbers": sorted(set(
            [p.get("contact_number") for p in linkedin_profiles if p.get("contact_number")]
            + [n for site in results for n in site.get("contact_numbers", [])]
        )),
        "linkedin_profiles": linkedin_profiles,
        "sites_data": results
    }


def _write_partial_output(query, results, total_start_time, output_filename, linkedin_profiles=None):
    elapsed = round(time.time() - total_start_time, 2)
    all_unique_emails = sorted({email for site in results for email in site["emails"]})
    partial = _build_output(query, results, elapsed, all_unique_emails, linkedin_profiles=linkedin_profiles)
    with _write_lock:
        with open(output_filename, "w", encoding="utf-8") as f:
            json.dump(partial, f, indent=4, ensure_ascii=False)


def run_query_email_scraper(query: str, max_serp_results: int = 50, max_pages_per_site: int = 5,
                             max_workers: int = 8, output_filename: str = "operations_head_emails.json",
                             max_linkedin_results: int = 20):
    total_start_time = time.time()

    linkedin_profiles = []
    with ThreadPoolExecutor(max_workers=2) as discovery_pool:
        linkedin_future = discovery_pool.submit(fetch_linkedin_profiles, query, max_linkedin_results)
        target_urls = fetch_serp_urls(query, max_results=max_serp_results)
        try:
            linkedin_profiles = linkedin_future.result()
        except Exception:
            linkedin_profiles = []

    results = []
    if target_urls:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {
                executor.submit(crawl_single_site, url, max_pages_per_site): url for url in target_urls
            }

            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    data = future.result()
                    results.append(data)
                except Exception as exc:
                    print(f"[X] Error processing {url}: {exc}")
                    continue

                _write_partial_output(query, results, total_start_time, output_filename,
                                       linkedin_profiles=linkedin_profiles)

    total_execution_time = round(time.time() - total_start_time, 2)
    all_unique_emails = sorted({email for site in results for email in site["emails"]})

    final_output = _build_output(query, results, total_execution_time, all_unique_emails,
                                  linkedin_profiles=linkedin_profiles)

    with _write_lock:
        with open(output_filename, "w", encoding="utf-8") as f:
            json.dump(final_output, f, indent=4, ensure_ascii=False)

    return final_output


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/api/scrape", methods=["POST"])
def api_scrape():
    payload = request.get_json(silent=True) or {}
    query = (payload.get("query") or "").strip()
    if not query:
        return jsonify({"error": "A search query is required."}), 400

    def clamp(value, default, lo, hi):
        try:
            return max(lo, min(hi, int(value)))
        except (TypeError, ValueError):
            return default

    max_results = clamp(payload.get("max_results"), 30, 1, 100)
    max_pages_per_site = clamp(payload.get("max_pages_per_site"), 5, 1, 15)
    max_workers = clamp(payload.get("max_workers"), 8, 1, 16)
    max_linkedin_results = clamp(payload.get("max_linkedin_results"), 20, 1, 50)

    try:
        result = run_query_email_scraper(
            query=query,
            max_serp_results=max_results,
            max_pages_per_site=max_pages_per_site,
            max_workers=max_workers,
            output_filename="operations_head_emails.json",
            max_linkedin_results=max_linkedin_results,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"error": f"Scrape failed: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)