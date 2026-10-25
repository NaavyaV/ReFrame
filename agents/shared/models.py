"""Typed messages the agents send each other.

Orchestrator -> technical helper (hmh-shopping):
    ProductSearchTask -> TaskAck, later RecommendationResponse
    TechHelpTask      -> TaskAck, later TechHelpResponse
Technical helper -> scraper (hmh-scrape):
    ScrapeRequest -> TaskAck, later ScrapeResponse
    PageRequest   -> TaskAck, later PageResponse
Orchestrator <-> caregiver (hmh-caregiver):
    CaregiverInviteRequest -> CaregiverInviteResult
    PreferenceQuery        -> PreferenceSnapshot
    UserRevokeRequest      -> PreferenceSnapshot
    caregiver -> orchestrator: CaregiverPreferenceResult when a caregiver changes a setting
A caregiver's own agent can send CaregiverPreferenceProposal to the caregiver agent.
People talk to the agents with the Agent Chat Protocol.

Every message names its task or request, the sending and receiving roles and when it
was sent. Receivers check the sender's address, not the role field, before trusting it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from uagents import Model

SHOPPING_PROTOCOL = ("hmh-shopping", "1.0.0")
SCRAPE_PROTOCOL = ("hmh-scrape", "1.0.0")
CAREGIVER_PROTOCOL = ("hmh-caregiver", "1.0.0")

# Shopping sites the scraper has an Apify Actor for (see scraper/actors.py).
APPROVED_SITES = {"amazon.com": "Amazon.com"}

ORCHESTRATOR = "orchestrator"
TECHNICAL_HELPER = "technical_helper"
SCRAPER = "scraper"
CAREGIVER = "caregiver"

# Terminal task states. A task in one of these never changes again.
SELECTED = "selected"
NO_MATCH = "no_match"
FAILED = "failed"
TIMED_OUT = "timed_out"
CANCELLED = "cancelled"
TERMINAL = frozenset({SELECTED, NO_MATCH, FAILED, TIMED_OUT, CANCELLED})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


class ErrorInfo(Model):
    code: str
    message: str
    retryable: bool = False


class TaskAck(Model):
    task_id: str
    request_id: str = ""
    sender_role: str
    recipient_role: str
    sent_at: str
    status: str  # "accepted", "duplicate" or "rejected"
    error: ErrorInfo | None = None


# Shopping


class HardConstraints(Model):
    category: str
    max_price: float | None = None
    min_price: float | None = None
    currency: str = "USD"
    required_features: list[str] = []
    exclude_urls: list[str] = []


class SoftPreferences(Model):
    features: list[str] = []
    intended_use: str = ""
    prefer_in_stock: bool = True
    prefer_well_reviewed: bool = True


class UserContext(Model):
    """Only what the helper needs. No names, addresses or contact details."""

    language: str = "en"
    highlight_clear_features: bool = False


class ProductSearchTask(Model):
    task_id: str
    sender_role: str = ORCHESTRATOR
    recipient_role: str = TECHNICAL_HELPER
    sent_at: str
    query: str
    hard_constraints: HardConstraints
    soft_preferences: SoftPreferences = SoftPreferences()
    source_site: str = "amazon.com"
    user_context: UserContext = UserContext()
    reply_to: str


class ScrapeFilters(Model):
    max_price: float | None = None
    min_price: float | None = None
    currency: str = "USD"
    must_include_any: list[str] = []
    exclude_urls: list[str] = []


class ScrapeRequest(Model):
    task_id: str
    request_id: str
    sender_role: str = TECHNICAL_HELPER
    recipient_role: str = SCRAPER
    sent_at: str
    search_query: str
    site: str
    filters: ScrapeFilters = ScrapeFilters()
    max_items: int = 3
    retry_number: int = 0


class ProductCandidate(Model):
    id: str
    title: str
    supplier: str = ""
    seller: str = ""
    price: float | None = None
    currency: str = ""
    url: str
    availability: str = ""  # "in_stock", "out_of_stock" or "" when the listing didn't say
    availability_text: str = ""
    rating: float | None = None
    review_count: int | None = None
    features: list[str] = []
    attributes: dict[str, str] = {}
    image_url: str = ""
    sponsored: bool = False
    source: str
    source_actor: str = ""
    collected_at: str
    missing_data: list[str] = []


class SearchMetadata(Model):
    query: str
    site: str
    actor_id: str = ""
    run_id: str = ""
    dataset_id: str = ""
    items_seen: int = 0
    items_dropped: int = 0
    cached: bool = False
    duration_seconds: float = 0.0


class ScrapeResponse(Model):
    task_id: str
    request_id: str
    sender_role: str = SCRAPER
    recipient_role: str = TECHNICAL_HELPER
    sent_at: str
    status: str  # "ok", "empty" or "error"
    candidates: list[ProductCandidate] = []
    search: SearchMetadata
    warnings: list[str] = []
    error: ErrorInfo | None = None


class RecommendationResponse(Model):
    task_id: str
    sender_role: str = TECHNICAL_HELPER
    recipient_role: str = ORCHESTRATOR
    sent_at: str
    status: str  # SELECTED, NO_MATCH or FAILED
    selected_candidate: ProductCandidate | None = None
    rationale: str = ""
    constraints_met: list[str] = []
    constraints_unverified: list[str] = []
    alternatives_considered_count: int = 0
    attempts: int = 0
    caveats: list[str] = []
    error: ErrorInfo | None = None


# Technical help


class TechHelpPrefs(Model):
    short: bool = False
    simple_wording: bool = False
    confirm_each_step: bool = False


class TechHelpTask(Model):
    task_id: str
    sender_role: str = ORCHESTRATOR
    recipient_role: str = TECHNICAL_HELPER
    sent_at: str
    conversation_ref: str  # pseudonymous, so the helper can keep the thread
    question: str
    page_url: str = ""
    prefs: TechHelpPrefs = TechHelpPrefs()
    reply_to: str


class TechHelpResponse(Model):
    task_id: str
    sender_role: str = TECHNICAL_HELPER
    recipient_role: str = ORCHESTRATOR
    sent_at: str
    status: str  # "answered" or FAILED
    answer: str = ""
    sources: list[str] = []
    error: ErrorInfo | None = None


class PageRequest(Model):
    task_id: str
    request_id: str
    sender_role: str = TECHNICAL_HELPER
    recipient_role: str = SCRAPER
    sent_at: str
    url: str


class PageLink(Model):
    label: str
    url: str


class PageResponse(Model):
    task_id: str
    request_id: str
    sender_role: str = SCRAPER
    recipient_role: str = TECHNICAL_HELPER
    sent_at: str
    status: str  # "ok" or "error"
    url: str = ""
    title: str = ""
    headings: list[str] = []
    links: list[PageLink] = []
    text: str = ""
    collected_at: str = ""
    error: ErrorInfo | None = None


# Caregiver


class CaregiverInviteRequest(Model):
    request_id: str
    sender_role: str = ORCHESTRATOR
    recipient_role: str = CAREGIVER
    sent_at: str
    user_ref: str
    scopes: list[str]
    approval: str  # how the person approved, e.g. "confirmed YES in chat"


class CaregiverInviteResult(Model):
    request_id: str
    sender_role: str = CAREGIVER
    recipient_role: str = ORCHESTRATOR
    sent_at: str
    user_ref: str
    status: str  # "created", "denied" or "error"
    invite_code: str = ""
    expires_at: str = ""
    scopes: list[str] = []
    error: ErrorInfo | None = None


class PreferenceQuery(Model):
    request_id: str
    sender_role: str = ORCHESTRATOR
    recipient_role: str = CAREGIVER
    sent_at: str
    user_ref: str


class UserRevokeRequest(Model):
    """The person, through the orchestrator, undoes caregiver changes."""

    request_id: str
    sender_role: str = ORCHESTRATOR
    recipient_role: str = CAREGIVER
    sent_at: str
    user_ref: str
    setting: str = ""  # empty means every caregiver-set preference
    revoke_access: bool = False
    approval: str


class PreferenceSnapshot(Model):
    request_id: str
    sender_role: str = CAREGIVER
    recipient_role: str = ORCHESTRATOR
    sent_at: str
    user_ref: str
    status: str  # "ok", "unavailable" or "error"
    preferences: dict[str, str] = {}
    set_by: dict[str, str] = {}  # setting -> shortened caregiver address
    caregivers: list[str] = []  # shortened addresses with active access
    error: ErrorInfo | None = None


class CaregiverPreferenceProposal(Model):
    proposal_id: str
    sender_role: str = CAREGIVER
    recipient_role: str = CAREGIVER
    sent_at: str
    caregiver_id: str  # must equal the sender address
    user_ref: str
    setting: str
    proposed_value: str


class CaregiverPreferenceResult(Model):
    proposal_id: str
    sender_role: str = CAREGIVER
    recipient_role: str
    sent_at: str
    caregiver_id: str
    user_ref: str
    setting: str
    old_value: str = ""
    new_value: str = ""
    scope: list[str] = []
    approval: str = ""
    status: str  # "applied", "cleared", "rejected", "needs_user_approval" or "error"
    error: ErrorInfo | None = None
