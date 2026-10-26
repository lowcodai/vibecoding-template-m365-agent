#!/usr/bin/env python3
"""orchestrate.py — sequential DEV → REVIEW → TEST state machine (ADR-0005).

The orchestrator agent (Hermes today, ADR-0007) never codes: it writes a task contract
(.ai/tasks/TASK-XXXX.md) and calls this script. The script runs exactly one
agent role at a time against one git worktree per task, persists every
transition under .ai/runs/TASK-XXXX/, and stops at READY_FOR_APPROVAL or
BLOCKED. Approval is always a human action. What happens around it depends on the
repo's autonomy level (.ai/autonomy.yaml, ADR-0008): at `branch` and `trusted` this
script pushes the task branch and opens a draft PR at READY_FOR_APPROVAL; at
`trusted` it also merges the PR after `approve`, once its checks are green; at `yolo`
(ADR-0009) there is no human approval: it waits for CI, merges on green, and sends a red
CI back to DEV with the failing logs (at most `limits.max_ci_cycles` times).
DEV itself never pushes or merges.

DEV runs on Claude Code or, where the repo allows it (`executors:` in .ai/autonomy.yaml), on the
OpenHands CLI, chosen by the task's `executor:` (ADR-0010). REVIEW and TEST are always Claude Code.
A diff larger than the REVIEW budget is reviewed in chunks of consecutive commits.

Usage:
  orchestrate.py new     TASK-0042 --title "Add JWT refresh"
  orchestrate.py run     TASK-0042          # start or resume
  orchestrate.py status  TASK-0042
  orchestrate.py approve TASK-0042 --by "Jeremie"
  orchestrate.py rework  TASK-0042 --feedback "..."   # human sends it back to DEV

Requires: python3 >= 3.9, PyYAML, git, the agent CLI configured in
.ai/orchestration.yaml (Claude Code; the OpenHands CLI for `executor: openhands`),
.claude/hooks/autonomy.py, and gh at autonomy levels `branch`, `trusted` and `yolo`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.exit("orchestrate.py: PyYAML is required (pip install pyyaml)")

sys.dont_write_bytecode = True   # no .claude/hooks/__pycache__ in the repo
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / ".claude" / "hooks"))
try:
    import autonomy  # the repo's autonomy policy (ADR-0008)
except ImportError:  # pragma: no cover - environment guard
    sys.exit("orchestrate.py: .claude/hooks/autonomy.py is required (ADR-0008)")

# ─── States ──────────────────────────────────────────────────────────────────
PLANNED = "PLANNED"
DEV = "DEV"
REVIEW = "REVIEW"
TEST = "TEST"
READY = "READY_FOR_APPROVAL"
APPROVED = "APPROVED"
BLOCKED = "BLOCKED"
TERMINAL = {READY, APPROVED, BLOCKED}

TASK_ID_RE = re.compile(r"^TASK-\d{4}$")
DEFAULT_EXECUTOR = "claude-code"
VERDICT_RANK = {"ACCEPTED": 0, "CHANGES_REQUESTED": 1, "BLOCKED": 2}
CHARS_PER_TOKEN = 3.5  # conservative estimate for Qwen tokenizer on code/English


class OrchestrationError(Exception):
    pass


# ─── Helpers ─────────────────────────────────────────────────────────────────
def now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(args: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise OrchestrationError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def truncate(text: str, max_chars: int, label: str) -> str:
    """Keep head and tail: diffs and logs carry signal at both ends."""
    if max_chars <= 0:
        return f"[{label} omitted: no context budget left]"
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    tail = max_chars - head
    dropped = len(text) - max_chars
    return f"{text[:head]}\n\n[... {label}: {dropped} chars truncated to fit the context budget ...]\n\n{text[-tail:]}"


def extract_result_json(text: str) -> dict | None:
    """Return the last JSON object in the agent's final message."""
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = list(reversed(fenced))
    # Fallback: last balanced-looking {...} block
    start = text.rfind("{")
    while start != -1:
        candidates.append(text[start:])
        start = text.rfind("{", 0, start)
        if len(candidates) > 50:
            break
    for cand in candidates:
        cand = cand.strip()
        for end in range(len(cand), 0, -1):
            if cand[end - 1] != "}":
                continue
            try:
                obj = json.loads(cand[:end])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def openhands_final_text(stdout: str) -> str:
    """Final agent message of an `openhands --headless --json` run (JSONL mixed with status lines):
    the last FinishAction message, else the last agent MessageEvent text."""
    final = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("source") not in (None, "agent"):
            continue
        action = ev.get("action") or {}
        if isinstance(action, dict) and action.get("kind") == "FinishAction":
            final = str(action.get("message") or "")
        elif ev.get("kind") == "MessageEvent":
            content = (ev.get("llm_message") or {}).get("content") or []
            text = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
            if text.strip():
                final = text
    return final


