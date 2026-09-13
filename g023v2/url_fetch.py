"""Browser-like GET of one http(s) URL, plus HTML-to-readable-text extraction.

Network engines, best first when installed: curl_cffi (Chrome TLS), httpx,
then stdlib urllib. Missing extras are not an error. No JavaScript runs, so a
client-rendered shell may come back nearly empty.
"""

from __future__ import annotations

import gzip
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urljoin, urlparse

from g023v2.constants import FETCH_MAX_BYTES

# A single Chrome-like identity. Order matters for urllib (fingerprint-ish);
# httpx/curl_cffi set Host themselves so that header is omitted here.
_BROWSER_HEADERS: list[tuple[str, str]] = [
    ("User-Agent", (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/146.0.0.0 Safari/537.36"
    )),
    ("Accept", (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    )),
    ("Accept-Language", "en-US,en;q=0.9"),
    ("Upgrade-Insecure-Requests", "1"),
]


class FetchError(RuntimeError):
    """Network or policy failure (not an HTTP error status)."""


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: str
    engine: str
    truncated: bool = False
    fetched_at: float = 0.0
    from_cache: bool = False
    age_seconds: float = 0.0


def _header_map() -> dict[str, str]:
    return dict(_BROWSER_HEADERS)


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _cap_bytes(chunks: Iterable[bytes], limit: int = FETCH_MAX_BYTES) -> tuple[bytes, bool]:
    parts: list[bytes] = []
    total = 0
    truncated = False
    for chunk in chunks:
        if not chunk:
            continue
        room = limit - total
        if room <= 0:
            truncated = True
            break
        if len(chunk) > room:
            parts.append(chunk[:room])
            total += room
            truncated = True
            break
        parts.append(chunk)
        total += len(chunk)
    return b"".join(parts), truncated


def _decode_body(data: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([^\s;]+)", content_type, re.I)
    if match:
        charset = match.group(1).strip("\"'").lower()
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _headers_from_pairs(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in pairs:
        out[str(key).lower()] = str(value)
    return out


class _HttpOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        nxt = urlparse(newurl)
        if nxt.scheme not in ("http", "https"):
            raise urllib.error.URLError(
                f"redirect to non-http(s) refused: {nxt.scheme or 'no scheme'}"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_urllib(url: str, timeout: int) -> FetchResult:
    opener = urllib.request.build_opener(_HttpOnlyRedirectHandler)
    req = urllib.request.Request(url, method="GET", headers=_header_map())
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # HTTPError is a file-like response; still read a capped body.
        resp = e
    try:
        status = int(getattr(resp, "status", None) or resp.getcode() or 0)
        raw_headers = resp.headers.items() if resp.headers is not None else []
        headers = _headers_from_pairs(raw_headers)
        final = str(getattr(resp, "geturl", lambda: url)())
        if not _is_http_url(final):
            raise FetchError(f"redirect to non-http(s) refused: {urlparse(final).scheme or 'no scheme'}")

        def _iter():
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                yield chunk

        data, truncated = _cap_bytes(_iter())
        encoding = (headers.get("content-encoding") or "").lower()
        if encoding == "gzip" and not truncated:
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
        ctype = headers.get("content-type") or "unknown"
        body = _decode_body(data, ctype)
        return FetchResult(
            url=url,
            final_url=final,
            status=status,
            headers=headers,
            body=body,
            engine="urllib",
            truncated=truncated,
        )
    finally:
        try:
            resp.close()
        except Exception:
            pass


def _fetch_httpx(url: str, timeout: int) -> FetchResult:
    import httpx  # type: ignore

    headers = _header_map()
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=10,
        http2=True,
        verify=True,
    ) as client:
        with client.stream("GET", url, headers=headers) as resp:
            final = str(resp.url)
            if not _is_http_url(final):
                raise FetchError(
                    f"redirect to non-http(s) refused: {urlparse(final).scheme or 'no scheme'}"
                )
            data, truncated = _cap_bytes(resp.iter_bytes(chunk_size=8192))
            hdrs = _headers_from_pairs(resp.headers.items())
            ctype = hdrs.get("content-type") or "unknown"
            body = _decode_body(data, ctype)
            return FetchResult(
                url=url,
                final_url=final,
                status=int(resp.status_code),
                headers=hdrs,
                body=body,
                engine="httpx",
                truncated=truncated,
            )


def _fetch_curl_cffi(url: str, timeout: int) -> FetchResult:
    from curl_cffi import requests as cffi  # type: ignore

    with cffi.Session(impersonate="chrome") as session:
        resp = session.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            max_redirects=10,
            stream=True,
        )
        try:
            final = str(resp.url)
            if not _is_http_url(final):
                raise FetchError(
                    f"redirect to non-http(s) refused: {urlparse(final).scheme or 'no scheme'}"
                )
            data, truncated = _cap_bytes(resp.iter_content(8192) or [])
            hdrs = _headers_from_pairs(dict(resp.headers).items())
            ctype = hdrs.get("content-type") or "unknown"
            body = _decode_body(data, ctype)
            return FetchResult(
                url=url,
                final_url=final,
                status=int(resp.status_code),
                headers=hdrs,
                body=body,
                engine="curl_cffi",
                truncated=truncated,
            )
        finally:
            try:
                resp.close()
            except Exception:
                pass


def _engine_funcs() -> list[tuple[str, object]]:
    engines: list[tuple[str, object]] = []
    try:
        from curl_cffi import requests as _cffi  # noqa: F401
        engines.append(("curl_cffi", _fetch_curl_cffi))
    except Exception:
        pass
    try:
        import httpx  # noqa: F401
        engines.append(("httpx", _fetch_httpx))
    except Exception:
        pass
    engines.append(("urllib", _fetch_urllib))
    return engines


def http_get(url: str, timeout: int) -> FetchResult:
    """GET url with the best installed engine. Transport failures become FetchError.

    HTTP error statuses (404, 500, …) are returned as FetchResult, not raised.
    Engines are not tried in sequence on timeout — that would multiply the wait.
    """
    _name, fn = _engine_funcs()[0]
    try:
        return fn(url, timeout)  # type: ignore[operator]
    except FetchError:
        raise
    except Exception as e:
        text = str(e).lower()
        if isinstance(e, TimeoutError) or "timed out" in text or "timeout" in text:
            raise FetchError(f"fetch timed out after {timeout}s") from e
        raise FetchError(f"fetch failed: {e}") from e


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "ul", "ol", "table", "tr", "blockquote", "pre", "form", "nav", "figure",
    "br", "hr",
}


