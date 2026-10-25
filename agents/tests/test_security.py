"""8 and the security rules: no secrets in output or files, signed bridge requests, no internal URLs."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest

from shared.security import (
    NonceCache,
    RedactingStream,
    UnsafeURL,
    bridge_signature,
    check_public_url,
    redact,
    verify_bridge,
)

REPO = Path(__file__).resolve().parents[2]
SECRET_NAMES = ("GROQ_API_KEY", "AGENTVERSE_API_KEY", "SPACETIME_TOKEN", "APIFY_API_TOKEN", "JWT_SECRET",
                "BRIDGE_SHARED_SECRET", "ORCHESTRATOR_SEED", "WEBSITE_AGENT_SEED", "CAREGIVER_SEED",
                "TECHNICAL_HELPER_SEED", "SCRAPER_SEED")


def real_secrets() -> list[str]:
    """Values from the developer's .env, read here only to make sure they appear nowhere else."""
    env = REPO / ".env"
    if not env.exists():
        return []
    values = []
    for line in env.read_text(encoding="utf-8").splitlines():
        name, _, value = line.partition("=")
        if name.strip() in SECRET_NAMES and len(value.strip()) >= 8:
            values.append(value.strip())
    return values


def repo_files() -> list[Path]:
    listed = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=REPO,
                            capture_output=True, text=True, check=True).stdout.splitlines()
    return [REPO / name for name in listed if name and name != ".env" and (REPO / name).is_file()]


def test_no_secret_from_env_is_in_any_project_file():
    secrets = real_secrets()
    if not secrets:
        pytest.skip("no .env to compare against")
    leaks = []
    for path in repo_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(secret in text for secret in secrets):
            leaks.append(str(path.relative_to(REPO)))
    assert leaks == [], f"secret values found in: {leaks}"


def test_env_file_is_git_ignored():
    result = subprocess.run(["git", "check-ignore", ".env"], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0


def test_output_is_redacted(monkeypatch):
    monkeypatch.setenv("APIFY_API_TOKEN", "apify_api_TESTSECRET123")
    monkeypatch.setenv("ORCHESTRATOR_SEED", "seed-value-that-is-secret")
    target = io.StringIO()
    stream = RedactingStream(target)
    stream.write("calling with apify_api_TESTSECRET123 and seed-value-that-is-secret\n")
    assert target.getvalue() == "calling with *** and ***\n"
    assert redact("short") == "short"


def test_bridge_signatures():
    nonces = NonceCache()
    secret, body = "bridge-secret", "Find a vacuum"
    signature = bridge_signature(secret, 1000, "n1", "user-1", body)
    assert verify_bridge(secret, 1000, "n1", "user-1", body, signature, nonces, now=1010) is None
    assert verify_bridge(secret, 1000, "n1", "user-1", body, signature, nonces, now=1010) == "replayed_request"
    assert verify_bridge(secret, 1000, "n2", "user-1", body + "!", signature, nonces, now=1010) == "bad_signature"
    assert verify_bridge(secret, 1000, "n3", "user-2", body, signature, nonces, now=1010) == "bad_signature"
    assert verify_bridge(secret, 1000, "n4", "user-1", body, signature, nonces, now=2000) == "stale_request"
    assert verify_bridge("", 1000, "n5", "user-1", body, signature, nonces, now=1010) == "bridge_disabled"


@pytest.mark.parametrize("url", [
    "http://localhost/admin", "http://127.0.0.1:8040/submit", "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/", "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "https://example.com:8443/",
    "https://user:pass@example.com/", "http://printer.local/", "http://intranet/",
])
def test_internal_and_odd_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        check_public_url(url, resolve=False)


def test_public_urls_are_allowed():
    assert check_public_url("https://www.amazon.com/dp/B0TEST0001", resolve=False)
    assert check_public_url("http://8.8.8.8/", resolve=False)


def test_page_reader_refuses_private_targets():
    from scraper.page import ScrapeError, fetch

    with pytest.raises(ScrapeError):
        fetch("http://127.0.0.1:8040/agent_info")