# ─── Task contract ───────────────────────────────────────────────────────────
def parse_task(path: Path) -> tuple[dict, str]:
    raw = path.read_text()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, flags=re.S)
    if not m:
        raise OrchestrationError(f"{path}: missing YAML front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    return meta, m.group(2)


def section(body: str, title: str) -> str:
    # Prefix match: "## Implementation" also matches "## Implementation Notes" (adr-generator).
    m = re.search(rf"^## {re.escape(title)}[^\n]*\n(.*?)(?=^## |\Z)", body, flags=re.S | re.M)
    return m.group(1).strip() if m else ""


# ─── Orchestrator ────────────────────────────────────────────────────────────
class Orchestrator:
    def __init__(self, repo: Path, task_id: str):
        if not TASK_ID_RE.match(task_id):
            raise OrchestrationError(f"invalid task id {task_id!r} (expected TASK-NNNN)")
        self.repo = repo
        self.task_id = task_id
        self.ai = repo / ".ai"
        self.cfg = yaml.safe_load((self.ai / "orchestration.yaml").read_text())
        self.task_path = self.ai / "tasks" / f"{task_id}.md"
        self.run_dir = self.ai / "runs" / task_id
        self.state_path = self.run_dir / "state.json"
        self._check_config()
        # Read once per invocation; never from the environment (ADR-0008 DEC-003).
        self.level, self.level_reason = autonomy.effective_level(repo)

    # -- config ---------------------------------------------------------------
    def _check_config(self) -> None:
        ex = self.cfg.get("execution", {})
        if ex.get("mode") != "sequential" or ex.get("max_concurrent_agents") != 1:
            raise OrchestrationError("orchestration.yaml: only execution.mode=sequential with max_concurrent_agents=1 is supported")
        for role in ("dev", "review", "test"):
            agent = self.cfg["agents"][role]
            if agent["gateway"] not in self.cfg["gateways"]:
                raise OrchestrationError(f"agents.{role}.gateway {agent['gateway']!r} is not defined under gateways")
        for name, ex in (self.cfg["agents"]["dev"].get("executors") or {}).items():
            if name not in autonomy.EXECUTORS or name == DEFAULT_EXECUTOR:
                raise OrchestrationError(f"agents.dev.executors.{name}: unknown executor (expected {', '.join(autonomy.EXECUTORS[1:])})")
            if ex.get("gateway") not in self.cfg["gateways"]:
                raise OrchestrationError(f"agents.dev.executors.{name}.gateway {ex.get('gateway')!r} is not defined under gateways")

    def prefix(self) -> list[str]:
        """Command prefix that drops agents and validations to the runner's unprivileged user
        (ADR-0010 DEC-004). The runner may impose it with ORCHESTRATE_RUNNER_PREFIX: an isolation
        it adds, never a permission; otherwise `runner.prefix` from .ai/orchestration.yaml."""
        env = os.environ.get("ORCHESTRATE_RUNNER_PREFIX", "").strip()
        return shlex.split(env) if env else list(self.cfg.get("runner", {}).get("prefix") or [])

    def limit(self, key: str) -> int:
        return int(self.cfg.get("limits", {}).get(key, 3))

    # -- state ------------------------------------------------------------------
    def load_state(self) -> dict:
        if not self.state_path.exists():
            raise OrchestrationError(f"{self.task_id}: no run yet (use `run`)")
        return json.loads(self.state_path.read_text())

    def save_state(self, st: dict) -> None:
        st["updated_at"] = now()
        write_json_atomic(self.state_path, st)

    def log(self, msg: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with (self.run_dir / "timeline.log").open("a") as fh:
            fh.write(f"{now()} {msg}\n")
        print(f"[{self.task_id}] {msg}", flush=True)

    def transition(self, st: dict, new_state: str, reason: str) -> None:
        self.log(f"{st['state']} -> {new_state}: {reason}")
        st["history"].append({"at": now(), "from": st["state"], "to": new_state, "reason": reason})
        st["state"] = new_state
        self.save_state(st)

    # -- git / worktree ---------------------------------------------------------
    def ensure_worktree(self, st: dict) -> Path:
        wt = Path(st["worktree"])
        if wt.exists():
            return wt
        wt.parent.mkdir(parents=True, exist_ok=True)
        branch_exists = git(["branch", "--list", st["branch"]], self.repo) != ""
        if branch_exists:
            git(["worktree", "add", str(wt), st["branch"]], self.repo)
        else:
            git(["worktree", "add", "-b", st["branch"], str(wt), st["base_sha"]], self.repo)
        self.log(f"worktree ready: {wt} on {st['branch']}")
        return wt

    def init_state(self) -> dict:
        if not self.task_path.exists():
            raise OrchestrationError(f"missing task contract {self.task_path} (use `new`)")
        meta, _ = parse_task(self.task_path)
        if meta.get("id") != self.task_id:
            raise OrchestrationError(f"{self.task_path}: front matter id {meta.get('id')!r} != {self.task_id}")
        g = self.cfg.get("git", {})
        base = meta.get("base_branch") or g.get("base_branch", "main")
        wt_root = Path(g.get("worktrees_dir", "../.worktrees/{repo}").format(repo=self.repo.name))
        if not wt_root.is_absolute():
            wt_root = (self.repo / wt_root).resolve()
        st = {
            "task_id": self.task_id,
            "state": PLANNED,
            "base_branch": base,
            "base_sha": git(["rev-parse", base], self.repo),
            "branch": f"{g.get('branch_prefix', 'agent/')}{self.task_id}",
            "worktree": str(wt_root / self.task_id),
            "executor": meta.get("executor") or DEFAULT_EXECUTOR,
            "counters": {"dev_runs": 0, "review_cycles": 0, "test_cycles": 0},
            "feedback": None,
            "history": [],
            "created_at": now(),
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.save_state(st)
        self.log(f"run initialised (base {base}@{st['base_sha'][:10]}, autonomy {self.level}: {self.level_reason})")
        return st

    # -- executor (ADR-0010) -----------------------------------------------------
    def executor(self, st: dict) -> str:
        return st.get("executor") or DEFAULT_EXECUTOR

    def executor_problem(self, st: dict) -> str | None:
        """Why the task's DEV executor may not run here, or None."""
        name = self.executor(st)
        if name not in autonomy.EXECUTORS:
            return f"unknown executor {name!r} (expected one of {', '.join(autonomy.EXECUTORS)})"
        allowed, reason = autonomy.effective_executors(self.repo)
        if name not in allowed:
            return f"executor {name!r} not allowed in this repo ({reason})"
        if name != DEFAULT_EXECUTOR and name not in (self.cfg["agents"]["dev"].get("executors") or {}):
            return f"executor {name!r} not configured under agents.dev.executors in .ai/orchestration.yaml"
        return None

    # -- context budget ---------------------------------------------------------
    def budget_chars(self, role: str, fixed_text: str) -> int:
        agent = self.cfg["agents"][role]
        gw = self.cfg["gateways"][agent["gateway"]]
        tokens = gw["context_tokens"] - gw["max_output_tokens"] - agent.get("overhead_tokens", 15000)
        return int(tokens * CHARS_PER_TOKEN) - len(fixed_text)

    # -- agent invocation -------------------------------------------------------
    def run_agent(self, role: str, work_order: str, cwd: Path, executor: str = DEFAULT_EXECUTOR) -> dict:
        if role == "dev" and executor != DEFAULT_EXECUTOR:
            return self.run_openhands(work_order, cwd)
        agent = self.cfg["agents"][role]
        gw = self.cfg["gateways"][agent["gateway"]]
        required = list(agent.get("required_files", []))
        if role == "dev":
            required.append(".claude/hooks/autonomy.py")   # tool_guardian imports it
        missing = [f for f in dict.fromkeys(required) if not (cwd / f).is_file()]
        if missing:
            # Never run a role unguarded: hooks/settings must be committed on the base branch.
            return {"_fatal": f"{role} preflight: missing in worktree (commit them on the base branch): {', '.join(missing)}"}
        runner = self.cfg["runner"]
        role_prompt = (self.repo / agent["role_prompt"]).resolve()
        order_file = self.run_dir / f"{role}-work-order.md"
        order_file.write_text(work_order)

        subst = {
            "role_prompt_file": str(role_prompt),
            "settings_file": str((self.repo / runner.get("settings_file", ".claude/settings.json")).resolve()),
            "tools": ",".join(agent.get("tools", [])),
            "disallowed_tools": ",".join(dict.fromkeys(list(runner.get("disallowed_tools", []))
                                                       + autonomy.disallowed_tools(self.level))),
            "model": gw["model"],
            "instruction": "Execute the work order given on stdin. End with the JSON result block required by your role.",
        }
        cmd = self.prefix() + [part.format(**subst) for part in runner["command"]]
        cmd += [part.format(**subst) for part in agent.get("extra_args", [])]

        env = os.environ.copy()
        env.update({
            "ANTHROPIC_BASE_URL": gw["base_url"],
            "ANTHROPIC_MODEL": gw["model"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": gw["model"],
            "ANTHROPIC_DEFAULT_SONNET_MODEL": gw["model"],
            "ANTHROPIC_DEFAULT_OPUS_MODEL": gw["model"],
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(gw["max_output_tokens"]),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "ORCHESTRATE_ROLE": role,
            "ORCHESTRATE_TASK": self.task_id,
            "CLAUDE_HOOK_LOG_DIR": str(self.run_dir / "hooks"),
        })
        key_env = gw.get("api_key_env")
        if key_env and os.environ.get(key_env):
            env["ANTHROPIC_API_KEY"] = os.environ[key_env]

        self.log(f"{role.upper()} agent started (gateway {agent['gateway']}, cmd: {shlex.join(cmd[:len(self.prefix()) + 3])} ...)")
        try:
            proc = subprocess.run(cmd, input=work_order, cwd=cwd, env=env, text=True,
                                  capture_output=True, timeout=agent.get("timeout_sec", 3600))
        except subprocess.TimeoutExpired:
            return {"_error": f"{role} agent timed out after {agent.get('timeout_sec', 3600)}s"}
        (self.run_dir / f"{role}-raw-output.txt").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        if proc.returncode != 0:
            return {"_error": f"{role} agent exited {proc.returncode}: {proc.stderr.strip()[-500:]}"}

        text = proc.stdout
        usage = None
        if runner.get("output_format") == "claude-json":
            try:
                envelope = json.loads(proc.stdout)
                text = envelope.get("result", "") or ""
                usage = envelope.get("usage")
            except json.JSONDecodeError:
                pass
        if usage:
            self.log(f"{role.upper()} usage: {json.dumps(usage)}")
        result = extract_result_json(text)
        if result is None:
            return {"_error": f"{role} agent returned no parseable JSON result"}
        return result

    def run_openhands(self, work_order: str, cwd: Path) -> dict:
        """DEV on the OpenHands CLI, headless (ADR-0010). Runs in the isolated runner through
        runner.prefix; gets the role prompt and the work order in one file, the LLM route as env."""
        ex = self.cfg["agents"]["dev"]["executors"]["openhands"]
        gw = self.cfg["gateways"][ex["gateway"]]
        role_prompt = (self.repo / ex.get("role_prompt", ".ai/roles/dev-openhands.md")).resolve()
        if not role_prompt.is_file():
            return {"_fatal": f"openhands preflight: missing role prompt {role_prompt}"}
        order_file = self.run_dir / "dev-work-order.md"
        order_file.write_text(role_prompt.read_text().rstrip() + "\n\n---\n\n" + work_order)
        subst = {"work_order_file": str(order_file.resolve()), "model": gw["model"]}
        cmd = self.prefix() + [part.format(**subst) for part in ex["command"]]
        env = os.environ.copy()
        env.update({
            "LLM_MODEL": gw["model"],
            "LLM_BASE_URL": gw["base_url"],
            "LLM_API_KEY": os.environ.get(gw.get("api_key_env") or "", "") or "local",
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "ORCHESTRATE_ROLE": "dev",
            "ORCHESTRATE_TASK": self.task_id,
        })
        timeout = ex.get("timeout_sec", 14400)
        self.log(f"DEV agent started (executor openhands, gateway {ex['gateway']}, cmd: {shlex.join(cmd[:4])} ...)")
        try:
            proc = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"_error": f"dev agent (openhands) timed out after {timeout}s"}
        except OSError as exc:
            return {"_fatal": f"openhands preflight: cannot start {cmd[0]!r}: {exc}"}
        (self.run_dir / "dev-raw-output.txt").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
        if proc.returncode != 0:
            return {"_error": f"dev agent (openhands) exited {proc.returncode}: {proc.stderr.strip()[-500:]}"}
        result = extract_result_json(openhands_final_text(proc.stdout))
        if result is None:
            return {"_error": "dev agent (openhands) returned no parseable JSON result"}
        return result

    def call_role(self, role: str, work_order: str, cwd: Path, executor: str = DEFAULT_EXECUTOR,
                  result_name: str | None = None) -> dict:
        retries = int(self.cfg.get("limits", {}).get("agent_result_parse_retries", 1))
        for attempt in range(retries + 1):
            result = self.run_agent(role, work_order, cwd, executor)
            if "_fatal" in result:
                result = {"_error": result["_fatal"]}
                self.log(result["_error"])
                break
            if "_error" not in result:
                break
            self.log(f"{role.upper()} attempt {attempt + 1} failed: {result['_error']}")
        write_json_atomic(self.run_dir / f"{result_name or role}-result.json", result)
        return result

    # -- validations (deterministic, run by the orchestrator, never by an agent) --
    def run_validations(self, wt: Path, base_sha: str) -> list[dict]:
        meta, _ = parse_task(self.task_path)
        env = {**os.environ, "ORCHESTRATE_BASE_SHA": base_sha, "ORCHESTRATE_TASK": self.task_id,
               "CLAUDE_HOOK_LOG_DIR": str(self.run_dir / "hooks")}
        commands = list(self.cfg.get("validation", {}).get("commands", [])) + list(meta.get("validations") or [])
        tail_n = int(self.cfg.get("validation", {}).get("log_tail_lines", 200))
        results = []
        logs_dir = self.run_dir / "logs"
        logs_dir.mkdir(exist_ok=True)
        # Built-in, not configurable: rendered guard-rail files must match the declared level.
        problems = autonomy.check(wt)
        out = "\n".join(problems) or "autonomy-check: OK"
        (logs_dir / "autonomy-check.log").write_text(out + "\n")
        results.append({"name": "autonomy-check", "run": ".claude/hooks/autonomy.py check (built-in)",
                        "exit_code": 1 if problems else 0, "log_tail": out})
        self.log(f"validation 'autonomy-check' exit={1 if problems else 0}")
        prefix = self.prefix()   # validations run the agent's code: same isolation as the agents
        for c in commands:
            name, run = c["name"], c["run"]
            try:
                proc = subprocess.run(prefix + ["sh", "-c", run] if prefix else run, shell=not prefix, cwd=wt,
                                      text=True, capture_output=True, timeout=c.get("timeout_sec", 900), env=env)
                code, out = proc.returncode, proc.stdout + proc.stderr
            except subprocess.TimeoutExpired as exc:
                code, out = 124, f"TIMEOUT after {exc.timeout}s"
            (logs_dir / f"{name}.log").write_text(out)
            tail = "\n".join(out.splitlines()[-tail_n:])
            results.append({"name": name, "run": run, "exit_code": code, "log_tail": tail})
            self.log(f"validation {name!r} exit={code}")
        return results

    # -- work orders (role-specific context, PDF §"Optimisation des contextes") --
    def order_dev(self, st: dict) -> str:
        task = self.task_path.read_text()
        fb = st.get("feedback")
        parts = [
            f"# Work order — DEV — {self.task_id}",
            f"Branch: {st['branch']} (worktree = current directory). Commit locally; never push, merge or rebase.",
            "Before coding, read AGENTS.md and every ADR/PRD listed in the task front matter.",
            "## Task contract", task,
        ]
        if fb:
            parts += ["## Feedback to address (from previous cycle)", json.dumps(fb, indent=2, ensure_ascii=False)]
        text = "\n\n".join(parts)
        return truncate(text, self.budget_chars("dev", ""), "work order")

    def review_fixed(self, note: str = "") -> tuple[dict, str]:
        meta, body = parse_task(self.task_path)
        head = [
            f"# Work order — REVIEW — {self.task_id}",
            "You review a diff; you do not edit files." + (f" {note}" if note else ""),
            "## Objective", section(body, "Objective"),
            "## Scope", section(body, "Scope"),
            "## Out of scope", section(body, "Out of scope"),
            "## Acceptance criteria", section(body, "Acceptance criteria"),
        ]
        return meta, "\n\n".join(head)

    def adr_excerpts(self, meta: dict, budget: int) -> str:
        adr_text = ""
        for adr in meta.get("adrs") or []:
            p = self.repo / adr
            if p.exists():
                adr_body = p.read_text()
                adr_text += f"\n\n### {adr}\n\n" + (section(adr_body, "Decision") + "\n\n" + section(adr_body, "Implementation")).strip()
        return truncate(adr_text, min(len(adr_text), budget // 4), "ADR excerpts")

    def order_review(self, st: dict, wt: Path, rev_range: str | None = None, note: str = "") -> str:
        meta, fixed = self.review_fixed(note)
        budget = self.budget_chars("review", fixed)
        adr_text = self.adr_excerpts(meta, budget)
        rev_range = rev_range or f"{st['base_sha']}...HEAD"
        diff = git(["diff", rev_range], wt)
        stat = git(["diff", "--stat", rev_range], wt)
        diff_budget = budget - len(adr_text) - len(stat) - 200
        return "\n\n".join([fixed, "## Relevant ADR decisions", adr_text or "(none listed)",
                            "## Diff stat", stat, "## Diff", truncate(diff, diff_budget, "diff")])

    def review_chunks(self, st: dict, wt: Path) -> list[tuple[str, str]]:
        """[(rev_range, note)]: one range when the whole diff fits the REVIEW budget, else consecutive
        commits grouped so that each group's diff fits (ADR-0010 DEC-006)."""
        meta, fixed = self.review_fixed("x" * 200)          # room for the chunk note
        budget = self.budget_chars("review", fixed)
        avail = budget - len(self.adr_excerpts(meta, budget)) - 2000   # diff stat + headers
        base = st["base_sha"]
        if len(git(["diff", f"{base}...HEAD"], wt)) <= avail:
            return [(f"{base}...HEAD", "")]
        commits = git(["rev-list", "--reverse", "--first-parent", f"{base}..HEAD"], wt).split()
        sizes = [len(git(["diff", f"{c}^!"], wt)) for c in commits]
        groups, cur, size = [], [], 0
        for c, s in zip(commits, sizes):
            if cur and size + s > avail:
                groups.append(cur)
                cur, size = [], 0
            cur.append(c)
            size += s
        if cur:
            groups.append(cur)
        out, prev = [], base
        for i, g in enumerate(groups, 1):
            note = (f"Chunk {i}/{len(groups)} of a large task: commits {g[0][:10]}..{g[-1][:10]} only. "
                    "Judge this chunk; other chunks are reviewed separately.")
            out.append((f"{prev}..{g[-1]}", note))
            prev = g[-1]
        return out

    def order_test(self, st: dict, validations: list[dict]) -> str:
        _, body = parse_task(self.task_path)
        head = [
            f"# Work order — TEST — {self.task_id}",
            "You analyse validation results; you do not edit files.",
            "## Acceptance criteria", section(body, "Acceptance criteria"),
        ]
        fixed = "\n\n".join(head)
        budget = self.budget_chars("test", fixed)
        per = max(budget // max(len(validations), 1), 0)
        blocks = []
        for v in validations:
            blocks.append(f"### {v['name']} — exit code {v['exit_code']}\n`{v['run']}`\n```\n{truncate(v['log_tail'], per - 200, 'log')}\n```")
        return "\n\n".join([fixed, "## Validation results (run by the orchestrator)", *blocks])

    # -- main loop --------------------------------------------------------------
    def step(self, st: dict) -> None:
        s, c = st["state"], st["counters"]
        if s == PLANNED:
            problem = self.executor_problem(st)
            if problem:
                self.transition(st, BLOCKED, problem)
                return
            self.ensure_worktree(st)
            self.transition(st, DEV, f"task contract accepted (executor {self.executor(st)})")
            return

        wt = self.ensure_worktree(st)
        if s == DEV:
            if c["dev_runs"] >= self.limit("max_dev_runs"):
                self.transition(st, BLOCKED, f"max_dev_runs={self.limit('max_dev_runs')} reached")
                return
            problem = self.executor_problem(st)
            if problem:
                self.transition(st, BLOCKED, problem)
                return
            c["dev_runs"] += 1
            self.save_state(st)
            res = self.call_role("dev", self.order_dev(st), wt, self.executor(st))
            if "_error" in res:
                self.transition(st, BLOCKED, res["_error"])
                return
            if res.get("status") == "BLOCKED":
                self.transition(st, BLOCKED, f"DEV blocked: {res.get('reason', 'no reason given')}")
                return
            dirty = git(["status", "--porcelain"], wt)
            ahead = git(["rev-list", "--count", f"{st['base_sha']}..HEAD"], wt)
            if dirty and self.cfg.get("git", {}).get("require_clean_tree_after_dev", True):
                st["feedback"] = {"source": "orchestrator", "issue": "uncommitted changes left in the worktree", "files": dirty.splitlines()[:30]}
                self.transition(st, DEV, "worktree not clean after DEV")
                return
            if ahead == "0":
                st["feedback"] = {"source": "orchestrator", "issue": "no commit was created on the task branch"}
                self.transition(st, DEV, "no commit produced")
                return
            protected = list(dict.fromkeys(list(self.cfg.get("git", {}).get("protected_paths", []))
                                           + list(autonomy.PROTECTED_PATHS)))
            changed = git(["diff", "--name-only", f"{st['base_sha']}..HEAD"], wt).splitlines()
            touched = [f for f in changed if any(f == p.rstrip("/") or f.startswith(p.rstrip("/") + "/") for p in protected)]
            if touched:
                # Deterministic guard: hooks, deny rules and the secrets allowlist are human-owned.
                st["feedback"] = {"source": "orchestrator", "issue": "protected paths modified — restore them in a new commit with "
                                  f"`git checkout {st['base_sha'][:12]} -- <file>` (delete files that did not exist)",
                                  "files": touched[:30]}
                self.transition(st, DEV, f"protected paths modified: {', '.join(touched[:5])}")
                return
            st["feedback"] = None
            self.transition(st, REVIEW, f"DEV done ({ahead} commit(s) ahead of base)")
            return

        if s == REVIEW:
            chunks = self.review_chunks(st, wt)
            if len(chunks) > 1:
                self.log(f"REVIEW in {len(chunks)} chunks (diff larger than the review budget)")
            verdict, findings, summaries, res = "ACCEPTED", [], [], {}
            for i, (rev_range, note) in enumerate(chunks, 1):
                name = "review" if len(chunks) == 1 else f"review-{i}"
                res = self.call_role("review", self.order_review(st, wt, rev_range, note), wt, result_name=name)
                v = res.get("verdict") if "_error" not in res else "BLOCKED"
                v = v if v in VERDICT_RANK else "BLOCKED"
                verdict = max(verdict, v, key=VERDICT_RANK.get)
                prefix = f"[chunk {i}/{len(chunks)}] " if len(chunks) > 1 else ""
                findings += [dict(f, chunk=i) if len(chunks) > 1 and isinstance(f, dict) else f
                             for f in res.get("findings", []) or []]
                summaries.append(prefix + str(res.get("_error") or res.get("summary", "no summary")))
                if v == "BLOCKED":
                    break
            if len(chunks) > 1:
                res = {"verdict": verdict, "summary": " ".join(summaries), "findings": findings}
                write_json_atomic(self.run_dir / "review-result.json", res)
            if verdict == "ACCEPTED":
                self.transition(st, TEST, "review accepted")
            elif verdict == "CHANGES_REQUESTED":
                c["review_cycles"] += 1
                if c["review_cycles"] > self.limit("max_review_cycles"):
                    self.transition(st, BLOCKED, f"max_review_cycles={self.limit('max_review_cycles')} exceeded")
                    return
                st["feedback"] = {"source": "review", "findings": findings}
                self.transition(st, DEV, f"review requested changes (cycle {c['review_cycles']})")
            else:
                self.transition(st, BLOCKED, f"review blocked: {' '.join(summaries)}")
            return

        if s == TEST:
            validations = self.run_validations(wt, st["base_sha"])
            write_json_atomic(self.run_dir / "validations.json", {"results": validations})
            res = self.call_role("test", self.order_test(st, validations), wt)
            verdict = res.get("verdict") if "_error" not in res else "BLOCKED"
            failed = [v["name"] for v in validations if v["exit_code"] != 0]
            if verdict == "PASS" and failed:
                # Deterministic guard: an LLM verdict never overrides a red command.
                self.log(f"TEST verdict PASS overridden to FAIL: failing validations {failed}")
                verdict = "FAIL"
                res.setdefault("failures", []).extend({"validation": n, "issue": "non-zero exit code"} for n in failed)
            if verdict == "PASS":
                self.transition(st, READY, "all validations green, test verdict PASS")
                if autonomy.at_least(self.level, "branch"):
                    self.publish(st, wt)
                if self.level == "yolo":
                    self.autoland(st)
            elif verdict == "FAIL":
                c["test_cycles"] += 1
                if c["test_cycles"] > self.limit("max_test_cycles"):
                    self.transition(st, BLOCKED, f"max_test_cycles={self.limit('max_test_cycles')} exceeded")
                    return
                st["feedback"] = {"source": "test", "failures": res.get("failures", []), "missing_cases": res.get("missing_cases", [])}
                self.transition(st, DEV, f"tests failed (cycle {c['test_cycles']})")
            else:
                self.transition(st, BLOCKED, f"test blocked: {res.get('_error') or res.get('summary', 'no summary')}")
            return

        raise OrchestrationError(f"unexpected state {s}")

    # -- publication (autonomy `branch`/`trusted`; deterministic, never done by an agent) --
    def _cmd(self, args: list[str], cwd: Path) -> tuple[int, str]:
        try:
            proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def publish(self, st: dict, wt: Path) -> None:
        """Push the task branch and open a PR (draft below `yolo`). A failure is recorded, never fatal.

        Idempotent: after a CI fix cycle the branch is pushed again and the existing PR is kept."""
        branch, base = st["branch"], st["base_branch"]
        prefix = self.cfg.get("git", {}).get("branch_prefix", "agent/")
        previous = st.get("publish") or {}
        pub = {"pushed": False}
        if not branch.startswith(prefix) or branch == base:
            pub["error"] = f"refusing to push {branch!r} (not under {prefix!r} or equal to {base!r})"
        else:
            code, out = self._cmd(["git", "push", "-u", "origin", f"{branch}:refs/heads/{branch}"], wt)
            if code != 0:
                pub["error"] = f"git push failed: {out[-500:]}"
            elif previous.get("pr_url"):
                pub.update(pushed=True, pr_url=previous["pr_url"])
            else:
                pub["pushed"] = True
                meta, _ = parse_task(self.task_path)
                yolo = self.level == "yolo"
                body = (f"Task contract: `.ai/tasks/{self.task_id}.md`. REVIEW accepted, validations green "
                        f"(READY_FOR_APPROVAL). " + ("Autonomy `yolo`: merged by the orchestrator once CI is green."
                        if yolo else f"Approve with `scripts/orchestrate.py approve {self.task_id} --by <name>`."))
                code, out = self._cmd(["gh", "pr", "create"] + ([] if yolo else ["--draft"]) +
                                      ["--base", base, "--head", branch,
                                       "--title", f"{self.task_id}: {meta.get('title') or 'task'}", "--body", body], wt)
                if code == 0:
                    pub["pr_url"] = out.splitlines()[-1].strip() if out else ""
                else:
                    pub["pr_error"] = f"gh pr create failed: {out[-500:]}"
        st["publish"] = pub
        self.save_state(st)
        self.log("published: " + (f"pushed {branch}" + (f", draft PR {pub['pr_url']}" if pub.get("pr_url") else
                                                         f" ({pub.get('pr_error', 'no PR')})")
                                  if pub["pushed"] else pub["error"]))

    def wait_for_checks(self, pr: str) -> tuple[str, str]:
        """Poll `gh pr checks` until a verdict: green | none (no check configured) | red | timeout."""
        lim = self.cfg.get("limits", {})
        timeout = float(lim.get("ci_timeout_sec", 1800))
        poll = float(lim.get("ci_poll_sec", 20))
        grace = float(lim.get("ci_no_checks_grace_sec", 90))   # checks may take a moment to register
        start = time.monotonic()
        while True:
            code, out = self._cmd(["gh", "pr", "checks", pr], self.repo)
            elapsed = time.monotonic() - start
            if code == 0:
                return "green", out
            if "no checks reported" in out.lower():
                if elapsed >= grace:
                    return "none", out
            elif code != 8:                                    # 8 = checks still pending
                return "red", out
            if elapsed >= timeout:
                return "timeout", out
            time.sleep(poll)

    def ci_failure_logs(self, checks_output: str) -> list[dict]:
        """Failed-step logs of the failing runs (at most 3), for DEV to fix."""
        logs = []
        for run_id in list(dict.fromkeys(re.findall(r"/actions/runs/(\d+)", checks_output)))[:3]:
            _code, out = self._cmd(["gh", "run", "view", run_id, "--log-failed"], self.repo)
            logs.append({"run": run_id, "log_tail": "\n".join(out.splitlines()[-150:])})
        return logs

    def autoland(self, st: dict) -> None:
        """Autonomy `yolo` (ADR-0009): merge on green CI, send red CI back to DEV, no human approval."""
        pub = st.get("publish") or {}
        pr = pub.get("pr_url")
        if not pr:
            self.transition(st, BLOCKED, f"autonomy yolo: no pull request ({pub.get('error') or pub.get('pr_error')})")
            return
        status, out = self.wait_for_checks(pr)
        if status in ("green", "none"):
            code, mout = self._cmd(["gh", "pr", "merge", pr, "--squash"], self.repo)
            if code == 0:
                st["merge"] = {"merged": True, "checks": status, "by": "orchestrator (autonomy yolo)"}
                self.transition(st, APPROVED, f"merged autonomously (autonomy yolo, checks {status}): {pr}")
            else:
                st["merge"] = {"merged": False, "error": f"gh pr merge failed: {mout[-300:]}"}
                self.transition(st, BLOCKED, st["merge"]["error"])
            return
        if status == "timeout":
            self.transition(st, BLOCKED, f"CI on {pr} did not finish within limits.ci_timeout_sec")
            return
        c = st["counters"]
        c["ci_cycles"] = c.get("ci_cycles", 0) + 1
        if c["ci_cycles"] > self.limit("max_ci_cycles"):
            self.transition(st, BLOCKED, f"max_ci_cycles={self.limit('max_ci_cycles')} exceeded: CI still red on {pr}")
            return
        st["feedback"] = {"source": "ci", "pr": pr, "checks": out[-3000:], "failed_logs": self.ci_failure_logs(out),
                          "issue": "the pull request's CI is red: fix the code so every check passes"}
        self.transition(st, DEV, f"CI red on {pr} (cycle {c['ci_cycles']})")

    def merge(self, st: dict) -> None:
        """Autonomy `trusted`, after human approval: merge the task PR once its checks are green."""
        pr = (st.get("publish") or {}).get("pr_url")
        result = {"merged": False}
        cwd = self.repo
        if not pr:
            result["error"] = "no pull request recorded for this task: merge manually"
        else:
            self._cmd(["gh", "pr", "ready", pr], cwd)   # drafts cannot be merged; harmless if already ready
            code, out = self._cmd(["gh", "pr", "checks", pr], cwd)
            if code != 0:
                result["error"] = f"checks not all green: {out[-300:]}"
            else:
                code, out = self._cmd(["gh", "pr", "merge", pr, "--squash"], cwd)
                result.update({"merged": True} if code == 0 else {"error": f"gh pr merge failed: {out[-300:]}"})
        st["merge"] = result
        self.save_state(st)
        self.log(f"merged {pr}" if result["merged"] else f"not merged — {result['error']}")

    def run(self) -> str:
        st = json.loads(self.state_path.read_text()) if self.state_path.exists() else self.init_state()
        while st["state"] not in TERMINAL:
            self.step(st)
        self.log(f"stopped in {st['state']}")
        return st["state"]


# ─── Global lock: one agent at a time on the whole repo ─────────────────────
class RepoLock:
    """Repo lock, plus `execution.shared_lock` when set: one coding agent per model (ADR-0010 DEC-008)."""

    def __init__(self, repo: Path, shared: str | None = None):
        self.paths = [repo / ".ai" / "runs" / ".lock"]
        if shared:
            self.paths.append(Path(os.path.expanduser(shared)))
        self.fhs = []

    def __enter__(self):
        for path in self.paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = path.open("a+")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fh.close()
                self.__exit__()
                raise OrchestrationError(f"another orchestration run holds {path} (one agent at a time)")
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            self.fhs.append(fh)
        return self

    def __exit__(self, *exc):
        for fh in self.fhs:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
        self.fhs = []


# ─── CLI ─────────────────────────────────────────────────────────────────────
def cmd_new(repo: Path, task_id: str, title: str) -> None:
    if not TASK_ID_RE.match(task_id):
        raise OrchestrationError(f"invalid task id {task_id!r}")
    dest = repo / ".ai" / "tasks" / f"{task_id}.md"
    if dest.exists():
        raise OrchestrationError(f"{dest} already exists")
    tmpl = (repo / ".ai" / "tasks" / "TASK-template.md").read_text()
    dest.write_text(tmpl.replace("TASK-XXXX", task_id).replace("{{TITLE}}", title))
    print(dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", help="repository root (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("task"); p.add_argument("--title", required=True)
    p = sub.add_parser("run"); p.add_argument("task")
    p = sub.add_parser("status"); p.add_argument("task")
    p = sub.add_parser("approve"); p.add_argument("task"); p.add_argument("--by", required=True)
    p = sub.add_parser("rework"); p.add_argument("task"); p.add_argument("--feedback", required=True)
    args = ap.parse_args(argv)
    repo = Path(args.repo).resolve()

    try:
        if args.cmd == "new":
            cmd_new(repo, args.task, args.title)
            return 0
        orch = Orchestrator(repo, args.task)
        if args.cmd == "status":
            st = orch.load_state()
            print(json.dumps({k: st.get(k) for k in ("task_id", "state", "executor", "branch", "worktree", "counters", "feedback")}, indent=2))
            return 0
        with RepoLock(repo, (orch.cfg.get("execution") or {}).get("shared_lock")):
            if args.cmd == "run":
                final = orch.run()
                return 0 if final in (READY, APPROVED) else 2
            st = orch.load_state()
            if args.cmd == "approve":
                if st["state"] != READY:
                    raise OrchestrationError(f"cannot approve from {st['state']}")
                if orch.level in ("trusted", "yolo"):
                    orch.transition(st, APPROVED, f"human approval by {args.by} — orchestrator merges (autonomy trusted)")
                    orch.merge(st)
                else:
                    orch.transition(st, APPROVED, f"human approval by {args.by} — merge {st['branch']} manually")
                return 0
            if args.cmd == "rework":
                if st["state"] not in (READY, BLOCKED):
                    raise OrchestrationError(f"cannot rework from {st['state']}")
                st["feedback"] = {"source": "human", "issue": args.feedback}
                st["counters"]["dev_runs"] = min(st["counters"]["dev_runs"], orch.limit("max_dev_runs") - 1)
                orch.transition(st, DEV, "human requested rework")
                return 0
    except OrchestrationError as exc:
        print(f"orchestrate.py: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
