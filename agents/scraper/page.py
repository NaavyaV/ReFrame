"""Public page reading for the scraper, used for "where do I click?" help.

Plain httpx with robots.txt respected. Product search goes through Apify (see
flow.py); this only reads one public page a person asked about. Every address,
including each redirect, must be a public http(s) host on a standard port, so a
message can't make the scraper fetch localhost or a private network.
Keeps only what a reader would see: title, headings, links and visible text.
Scripts, styles and form values are dropped, and the text is treated as untrusted.
"""

from __future__ import annotations

import re
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
    link_where: list[str] = field(default_factory=list)  # where each link sits, same order as links
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (label, where)
    fields: list[str] = field(default_factory=list)
    popups: list[tuple[str, str]] = field(default_factory=list)  # (title, button that closes it)
    text: str = ""


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
GROUP_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "strong", "legend", "dt", "caption"}
DISMISS = re.compile(r"^(?:no,? thanks|no thank you|not now|maybe later|later|close|dismiss|skip|cancel|×|x)$", re.I)


def _words(parts: list[str]) -> str:
    return " ".join(" ".join(parts).split())


class _Area:
    """A part of the page a person can find by eye: the top menu, a column, a pop-up."""

    def __init__(self, name: str, label: str = "") -> None:
        self.name = name
        self.group = ""  # the last heading or bold label seen in it
        self.title = ""  # its first heading
        self.label = label  # aria-label, used when there's no heading
        self.buttons: list[str] = []

    def where(self) -> str:
        return f"{self.name}, under “{self.group}”" if self.group else self.name


class _Reader(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.skip = 0
        self.in_title = False
        self.heading: list[str] | None = None
        self.link: tuple[str, list[str], str] | None = None
        self.group: list[str] | None = None
        self.button: list[str] | None = None
        self.label: list[str] | None = None
        self.page_area = _Area("the page")
        self.stack: list[tuple[str, _Area]] = []
        self.dialogs: list[_Area] = []
        self.seen_main = False
        self.title: list[str] = []
        self.headings: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.link_where: list[str] = []
        self.buttons: list[tuple[str, str]] = []
        self.fields: list[str] = []
        self.text: list[str] = []

    def area(self) -> _Area:
        return self.stack[-1][1] if self.stack else self.page_area

    def _new_area(self, tag: str, attrs: dict) -> _Area | None:
        role = (attrs.get("role") or "").lower()
        if tag == "dialog" or role in {"dialog", "alertdialog"}:
            dialog = _Area("a pop-up", clean_text(attrs.get("aria-label"), 80))
            self.dialogs.append(dialog)
            return dialog
        if tag in {"header", "nav"} or role in {"banner", "navigation"}:
            return _Area("the menu at the top")
        if tag == "footer" or role == "contentinfo":
            return _Area("the bottom of the page")
        if tag == "aside":
            return _Area("the right column" if self.seen_main else "the left column")
        if tag == "main" or role == "main":
            self.seen_main = True
            return _Area("the middle of the page")
        return None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in SKIP_TAGS:
            self.skip += 1
        if tag not in VOID_TAGS:
            self.stack.append((tag, self._new_area(tag, attrs) or self.area()))
        if tag == "title":
            self.in_title = True
        elif tag in {"h1", "h2", "h3"}:
            self.heading = []
        if tag in GROUP_TAGS and self.link is None:
            self.group = []
        if tag == "a":
            href = attrs.get("href") or ""
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.link = (urljoin(self.base, href).split("#", 1)[0], [], self.area().where())
        elif tag == "button":
            self.button = []
        elif tag == "label":
            self.label = []
        elif tag == "input":
            kind = (attrs.get("type") or "text").lower()
            if kind in {"submit", "button"} and attrs.get("value"):
                self._add_button(clean_text(attrs["value"], 60))
            elif kind != "hidden" and attrs.get("aria-label"):
                self.fields.append(clean_text(attrs["aria-label"], 60))

    def _add_button(self, label: str) -> None:
        if label:
            self.buttons.append((label, self.area().where()))
            self.area().buttons.append(label)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in {"h1", "h2", "h3"} and self.heading is not None:
            words = _words(self.heading)
            if words:
                self.headings.append(words)
            self.heading = None
        if tag in GROUP_TAGS and self.group is not None:
            words = _words(self.group)
            if words and len(words) <= 60:
                self.area().group = words
                if not self.area().title:
                    self.area().title = words
            self.group = None
        if tag == "a" and self.link is not None:
            words = _words(self.link[1])
            if words and len(self.links) < MAX_LINKS:
                self.links.append((words[:80], self.link[0]))
                self.link_where.append(self.link[2])
            self.link = None
        elif tag == "button" and self.button is not None:
            self._add_button(clean_text(_words(self.button), 60))
            self.button = None
        elif tag == "label" and self.label is not None:
            words = _words(self.label)
            if words:
                self.fields.append(clean_text(words, 60))
            self.label = None
        if any(open_tag == tag for open_tag, _ in self.stack):
            while self.stack and self.stack.pop()[0] != tag:
                pass

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title.append(data)
            return
        for parts in (self.heading, self.group, self.button, self.label):
            if parts is not None:
                parts.append(data)
        if self.link is not None:
            self.link[1].append(data)
        if data.strip():
            self.text.append(data.strip())

    def popups(self) -> list[tuple[str, str]]:
        found = []
        for dialog in self.dialogs:
            dismiss = next((label for label in dialog.buttons if DISMISS.match(label)), "")
            found.append((dialog.title or dialog.label or "a pop-up", dismiss))
        return found


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

    return read_html(final_url, body)


def read_html(url: str, body: str) -> Page:
    reader = _Reader(url)
    reader.feed(body)
    return Page(
        url=url.split("?", 1)[0],
        title=clean_text(" ".join(reader.title), 200),
        headings=[clean_text(heading, 120) for heading in reader.headings[:30]],
        links=[(clean_text(label, 80), href) for label, href in reader.links],
        link_where=reader.link_where,
        buttons=reader.buttons[:30],
        fields=reader.fields[:20],
        popups=reader.popups()[:3],
        text=clean_text(" ".join(reader.text), MAX_TEXT),
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
