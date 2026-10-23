#!/usr/bin/env python3
"""tool_guardian.py — Claude Code PreToolUse hook (matcher: Bash).

Port of the Copilot `tool-guardian` hook (hooks/tool-guardian/ in vibecoding-copilot-governance)
to the Claude Code hook protocol, for the sequential coding team (ADR-0005) and the per-repo
autonomy levels (ADR-0008):

- input:  JSON on stdin — {"tool_name": "Bash", "tool_input": {"command": "..."}, "cwd": ...}
- block:  exit 2, reason on stderr (Claude Code feeds it back to the model)
- allow:  exit 0

Two layers of rules:
1. Invariants (PATTERNS, check_rm, check_git): blocked at every autonomy level.
2. Level rules (check_level): push, merge, curl/wget and gh depend on the effective level read
   from .ai/autonomy.yaml through autonomy.py — never from an environment variable. Orchestrated
   runs (ORCHESTRATE_ROLE set) never push, merge or write through gh, whatever the level.

Fail-closed: an internal error blocks the command (exit 2) instead of silently letting it run.

Environment (set by humans, never by the agent; none of them can raise the autonomy level):
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
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autonomy  # noqa: E402  (same directory, standard library only)

_PROTECTED = "|".join(re.escape(p) for p in autonomy.PROTECTED_PATHS)
_ENV_FILE = r"(?:^|[\s/'\"=])\.env(?!\.(?:example|sample|template|dist)\b)(?:\.[\w-]+)?(?=$|[\s'\";|&)])"

# (category, severity, regex, suggestion) — invariants, matched case-insensitively on the whole command
PATTERNS: list[tuple[str, str, str, str]] = [
    # Git policy common to every level (ADR-0005, ADR-0008 DEC-002)
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?rebase\b", "Do not rewrite history; add a new commit instead"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?pull\b", "Use `git fetch`, then ask (or merge the base branch into your own branch if your level allows it)"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?branch\s+(?:\S+\s+)*-D\b", "Do not delete branches"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?worktree\b", "Worktrees are managed by scripts/orchestrate.py"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?(?:checkout|switch)\s+(?:-\S+\s+)*(?:main|master)\b", "Stay on the task branch"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?commit\b[^;&|]*--no-verify", "Do not bypass commit hooks"),
    ("agent_git_policy", "high", r"\bgit\s+(?:-C\s+\S+\s+)?push\b[^;&|\n]*(?:--tags|--follow-tags|refs/tags/)", "Tags are pushed by humans"),
    ("agent_git_policy", "high", r"\bgh\s+pr\s+merge\b[^;&|\n]*--admin", "Never bypass branch protection"),
    # Protected configuration: agents never modify human-owned files (ADR-0008 DEC-004).
    ("protected_config", "high", rf">>?\s*['\"]?[^\s'\"|;&]*(?:{_PROTECTED})", "Human-owned file; report the need instead"),
    ("protected_config", "high", rf"\b(?:tee|sed\s+-i\S*|cp|mv|ln|install|rsync|rm|chmod|truncate|dd|curl|wget)\b[^|;&\n]*(?:{_PROTECTED})", "Human-owned file; report the need instead"),
    # Secrets are never read by the agent (deny rules cover the Read tool; this covers the shell).
    ("secret_read", "high", rf"\b(?:cat|less|more|head|tail|grep|egrep|rg|awk|sed|strings|xxd|od|base64|source|bat|nl|cut)\b[^|;&\n]*?{_ENV_FILE}", "Do not read environment files; ask for the variable you need"),
    # Destructive git operations
    ("destructive_git_ops", "critical", r"\bgit\s+push\b[^;&|]*(?:--force\b|--force-with-lease\b|\s-f\b|--mirror\b|--delete\b|\s\+\S)", "Never force-push, mirror or delete remote refs"),
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
    ("network_exfiltration", "high", r"\bwget\b[^\n]*--post-file", "Do not upload local files"),
    # System danger / publication
    ("system_danger", "high", r"(?:^|[;&|]\s*)sudo\s", "Run with least privilege; no sudo"),
    ("system_danger", "high", r"\b(?:npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bdocker\s+push\b", "Publishing is a human action"),
    ("system_danger", "high", r"\bterraform\s+(?:apply|destroy)\b|\bkubectl\s+delete\b|\bhelm\s+(?:uninstall|delete)\b", "Infra changes are applied by humans or a Runbook"),
    ("system_danger", "critical", r"\bmkfs(?:\.\w+)?\b|\bdd\b[^\n]*\bof=/dev/", "Never write to block devices"),
]

DANGEROUS_RM_TARGETS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", ".", "./", "./*", "..", "../", "*"}
WRAPPERS = ("sudo", "env", "command", "exec", "nohup", "time")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "eval", "xargs", "su", "script", "watch", "timeout", "nice", "parallel", "python", "python3", "perl", "ruby", "node"}
GIT_REDIRECT_KEYS = re.compile(r"^(?:remote\..+\.(?:push|pushurl|url)|push\..+|url\..+\.(?:pushinsteadof|insteadof)|core\.hookspath)$", re.I)
GH_READ = {("pr", "view"), ("pr", "list"), ("pr", "status"), ("pr", "checks"), ("pr", "diff"),
           ("run", "list"), ("run", "view"), ("run", "watch"), ("issue", "view"), ("issue", "list"),
           ("repo", "view"), ("auth", "status")}
GH_WRITE = {("pr", "create"), ("pr", "comment")}


def finding(category: str, severity: str, match: str, suggestion: str) -> dict:
    return {"category": category, "severity": severity, "match": match[:120], "suggestion": suggestion}


def split_segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||[;|\n]", command) if s.strip()]


def tokens_of(seg: str) -> list[str]:
    try:
        tokens = shlex.split(seg)
    except ValueError:
        tokens = seg.split()
    while tokens and (tokens[0] in WRAPPERS or re.match(r"^\w+=", tokens[0])):
        tokens = tokens[1:]
    return tokens


def run_git(cwd: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def current_branch(cwd: Path) -> str | None:
    name = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return None if name in (None, "", "HEAD") else name


def parse_git(tokens: list[str], cwd: Path) -> tuple[str | None, list[str], Path, list[dict]]:
    """(subcommand, args, effective cwd, findings) for a `git ...` token list."""
    found, i = [], 1
    while i < len(tokens) and tokens[i].startswith("-"):
        t = tokens[i]
        if t == "-C" and i + 1 < len(tokens):
            cwd = (cwd / tokens[i + 1]).resolve()
            i += 2
            continue
        if t == "-c" or t.startswith("-c") or t.startswith("--config-env"):
            found.append(finding("agent_git_policy", "high", " ".join(tokens), "Do not override git configuration on the command line"))
        elif t.split("=")[0] in ("--git-dir", "--work-tree", "--namespace", "--exec-path"):
            found.append(finding("agent_git_policy", "high", " ".join(tokens), "Run git in the repository, without --git-dir/--work-tree"))
            i += 1 if "=" in t else 2
            continue
        i += 1
    sub = tokens[i] if i < len(tokens) else None
    return sub, tokens[i + 1:], cwd, found


# ─── Invariants ──────────────────────────────────────────────────────────────
def check_rm(tokens: list[str], seg: str) -> list[dict]:
    """Structural check of rm invocations (regexes alone flag `rm -rf ./build`)."""
    findings = []
    if not tokens or os.path.basename(tokens[0]) not in ("rm", "unlink"):
        return findings
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


def check_git_invariants(sub: str | None, args: list[str], cwd: Path, seg: str, base: str) -> list[dict]:
    found = []
    if sub == "commit" and current_branch(cwd) == base:
        found.append(finding("agent_git_policy", "high", seg, f"Never commit on {base}: work on a task branch"))
    if sub == "config":
        reads = {"--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--name-only"}
        keys = [a for a in args if not a.startswith("-")]
        if keys and GIT_REDIRECT_KEYS.match(keys[0]) and not reads.intersection(args):
            found.append(finding("agent_git_policy", "high", seg, "Do not change push targets, URL rewrites or hook paths"))
    return found


# ─── Level rules ─────────────────────────────────────────────────────────────
def check_push(args: list[str], cwd: Path, base: str) -> list[dict]:
    seg = "git push " + " ".join(args)
    rest = [a for a in args if a not in ("-u", "--set-upstream")]
    branch = current_branch(cwd)
    allowed = f"`git push -u origin {branch or '<branch>'}`"
    if branch is None:
        return [finding("autonomy", "high", seg, "Detached HEAD or not a git repository: nothing to push")]
    if branch == base:
        return [finding("autonomy", "high", seg, f"Never push from {base}: work on a task branch")]
    if len(rest) != 2 or rest[0] != "origin" or rest[1] not in (branch, "HEAD") or len(args) - len(rest) > 1:
        return [finding("autonomy", "high", seg, f"Only your current branch can be pushed, exactly as {allowed}")]
    if run_git(cwd, "config", "--get-all", "remote.origin.push"):
        return [finding("autonomy", "high", seg, "remote.origin.push is configured: push refused, ask a human")]
    return []


def check_merge(args: list[str], cwd: Path, base: str) -> list[dict]:
    seg = "git merge " + " ".join(args)
    if args in (["--abort"], ["--continue"]):
        return []
    targets = [a for a in args if not a.startswith("-")]
    flags = [a for a in args if a.startswith("-")]
    branch = current_branch(cwd)
    if (branch and branch != base and len(targets) == 1 and targets[0] in (base, f"origin/{base}")
            and set(flags) <= {"--no-edit", "--no-ff", "--ff"}):
        return []
    return [finding("autonomy", "high", seg, f"Only `git merge origin/{base}` into your own (non-{base}) branch is allowed")]


def run_gh(cwd: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check_gh(tokens: list[str], cwd: Path, level: str, base: str, orchestrated: bool) -> list[dict]:
    seg = " ".join(tokens)
    args = tokens[1:]
    if any(a in ("-R", "--repo") or a.startswith("--repo=") for a in args):
        return [finding("autonomy", "high", seg, "Stay on this repository: no -R/--repo")]
    words = [a for a in args if not a.startswith("-")]
    key = (words[0] if words else None, words[1] if len(words) > 1 else None)
    if key[0] == "api":
        bad = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
        if any(a in bad or a.split("=")[0] in bad or re.match(r"^-[XfF]\S", a) for a in args):
            return [finding("autonomy", "high", seg, "gh api is read-only: GET without fields (the default)")]
        return []
    if key in GH_READ:
        return []
    if key in GH_WRITE and not orchestrated:
        if key == ("pr", "create") and not ({"--draft", "-d"} & set(args)):
            return [finding("autonomy", "high", seg, "Open pull requests as drafts: add --draft")]
        return []
    if key == ("pr", "merge") and level == "trusted" and not orchestrated:
        return check_pr_merge(args, words, cwd, base, seg)
    return [finding("autonomy", "high", seg, f"gh `{' '.join(w for w in key if w)}` is not allowed at level `{level}`"
                    + (" in an orchestrated run" if orchestrated else ""))]


def check_pr_merge(args: list[str], words: list[str], cwd: Path, base: str, seg: str) -> list[dict]:
    if len(words) < 3 or not re.match(r"^(?:\d+|https://\S+/pull/\d+)$", words[2]):
        return [finding("autonomy", "high", seg, "Name the pull request: `gh pr merge <number> --squash`")]
    if not ({"--squash", "-s", "--merge", "-m"} & set(args)):
        return [finding("autonomy", "high", seg, "Choose --squash (or --merge); never --rebase")]
    pr = words[2]
    view = run_gh(cwd, "pr", "view", pr, "--json", "headRefName,baseRefName,isDraft")
    try:
        info = json.loads(view.stdout) if view and view.returncode == 0 else None
    except json.JSONDecodeError:
        info = None
    if not info:
        return [finding("autonomy", "high", seg, "Could not read the pull request: merge refused")]
    branch = current_branch(cwd)
    if info.get("headRefName") != branch or info.get("baseRefName") != base or info.get("isDraft"):
        return [finding("autonomy", "high", seg, f"Only your own ready PR (head = current branch, base = {base}) can be merged")]
    checks = run_gh(cwd, "pr", "checks", pr)
    if checks is None or checks.returncode != 0:
        return [finding("autonomy", "high", seg, "Checks are not all green (or could not be read): merge refused")]
    return []


def check_level(command: str, cwd: Path, root: Path, level: str, base: str) -> list[dict]:
    orchestrated = bool(os.environ.get("ORCHESTRATE_ROLE"))
    supervised = level == "supervised"
    found = []
    for seg in split_segments(command):
        tokens = tokens_of(seg)
        if not tokens:
            continue
        cmd = os.path.basename(tokens[0])
        if cmd == "git":
            sub, args, gcwd, f = parse_git(tokens, cwd)
            sub = sub.strip("\"'") if sub else sub
            found += f
            found += check_git_invariants(sub, args, gcwd, seg, base)
            if sub == "push":
                if supervised or orchestrated:
                    found.append(finding("autonomy", "high", seg, "Agents do not push at this level" if supervised
                                         else "In orchestrated runs the orchestrator pushes, not DEV"))
                else:
                    found += check_push(args, gcwd, base)
            elif sub == "merge":
                if supervised or orchestrated:
                    found.append(finding("autonomy", "high", seg, "Agents do not merge at this level"))
                else:
                    found += check_merge(args, gcwd, base)
        elif cmd == "gh":
            if supervised:
                found.append(finding("autonomy", "high", seg, "gh is not allowed at level `supervised`"))
            else:
                found += check_gh(tokens, cwd, level, base, orchestrated)
        elif cmd in ("curl", "wget") and supervised:
            found.append(finding("autonomy", "high", seg, f"{cmd} is not allowed at level `supervised`"))
    # Commands hidden in a subshell, `bash -c`, `xargs` or `eval` escape the structured pass: refuse them.
    starts = [m.start() for m in re.finditer(r"\$\(|`", command)]
    starts += [command.find(s) for s in split_segments(command)
               if (t := tokens_of(s)) and os.path.basename(t[0]) in SHELLS]
    if starts:
        nested = command[min(starts):]   # plain commands before the first nested part were parsed above
        hidden = [(r"\bgit\s+(?:-\S+\s+(?:\S+\s+)?)*push\b", "git push"),
                  (r"\bgit\s+(?:-\S+\s+(?:\S+\s+)?)*merge\b", "git merge"),
                  (r"(?:^|[\s;&|(`$'\"])gh\s+\w", "gh")]
        if supervised:
            hidden.append((r"(?:^|[\s;&|(`$'\"])(?:curl|wget)\b", "curl/wget"))
        for regex, what in hidden:
            if re.search(regex, nested):
                found.append(finding("autonomy", "high", nested, f"Run {what} as a plain command, not inside a subshell or string"))
    return found


def scan(command: str, cwd: Path | None = None) -> tuple[list[dict], str]:
    cwd = cwd or Path.cwd()
    root_s = run_git(cwd, "rev-parse", "--show-toplevel") or os.environ.get("CLAUDE_PROJECT_DIR") or str(cwd)
    root = Path(root_s)
    level, _reason = autonomy.effective_level(root)
    base = autonomy.base_branch(root)
    found = []
    for seg in split_segments(command):
        found += check_rm(tokens_of(seg), seg)
    for category, severity, regex, suggestion in PATTERNS:
        m = re.search(regex, command, flags=re.IGNORECASE)
        if m:
            found.append(finding(category, severity, m.group(0), suggestion))
    found += check_level(command, cwd, root, level, base)
    return found, level


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

    cwd = Path(payload.get("cwd") or os.getcwd())
    findings, level = scan(command, cwd)
    mode = os.environ.get("GUARD_MODE", "block")
    if not findings:
        log({"event": "guard_passed", "level": level, "command": command[:200]})
        return 0

    log({"event": "threats_detected", "mode": mode, "level": level, "command": command[:200], "threats": findings})
    lines = [f"Tool Guardian blocked this command ({len(findings)} finding(s), autonomy level `{level}`):"]
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