class _TextExtractor(HTMLParser):
    def __init__(self, base_url: str, markdown: bool):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.markdown = markdown
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip = 0
        self._in_title = False
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = {str(k).lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            href = attrs_d.get("href", "").strip()
            if href and not href.startswith(("javascript:", "#")):
                self._link_href = urljoin(self.base_url, href)
                self._link_text = []
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- " if self.markdown else "\n• ")
        elif len(tag) == 2 and tag[0] == "h" and tag[1].isdigit():
            level = int(tag[1])
            self.parts.append("\n\n" + ("#" * level + " " if self.markdown else ""))
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link_href:
            text = " ".join("".join(self._link_text).split())
            self.links.append((text[:120], self._link_href))
            if self.markdown and text:
                self.parts.append(f"[{text}]({self._link_href})")
            else:
                self.parts.append(text)
            self._link_href = None
            self._link_text = []
        elif tag in _BLOCK_TAGS or (len(tag) == 2 and tag[0] == "h" and tag[1].isdigit()):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        if self._link_href is not None:
            self._link_text.append(data)
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = unescape("".join(self.parts))
        lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in raw.splitlines()]
        out: list[str] = []
        blank = False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif out and not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def _looks_html(body: str, content_type: str) -> bool:
    if "html" in content_type.lower():
        return True
    head = body.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def extract_page(
    body: str,
    *,
    content_type: str,
    final_url: str,
    mode: str,
) -> dict[str, object]:
    """Turn a stored body into model-facing text. mode: text/markdown/links/raw."""
    html = _looks_html(body, content_type)
    if mode == "raw" or not html:
        return {
            "content": body,
            "kind": mode,
            "title": "",
            "link_count": 0,
        }

    parser = _TextExtractor(final_url, markdown=(mode == "markdown"))
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        pass
    title = " ".join(parser.title.split())
    if mode == "links":
        seen: set[str] = set()
        lines: list[str] = []
        for text, href in parser.links:
            if href in seen:
                continue
            seen.add(href)
            label = text or href
            lines.append(f"{label} {href}")
            if len(lines) >= 200:
                break
        return {
            "content": "\n".join(lines),
            "kind": "links",
            "title": title,
            "link_count": len(lines),
        }
    return {
        "content": parser.text(),
        "kind": mode,
        "title": title,
        "link_count": len(parser.links),
    }
