#!/usr/bin/env python3
"""
Agent Loop — Fully automated implement → test → fix cycle.

Architecture:
  - Python script: loop coordinator, reads tasks.json for status
  - Main agent: implements/fixes code, compiles
  - Test agent (owl-test): verifies goals, edits tasks.json status, writes result.md
  - Plan agent (optional): generates goals file from C++ reference

Flow:
  tasks.json[pending] → (plan?) → implement → compile → test
                                                          ↓
                                         tasks.json[done] → git commit → next
                                         tasks.json[failed] → read result.md → fix → loop

Usage:
    python3 agent-loop/run.py                       # Run all pending
    python3 agent-loop/run.py --task find-in-page   # Run one task
    python3 agent-loop/run.py --reset find-in-page  # Reset task
    python3 agent-loop/run.py --dry-run              # Preview
    python3 agent-loop/run.py --safe                 # Require permission approval
"""

import argparse
import json
import shutil
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
BUILD_SCRIPT = PROJECT_ROOT / "joyme" / "owl-client" / "build.sh"

# ── Defaults ─────────────────────────────────────────────────
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_MAX_TURNS = 0  # 0 = unlimited turns
DEFAULT_TIMEOUT = 0           # 0 = unlimited
DEFAULT_COMPILE_TIMEOUT = 300  # 5 min compile

# ── ANSI ─────────────────────────────────────────────────────
G, R, Y, B, BD, RS = (
    "\033[92m", "\033[91m", "\033[93m", "\033[94m", "\033[1m", "\033[0m"
)

# ── Global state ─────────────────────────────────────────────
shutdown = False
skip_permissions = True  # default: fully autonomous


def on_signal(sig, _):
    global shutdown
    shutdown = True
    log("Shutdown requested — finishing current step...", Y)


signal.signal(signal.SIGINT, on_signal)
signal.signal(signal.SIGTERM, on_signal)


# ── Helpers ──────────────────────────────────────────────────


def log(msg, color="", also_file=True):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(f"{BD}{line[:8]}{RS} {color}{line[8:]}{RS}", flush=True)
    if also_file:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")


def load_tasks():
    with open(TASKS_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_tasks(data):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_task(data, task_id):
    """Find task by id in data, return the dict reference."""
    for t in data["tasks"]:
        if t["id"] == task_id:
            return t
    return None


def read_result_md(task_id):
    """Read result.md from evidence dir. Returns content or empty string."""
    result_file = EVIDENCE_DIR / task_id / "result.md"
    if result_file.exists():
        return result_file.read_text(encoding="utf-8")
    return ""


# ── Claude CLI ───────────────────────────────────────────────


def run_claude(prompt, agent=None, max_turns=DEFAULT_MAX_TURNS,
               timeout=DEFAULT_TIMEOUT, allowed_tools=None):
    """
    Run `claude -p` with stream-json output.
    Returns (result_text: str, cost_usd: float, is_error: bool).
    """
    cmd = ["claude", "-p", "--output-format", "stream-json",
           "--verbose"]
    if max_turns > 0:
        cmd.extend(["--max-turns", str(max_turns)])

    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")

    if agent:
        cmd.extend(["--agent", agent])

    if allowed_tools:
        cmd.extend(["--allowedTools", ",".join(allowed_tools)])

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(SCRIPT_DIR / "claude_stderr.log", "a"),
        text=True,
        cwd=str(PROJECT_ROOT),
    )

    # Send prompt via stdin, then close to signal EOF
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
    except BrokenPipeError:
        return "Agent failed to start", 0, True

    result_text = ""
    cost_usd = 0.0
    is_error = False
    deadline = time.time() + timeout if timeout > 0 else float('inf')

    try:
        for line in proc.stdout:
            if time.time() > deadline:
                proc.kill()
                return "TIMEOUT: exceeded time limit", cost_usd, True

            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
                etype = event.get("type")

                if etype == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            if text:
                                sys.stdout.write(text)
                                sys.stdout.flush()
                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp = block.get("input", {})
                            if name in ("Read", "Edit", "Write", "Glob"):
                                target = (inp.get("file_path")
                                          or inp.get("pattern", ""))
                                short = str(target).replace(
                                    str(PROJECT_ROOT) + "/", "")
                                log(f"  > {name}: {short}", B,
                                    also_file=False)
                            elif name == "Bash":
                                cmd_str = inp.get("command", "")[:100]
                                log(f"  > Bash: {cmd_str}", B,
                                    also_file=False)
                            else:
                                log(f"  > {name}", B, also_file=False)

                elif etype == "result":
                    result_text = event.get("result", "")
                    is_error = event.get("is_error", False)
                    cost_usd = event.get("total_cost_usd", 0) or event.get("cost_usd", 0)
                    turns = event.get("num_turns", 0)
                    log(f"  Agent done: {turns} turns, ${cost_usd:.3f}")

            except json.JSONDecodeError:
                continue

        proc.wait(timeout=60)
    except Exception as e:
        proc.kill()
        return f"Error: {e}", cost_usd, True

    print()  # newline after streaming output
    return result_text, cost_usd, is_error


