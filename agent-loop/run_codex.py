#!/usr/bin/env python3
"""
Agent Loop for OpenAI Codex CLI — Fully automated implement → test → fix cycle.

This is the Codex CLI adaptation of run.py (which uses Claude Code CLI).
It uses `codex` CLI with similar patterns but adapted for Codex's interface.

Architecture:
  - Python script: loop coordinator, reads tasks.json for status
  - Main agent: implements/fixes code, compiles
  - Test agent: verifies goals, edits tasks.json status, writes result.md
  - Plan agent (optional): generates goals file from reference implementation

Flow:
  tasks.json[pending] → (plan?) → implement → compile → test
                                                          ↓
                                         tasks.json[done] → git commit → next
                                         tasks.json[failed] → read result.md → fix → loop

Usage:
    python3 agent-loop/run_codex.py                       # Run all pending
    python3 agent-loop/run_codex.py --task my-feature     # Run one task
    python3 agent-loop/run_codex.py --reset my-feature    # Reset task
    python3 agent-loop/run_codex.py --dry-run              # Preview
    python3 agent-loop/run_codex.py --safe                 # Require approval

Prerequisites:
    - `codex` CLI installed: npm install -g @openai/codex
    - OPENAI_API_KEY set in environment
"""

import argparse
import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
EVIDENCE_DIR = SCRIPT_DIR / "evidence"
TASKS_FILE = SCRIPT_DIR / "tasks.json"
LOG_FILE = SCRIPT_DIR / "loop.log"

# ── Defaults ─────────────────────────────────────────────────
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TIMEOUT = 0           # 0 = unlimited
DEFAULT_COMPILE_TIMEOUT = 300  # 5 min compile

# ── ANSI ─────────────────────────────────────────────────────
G, R, Y, B, BD, RS = (
    "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[1m", "\033[0m"
)

# ── Globals ──────────────────────────────────────────────────
shutdown = False
approval_mode = "full-auto"  # "full-auto" | "suggest" | "auto-edit"


def handle_signal(sig, frame):
    global shutdown
    print(f"\n{Y}Shutdown requested — finishing current step...{RS}")
    shutdown = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ── Utilities ────────────────────────────────────────────────


def log(msg, color=""):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"{BD}{color}{line}{RS}")
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def load_tasks():
    return json.loads(TASKS_FILE.read_text(encoding="utf-8"))


def save_tasks(data):
    TASKS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8"
    )


def find_task(data, task_id):
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    return None


def read_result_md(task_id):
    result_file = EVIDENCE_DIR / task_id / "result.md"
    if result_file.exists():
        return result_file.read_text(encoding="utf-8")
    return ""


# ── Codex CLI ────────────────────────────────────────────────


def run_codex(prompt, timeout=DEFAULT_TIMEOUT):
    """
    Run `codex` CLI with a prompt.
    Returns (result_text: str, is_error: bool).

    Codex CLI usage:
      codex "prompt"                           # interactive
      echo "prompt" | codex --quiet            # non-interactive
      codex --approval-mode full-auto "prompt" # fully autonomous
    """
    cmd = ["codex", "--approval-mode", approval_mode, "--quiet"]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    # Send prompt via stdin
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        return "Agent failed to start", True

    result_text = ""
    is_error = False
    deadline = time.time() + timeout if timeout > 0 else float('inf')

    try:
        # Read stdout
        while True:
            if time.time() > deadline:
                proc.kill()
                return "TIMEOUT: exceeded time limit", True

            line = proc.stdout.readline()
            if not line:
                break
            result_text += line
            # Log tool usage for visibility
            stripped = line.strip()
            if stripped:
                # Print abbreviated progress
                if len(stripped) > 120:
                    stripped = stripped[:120] + "..."
                log(f"  > {stripped}", B)

    except Exception as e:
        is_error = True
        result_text = f"Error: {e}"

    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()

    if proc.returncode and proc.returncode != 0:
        stderr = proc.stderr.read() if proc.stderr else ""
        if stderr:
            result_text += f"\nSTDERR: {stderr}"
        is_error = True

    log(f"  Agent done")
    return result_text, is_error


# ── Compile check ────────────────────────────────────────────

# Customize this for your project's build system
BUILD_CMD = None  # Set to e.g. ["make", "build"] or ["npm", "run", "build"]


