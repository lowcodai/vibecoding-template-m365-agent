#!/usr/bin/env python3
"""secrets_scanner.py — Claude Code PreToolUse hook + CLI scanner.

Port of the Copilot `secrets-scanner` hook (hooks/secrets-scanner/ in
vibecoding-copilot-governance). The Copilot version scanned the diff at session end, in warn
mode — after the secret was already on disk. This version stops it earlier, at three points:

1. Hook, matcher Write|Edit|MultiEdit|NotebookEdit: scans the content about to be written.
2. Hook, matcher Bash, on `git commit`: scans staged, unstaged and untracked changes (a superset
   of what `git add ... && git commit` can commit).
3. CLI, `secrets_scanner.py --range BASE..HEAD`: deterministic scan of a task branch, run by
   scripts/orchestrate.py as a validation command (works even for headless runs without hooks).

Blocking: findings at or above SECRETS_BLOCK_SEVERITY (default: high) → exit 2 (hook) / 1 (CLI).
Lower-severity findings (JWT-like strings, internal IP:port) are reported, not blocked.

Known false positives (e.g. documentation examples) go in `.claude/hooks/secrets-allowlist.txt`,
one `<path glob> <PATTERN[,PATTERN...]>` per line, relative to the repo root. Prefer listing the
patterns: a bare glob silences every pattern in those files. The file sits under `.claude/`, which
agents cannot modify (deny rules, tool_guardian, orchestrator protected paths): only humans edit it.

Environment (set by humans, never by the agent):
  SKIP_SECRETS_SCAN       "true" disables the scanner
  SECRETS_ALLOWLIST       comma-separated substrings; a match containing one is ignored
  SECRETS_BLOCK_SEVERITY  critical | high (default) | medium
  CLAUDE_HOOK_LOG_DIR     JSONL log directory (default ~/.local/state/claude-hooks/<project>)
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# (name, severity, regex)
PATTERNS: list[tuple[str, str, str]] = [
    # Cloud provider credentials
    ("AWS_ACCESS_KEY", "critical", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("AWS_SECRET_KEY", "critical", r"aws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}"),
    ("GCP_SERVICE_ACCOUNT", "critical", r"\"type\"\s*:\s*\"service_account\""),
    ("GCP_API_KEY", "high", r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ("AZURE_CLIENT_SECRET", "critical", r"azure[_-]?client[_-]?secret\s*[:=]\s*['\"]?[A-Za-z0-9_~.-]{34,}"),
    # GitHub tokens
    ("GITHUB_PAT", "critical", r"\bghp_[0-9A-Za-z]{36}\b"),
    ("GITHUB_OAUTH", "critical", r"\bgho_[0-9A-Za-z]{36}\b"),
    ("GITHUB_APP_TOKEN", "critical", r"\bghs_[0-9A-Za-z._-]{36,}"),
    ("GITHUB_REFRESH_TOKEN", "critical", r"\bghr_[0-9A-Za-z]{36}\b"),
    ("GITHUB_FINE_GRAINED_PAT", "critical", r"\bgithub_pat_[0-9A-Za-z_]{82}\b"),
    # AI provider keys (relevant to gateway credentials)
    ("ANTHROPIC_API_KEY", "critical", r"\bsk-ant-[0-9A-Za-z_-]{20,}"),
    ("OPENAI_API_KEY", "critical", r"\bsk-(?:proj-)?[0-9A-Za-z_-]{32,}"),
    ("HUGGINGFACE_TOKEN", "high", r"\bhf_[0-9A-Za-z]{30,}\b"),
    # Private keys
    ("PRIVATE_KEY", "critical", r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    # Generic secrets: quoted literal only (unquoted `password = get_password()` is code, not a secret)
    ("GENERIC_SECRET", "high",
     r"(?i)\b(?:secret|token|password|passwd|pwd|api[_-]?key|apikey|access[_-]?key|auth[_-]?token|client[_-]?secret)\w*\s*[:=]\s*['\"][A-Za-z0-9_/+=~.!@#$%^&*-]{8,}['\"]"),
    ("CONNECTION_STRING", "high", r"\b(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp|mssql)://[^\s'\"@/]+:[^\s'\"@/]+@[^\s'\"]+"),
    ("BEARER_TOKEN", "medium", r"[Bb]earer\s+[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"),
    # Messaging and SaaS tokens
    ("SLACK_TOKEN", "high", r"\bxox[baprs]-[0-9]{10,}-[0-9A-Za-z-]+"),
    ("SLACK_WEBHOOK", "high", r"https://hooks\.slack\.com/services/T[0-9A-Z]{8,}/B[0-9A-Z]{8,}/[0-9A-Za-z]{24}"),
    ("DISCORD_TOKEN", "high", r"\b[MN][A-Za-z0-9]{23,}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"),
    ("TWILIO_API_KEY", "high", r"\bSK[0-9a-fA-F]{32}\b"),
    ("SENDGRID_API_KEY", "high", r"\bSG\.[0-9A-Za-z_-]{22}\.[0-9A-Za-z_-]{43}\b"),
    ("STRIPE_SECRET_KEY", "critical", r"\bsk_live_[0-9A-Za-z]{24,}"),
    ("STRIPE_RESTRICTED_KEY", "high", r"\brk_live_[0-9A-Za-z]{24,}"),
    ("NPM_TOKEN", "high", r"\bnpm_[0-9A-Za-z]{36}\b"),
    # Lower confidence: reported, not blocked by default
    ("JWT_TOKEN", "medium", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("INTERNAL_IP_PORT", "medium",
     r"(?<![.\d])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}):\d{2,5}(?!\d)"),
]
SEVERITY_RANK = {"medium": 1, "high": 2, "critical": 3}
PLACEHOLDER = re.compile(r"(?i)example|placeholder|your[_-]|xxx|changeme|todo|fixme|replace[_-]?me|dummy|fake|test[_-]?key|sample|<[^>]+>|\$\{")
SKIP_FILES = re.compile(r"(?:^|/)(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|go\.sum|poetry\.lock|uv\.lock)$|\.lock$")
ALLOWLIST_FILE = ".claude/hooks/secrets-allowlist.txt"
PATH_ALLOWLIST: list[tuple[str, set[str] | None]] = []  # loaded per run by load_allowlist()
COMMIT_RE = re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")
MAX_FILE_BYTES = 1_000_000


# ─── Path allowlist ──────────────────────────────────────────────────────────
def repo_root(start: Path) -> Path:
    proc = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=start, text=True, capture_output=True)
    return Path(proc.stdout.strip()) if proc.returncode == 0 else start


def load_allowlist(root: Path) -> None:
    """Parse `<glob> [PATTERN,PATTERN]` lines; unknown pattern names are reported, not trusted."""
    PATH_ALLOWLIST.clear()
    path = root / ALLOWLIST_FILE
    if not path.is_file():
        return
    known = {name for name, _, _ in PATTERNS}
    for n, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        names = None
        if len(parts) > 1:
            names = {x.strip() for x in parts[1].split(",") if x.strip()}
            unknown = names - known
            if unknown:
                print(f"{ALLOWLIST_FILE}:{n}: unknown pattern(s) {sorted(unknown)} ignored", file=sys.stderr)
                names -= unknown
                if not names:
                    continue
        PATH_ALLOWLIST.append((parts[0], names))


def path_allowed(path: str, pattern_name: str) -> bool:
    return any(fnmatch.fnmatch(path, glob) and (names is None or pattern_name in names)
               for glob, names in PATH_ALLOWLIST)


# ─── Scanning ────────────────────────────────────────────────────────────────
def redact(s: str) -> str:
    return "[REDACTED]" if len(s) <= 12 else f"{s[:4]}...{s[-4:]}"


def scan_lines(path: str, lines: list[tuple[int, str]]) -> list[dict]:
    if SKIP_FILES.search(path):
        return []
    allow = [p.strip() for p in os.environ.get("SECRETS_ALLOWLIST", "").split(",") if p.strip()]
    findings = []
    for line_no, text in lines:
        for name, severity, regex in PATTERNS:
            for m in re.finditer(regex, text):
                match = m.group(0)
                if PLACEHOLDER.search(match) or any(a in match for a in allow) or path_allowed(path, name):
                    continue
                findings.append({"file": path, "line": line_no, "pattern": name, "severity": severity, "match": redact(match)})
    return findings


def added_lines_from_diff(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Parse `git diff -U0` output into {file: [(new_line_no, added_text), ...]}."""
    files: dict[str, list[tuple[int, str]]] = {}
    current, line_no = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            current = None if target == "/dev/null" else re.sub(r"^b/", "", target)
            if current:
                files.setdefault(current, [])
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line_no = int(m.group(1)) if m else 0
        elif raw.startswith("+") and current is not None:
            files[current].append((line_no, raw[1:]))
            line_no += 1
    return files


