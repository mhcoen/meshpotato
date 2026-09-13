"""Search/extraction adapted from Michael H. Coen's Episodic Muse.

Copyright 2025 Michael H. Coen. SPDX-License-Identifier: Apache-2.0
See EPISODIC-LICENSE. Modified for Mesh Potato, September 2026: bounded worker,
public-address pinning, per-redirect validation, and page-only evidence.
"""
from __future__ import annotations

import concurrent.futures
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import time
from urllib.parse import urljoin, urlsplit

import trafilatura
import certifi
from bs4 import BeautifulSoup
from ddgs import DDGS
from bot.web_evidence import qualifiers

def extract_text(html: str) -> str:
    # Keep tables: retail prices and hours can be in them.
    extracted = trafilatura.extract(
        html, include_comments=False, include_tables=True, output_format="txt"
    )
    if extracted and len(extracted.strip()) >= 20:
        return re.sub(r"\n{3,}", "\n\n", extracted).strip()
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "iframe"]):
        tag.decompose()
    for selector in ["main", "article", '[role="main"]', "#content", ".content", "body"]:
        node = soup.select_one(selector)
        if node:
            text = node.get_text(separator=" ", strip=True)
            if len(text) >= 20:
                return text
    return ""


def access_challenge(text: str) -> bool:
    return any(marker in text.lower() for marker in (
        "made us think you were a bot", "verify you are human",
        "enable javascript and cookies to continue", "please complete the security check",
        "incapsula incident id", "request unsuccessful",
        "just a moment", "checking your browser", "access denied", "captcha",
    ))


def public_target(url: str) -> tuple[str, int, str, str]:
    """Resolve once; the HTTP connection uses the returned public IP directly."""
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError("only public HTTP(S) URLs on standard ports are allowed")
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    def direct_public(address):
        ip = ipaddress.ip_address(address)
        # Reject translation/tunneling ranges as well as ordinary local IPs.
        # A public NAT64 endpoint can translate an embedded private IPv4 address.
        translated = isinstance(ip, ipaddress.IPv6Address) and (
            ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48")
            or ip.ipv4_mapped is not None or ip.sixtofour is not None or ip.teredo is not None)
        return ip.is_global and not translated
    if not addresses or any(not direct_public(a[4][0]) for a in addresses):
        raise ValueError("non-public address")
    return host, port, addresses[0][4][0], parsed.scheme


