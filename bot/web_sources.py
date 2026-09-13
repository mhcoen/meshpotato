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
from urllib.parse import urljoin, urlsplit

import trafilatura
import certifi
from bs4 import BeautifulSoup
from ddgs import DDGS

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
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("non-public address")
    return host, port, addresses[0][4][0], parsed.scheme


def fetch_page(url: str) -> dict:
    """No proxies, cookies, JS, credential forwarding, or second DNS resolution."""
    try:
        for _ in range(4):
            host, port, address, scheme = public_target(url)
            conn = http.client.HTTPConnection(host, port, timeout=4)
            sock = socket.create_connection((address, port), timeout=4)
            try:
                if scheme == "https":
                    sock = ssl.create_default_context(cafile=certifi.where()).wrap_socket(sock, server_hostname=host)
                conn.sock = sock
                parsed = urlsplit(url)
                target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                conn.request("GET", target, headers={"Host": host, "User-Agent": "MeshPotato/1.7",
                                                     "Accept-Encoding": "identity"})
                response = conn.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.getheader("location", ""))
                    continue
                if response.status != 200:
                    raise ValueError("page unavailable")
                kind = response.getheader("content-type", "").lower()
                if not any(t in kind for t in ("text/html", "application/xhtml+xml", "text/plain")):
                    raise ValueError("unsupported content type")
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("page exceeds 1 MB")
                charset = re.search(r"charset=([\w-]+)", kind)
                html = raw.decode(charset[1] if charset else "utf-8", errors="replace")
                text = extract_text(html) if "html" in kind else html
                if len(text) < 80 or access_challenge(text):
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
                        "product": product}
            finally:
                conn.close()
                sock.close()
        raise ValueError("too many redirects")
    except (OSError, ValueError, LookupError, http.client.HTTPException):
        return {}


def collect(query: str) -> list[dict]:
    # Pin the engine instead of DDGS auto selecting undisclosed providers.
    results = DDGS(timeout=5).text(query, max_results=5, region="us-en", backend="duckduckgo")
    urls = list(dict.fromkeys(r["href"] for r in results if r.get("href")))[:3]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        pages = list(pool.map(fetch_page, urls))
    return [{**page, "text": evidence_excerpt(page["text"], query, 4000)}
            for page in pages if page]


def main() -> None:
    # Parent bounds runtime and kills this disposable worker on cancellation.
    query = json.loads(sys.stdin.buffer.read(4096))["query"]
    try:
        pages = collect(query)
    except Exception:
        pages = []
    print(json.dumps(pages))



def evidence_excerpt(text: str, query: str, limit: int = 4000) -> str:
    """Keep the lead plus query-relevant paragraphs, in original document order."""
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
