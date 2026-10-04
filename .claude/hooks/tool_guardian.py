#!/usr/bin/env python3
"""tool_guardian.py — Claude Code PreToolUse hook (matcher: Bash).

Port of the Copilot `tool-guardian` hook (hooks/tool-guardian/ in vibecoding-copilot-governance)
to the Claude Code hook protocol, tightened for the sequential coding team (ADR-0005):

- input:  JSON on stdin — {"tool_name": "Bash", "tool_input": {"command": "..."}, "cwd": ...}
- block:  exit 2, reason on stderr (Claude Code feeds it back to the model)
- allow:  exit 0

Fail-closed: an internal error blocks the command (exit 2) instead of silently letting it run.

Environment (set by humans, never by the agent):
  GUARD_MODE            block (default) | warn
  SKIP_TOOL_GUARD       "true" disables the hook
  TOOL_GUARD_ALLOWLIST  comma-separated substrings; a command containing one is not scanned
  CLAUDE_HOOK_LOG_DIR   JSONL log directory (orchestrate.py points it at the run directory);
                        default ~/.local/state/claude-hooks/<project>. Never inside the repo:
                        logs would dirty the worktree.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import sys
from pathlib import Path

# (category, severity, regex, suggestion) — matched case-insensitively on the whole command
PATTERNS: list[tuple[str, str, str, str]] = [
    # Agent git policy (ADR-0005): agents commit locally; humans push and merge.
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?push\b", "Agents never push: the human pushes after approval"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?merge\b", "Agents never merge: the human merges after approval"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?rebase\b", "Do not rewrite history; add a new commit instead"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?branch\s+(?:\S+\s+)*-D\b", "Do not delete branches"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?worktree\b", "Worktrees are managed by scripts/orchestrate.py"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?(?:checkout|switch)\s+(?:-\S+\s+)*(?:main|master)\b", "Stay on the task branch"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?commit\b[^;&|]*--no-verify", "Do not bypass commit hooks"),
    ("agent_git_policy", "high", r"\bgh\s+pr\s+merge\b", "Agents never merge"),
    # Destructive git operations
    ("destructive_git_ops", "critical", r"\bgit\s+push\b[^;&|]*(?:--force\b|\s-f\b)", "Never force-push"),
    ("destructive_git_ops", "high", r"\bgit\s+reset\s+(?:\S+\s+)*--hard\b", "Use 'git stash' or 'git reset --soft'"),
    ("destructive_git_ops", "high", r"\bgit\s+clean\s+(?:\S+\s+)*-[a-zA-Z]*f", "Preview with 'git clean -n' and remove files explicitly"),
    ("destructive_git_ops", "high", r"\bgit\s+(?:checkout\s+--|restore)\s+\.(?:\s|$)", "Do not discard all changes; revert specific files"),
    # Database destruction
    ("database_destruction", "critical", r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b", "Write a migration with a rollback instead"),
    ("database_destruction", "critical", r"\bTRUNCATE\s+(?:TABLE\s+)?[\w.\"`]+", "Use DELETE ... WHERE, or a migration"),
    ("database_destruction", "high", r"\bDELETE\s+FROM\s+[\w.\"`]+\s*(?:;|\"|'|$)", "Add a WHERE clause"),
    # Permission abuse
    ("permission_abuse", "high", r"\bchmod\s+(?:-R\s+)?(?:0?777|a\+rwx)\b", "Use 755 for directories, 644 for files"),
    # Network exfiltration / remote code execution
    ("network_exfiltration", "critical", r"\b(?:curl|wget)\b[^\n]*\|\s*(?:sudo\s+)?(?:ba|z|da)?sh\b", "Download, review, then execute"),
    ("network_exfiltration", "high", r"\bcurl\b[^\n]*(?:--data(?:-binary|-raw)?|-d|-F|--form)\s+\S*@", "Do not upload local files"),
    ("network_exfiltration", "high", r"\bcurl\b[^\n]*(?:--upload-file|\s-T)\s", "Do not upload local files"),
    # System danger / publication
    ("system_danger", "high", r"(?:^|[;&|]\s*)sudo\s", "Run with least privilege; no sudo"),
    ("system_danger", "high", r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b", "Publishing is a human action"),
    ("system_danger", "high", r"\bterraform\s+(?:apply|destroy)\b|\bkubectl\s+delete\b|\bhelm\s+(?:uninstall|delete)\b", "Infra changes are applied by humans or a Runbook"),
    ("system_danger", "critical", r"\bmkfs(?:\.\w+)?\b|\bdd\b[^\n]*\bof=/dev/", "Never write to block devices"),
]

DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", ".", "./", "./*", "..", "../", "*"}


def split_segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||[;|\n]", command) if s.strip()]


def check_rm(command: str) -> list[dict]:
    """Structural check of rm invocations (regexes alone flag `rm -rf ./build`)."""
    findings = []
    for seg in split_segments(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        while tokens and (tokens[0] in ("sudo", "env", "command", "exec") or re.match(r"^\w+=", tokens[0])):
            tokens = tokens[1:]
        if not tokens or os.path.basename(tokens[0]) not in ("rm", "unlink"):
            continue
        flags = [t for t in tokens[1:] if t.startswith("-")]
        targets = [t for t in tokens[1:] if not t.startswith("-")]
        recursive = any(f == "--recursive" or (not f.startswith("--") and re.search(r"[rR]", f)) for f in flags)
        for t in targets:
            norm = t.rstrip("/") or "/"
            base = os.path.basename(t.rstrip("/"))
            if recursive and (t in DANGEROUS_RM_TARGETS or norm in DANGEROUS_RM_TARGETS):
                findings.append(finding("destructive_file_ops", "critical", seg, "Remove specific paths, never root, home, cwd or parent"))
            elif base == ".git":
                findings.append(finding("destructive_file_ops", "critical", seg, "Never delete the .git directory"))
            elif base == ".env" or base.startswith(".env."):
                findings.append(finding("destructive_file_ops", "critical", seg, "Do not delete environment files"))
    return findings


def finding(category: str, severity: str, match: str, suggestion: str) -> dict:
    return {"category": category, "severity": severity, "match": match[:120], "suggestion": suggestion}


def scan(command: str) -> list[dict]:
    found = check_rm(command)
    for category, severity, regex, suggestion in PATTERNS:
        m = re.search(regex, command, flags=re.IGNORECASE)
        if m:
            found.append(finding(category, severity, m.group(0), suggestion))
    return found


def log(event: dict) -> None:
    try:
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).name
        log_dir = Path(os.environ.get("CLAUDE_HOOK_LOG_DIR") or Path.home() / ".local/state/claude-hooks" / project)
        log_dir.mkdir(parents=True, exist_ok=True)
        event["timestamp"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (log_dir / "tool-guardian.jsonl").open("a") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass  # logging must never decide the outcome


def main() -> int:
    if os.environ.get("SKIP_TOOL_GUARD") == "true":
        return 0
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "") or ""
    allow = [p.strip() for p in os.environ.get("TOOL_GUARD_ALLOWLIST", "").split(",") if p.strip()]
    if any(p in command for p in allow):
        log({"event": "guard_skipped", "reason": "allowlisted", "command": command[:200]})
        return 0

    findings = scan(command)
    mode = os.environ.get("GUARD_MODE", "block")
    if not findings:
        log({"event": "guard_passed", "command": command[:200]})
        return 0

    log({"event": "threats_detected", "mode": mode, "command": command[:200], "threats": findings})
    lines = [f"Tool Guardian blocked this command ({len(findings)} finding(s)):"]
    lines += [f"- [{f['severity']}] {f['category']}: `{f['match']}` -> {f['suggestion']}" for f in findings]
    if mode == "block":
        lines.append("Do not retry a variant of this command. If the task genuinely needs it, stop and report it as a blocker.")
        print("\n".join(lines), file=sys.stderr)
        return 2
    print("\n".join(lines).replace("blocked", "flagged"), file=sys.stderr)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail closed
        print(f"Tool Guardian internal error ({exc.__class__.__name__}: {exc}); command blocked. "
              "Report this as a blocker.", file=sys.stderr)
        sys.exit(2)