# ── Compile check ────────────────────────────────────────────


def compile_check():
    """Quick build check. Returns (success, error_output)."""
    log("  Compile check...", B)
    try:
        result = subprocess.run(
            ["bash", str(BUILD_SCRIPT)],
            capture_output=True, text=True,
            timeout=DEFAULT_COMPILE_TIMEOUT,
            cwd=str(PROJECT_ROOT / "joyme" / "owl-client"),
        )
        if result.returncode == 0:
            log("  Compile OK", G)
            return True, ""
        else:
            err = (result.stdout + "\n" + result.stderr)[-3000:]
            log("  Compile FAILED", R)
            return False, err
    except subprocess.TimeoutExpired:
        return False, "Compile timeout (5 min)"
    except Exception as e:
        return False, str(e)


# ── Prompts ──────────────────────────────────────────────────

# Tools allowed for the main agent (no Agent tool to prevent self-dispatching owl-test)
MAIN_AGENT_TOOLS = [
    "Bash", "Edit", "Glob", "Grep", "LSP", "Read", "Skill",
    "ToolSearch", "WebFetch", "Write",
]


def prompt_plan(task):
    """Phase 0: Read-only agent generates goals file from C++ reference."""
    refs = "\n".join(f"- {r}" for r in task.get("reference_files", []))
    goals_file = task.get("goals_file", "")

    return f"""## 任务: 为 "{task['name']}" 编写测试验收目标 (Goals)

你是一个只读的架构分析 agent。你的职责是阅读 C++ 参考实现，
为 OWL Client 功能编写完整的 Goals 验收标准。

## 功能描述
{task['description']}

## C++ 参考文件
{refs}

## 输出文件
将 Goals 写入: {goals_file}

## Goals 编写规范（严格遵循 CLAUDE.md）
每个 goal 必须包含：前置条件 → 操作步骤 → 预期结果 → 验证方法

覆盖要求：
- 数据过滤链（每个过滤条件一个 goal）
- 操作语义（增删改查各一个 goal）
- 状态切换（每个状态组合一个 goal）
- 边界条件（空数据、极端值）
- 验证方法必须具体（AX 命令、log grep 命令、截图确认）

## 限制
- 不要实现任何功能代码
- 不要修改 tasks.json
- 只输出 Goals 文件
"""


def prompt_implement(task):
    """Main agent: implement feature."""
    refs = "\n".join(f"- {r}" for r in task.get("reference_files", []))
    refs_section = f"\n## C++ 参考文件\n{refs}" if refs else ""
    goals_file = task.get("goals_file", "")

    return f"""## ⚠️ 自动化循环模式
你在 agent-loop 自动化循环中运行，脚本负责调度测试。
- 不要执行 CLAUDE.md 中的第 5 步（TEST）— 测试由脚本单独调度 owl-test agent
- 不要运行 app 或手动测试
- 不要修改 Goals 文件（它是独立的验收标准，只读参考）
- 不要修改 agent-loop/tasks.json

## 任务: {task['name']}

{task['description']}
{refs_section}

## Goals 文件（只读参考，了解需要实现的具体行为）
路径: {goals_file}
阅读 Goals 理解每个验收条件，确保你的实现能通过这些检查。

## 工作流程
1. 阅读 C++ 参考实现 + Goals 文件，理解完整需求
2. 实现功能代码（遵循 MVVM 架构）
3. 添加诊断日志: owlLog("<feature>_<action>: key=value") 方便 test agent 验证
4. 编译: cd joyme/owl-client && bash build.sh
5. 确认编译通过后结束
"""


def prompt_fix(task, failure_info, attempt):
    """Main agent: fix based on test failure."""
    goals_file = task.get("goals_file", "")

    return f"""## ⚠️ 自动化循环模式
你在 agent-loop 自动化循环中运行。功能 "{task['name']}" 第 {attempt} 次尝试失败。
- 不要执行测试步骤 — 测试由脚本单独调度
- 不要修改 Goals 文件
- 不要修改 agent-loop/tasks.json

## 任务描述
{task['description']}

## 失败信息
```
{failure_info[-5000:]}
```

## Goals 文件（参考预期行为）
{goals_file}

## 修复流程
1. 仔细阅读失败信息，定位根因（不要猜测）
2. 阅读相关源码确认问题
3. 最小改动修复
4. 编译: cd joyme/owl-client && bash build.sh
5. 确认编译通过后结束
"""