def fetch_page(url: str, *, json_response: bool = False) -> dict:
    """No proxies, cookies, JS, credential forwarding, or second DNS resolution."""
    deadline = time.monotonic() + 8
    try:
        for _ in range(4):
            if time.monotonic() >= deadline:
                raise TimeoutError("page deadline")
            if json_response and (urlsplit(url).hostname != "site.api.espn.com" or urlsplit(url).scheme != "https"):
                raise ValueError("unexpected scoreboard origin")
            host, port, address, scheme = public_target(url)
            conn = http.client.HTTPConnection(host, port, timeout=4)
            sock = socket.create_connection((address, port), timeout=4)
            try:
                if scheme == "https":
                    sock = ssl.create_default_context(cafile=certifi.where()).wrap_socket(sock, server_hostname=host)
                conn.sock = sock
                parsed = urlsplit(url)
                target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                conn.request("GET", target, headers={"Host": host, "Accept-Encoding": "identity",
                                                     **({"Cache-Control": "no-cache", "Accept": "application/json",
                                                         "User-Agent": "Mozilla/5.0 (compatible; MeshPotato/1.7; +https://github.com/mhcoen/meshpotato)"}
                                                        if json_response else {"User-Agent": "MeshPotato/1.7"})})
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    if json_response:
                        raise ValueError("scoreboard redirect refused")
                    url = urljoin(url, response.getheader("location", ""))
                    continue
                if response.status != 200:
                    raise ValueError(f"scoreboard HTTP {response.status}" if json_response else "page unavailable")
                if response.getheader("content-encoding", "identity").lower() not in {"", "identity"}:
                    raise ValueError("unsupported content encoding")
                kind = response.getheader("content-type", "").lower()
                types = ("application/json",) if json_response else ("text/html", "application/xhtml+xml", "text/plain")
                if not any(t in kind for t in types):
                    raise ValueError("unsupported content type")
                raw = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("page deadline")
                    sock.settimeout(min(4, remaining))
                    chunk = response.read1(min(65536, 1_000_001 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > 1_000_000:
                        raise ValueError("page exceeds 1 MB")
                if json_response:
                    # Scores may be cached by the provider, but never knowingly use
                    # a stale CDN response or follow a redirect to another provider.
                    if urlsplit(url).hostname != "site.api.espn.com" or int(response.getheader("age", "0")) > 60:
                        raise ValueError("stale or unexpected scoreboard origin")
                    data = json.loads(raw)
                    return data if isinstance(data, dict) else {}
                charset = re.search(r"charset=([\w-]+)", kind)
                html = raw.decode(charset[1] if charset else "utf-8", errors="replace")
                text = extract_text(html) if "html" in kind else html
                if len(text.strip()) < 80 or access_challenge(text):
                    raise ValueError("no usable page evidence")
                # Published dates and Product/Offer metadata help distinguish an old
                # article from a live product listing. Snippets are never evidence.
                soup = BeautifulSoup(html, "html.parser") if "html" in kind else None
                published = ""
                if soup:
                    tag = soup.find("meta", attrs={"property": "article:published_time"})
                    published = str(tag.get("content", "")) if tag else ""
                product = bool(re.search(r'"@type"\s*:\s*"(?:Product|Offer)"', html))
                return {"url": url, "text": text[:24000], "published": published,
                        "product": product, "qualifiers": sorted(qualifiers(text))}
            finally:
                conn.close()
                sock.close()
        raise ValueError("too many redirects")
    except Exception as exc:  # one broken page/extractor must not discard the other results
        if json_response:
            return {"_error": str(exc)[:120] if isinstance(exc, ValueError) else type(exc).__name__}
        return {}


def collect(query: str) -> list[dict]:
    from bot.sports import is_score_query, collect_scores
    if is_score_query(query):
        return collect_scores(query, lambda url: fetch_page(url, json_response=True))
    # Pin the engine instead of DDGS auto selecting undisclosed providers.
    results = DDGS(timeout=5).text(query, max_results=5, region="us-en", backend="duckduckgo")
    urls = list(dict.fromkeys(r["href"] for r in results if r.get("href")))[:3]
    def isolated_fetch(url):
        try:
            return fetch_page(url)
        except Exception:
            return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(isolated_fetch, urls))
    usable = []
    for page in pages:
        if isinstance(page, dict) and isinstance(page.get("text"), str) and page["text"].strip():
            excerpt = evidence_excerpt(page["text"], query, 4000)
            if excerpt:
                usable.append({**page, "full_text": page["text"], "text": excerpt})
    return usable


def main() -> None:
    # Parent bounds runtime and kills this disposable worker on cancellation.
    try:
        query = json.loads(sys.stdin.buffer.read(4096))["query"]
        pages = collect(query)
    except Exception:
        pages = {"error": "lookup-failed"}
    print(json.dumps(pages))



def evidence_excerpt(text: str, query: str, limit: int = 4000) -> str:
    """Keep the lead plus query-relevant paragraphs, in original document order."""
    if not text.strip():
        return ""
    if len(text) <= limit:
        return text
    terms = set(re.findall(r"[a-z0-9]{3,}", query.lower())) - {
        "the", "how", "much", "what", "does", "for", "and", "from", "today"
    }
    # Split long paragraphs as well; retain exact source substrings for audit.
    chunks = [p[i:i+500] for p in text.splitlines() if p.strip()
              for i in range(0, len(p), 500)]
    scores = [sum(t in p.lower() for t in terms) + (2 if "$" in p else 0)
              for p in chunks]
    chosen = {0}
    room = limit - len(chunks[0])
    for index in sorted(range(1, len(chunks)), key=lambda i: scores[i], reverse=True):
        if len(chunks[index]) + 1 <= room:
            chosen.add(index)
            room -= len(chunks[index]) + 1
    return "\n".join(chunks[i] for i in sorted(chosen))


if __name__ == "__main__":
    main()
