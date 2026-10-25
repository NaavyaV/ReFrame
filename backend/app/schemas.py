import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, field_validator

REF_PATTERN = r"^r\d{1,5}$"
Ref = Annotated[str, Field(pattern=REF_PATTERN)]


class SnapElement(BaseModel):
    ref: Ref
    kind: Literal["control", "block"]
    tag: str = Field(max_length=20)
    role: str | None = Field(default=None, max_length=30)
    text: str = Field(default="", max_length=200)
    label: str | None = Field(default=None, max_length=120)
    context: str | None = Field(default=None, max_length=160)
    href: str | None = Field(default=None, max_length=300)
    input_type: str | None = Field(default=None, max_length=30)
    cls: str | None = Field(default=None, max_length=80)
    sensitive: bool = False
    region: str | None = Field(default=None, max_length=20)
    hidden: bool = False


class Snapshot(BaseModel):
    url: str = Field(max_length=500)
    title: str = Field(default="", max_length=200)
    headings: list[str] = Field(default_factory=list, max_length=40)
    has_dialog: bool = False
    theme: dict[str, Any] = Field(default_factory=dict)
    elements: list[SnapElement] = Field(max_length=500)

    @field_validator("theme")
    @classmethod
    def _small_theme(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(json.dumps(v)) > 4000:
            raise ValueError("theme too large")
        return v


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=2000)


class ReframeRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    snapshot: Snapshot
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)
    applied: list[str] = Field(default_factory=list, max_length=20)
    continuation: bool = False
    spoken: bool = False


# Operations the extension knows how to apply. Anything else is dropped.

ALLOWED_STYLE_PROPS = {
    "color", "background-color", "font-size", "font-weight", "font-family", "line-height",
    "letter-spacing", "word-spacing", "text-align", "text-decoration", "text-transform",
    "padding", "margin", "border", "border-radius", "outline", "box-shadow",
    "width", "max-width", "min-width", "height", "min-height", "max-height",
    "display", "opacity", "gap", "cursor",
}
_BAD_STYLE_VALUE = re.compile(r"url\(|expression|javascript:|@|[;{}<>\\]|/\*", re.I)


class RenderOp(BaseModel):
    op: Literal["render"]
    html: str = Field(min_length=1, max_length=16000)
    css: str = Field(default="", max_length=8000)
    anchor_ref: Ref | None = None
    position: Literal["before", "after", "prepend", "append"] = "prepend"
    isolate: bool = True
    keep_refs: list[Ref] = Field(default_factory=list, max_length=20)


class RefsOp(BaseModel):
    op: Literal["hide", "unhide", "highlight"]
    refs: list[Ref] = Field(min_length=1, max_length=60)


class RelabelOp(BaseModel):
    op: Literal["relabel"]
    ref: Ref
    text: str = Field(min_length=1, max_length=80)


class StyleOp(BaseModel):
    op: Literal["style"]
    ref: Ref
    styles: dict[str, str] = Field(min_length=1, max_length=12)

    @field_validator("styles")
    @classmethod
    def _safe_styles(cls, v: dict[str, str]) -> dict[str, str]:
        clean = {}
        for prop, value in v.items():
            prop = prop.strip().lower()
            value = str(value).strip()
            if prop in ALLOWED_STYLE_PROPS and len(value) <= 80 and not _BAD_STYLE_VALUE.search(value):
                clean[prop] = value
        if not clean:
            raise ValueError("no allowed styles")
        return clean


class PageStyleOp(BaseModel):
    op: Literal["page_style"]
    font_scale: float | None = Field(default=None, ge=0.8, le=2.5)
    line_height: float | None = Field(default=None, ge=1.0, le=2.5)
    high_contrast: bool | None = None
    dark_mode: bool | None = None
    hide_images: bool | None = None
    reduce_motion: bool | None = None
    readable_font: bool | None = None
    underline_links: bool | None = None


class ScrollOp(BaseModel):
    op: Literal["scroll_to"]
    ref: Ref


class RestoreOp(BaseModel):
    op: Literal["restore"]


Operation = Annotated[
    Union[RenderOp, RefsOp, RelabelOp, StyleOp, PageStyleOp, ScrollOp, RestoreOp],
    Field(discriminator="op"),
]


class ReframeResponse(BaseModel):
    reply: str
    operations: list[Operation]
    dropped: int = 0
    done: bool = False
    model_ms: int | None = None
