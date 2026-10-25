"""Public page reading for the scraper, used for "where do I click?" help.

Plain httpx with robots.txt respected. Product search goes through Apify (see
flow.py); this only reads one public page a person asked about. Every address,
including each redirect, must be a public http(s) host on a standard port, so a
message can't make the scraper fetch localhost or a private network.
Keeps only what a reader would see: title, headings, links and visible text.
Scripts, styles and form values are dropped, and the text is treated as untrusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from shared.security import UnsafeURL, check_public_url, clean_text, find_url  # noqa: F401

USER_AGENT = "HoldMyHandScraper/0.2"
MAX_BYTES = 2_000_000
MAX_TEXT = 6000
MAX_LINKS = 60
MAX_REDIRECTS = 4
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}


class ScrapeError(Exception):
    pass


@dataclass
class Page:
    url: str
    title: str = ""
    headings: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)
    text: str = ""


class _Reader(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip = 0
        self.in_title = False
        self.heading: list[str] | None = None
        self.link: tuple[str, list[str]] | None = None
        self.title: list[str] = []
        self.headings: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
        elif tag == "title":
            self.in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self.heading = []
        elif tag == "a":
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.link = (urljoin(self.base, href).split("#", 1)[0], [])

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in {"h1", "h2", "h3"} and self.heading is not None:
            words = " ".join(" ".join(self.heading).split())
            if words:
                self.headings.append(words)
            self.heading = None
        elif tag == "a" and self.link is not None:
            words = " ".join(" ".join(self.link[1]).split())
            if words and len(self.links) < MAX_LINKS:
                self.links.append((words[:80], self.link[0]))
            self.link = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title.append(data)
            return
        if self.heading is not None:
            self.heading.append(data)
        if self.link is not None:
            self.link[1].append(data)
        if data.strip():
            self.text.append(data.strip())


def _get(client: httpx.Client, url: str, limit: int = MAX_BYTES) -> tuple[httpx.Response, str, str]:
    """GET a public URL, checking every redirect hop. Returns (response, final url, body text)."""
    for _ in range(MAX_REDIRECTS + 1):
        try:
            check_public_url(url)
        except UnsafeURL as exc:
            raise ScrapeError(str(exc)) from exc
        try:
            with client.stream("GET", url) as response:
                if response.is_redirect and response.headers.get("location"):
                    url = urljoin(url, response.headers["location"])
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= limit:
                        break
                text = bytes(body[:limit]).decode(response.encoding or "utf-8", errors="replace")
                return response, url, text
        except httpx.HTTPError as exc:
            raise ScrapeError(f"I couldn't open that page ({exc.__class__.__name__}).") from exc
    raise ScrapeError("That page sends me on too many redirects.")


def _allowed(client: httpx.Client, url: str) -> bool:
    parts = urlsplit(url)
    robots = RobotFileParser()
    try:
        response, _, text = _get(client, f"{parts.scheme}://{parts.netloc}/robots.txt", 200_000)
    except ScrapeError:
        return True
    if response.status_code >= 400:
        return True
    robots.parse(text.splitlines())
    return robots.can_fetch(USER_AGENT, url)


def fetch(url: str) -> Page:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ScrapeError("That doesn't look like a web address.")
    with httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=False, timeout=20.0) as client:
        if not _allowed(client, url):
            raise ScrapeError("That site asks not to be read by tools like me.")
        response, final_url, body = _get(client, url)
    if response.status_code >= 400:
        raise ScrapeError(f"The site answered with error {response.status_code}.")
    if "html" not in response.headers.get("content-type", "html"):
        raise ScrapeError("That address isn't a web page I can read.")

    reader = _Reader(final_url)
    reader.feed(body)
    text = clean_text(" ".join(reader.text), MAX_TEXT)
    return Page(
        url=final_url.split("?", 1)[0],
        title=clean_text(" ".join(reader.title), 200),
        headings=[clean_text(heading, 120) for heading in reader.headings[:30]],
        links=[(clean_text(label, 80), href) for label, href in reader.links],
        text=text,
    )


def describe(page: Page) -> str:
    links = "\n".join(f"- {label} -> {urlsplit(href).path or '/'}" for label, href in page.links)
    headings = "\n".join(f"- {heading}" for heading in page.headings) or "None"
    return (
        f"URL: {page.url}\n"
        f"Title: {page.title or 'None'}\n"
        f"Headings:\n{headings}\n"
        f"Links:\n{links or 'None'}\n"
        f"Text:\n{page.text or 'None'}"
    )