def prompt_test(task, attempt):
    """Test agent: verify goals, update tasks.json, write result.md."""
    task_id = task["id"]
    goals_file = task.get("goals_file", "")
    edir = EVIDENCE_DIR / task_id

    return f"""验证功能 "{task['name']}" 的所有测试目标。

## !! 第一步：读 LESSONS.md（强制）!!
先执行: `cat joyme/owl-client/Tests/Tools/LESSONS.md`
包含导航、点击、地址栏等关键经验，不读会重复犯错。

## Goals 文件
{goals_file}

## 测试策略（强制分层）

对每个 goal，选择最高效的测试级别：

**Level 1 — XCTest（优先）**: 数据逻辑、协议解析、Service 行为
→ 写 XCTest 在 `joyme/owl-client/Tests/OWLClientTests/`，运行 `swift test`

**Level 2 — 协议注入**: Client 对 Host 事件的处理（dialog、file chooser、permission）
→ 用 `inject_event.swift` 发事件到 Client socket，验证 log/AX 响应

**Level 3 — E2E UI（仅在 L1/L2 无法验证时）**: 视觉布局、用户交互流程
→ AX 工具 + 截图 + 日志

每个 goal 必须标注使用的 Level。

## 关键工具提醒
- **导航**: `swift joyme/owl-client/Tests/Tools/navigate_tool.swift "URL"`（用剪贴板粘贴，避免 IME 问题）
- **点击 web 内容**: `swift $AX click_at <x> <y>`（用 postToPid 避免被其他窗口拦截）
- **协议注入**: `swift joyme/owl-client/Tests/Tools/inject_event.swift`
- **搜索框 ≠ 地址栏**: 搜索框在标题栏（search_bar_field），地址栏只在临时 tab 上（address_bar_field）

## 证据管理

证据目录: {edir}/
- 截图: goal_01_<描述>.png
- 测试输出: goal_01_test_output.txt
- 日志片段: goal_01_logs.txt
- 每个 goal 至少 1 个证据文件

## 状态更新（强制 — 必须在结束前执行）

### 1. 更新 tasks.json
编辑 `{TASKS_FILE}`，找到 id 为 "{task_id}" 的任务：
- 全部通过: "status" → "done"
- 有失败项: "status" → "failed"

### 2. 写入 result.md → {edir}/result.md

```markdown
# Test Result: {task['name']}
Attempt: {attempt}
Status: PASS / FAIL

## Goal Results
（逐条：Level / Status / Method / Evidence / Details）

## Summary
Total: N | Pass: X | Fail: Y
Failed goals: （如果有，含根因分析）
```
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
        f"feat(owl): {task['name']}\n\n"
        f"Agent-loop task: {task['id']}\n"
        f"Attempts: {task.get('attempts', 1)}\n"
        f"Evidence: agent-loop/evidence/{task['id']}/"
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
    total_cost = task.get("total_cost_usd", 0)

    # Evidence dir
    edir = EVIDENCE_DIR / task_id
    edir.mkdir(parents=True, exist_ok=True)

    # ── Phase 0: Plan (mandatory — generate goals if missing) ──
    goals_file = task.get("goals_file", "")
    if not goals_file:
        # Auto-generate goals path from task id
        goals_file = f"joyme/owl-client/Tests/Goals/{task_id}.md"
        task["goals_file"] = goals_file
        save_tasks(data)

    goals_path = PROJECT_ROOT / goals_file
    if not goals_path.exists():
        log(f"[{task_id}] Phase 0: PLAN (generating goals)", B)
        task["status"] = "planning"
        save_tasks(data)

        plan_prompt = prompt_plan(task)
        _, plan_cost, plan_err = run_claude(
            plan_prompt, agent="planner"
        )
        total_cost += plan_cost
        task["total_cost_usd"] = round(total_cost, 4)

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
    failure_info = None  # None = first attempt, string = previous failure

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

        impl_text, impl_cost, impl_err = run_claude(
            prompt, allowed_tools=MAIN_AGENT_TOOLS
        )
        total_cost += impl_cost
        task["total_cost_usd"] = round(total_cost, 4)
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
            failure_info = f"编译失败:\n{compile_err}"
            task["status"] = "compile_failed"
            save_tasks(data)
            log("Compile failed — will retry", R)
            continue

        if shutdown:
            break

        # ── Phase 3: Test (owl-test agent) ──
        log("Phase 3: TEST", B)
        task["status"] = "testing"
        save_tasks(data)

        test_prompt = prompt_test(task, attempts)
        test_text, test_cost, test_err = run_claude(
            test_prompt, agent="owl-test"
        )
        total_cost += test_cost
        task["total_cost_usd"] = round(total_cost, 4)

        # ── Phase 4: Read status from tasks.json (test agent updates it) ──
        data = load_tasks()  # re-read — test agent may have edited it
        task = find_task(data, task_id)

        if task["status"] == "done":
            task["completed_at"] = datetime.now().isoformat()
            task["total_cost_usd"] = round(total_cost, 4)
            save_tasks(data)
            log(f"PASSED: {task['name']} (${total_cost:.3f})", G)

            try:
                git_commit(task)
            except Exception as e:
                log(f"Git commit failed: {e}", R)

            return True

        elif task["status"] == "failed":
            # Read result.md for failure details to send to fix agent
            result_md = read_result_md(task_id)
            if result_md:
                failure_info = result_md
            elif test_text:
                # Fallback: use agent's text output
                failure_info = test_text[-5000:]
            else:
                failure_info = "Test agent did not produce result details."

            log(f"FAILED — will retry (${total_cost:.3f} so far)", R)

        else:
            # Test agent didn't update status (error or unexpected state)
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
        task["total_cost_usd"] = round(total_cost, 4)
        save_tasks(data)
        log(f"STUCK: {task['name']} after {attempts} attempts "
            f"(${total_cost:.3f})", Y)

    return False


# ── Main ─────────────────────────────────────────────────────


def main():
    global skip_permissions

    parser = argparse.ArgumentParser(
        description="Agent Loop — fully automated implement + test cycle"
    )
    parser.add_argument("--task", help="Run specific task by ID")
    parser.add_argument("--reset", help="Reset task status to pending")
    parser.add_argument("--dry-run", action="store_true", help="Preview tasks")
    parser.add_argument("--safe", action="store_true",
                        help="Require permission approval (default: skip)")
    parser.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help="Timeout per agent run in seconds")
    args = parser.parse_args()

    if args.safe:
        skip_permissions = False

    if not shutil.which("claude"):
        sys.exit("Error: 'claude' CLI not found in PATH")

    if not TASKS_FILE.exists():
        sys.exit(f"Error: {TASKS_FILE} not found.\n"
                 f"Copy tasks.example.json → tasks.json and fill in tasks.")

    data = load_tasks()

    # ── Reset command ──
    if args.reset:
        for t in data["tasks"]:
            if t["id"] == args.reset:
                t["status"] = "pending"
                t["attempts"] = 0
                t.pop("total_cost_usd", None)
                t.pop("completed_at", None)
                save_tasks(data)
                log(f"Reset: {t['name']}", Y)
                return
        sys.exit(f"Task not found: {args.reset}")

    # ── Filter tasks ──
    tasks = data["tasks"]
    if args.task:
        tasks = [t for t in tasks if t["id"] == args.task]
        if not tasks:
            sys.exit(f"Task not found: {args.task}")

    pending = [t for t in tasks if t.get("status") != "done"]
    if not pending:
        log("All tasks completed!", G)
        return

    # ── Dry run ──
    if args.dry_run:
        log("Tasks to process:")
        for t in pending:
            status = t.get("status", "pending")
            attempts = t.get("attempts", 0)
            print(f"  [{status:>14}] {t['id']}: {t['name']} "
                  f"(attempts: {attempts})")
        return

    # ── Run loop ──
    log(f"Starting agent loop: {len(pending)} task(s)")
    log(f"Permissions: "
        f"{'SKIP (autonomous)' if skip_permissions else 'require approval'}")
    start_time = time.time()
    completed = 0

    for task in pending:
        if shutdown:
            break
        if process_task(task["id"], data):
            completed += 1
        # Re-read data in case agents modified tasks.json
        data = load_tasks()

    # ── Summary ──
    elapsed = time.time() - start_time
    total_done = sum(1 for t in data["tasks"] if t.get("status") == "done")
    total = len(data["tasks"])
    total_cost = sum(t.get("total_cost_usd", 0) for t in data["tasks"])

    log("=" * 60)
    log(f"Summary: {total_done}/{total} tasks done | "
        f"This run: {completed} completed | "
        f"${total_cost:.3f} total | "
        f"{elapsed/60:.1f} min")
    log("=" * 60)


if __name__ == "__main__":
    main()
