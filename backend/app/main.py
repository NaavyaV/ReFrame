import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from groq import APIError

from .auth import PublicUser, current_user, router as auth_router
from .config import get_settings
from .demo import demo_reframe, is_demo
from .quick import quick_reframe
from .ratelimit import RateLimited, UserLimiter
from .reframer import reframe
from .schemas import ReframeRequest, ReframeResponse
from .stdb import SpacetimeClient, SpacetimeError
from .tracker import Tracker, clear_user_runs
from .voice import router as voice_router

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("reframe")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    app.state.stdb = SpacetimeClient()
    app.state.user_limiter = UserLimiter(s.user_rpm, s.user_rph)
    yield
    await app.state.stdb.close()


app = FastAPI(title="ReFrame", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Retry-After"],
)
app.include_router(auth_router)
app.include_router(voice_router)


@app.exception_handler(RateLimited)
async def rate_limited(_request: Request, exc: RateLimited) -> JSONResponse:
    return JSONResponse(
        {"detail": exc.message, "retry_after": exc.retry_after},
        status_code=429,
        headers={"Retry-After": str(exc.retry_after)},
    )


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/tracker/clear")
async def clear_tracker(request: Request, user: PublicUser = Depends(current_user)) -> dict:
    try:
        await clear_user_runs(request.app.state.stdb, user.user_id)
    except SpacetimeError as err:
        log.warning("tracker clear failed: %s", err)
        raise HTTPException(502, "Couldn't clear the tracker.")
    return {"ok": True}


@app.get("/caretaker/notes")
async def caretaker_notes(request: Request, user: PublicUser = Depends(current_user)) -> dict:
    try:
        notes = await request.app.state.stdb.caretaker_notes(user.display_name)
    except SpacetimeError as err:
        log.warning("caretaker notes failed: %s", err)
        notes = []
    return {"notes": notes}


@app.post("/reframe", response_model=ReframeResponse, response_model_exclude_none=True)
async def reframe_page(
    body: ReframeRequest, request: Request, user: PublicUser = Depends(current_user)
) -> ReframeResponse:
    tracker = Tracker(request.app.state.stdb, user, body)
    # Simple visual commands are answered locally and don't count against the limits.
    if not body.continuation:
        quick = quick_reframe(body.message, body.applied)
        if quick is not None:
            log.info("reframe handled locally: %s", [o.op for o in quick.operations])
            tracker.start()
            tracker.record_result(quick)
            tracker.finish("done", quick.reply)
            return quick
    if is_demo(body.snapshot.url):
        scripted = await demo_reframe(body, tracker)
        if scripted is not None:
            log.info("reframe demo step on %s", body.snapshot.url)
            return scripted
    tracker.start()
    tracker.event("scan", "Page", f"{len(body.snapshot.elements)} elements", "Reading the page", 10)
    # Only messages the user types count; automatic page rebuilds are bounded by the extension and Groq budget.
    try:
        async with request.app.state.user_limiter.slot(user.user_id, count=not body.continuation):
            res = await reframe(body)
    except APIError as err:
        log.warning("Groq error: %s", err)
        tracker.alert("critical", "AI service unavailable", "The page was left unchanged")
        tracker.finish("error", "")
        raise HTTPException(502, "The AI service is unavailable.")
    except RateLimited:
        tracker.alert("warning", "Rate limit reached", "The page was left unchanged")
        tracker.finish("error", "")
        raise
    tracker.record_result(res)
    tracker.finish("done" if res.operations else "answered", res.reply)
    return res