def read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def scan_diff(diff: str) -> list[dict]:
    out = []
    for path, lines in added_lines_from_diff(diff).items():
        out += scan_lines(path, lines)
    return out


def scan_worktree_changes(cwd: Path) -> list[dict]:
    """Everything a `git add ... && git commit` could commit: staged + unstaged + untracked."""
    findings = scan_diff(git(["diff", "--cached", "-U0", "--no-color", "--no-ext-diff"], cwd))
    findings += scan_diff(git(["diff", "-U0", "--no-color", "--no-ext-diff"], cwd))
    for rel in git(["ls-files", "--others", "--exclude-standard", "-z"], cwd).split("\0"):
        if rel:
            text = read_text(cwd / rel)
            if text is not None:
                findings += scan_lines(rel, list(enumerate(text.splitlines(), start=1)))
    # de-duplicate (a file can be both staged and unstaged)
    unique = {(f["file"], f["line"], f["pattern"], f["match"]): f for f in findings}
    return list(unique.values())


def scan_write(tool_name: str, tool_input: dict, root: Path | None = None) -> list[dict]:
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or "<unknown>"
    if root is not None and os.path.isabs(path):
        try:
            path = str(Path(path).resolve().relative_to(root.resolve()))
        except ValueError:
            pass
    chunks = []
    if tool_name == "Write":
        chunks.append(tool_input.get("content", ""))
    elif tool_name == "Edit":
        chunks.append(tool_input.get("new_string", ""))
    elif tool_name == "MultiEdit":
        chunks += [e.get("new_string", "") for e in tool_input.get("edits", [])]
    elif tool_name == "NotebookEdit":
        chunks.append(tool_input.get("new_source", ""))
    lines = [(i, l) for chunk in chunks for i, l in enumerate(chunk.splitlines(), start=1)]
    return scan_lines(path, lines)


