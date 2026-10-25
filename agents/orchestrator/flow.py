"""The orchestrator's conversations and tasks, without the uAgents plumbing.

A person's message becomes one of:
  - a product search: a ProductSearchTask for the technical helper, one at a time per person
  - a tech-help question: a TechHelpTask for the technical helper
  - a caregiver request: an invite, a look at their settings, or undoing changes, each
    confirmed with a yes before anything is sent to the caregiver agent
  - a clarifying question back, when the request isn't clear yet

Every task has an id, is acknowledged by the helper within ACK_TIMEOUT_SECONDS, and
ends selected, no_match, failed, timed_out or cancelled. Answers for a finished task
are ignored, so a duplicate can never produce a second reply. Methods return Say and
Send actions; agent.py carries them out.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from uagents import Model

from shared.models import (
    CANCELLED,
    FAILED,
    NO_MATCH,
    SELECTED,
    TERMINAL,
    TIMED_OUT,
    CaregiverInviteRequest,
    CaregiverInviteResult,
    CaregiverPreferenceResult,
    HardConstraints,
    PreferenceQuery,
    PreferenceSnapshot,
    ProductSearchTask,
    RecommendationResponse,
    SoftPreferences,
    TaskAck,
    TechHelpPrefs,
    TechHelpResponse,
    TechHelpTask,
    UserContext,
    UserRevokeRequest,
    new_id,
    now_iso,
)
from shared.preferences import SETTINGS, defaults, describe, parse_scopes, sanitize, valid
from shared.security import clean_text, mask_personal, short_address, user_ref

from orchestrator import intake, replies
from orchestrator.tasklog import TaskLog

ACK_TIMEOUT_SECONDS = 30
PRODUCT_TIMEOUT_SECONDS = 420
HELP_TIMEOUT_SECONDS = 120
PROGRESS_AFTER_SECONDS = 75
CAREGIVER_TIMEOUT_SECONDS = 20
PREFS_REFRESH_SECONDS = 600
MAX_SESSIONS = 2000

RESET = {"start over", "reset", "new problem", "begin again"}
YES = re.compile(r"^\s*(?:yes|y|yeah|yep|ok|okay|sure|please do|go ahead|confirm|do it|correct|that'?s right)\b", re.I)
NO = re.compile(r"^\s*(?:no|n|nope|cancel|stop|don'?t|do not|never ?mind|not now)\b", re.I)
ANOTHER = re.compile(r"\b(?:another|different one|something else|other (?:one|option)|next one|show me more)\b", re.I)
CHEAPER = re.compile(r"\b(?:cheaper|less expensive|lower price)\b", re.I)
CAREGIVER_WORD = r"(?:caregiver|carer|care ?giver|care ?taker)"
INVITE = re.compile(
    rf"\b(?:invite|add|let|allow|link|connect|set ?up)\b.*\b{CAREGIVER_WORD}\b|\b{CAREGIVER_WORD}\b.*\b(?:invite|code|link|connect|set ?up|access)\b",
    re.I,
)
SHOW_SETTINGS = re.compile(rf"\b(?:{CAREGIVER_WORD}|my) settings\b|\bshow (?:me )?my settings\b|\bwhat (?:did|has) my {CAREGIVER_WORD}\b", re.I)
UNDO_CHANGES = re.compile(rf"\b(?:undo|reset|clear)\b.*\b{CAREGIVER_WORD}\b|\b{CAREGIVER_WORD}\b.*\b(?:undo|reset)\b", re.I)
REMOVE_CAREGIVER = re.compile(rf"\b(?:remove|revoke|disconnect|unlink|block|stop)\b.*\b{CAREGIVER_WORD}\b", re.I)
SCOPE_MENU = "\n".join(f"{index}. {setting.label[0].upper()}{setting.label[1:]}"
                       for index, setting in enumerate(SETTINGS.values(), start=1))


@dataclass
class Say:
    session: str
    text: str
    end_session: bool = False


@dataclass
class Send:
    to: str
    message: Model


Action = Say | Send


@dataclass
class Pending:
    kind: str  # "product", "kind", "confirm_search", "invite_scopes", "invite_confirm", "revoke_confirm"
    text: str = ""
    search: ProductSearchTask | None = None
    scopes: list[str] = field(default_factory=list)
    revoke_access: bool = False
    asked: int = 0


@dataclass
class Session:
    key: str
    user: str
    mode: str = ""  # "tech_help" while a problem is being worked through
    pending: Pending | None = None
    task_id: str = ""
    last_search: ProductSearchTask | None = None
    last_price: float | None = None
    shown_urls: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    prefs_at: float = -math.inf


@dataclass
class Task:
    task_id: str
    session: str
    user: str
    kind: str  # "product" or "tech_help"
    message: ProductSearchTask | TechHelpTask
    created: float
    status: str = "delegated"
    acked: bool = False
    progress_sent: bool = False
    result: Model | None = None
    reply: str = ""
    finished: float = 0.0

    def summary(self) -> str:
        if isinstance(self.message, ProductSearchTask):
            return replies.describe_search(self.message)
        return "tech help" + (" with a page" if self.message.page_url else "")


@dataclass
class CaregiverCall:
    session: str
    purpose: str  # "invite", "show", "revoke" or "refresh"
    started: float
    revoke_access: bool = False


Understand = Callable[[str, list[str]], Awaitable[intake.Request]]
Translate = Callable[[str, str], Awaitable[str]]


class Orchestrator:
    def __init__(
        self,
        *,
        own_address: str,
        technical_helper: str,
        caregiver: str,
        understand: Understand = intake.understand,
        translate: Translate = replies.translate,
        task_log: TaskLog | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.own_address = own_address
        self.technical_helper = technical_helper
        self.caregiver = caregiver
        self._understand = understand
        self._translate = translate
        self.task_log = task_log or TaskLog()
        self._clock = clock
        self.sessions: dict[str, Session] = {}
        self.tasks: dict[str, Task] = {}
        self.calls: dict[str, CaregiverCall] = {}
        self.prefs: dict[str, dict[str, str]] = {}
        self.set_by: dict[str, dict[str, str]] = {}
        self.notices: dict[str, list[str]] = {}

    # Sessions and preferences

    def session(self, key: str) -> Session:
        session = self.sessions.get(key)
        if session is None:
            if len(self.sessions) >= MAX_SESSIONS:
                idle = [k for k, s in self.sessions.items() if not s.task_id]
                for old in idle[: len(idle) // 2 or 1]:
                    del self.sessions[old]
            identity = key.removeprefix("bridge:")
            session = Session(key, user_ref("app:" + identity if key.startswith("bridge:") else identity))
            self.sessions[key] = session
        return session

    def preferences(self, user: str) -> dict[str, str]:
        merged = defaults()
        merged.update(sanitize(self.prefs.get(user, {})))
        return merged

    def ui_hints(self, user: str) -> dict[str, str]:
        """Display hints for the browser extension. It decides how to apply them."""
        return {"text_size": self.preferences(user)["text_size"]}

    def _refresh_prefs(self, session: Session) -> list[Action]:
        now = self._clock()
        if now - session.prefs_at < PREFS_REFRESH_SECONDS:
            return []
        session.prefs_at = now
        return [self._caregiver_call(session, "refresh", PreferenceQuery(
            request_id=new_id("prefs"), sent_at=now_iso(), user_ref=session.user))]

    def _caregiver_call(self, session: Session, purpose: str, message: Model, revoke_access: bool = False) -> Send:
        self.calls[message.request_id] = CaregiverCall(session.key, purpose, self._clock(), revoke_access)
        return Send(self.caregiver, message)

    async def _render(self, actions: list[Action]) -> list[Action]:
        """Add pending caregiver notices and translate, per the person's own settings."""
        for action in actions:
            if not isinstance(action, Say):
                continue
            session = self.sessions.get(action.session)
            if session is None:
                continue
            notices = self.notices.pop(session.user, [])
            if notices:
                action.text = f"{action.text}\n\n{replies.caregiver_notice(notices)}"
            language = self.preferences(session.user)["language"]
            if language != "en":
                action.text = await self._translate(action.text, language)
        return actions

    # People

    async def on_text(self, key: str, text: str) -> list[Action]:
        session = self.session(key)
        actions = self._refresh_prefs(session)
        actions += await self._on_text(session, clean_text(text, 1500))
        return await self._render(actions)

    async def _on_text(self, session: Session, text: str) -> list[Action]:
        lowered = text.lower().strip(" .!")
        if lowered in RESET:
            return self._reset(session)
        if session.pending is not None:
            handled = await self._on_pending(session, text)
            if handled is not None:
                return handled
        if SHOW_SETTINGS.search(text):
            return [self._caregiver_call(session, "show", PreferenceQuery(
                request_id=new_id("prefs"), sent_at=now_iso(), user_ref=session.user))]
        if UNDO_CHANGES.search(text) or REMOVE_CAREGIVER.search(text):
            remove = bool(REMOVE_CAREGIVER.search(text))
            session.pending = Pending("revoke_confirm", revoke_access=remove)
            question = ("Remove your caregiver's access and undo the changes they made?" if remove
                        else "Undo all the setting changes your caregiver made?")
            return [Say(session.key, f"{question} Please reply yes or no.")]
        if INVITE.search(text):
            session.pending = Pending("invite_scopes")
            return [Say(session.key, (
                "A caregiver can adjust a few things about how I talk with you. They can't buy anything, read our "
                "conversations, or change what I recommend, and you can undo their changes at any time.\n\n"
                f"Which of these may they change?\n{SCOPE_MENU}\n\n"
                "Reply with the numbers (for example \"1, 2 and 4\"), \"all\", or \"cancel\"."
            ))]

        active = self.tasks.get(session.task_id)
        if active is not None and active.status not in TERMINAL:
            if lowered in {"cancel", "stop"}:
                return await self._end(active, CANCELLED, "Okay, I've stopped. What would you like to do instead?")
            return [Say(session.key, f"I'm still working on {active.summary()}. I'll message you as soon as I have "
                                     "it. Say \"cancel\" to stop.")]
        if lowered in {"help", "what can you do", "what do you do", "?"}:
            return [Say(session.key, replies.help_text())]
        if session.last_search is not None and (ANOTHER.search(text) or CHEAPER.search(text)):
            return await self._search_again(session, cheaper=bool(CHEAPER.search(text)))

        request = await self._understand(text, session.history)
        session.history = [*session.history, text][-6:]
        return await self._route(session, request)

    async def _route(self, session: Session, request: intake.Request) -> list[Action]:
        if request.kind == "unclear" and session.mode == "tech_help":
            request.kind = "tech_help"
        if request.kind == "chat":
            return [Say(session.key, "Hello! " + replies.help_text())]
        if request.kind == "unclear":
            session.pending = Pending("kind", text=request.text)
            return [Say(session.key, "Are you looking for something to buy, or do you need help with a device, "
                                     "app or website?")]
        if request.kind == "tech_help":
            return await self._start_help(session, request)
        if not request.category:
            session.pending = Pending("product", text=request.text)
            return [Say(session.key, "What would you like me to find? For example: a vacuum, walking shoes or a "
                                     "phone.")]
        search = self._build_search(session, request)
        actions: list[Action] = []
        if request.other_shop:
            actions.append(Say(session.key, replies.other_shop(request.other_shop)))
        if self.preferences(session.user)["confirm_complex_steps"] == "on":
            session.pending = Pending("confirm_search", search=search)
            return actions + [Say(session.key, replies.confirm_search(search))]
        return actions + await self._start_search(session, search)

    async def _on_pending(self, session: Session, text: str) -> list[Action] | None:
        """Handle a reply to our last question, or None to treat it as a new message."""
        pending = session.pending
        assert pending is not None
        session.pending = None
        if pending.kind == "confirm_search" and pending.search is not None:
            if YES.search(text):
                return await self._start_search(session, pending.search)
            if NO.search(text):
                return [Say(session.key, "Okay, I won't search. What would you like to change?")]
            return None
        if pending.kind == "product":
            if NO.search(text):
                return [Say(session.key, "Okay. Let me know if there's anything else.")]
            request = await self._understand(f"{pending.text}. Find {text}", session.history)
            request.kind = "shopping"
            if not request.category and pending.asked < 1:
                session.pending = Pending("product", text=pending.text, asked=pending.asked + 1)
                return [Say(session.key, "Sorry, I didn't catch the kind of product. Could you name it in a word or "
                                         "two, like \"vacuum\" or \"reading lamp\"?")]
            return await self._route(session, request) if request.category else [Say(session.key, replies.help_text())]
        if pending.kind == "kind":
            if re.search(r"\b(?:buy|shop|shopping|purchase|find|product)\b", text, re.I):
                request = intake.parse(pending.text)
                request.kind = "shopping"
                return await self._route(session, request)
            if re.search(r"\b(?:help|problem|device|app|website|fix|tech)\b", text, re.I):
                request = intake.parse(pending.text)
                request.kind = "tech_help"
                return await self._route(session, request)
            return None
        if pending.kind == "invite_scopes":
            if NO.search(text):
                return [Say(session.key, "Okay, no caregiver will be added.")]
            scopes = parse_scopes(text)
            if not scopes:
                if pending.asked < 1:
                    session.pending = Pending("invite_scopes", asked=1)
                    return [Say(session.key, f"Please reply with numbers from this list, or \"all\":\n{SCOPE_MENU}")]
                return [Say(session.key, "Okay, I'll leave that for now.")]
            session.pending = Pending("invite_confirm", scopes=scopes)
            names = "\n".join(f"- {SETTINGS[key].label}" for key in scopes)
            return [Say(session.key, f"Your caregiver will be able to change:\n{names}\n\nNothing else. Shall I "
                                     "create a one-time code to give them? Please reply yes or no.")]
        if pending.kind == "invite_confirm":
            if YES.search(text):
                request = CaregiverInviteRequest(
                    request_id=new_id("invite"), sent_at=now_iso(), user_ref=session.user, scopes=pending.scopes,
                    approval=f"user confirmed yes in chat at {now_iso()}",
                )
                return [self._caregiver_call(session, "invite", request),
                        Say(session.key, "One moment while I make the code.")]
            return [Say(session.key, "Okay, no caregiver will be added.")]
        if pending.kind == "revoke_confirm":
            if YES.search(text):
                request = UserRevokeRequest(
                    request_id=new_id("revoke"), sent_at=now_iso(), user_ref=session.user,
                    revoke_access=pending.revoke_access, approval=f"user confirmed yes in chat at {now_iso()}",
                )
                return [self._caregiver_call(session, "revoke", request, pending.revoke_access)]
            return [Say(session.key, "Okay, nothing has changed.")]
        return None

    def _reset(self, session: Session) -> list[Action]:
        actions: list[Action] = []
        active = self.tasks.get(session.task_id)
        if active is not None and active.status not in TERMINAL:
            active.status = CANCELLED
            active.finished = self._clock()
        self.sessions[session.key] = Session(session.key, session.user, prefs_at=session.prefs_at)
        actions.append(Say(session.key, "Okay, let's start again. What can I help you with?"))
        return actions

    # Product search

    def _build_search(self, session: Session, request: intake.Request) -> ProductSearchTask:
        prefs = self.preferences(session.user)
        return ProductSearchTask(
            task_id=new_id("task"),
            sent_at=now_iso(),
            query="",
            hard_constraints=HardConstraints(
                category=request.category,
                max_price=request.budget.max_price,
                min_price=request.budget.min_price,
                currency=request.budget.currency,
                required_features=request.required,
            ),
            soft_preferences=SoftPreferences(features=request.preferred, intended_use=request.intended_use),
            source_site=request.site,
            user_context=UserContext(language=prefs["language"],
                                     highlight_clear_features=prefs["highlight_clear_features"] == "on"),
            reply_to=self.own_address,
        )

    async def _search_again(self, session: Session, *, cheaper: bool) -> list[Action]:
        previous = session.last_search
        assert previous is not None
        search = previous.copy(deep=True)
        search.task_id = new_id("task")
        search.sent_at = now_iso()
        search.hard_constraints.exclude_urls = list(dict.fromkeys(session.shown_urls))[-10:]
        if cheaper and session.last_price is not None:
            search.hard_constraints.max_price = math.floor(session.last_price * 0.95 * 100) / 100
        return await self._start_search(session, search)

    async def _start_search(self, session: Session, search: ProductSearchTask) -> list[Action]:
        search.query = replies.describe_search(search)  # what's sent onward: the summary, not their words
        task = Task(search.task_id, session.key, session.user, "product", search, self._clock())
        self.tasks[task.task_id] = task
        session.task_id = task.task_id
        session.last_search = search
        session.mode = ""
        await self.task_log.record(task.task_id, task.user, task.kind, task.status, task.summary())
        short = self.preferences(session.user)["explanation_length"] == "short"
        return [Send(self.technical_helper, search), Say(session.key, replies.searching(search, short))]

    # Tech help

    async def _start_help(self, session: Session, request: intake.Request) -> list[Action]:
        prefs = self.preferences(session.user)
        help_task = TechHelpTask(
            task_id=new_id("help"),
            sent_at=now_iso(),
            conversation_ref=session.user,
            question=mask_personal(request.text),
            page_url=request.url,
            prefs=TechHelpPrefs(
                short=prefs["explanation_length"] == "short",
                simple_wording=prefs["wording"] == "simple",
                confirm_each_step=prefs["confirm_complex_steps"] == "on",
            ),
            reply_to=self.own_address,
        )
        task = Task(help_task.task_id, session.key, session.user, "tech_help", help_task, self._clock())
        self.tasks[task.task_id] = task
        session.task_id = task.task_id
        session.mode = "tech_help"
        await self.task_log.record(task.task_id, task.user, task.kind, task.status, task.summary())
        actions: list[Action] = [Send(self.technical_helper, help_task)]
        if request.url:
            actions.append(Say(session.key, "One moment while I look at that page."))
        return actions

    # Agents

    async def on_ack(self, sender: str, ack: TaskAck) -> list[Action]:
        task = self.tasks.get(ack.task_id)
        if sender != self.technical_helper or task is None or task.status in TERMINAL:
            return []
        if ack.status in {"accepted", "duplicate"}:
            task.acked = True
            if task.status == "delegated":
                task.status = "searching" if task.kind == "product" else "working"
                await self.task_log.record(task.task_id, task.user, task.kind, task.status, task.summary())
            return []
        reason = ack.error.message if ack.error else "it was refused"
        return await self._render(await self._end(task, FAILED, f"Sorry, I couldn't start that ({reason})."))

    async def on_recommendation(self, sender: str, rec: RecommendationResponse) -> list[Action]:
        task = self.tasks.get(rec.task_id)
        if sender != self.technical_helper or task is None or task.status in TERMINAL:
            return []
        assert isinstance(task.message, ProductSearchTask)
        task.result = rec
        session = self.session(task.session)
        prefs = self.preferences(task.user)
        short = prefs["explanation_length"] == "short"
        if rec.status == SELECTED and rec.selected_candidate is not None:
            text = replies.product(rec, task.message, short=short, highlight=prefs["highlight_clear_features"] == "on")
            session.last_price = rec.selected_candidate.price
            session.shown_urls.append(rec.selected_candidate.url)
            status = SELECTED
        elif rec.status == NO_MATCH:
            text = replies.no_match(rec, task.message, short=short)
            status = NO_MATCH
        else:
            text = replies.failed(rec.error.code if rec.error else "")
            status = FAILED
        return await self._render(await self._end(task, status, text))

    async def on_tech_help(self, sender: str, response: TechHelpResponse) -> list[Action]:
        task = self.tasks.get(response.task_id)
        if sender != self.technical_helper or task is None or task.status in TERMINAL:
            return []
        task.result = response
        text = response.answer or "Sorry, I couldn't work that out just now. Please try again in a moment."
        status = SELECTED if response.status == "answered" else FAILED
        return await self._render(await self._end(task, status, text))

    async def _end(self, task: Task, status: str, text: str) -> list[Action]:
        task.status = status
        task.reply = text
        task.finished = self._clock()
        session = self.sessions.get(task.session)
        if session is not None and session.task_id == task.task_id:
            session.task_id = ""
        await self.task_log.record(task.task_id, task.user, task.kind, status, task.summary())
        return [Say(task.session, text)]

    # Caregiver agent

    def _from_caregiver(self, sender: str, request_id: str) -> CaregiverCall | None:
        if sender != self.caregiver:
            return None
        return self.calls.pop(request_id, None)

    async def on_invite_result(self, sender: str, result: CaregiverInviteResult) -> list[Action]:
        call = self._from_caregiver(sender, result.request_id)
        if call is None:
            return []
        if result.status == "created" and result.invite_code:
            names = ", ".join(SETTINGS[key].label for key in result.scopes if key in SETTINGS)
            text = (f"Here is the code for your caregiver: **{result.invite_code}**\n\n"
                    f"It works once and expires {result.expires_at[:16].replace('T', ' at ')} UTC. Ask them to send it "
                    f"to Hold My Hand Caregiver ({self.caregiver}). They'll be able to change: {names}.\n\n"
                    "You can say \"caregiver settings\" to check, or \"remove my caregiver\" at any time.")
        else:
            reason = result.error.message if result.error else "the caregiver service refused"
            text = f"I couldn't set that up: {reason}"
        return await self._render([Say(call.session, text)])

    async def on_snapshot(self, sender: str, snapshot: PreferenceSnapshot) -> list[Action]:
        call = self._from_caregiver(sender, snapshot.request_id)
        if call is None:
            return []
        if snapshot.status == "ok":
            self.prefs[snapshot.user_ref] = sanitize(snapshot.preferences)
            self.set_by[snapshot.user_ref] = dict(snapshot.set_by)
        if call.purpose == "refresh":
            return []
        if snapshot.status != "ok":
            reason = snapshot.error.message if snapshot.error else "it isn't available"
            return await self._render([Say(call.session, f"Caregiver settings aren't available right now: {reason}")])
        if call.purpose == "revoke":
            done = "Done. Your caregiver's changes are undone"
            done += " and they no longer have access." if call.revoke_access else "."
            return await self._render([Say(call.session, done)])
        prefs = self.preferences(snapshot.user_ref)
        lines = ["Your settings:"]
        for key, value in prefs.items():
            who = snapshot.set_by.get(key)
            lines.append(f"- {describe(key, value)}" + (f" (set by caregiver {who})" if who else ""))
        if snapshot.caregivers:
            lines.append("")
            lines.append("Caregivers with access: " + ", ".join(snapshot.caregivers))
            lines.append("Say \"undo caregiver changes\" or \"remove my caregiver\" to change this.")
        else:
            lines.append("")
            lines.append("No caregiver has access. Say \"add a caregiver\" if you'd like one.")
        return await self._render([Say(call.session, "\n".join(lines))])

    async def on_preference_result(self, sender: str, result: CaregiverPreferenceResult) -> list[Action]:
        """A caregiver changed a setting. Update the cache and tell the person next time they hear from us."""
        if sender != self.caregiver or result.status not in {"applied", "cleared"} or result.setting not in SETTINGS:
            return []
        if result.status == "applied" and not valid(result.setting, result.new_value):
            return []  # never trust a value outside the allowed list, even from our own agent
        prefs = self.prefs.setdefault(result.user_ref, {})
        set_by = self.set_by.setdefault(result.user_ref, {})
        if result.status == "applied":
            prefs[result.setting] = result.new_value
            set_by[result.setting] = short_address(result.caregiver_id)
            change = describe(result.setting, result.new_value)
        else:
            prefs.pop(result.setting, None)
            set_by.pop(result.setting, None)
            change = f"{SETTINGS[result.setting].label} set back to normal"
        self.notices.setdefault(result.user_ref, []).append(change)
        return []

    # Timers

    async def sweep(self) -> list[Action]:
        now = self._clock()
        actions: list[Action] = []
        for task in list(self.tasks.values()):
            if task.status in TERMINAL:
                if now - task.finished > 3600:
                    del self.tasks[task.task_id]
                continue
            age = now - task.created
            limit = PRODUCT_TIMEOUT_SECONDS if task.kind == "product" else HELP_TIMEOUT_SECONDS
            if not task.acked and age > ACK_TIMEOUT_SECONDS:
                actions += await self._end(task, FAILED, "Sorry, the Hold My Hand Technical Helper isn't answering, "
                                                         "so I can't do that right now. Please try again later.")
            elif age > limit:
                actions += await self._end(task, TIMED_OUT, "This is taking much longer than it should, so I've "
                                                            "stopped. Please try again in a few minutes.")
            elif task.kind == "product" and age > PROGRESS_AFTER_SECONDS and not task.progress_sent:
                task.progress_sent = True
                actions.append(Say(task.session, "Still looking. Thank you for waiting."))
        for request_id, call in list(self.calls.items()):
            if now - call.started > CAREGIVER_TIMEOUT_SECONDS:
                del self.calls[request_id]
                if call.purpose != "refresh":
                    actions.append(Say(call.session, "The caregiver service isn't answering right now. Please try "
                                                     "again later."))
        return await self._render(actions)

    def status(self, task_id: str, user: str) -> Task | None:
        task = self.tasks.get(task_id)
        return task if task is not None and task.user == user else None
