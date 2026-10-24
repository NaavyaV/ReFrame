import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from groq import APIError, RateLimitError

from .auth import PublicUser, current_user, router as auth_router
from .config import get_settings
from .reframer import reframe
from .schemas import ReframeRequest, ReframeResponse
from .stdb import SpacetimeClient

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings()
    app.state.stdb = SpacetimeClient()
    yield
    await app.state.stdb.close()


app = FastAPI(title="ReFrame", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^chrome-extension://[a-p]{32}$",
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.include_router(auth_router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/reframe", response_model=ReframeResponse, response_model_exclude_none=True)
async def reframe_page(body: ReframeRequest, user: PublicUser = Depends(current_user)) -> ReframeResponse:
    try:
        return await reframe(body)
    except RateLimitError:
        raise HTTPException(429, "Too many requests. Wait a moment and try again.")
    except APIError:
        raise HTTPException(502, "The AI service is unavailable.")
