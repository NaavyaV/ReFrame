"""Public page fetching for the scraper.

Plain httpx with robots.txt respected. Keeps only what a reader would see:
title, headings, links and visible text. Scripts, styles and form values are dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

USER_AGENT = "ReFrameScraper/0.1"
MAX_BYTES = 2_000_000
MAX_TEXT = 6000
MAX_LINKS = 60
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe"}
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)


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


def find_url(text: str) -> str | None:
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;:!?")
    bare = re.search(r"\b((?:[a-z0-9-]+\.)+[a-z]{2,})(/\S*)?", text, re.I)
    if bare:
        return "https://" + bare.group(0).rstrip(".,;:!?")
    return None


def _allowed(client: httpx.Client, url: str) -> bool:
    parts = urlsplit(url)
    robots = RobotFileParser()
    try:
        response = client.get(f"{parts.scheme}://{parts.netloc}/robots.txt")
    except httpx.HTTPError:
        return True
    if response.status_code >= 400:
        return True
    robots.parse(response.text.splitlines())
    return robots.can_fetch(USER_AGENT, url)


def fetch(url: str) -> Page:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ScrapeError("That doesn't look like a web address.")
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=20.0,
    ) as client:
        if not _allowed(client, url):
            raise ScrapeError("That site asks not to be read by tools like me.")
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            raise ScrapeError(f"I couldn't open that page. {exc}") from exc
    if response.status_code >= 400:
        raise ScrapeError(f"The site answered with error {response.status_code}.")
    if "html" not in response.headers.get("content-type", "html"):
        raise ScrapeError("That address isn't a web page I can read.")

    reader = _Reader(str(response.url))
    reader.feed(response.text[:MAX_BYTES])
    text = " ".join(reader.text)
    return Page(
        url=str(response.url).split("?", 1)[0],
        title=" ".join(" ".join(reader.title).split()),
        headings=reader.headings[:30],
        links=reader.links,
        text=text[:MAX_TEXT],
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
