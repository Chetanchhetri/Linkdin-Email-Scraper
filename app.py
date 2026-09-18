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
            name = raw_title.split(" - ")[0].split(" | ")[0].strip() or None
            snippet = (result.get("body") or "").strip()

            # Extract emails directly from snippet/description text
            discovered_emails = list(set(
                m.lower().rstrip('.') for m in re.findall(EMAIL_REGEX, snippet + " " + raw_title, re.IGNORECASE)
            ))

            profiles.append({
                "name": name,
                "profile_url": clean_url,
                "snippet": snippet,
                "emails": sorted(discovered_emails)
            })

    try:
        with DDGS() as ddgs:
            raw_results = list(ddgs.text(linkedin_query, max_results=max_results))
            parse_results(raw_results)
    except Exception as e:
        print(f"[!] Warning during LinkedIn search execution: {e}")

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