def compile_check():
    """Run project build. Returns (ok: bool, error_output: str)."""
    if BUILD_CMD is None:
        log("  No BUILD_CMD configured, skipping compile check", Y)
        return True, ""

    log("  Compile check...", B)
    try:
        result = subprocess.run(
            BUILD_CMD,
            capture_output=True,
            text=True,
            timeout=DEFAULT_COMPILE_TIMEOUT,
            cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            log("  Compile OK", G)
            return True, ""
        else:
            error = result.stderr[-3000:] if result.stderr else result.stdout[-3000:]
            log("  Compile FAILED", R)
            return False, error
    except subprocess.TimeoutExpired:
        return False, "Compile timeout"
    except FileNotFoundError:
        log(f"  BUILD_CMD not found: {BUILD_CMD}", R)
        return True, ""  # Skip if build tool not found


# ── Prompt builders ──────────────────────────────────────────


def prompt_plan(task):
    goals_file = task.get("goals_file", "")
    ref_files = task.get("reference_files", [])
    ref_section = "\n".join(f"- {f}" for f in ref_files)

    return f"""You are a feature planner. Read the reference files and generate a Goals file.

## Task
{task['name']}: {task.get('description', '')}

## Reference Files (read these first)
{ref_section}

## Output
Write a Goals file to: {goals_file}

The Goals file must contain test acceptance criteria. Each goal must include:
- Precondition (setup state)
- Action steps (what to do)
- Expected result (what should happen)
- Verification method (how to check — log grep, test assertion, UI check)

Write ONLY the goals file. Do not write any feature code.
"""


def prompt_implement(task):
    goals_file = task.get("goals_file", "")
    return f"""Implement the following feature. The goals file at {goals_file} contains the acceptance criteria.

## Task: {task['name']}

{task.get('description', '')}

After implementation, make sure the code compiles. Do not run tests — that will be done separately.
"""


def prompt_fix(task, failure_info, attempt):
    goals_file = task.get("goals_file", "")
    return f"""Fix the failing tests for "{task['name']}". This is attempt {attempt}.

## Goals file
{goals_file}

## Failure Report
{failure_info[:8000]}

Read the failure details carefully. Fix the root cause, not just symptoms.
Make sure the code compiles after your fix. Do not run tests.
"""


def prompt_test(task, attempt):
    task_id = task["id"]
    goals_file = task.get("goals_file", "")
    edir = EVIDENCE_DIR / task_id

    return f"""Verify all test goals for "{task['name']}".

## First Step (MANDATORY)
Read Tests/Tools/LESSONS.md if it exists — contains critical knowledge from previous test runs.

## Goals File
{goals_file}

## Testing Strategy (use layered approach)

Level 1 — Unit/Integration Tests (preferred): write and run tests for data logic, parsing, services.
Level 2 — Protocol/API Tests: inject events, verify responses.
Level 3 — E2E UI Tests (only when L1/L2 can't verify): screenshots, UI automation, logs.

For each goal, state which level you're using.

## Evidence Directory: {edir}/
Save evidence files (screenshots, logs, test output) here.

## Status Update (MANDATORY — do this before finishing)

### 1. Update tasks.json
Edit `{TASKS_FILE}`, find task id "{task_id}":
- All pass: set "status" to "done"
- Any fail: set "status" to "failed"

### 2. Write result.md → {edir}/result.md
Include: per-goal Level/Status/Method/Evidence/Details + Summary.
"""


# ── Git ──────────────────────────────────────────────────────


def git_commit(task):
    cwd = str(PROJECT_ROOT)

    subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)

    r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=cwd)
    if r.returncode == 0:
        log("  No changes to commit", Y)
        return

    msg = (
        f"feat: {task['name']}\n\n"
        f"Agent-loop task: {task['id']}\n"
        f"Attempts: {task.get('attempts', 1)}"
    )
    subprocess.run(["git", "commit", "-m", msg], cwd=cwd, check=True)
    log(f"  Committed: {task['name']}", G)


# ── Task processor ───────────────────────────────────────────


