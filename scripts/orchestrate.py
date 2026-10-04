#!/usr/bin/env python3
"""orchestrate.py — sequential DEV → REVIEW → TEST state machine (ADR-0005).

Hermes (the Engineering-Manager agent) never codes: it writes a task contract
(.ai/tasks/TASK-XXXX.md) and calls this script. The script runs exactly one
Claude Code role at a time against one git worktree per task, persists every
transition under .ai/runs/TASK-XXXX/, and stops at READY_FOR_APPROVAL or
BLOCKED. Merging is always a human action.

Usage:
  orchestrate.py new     TASK-0042 --title "Add JWT refresh"
  orchestrate.py run     TASK-0042          # start or resume
  orchestrate.py status  TASK-0042
  orchestrate.py approve TASK-0042 --by "Jeremie"
  orchestrate.py rework  TASK-0042 --feedback "..."   # human sends it back to DEV

Requires: python3 >= 3.9, PyYAML, git, and the agent CLI configured in
.ai/orchestration.yaml (Claude Code by default).
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
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    sys.exit("orchestrate.py: PyYAML is required (pip install pyyaml)")

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

    # -- config ---------------------------------------------------------------
    def _check_config(self) -> None:
        ex = self.cfg.get("execution", {})
        if ex.get("mode") != "sequential" or ex.get("max_concurrent_agents") != 1:
            raise OrchestrationError("orchestration.yaml: only execution.mode=sequential with max_concurrent_agents=1 is supported")
        for role in ("dev", "review", "test"):
            agent = self.cfg["agents"][role]
            if agent["gateway"] not in self.cfg["gateways"]:
                raise OrchestrationError(f"agents.{role}.gateway {agent['gateway']!r} is not defined under gateways")

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
            "counters": {"dev_runs": 0, "review_cycles": 0, "test_cycles": 0},
            "feedback": None,
            "history": [],
            "created_at": now(),
        }
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.save_state(st)
        self.log(f"run initialised (base {base}@{st['base_sha'][:10]})")
        return st

    # -- context budget ---------------------------------------------------------
    def budget_chars(self, role: str, fixed_text: str) -> int:
        agent = self.cfg["agents"][role]
        gw = self.cfg["gateways"][agent["gateway"]]
        tokens = gw["context_tokens"] - gw["max_output_tokens"] - agent.get("overhead_tokens", 15000)
        return int(tokens * CHARS_PER_TOKEN) - len(fixed_text)

    # -- agent invocation -------------------------------------------------------
    def run_agent(self, role: str, work_order: str, cwd: Path) -> dict:
        agent = self.cfg["agents"][role]
        gw = self.cfg["gateways"][agent["gateway"]]
        missing = [f for f in agent.get("required_files", []) if not (cwd / f).is_file()]
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
            "disallowed_tools": ",".join(runner.get("disallowed_tools", [])),
            "model": gw["model"],
            "instruction": "Execute the work order given on stdin. End with the JSON result block required by your role.",
        }
        cmd = [part.format(**subst) for part in runner["command"]]
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

        self.log(f"{role.upper()} agent started (gateway {agent['gateway']}, cmd: {shlex.join(cmd[:3])} ...)")
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

    def call_role(self, role: str, work_order: str, cwd: Path) -> dict:
        retries = int(self.cfg.get("limits", {}).get("agent_result_parse_retries", 1))
        for attempt in range(retries + 1):
            result = self.run_agent(role, work_order, cwd)
            if "_fatal" in result:
                result = {"_error": result["_fatal"]}
                self.log(result["_error"])
                break
            if "_error" not in result:
                break
            self.log(f"{role.upper()} attempt {attempt + 1} failed: {result['_error']}")
        write_json_atomic(self.run_dir / f"{role}-result.json", result)
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
        for c in commands:
            name, run = c["name"], c["run"]
            try:
                proc = subprocess.run(run, shell=True, cwd=wt, text=True, capture_output=True,
                                      timeout=c.get("timeout_sec", 900), env=env)
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

    def order_review(self, st: dict, wt: Path) -> str:
        meta, body = parse_task(self.task_path)
        head = [
            f"# Work order — REVIEW — {self.task_id}",
            "You review a diff; you do not edit files.",
            "## Objective", section(body, "Objective"),
            "## Scope", section(body, "Scope"),
            "## Out of scope", section(body, "Out of scope"),
            "## Acceptance criteria", section(body, "Acceptance criteria"),
        ]
        fixed = "\n\n".join(head)
        budget = self.budget_chars("review", fixed)
        adr_text = ""
        for adr in meta.get("adrs") or []:
            p = self.repo / adr
            if p.exists():
                adr_body = p.read_text()
                adr_text += f"\n\n### {adr}\n\n" + (section(adr_body, "Decision") + "\n\n" + section(adr_body, "Implementation")).strip()
        diff = git(["diff", f"{st['base_sha']}...HEAD"], wt)
        stat = git(["diff", "--stat", f"{st['base_sha']}...HEAD"], wt)
        adr_budget = min(len(adr_text), budget // 4)
        adr_text = truncate(adr_text, adr_budget, "ADR excerpts")
        diff_budget = budget - len(adr_text) - len(stat) - 200
        return "\n\n".join([fixed, "## Relevant ADR decisions", adr_text or "(none listed)",
                            "## Diff stat", stat, "## Diff", truncate(diff, diff_budget, "diff")])

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
            self.ensure_worktree(st)
            self.transition(st, DEV, "task contract accepted")
            return

        wt = self.ensure_worktree(st)
        if s == DEV:
            if c["dev_runs"] >= self.limit("max_dev_runs"):
                self.transition(st, BLOCKED, f"max_dev_runs={self.limit('max_dev_runs')} reached")
                return
            c["dev_runs"] += 1
            self.save_state(st)
            res = self.call_role("dev", self.order_dev(st), wt)
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
            st["feedback"] = None
            self.transition(st, REVIEW, f"DEV done ({ahead} commit(s) ahead of base)")
            return

        if s == REVIEW:
            res = self.call_role("review", self.order_review(st, wt), wt)
            verdict = res.get("verdict") if "_error" not in res else "BLOCKED"
            if verdict == "ACCEPTED":
                self.transition(st, TEST, "review accepted")
            elif verdict == "CHANGES_REQUESTED":
                c["review_cycles"] += 1
                if c["review_cycles"] > self.limit("max_review_cycles"):
                    self.transition(st, BLOCKED, f"max_review_cycles={self.limit('max_review_cycles')} exceeded")
                    return
                st["feedback"] = {"source": "review", "findings": res.get("findings", [])}
                self.transition(st, DEV, f"review requested changes (cycle {c['review_cycles']})")
            else:
                self.transition(st, BLOCKED, f"review blocked: {res.get('_error') or res.get('summary', 'no summary')}")
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

    def run(self) -> str:
        st = json.loads(self.state_path.read_text()) if self.state_path.exists() else self.init_state()
        while st["state"] not in TERMINAL:
            self.step(st)
        self.log(f"stopped in {st['state']}")
        return st["state"]


# ─── Global lock: one agent at a time on the whole repo ─────────────────────
class RepoLock:
    def __init__(self, repo: Path):
        self.path = repo / ".ai" / "runs" / ".lock"
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OrchestrationError("another orchestration run holds .ai/runs/.lock (max_concurrent_agents=1)")
        self.fh.write(str(os.getpid()))
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


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
            print(json.dumps({k: st[k] for k in ("task_id", "state", "branch", "worktree", "counters", "feedback")}, indent=2))
            return 0
        with RepoLock(repo):
            if args.cmd == "run":
                final = orch.run()
                return 0 if final == READY else 2
            st = orch.load_state()
            if args.cmd == "approve":
                if st["state"] != READY:
                    raise OrchestrationError(f"cannot approve from {st['state']}")
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
