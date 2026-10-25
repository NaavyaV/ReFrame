"""Start all four Hold My Hand agents, each in its own process, with labelled logs.

    python run_all.py                      # all four; type here to chat with the orchestrator
    python run_all.py --only scraper,technical_helper
    python run_all.py --no-chat            # logs only

Before starting, any agent seed that is empty in .env is filled once with a secure
random value (existing seeds are never changed). Ctrl+C stops every agent cleanly;
press it again to force them to stop. One agent crashing doesn't stop the others.
Uses agents/.venv if the current Python doesn't have uagents installed.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENTS_DIR = ROOT / "agents"
# name: (folder, colour)
AGENTS = {
    "orchestrator": ("orchestrator", "36"),
    "technical_helper": ("technical_helper", "35"),
    "scraper": ("scraper", "33"),
    "caregiver": ("caretaker", "32"),
}
STOP_SECONDS = 15
# Each agent gets its own process group, so this script decides when to stop it (see stop()).
OWN_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def python_for_agents() -> str:
    if importlib.util.find_spec("uagents") is not None:
        return sys.executable
    for candidate in (AGENTS_DIR / ".venv" / "Scripts" / "python.exe", AGENTS_DIR / ".venv" / "bin" / "python",
                      ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if candidate.exists():
            return str(candidate)
    raise SystemExit(
        "uagents isn't installed for this Python. From the repo root run:\n"
        "  python -m pip install -r agents/requirements.txt\n"
        "or, with uv:  cd agents; uv sync"
    )


def fill_seeds(python: str) -> None:
    """Fill empty seeds in .env using the agents' own code, in the agents' interpreter."""
    code = (
        "import sys; sys.path.insert(0, 'agents');"
        "from shared.agents import fill_missing_seeds;"
        "filled = fill_missing_seeds();"
        "print('Generated new seed(s) in .env for: ' + ', '.join(filled) + '. Keep them: they are the agents identities.')"
        " if filled else None"
    )
    result = subprocess.run([python, "-c", code], cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"Couldn't check the seeds in .env:\n{result.stderr[-800:]}")
    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)


def pump(name: str, colour: str, stream, use_colour: bool) -> None:
    label = f"\033[{colour}m[{name}]\033[0m" if use_colour else f"[{name}]"
    for line in iter(stream.readline, ""):
        print(f"{label} {line.rstrip()}", flush=True)
    stream.close()


def forward_input(target: subprocess.Popen) -> None:
    for line in sys.stdin:
        if target.poll() is not None or target.stdin is None:
            return
        try:
            target.stdin.write(line)
            target.stdin.flush()
        except (BrokenPipeError, OSError):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated agents to start: " + ", ".join(AGENTS))
    parser.add_argument("--no-chat", action="store_true", help="don't pass typed lines to the orchestrator")
    args = parser.parse_args()
    names = [name.strip() for name in args.only.split(",")] if args.only else list(AGENTS)
    unknown = [name for name in names if name not in AGENTS]
    if unknown:
        raise SystemExit(f"Unknown agent(s): {', '.join(unknown)}. Choose from {', '.join(AGENTS)}.")
    if not (ROOT / ".env").exists():
        raise SystemExit("No .env file. Copy .env.example to .env and fill it in first.")

    python = python_for_agents()
    fill_seeds(python)
    use_colour = sys.stdout.isatty() and os.getenv("NO_COLOR") is None
    chat = "orchestrator" in names and not args.no_chat and sys.stdin is not None and sys.stdin.isatty()

    processes: dict[str, subprocess.Popen] = {}
    for name in names:
        folder, colour = AGENTS[name]
        env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        for other in AGENTS:
            repl = {"caregiver": "CARETAKER_REPL"}.get(other, f"{other.upper()}_REPL")
            env[repl] = "1" if (other == "orchestrator" and chat) else "0"
        process = subprocess.Popen(
            [python, "-u", str(AGENTS_DIR / folder / "agent.py")],
            cwd=AGENTS_DIR / folder,
            env=env,
            stdin=subprocess.PIPE if (name == "orchestrator" and chat) else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **OWN_GROUP,
        )
        processes[name] = process
        threading.Thread(target=pump, args=(name, colour, process.stdout, use_colour), daemon=True).start()
        print(f"Started {name} (pid {process.pid})", flush=True)
    if chat:
        threading.Thread(target=forward_input, args=(processes["orchestrator"],), daemon=True).start()
        print("Type a message and press Enter to talk to the orchestrator. Ctrl+C stops everything.", flush=True)

    reported: set[str] = set()
    try:
        while any(process.poll() is None for process in processes.values()):
            for name, process in processes.items():
                if process.poll() is not None and name not in reported:
                    reported.add(name)
                    print(f"{name} stopped (exit code {process.returncode}). The others keep running.", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping the agents...", flush=True)
        stop(processes)
    else:
        print("All agents have stopped.", flush=True)


def stop(processes: dict[str, subprocess.Popen]) -> None:
    """Ask each agent once to shut down (it finishes in-flight work), then force any that don't."""
    for process in processes.values():
        if process.poll() is None:
            try:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            except OSError:
                pass
    deadline = time.monotonic() + STOP_SECONDS
    try:
        while time.monotonic() < deadline and any(p.poll() is None for p in processes.values()):
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    for name, process in processes.items():
        if process.poll() is None:
            print(f"Forcing {name} to stop.", flush=True)
            process.kill()
    for process in processes.values():
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    print("All agents stopped.", flush=True)


if __name__ == "__main__":
    main()