def process_task(task_id, data):
    """Process one task through plan → implement → test → fix loop."""
    global shutdown

    task = find_task(data, task_id)
    max_attempts = task.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
    attempts = task.get("attempts", 0)

    # Evidence dir
    edir = EVIDENCE_DIR / task_id
    edir.mkdir(parents=True, exist_ok=True)

    # ── Phase 0: Plan (generate goals if missing) ──
    goals_file = task.get("goals_file", "")
    if not goals_file:
        goals_file = f"Tests/Goals/{task_id}.md"
        task["goals_file"] = goals_file
        save_tasks(data)

    goals_path = PROJECT_ROOT / goals_file
    if not goals_path.exists():
        log(f"[{task_id}] Phase 0: PLAN (generating goals)", B)
        task["status"] = "planning"
        save_tasks(data)

        plan_prompt = prompt_plan(task)
        _, plan_err = run_codex(plan_prompt)

        if plan_err:
            log("Plan agent error", R)
            task["status"] = "plan_failed"
            save_tasks(data)
            return False

        if not goals_path.exists():
            log(f"Plan agent did not create goals file: {goals_file}", R)
            task["status"] = "plan_failed"
            save_tasks(data)
            return False

        log(f"Goals file created: {goals_file}", G)
    else:
        log(f"[{task_id}] Goals file exists, skipping plan phase", G)

    # ── Main loop: implement → compile → test → (fix) ──
    failure_info = None

    while attempts < max_attempts and not shutdown:
        attempts += 1
        task["attempts"] = attempts

        sep = "=" * 60
        log(sep)
        log(f"[{task_id}] Attempt {attempts}/{max_attempts}")
        log(sep)

        # ── Phase 1: Implement / Fix ──
        if failure_info is None:
            log("Phase 1: IMPLEMENT", G)
            prompt = prompt_implement(task)
        else:
            log("Phase 1: FIX", Y)
            prompt = prompt_fix(task, failure_info, attempts)

        task["status"] = "implementing"
        save_tasks(data)

        impl_text, impl_err = run_codex(prompt)
        save_tasks(data)

        if impl_err:
            log(f"Implement agent error: {impl_text[:200]}", R)
            failure_info = f"Implementation agent error:\n{impl_text[:2000]}"
            continue

        if shutdown:
            break

        # ── Phase 2: Compile check ──
        compile_ok, compile_err = compile_check()
        if not compile_ok:
            failure_info = f"Build failed:\n{compile_err}"
            task["status"] = "compile_failed"
            save_tasks(data)
            log("Compile failed — will retry", R)
            continue

        if shutdown:
            break

        # ── Phase 3: Test ──
        log("Phase 3: TEST", B)
        task["status"] = "testing"
        save_tasks(data)

        test_prompt = prompt_test(task, attempts)
        test_text, test_err = run_codex(test_prompt)

        # ── Phase 4: Read status from tasks.json ──
        data = load_tasks()
        task = find_task(data, task_id)

        if task["status"] == "done":
            task["completed_at"] = datetime.now().isoformat()
            save_tasks(data)
            log(f"PASSED: {task['name']}", G)

            try:
                git_commit(task)
            except Exception as e:
                log(f"Git commit failed: {e}", R)

            return True

        elif task["status"] == "failed":
            result_md = read_result_md(task_id)
            if result_md:
                failure_info = result_md
            elif test_text:
                failure_info = test_text[-5000:]
            else:
                failure_info = "Test agent did not produce result details."

            log(f"FAILED — will retry", R)

        else:
            log(f"Test agent did not update tasks.json status "
                f"(current: {task['status']})", Y)
            if test_err:
                failure_info = f"Test agent error:\n{test_text[:2000]}"
            else:
                failure_info = (
                    f"Test agent completed but did not update tasks.json.\n"
                    f"Agent output:\n{test_text[-3000:]}"
                )
            task["status"] = "failed"
            save_tasks(data)

    # Exceeded max attempts
    if task["status"] != "done":
        task["status"] = "stuck"
        save_tasks(data)
        log(f"STUCK: {task['name']} after {attempts} attempts", Y)

    return False


# ── Main ─────────────────────────────────────────────────────


def main():
    global approval_mode

    parser = argparse.ArgumentParser(
        description="Agent Loop (Codex) — fully automated implement + test cycle"
    )
    parser.add_argument("--task", help="Run specific task by ID")
    parser.add_argument("--reset", help="Reset task status to pending")
    parser.add_argument("--dry-run", action="store_true", help="Preview tasks")
    parser.add_argument("--safe", action="store_true",
                        help="Use 'suggest' approval mode (requires confirmation)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="Timeout per agent run in seconds")
    parser.add_argument("--build-cmd", nargs="+",
                        help="Build command (e.g. --build-cmd make build)")
    args = parser.parse_args()

    global BUILD_CMD
    if args.build_cmd:
        BUILD_CMD = args.build_cmd

    if args.safe:
        approval_mode = "suggest"

    # Load tasks
    if not TASKS_FILE.exists():
        sys.exit(f"Tasks file not found: {TASKS_FILE}")

    data = load_tasks()

    # Reset
    if args.reset:
        for t in data["tasks"]:
            if t["id"] == args.reset:
                t["status"] = "pending"
                t["attempts"] = 0
                t.pop("completed_at", None)
                save_tasks(data)
                print(f"Reset: {args.reset}")
                return
        sys.exit(f"Task not found: {args.reset}")

    # Filter
    if args.task:
        tasks = [t for t in data["tasks"] if t["id"] == args.task]
        if not tasks:
            sys.exit(f"Task not found: {args.task}")
    else:
        tasks = data["tasks"]

    pending = [t for t in tasks if t.get("status") != "done"]
    if not pending:
        log("All tasks completed!", G)
        return

    # Dry run
    if args.dry_run:
        log("Tasks to process:")
        for t in pending:
            status = t.get("status", "pending")
            attempts = t.get("attempts", 0)
            print(f"  [{status:>14}] {t['id']}: {t['name']} "
                  f"(attempts: {attempts})")
        return

    # Run loop
    log(f"Starting agent loop: {len(pending)} task(s)")
    log(f"Approval mode: {approval_mode}")
    start_time = time.time()
    completed = 0

    for task in pending:
        if shutdown:
            break
        if process_task(task["id"], data):
            completed += 1
        data = load_tasks()

    elapsed = (time.time() - start_time) / 60
    total_tasks = len(data["tasks"])
    done_tasks = sum(1 for t in data["tasks"] if t["status"] == "done")
    sep = "=" * 60
    log(sep)
    log(f"Summary: {done_tasks}/{total_tasks} tasks done | "
        f"This run: {completed} completed | {elapsed:.1f} min")
    log(sep)


if __name__ == "__main__":
    main()