# ─── Reporting ───────────────────────────────────────────────────────────────
def blocking(findings: list[dict]) -> list[dict]:
    threshold = SEVERITY_RANK.get(os.environ.get("SECRETS_BLOCK_SEVERITY", "high"), 2)
    return [f for f in findings if SEVERITY_RANK[f["severity"]] >= threshold]


def report(findings: list[dict], header: str) -> str:
    lines = [header]
    lines += [f"- [{f['severity']}] {f['pattern']} in {f['file']}:{f['line']} ({f['match']})" for f in findings[:30]]
    if len(findings) > 30:
        lines.append(f"- ... {len(findings) - 30} more")
    return "\n".join(lines)


def log(event: dict) -> None:
    try:
        project = Path(os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())).name
        log_dir = Path(os.environ.get("CLAUDE_HOOK_LOG_DIR") or Path.home() / ".local/state/claude-hooks" / project)
        log_dir.mkdir(parents=True, exist_ok=True)
        event["timestamp"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with (log_dir / "secrets-scanner.jsonl").open("a") as fh:
            fh.write(json.dumps(event) + "\n")
    except OSError:
        pass


ADVICE = ("Remove the value, read it from an environment variable or the secret manager instead, and "
          "do not print it anywhere. If this is a false positive, stop and report it: only a human can "
          "allowlist it (SECRETS_ALLOWLIST).")


def run_hook() -> int:
    payload = json.load(sys.stdin)
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    root = repo_root(cwd)
    load_allowlist(root)
    if tool_name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        findings, what = scan_write(tool_name, tool_input, root), f"{tool_name} {tool_input.get('file_path', '')}"
    elif tool_name == "Bash" and COMMIT_RE.search(tool_input.get("command", "") or ""):
        findings, what = scan_worktree_changes(root), "git commit"
    else:
        return 0
    block = blocking(findings)
    log({"event": "scan", "trigger": what, "findings": findings, "blocked": bool(block)})
    if block:
        print(report(block, f"Secrets Scanner blocked {what}: {len(block)} potential secret(s).") + "\n" + ADVICE, file=sys.stderr)
        return 2
    if findings:
        print(report(findings, f"Secrets Scanner warning ({what}): low-confidence matches, verify they are not secrets."), file=sys.stderr)
    return 0


def run_cli(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Scan a commit range for secrets (added lines only).")
    ap.add_argument("--range", required=True, help="e.g. BASE_SHA..HEAD")
    ap.add_argument("--repo", default=".")
    args = ap.parse_args(argv)
    load_allowlist(repo_root(Path(args.repo)))
    findings = scan_diff(git(["diff", "-U0", "--no-color", "--no-ext-diff", args.range], Path(args.repo)))
    block = blocking(findings)
    log({"event": "range_scan", "range": args.range, "findings": findings, "blocked": bool(block)})
    if findings:
        print(report(findings, f"{len(findings)} potential secret(s) in {args.range} ({len(block)} blocking):"))
    else:
        print(f"No secrets detected in {args.range}")
    return 1 if block else 0


if __name__ == "__main__":
    if os.environ.get("SKIP_SECRETS_SCAN") == "true":
        sys.exit(0)
    if len(sys.argv) > 1:
        sys.exit(run_cli(sys.argv[1:]))
    try:
        sys.exit(run_hook())
    except Exception as exc:  # fail closed
        print(f"Secrets Scanner internal error ({exc.__class__.__name__}: {exc}); operation blocked. "
              "Report this as a blocker.", file=sys.stderr)
        sys.exit(2)
