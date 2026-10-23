#!/usr/bin/env python3
"""autonomy.py — agent autonomy levels of this repository (ADR-0008). The only policy definition.

The level is declared in .ai/autonomy.yaml (human-owned, protected). Everything that depends on it
comes from this module:

- rendered files: .claude/settings.json (deny rules) and the managed autonomy blocks of CLAUDE.md
  and AGENTS.md (`render`, run by sync-governance.sh; `check` verifies them);
- run-time decisions: tool_guardian.py and scripts/orchestrate.py call `effective_level`.

Effective level = the most restrictive of the working-tree file and its copy on the base branch,
so a wider level only applies once a human has merged it. Anything missing, unreadable or invalid
resolves to `supervised`. No environment variable is consulted.

Usage:
  autonomy.py effective [--root DIR]          # print the effective level and why
  autonomy.py render    [--root DIR] [--level L] [--dry-run]
  autonomy.py check     [--root DIR]          # exit 1 if a rendered file does not match the level
  autonomy.py agents-block --level L [--base main]
  autonomy.py claude-line  --level L [--base main]

Standard library only: hooks must not depend on project packages.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

LEVELS = ("supervised", "branch", "trusted")   # ordered from most to least restrictive
DEFAULT_LEVEL = "supervised"
LEVEL_FILE = ".ai/autonomy.yaml"
DEFAULT_BASE = "main"
# Human-owned paths: agents never change them (deny rules, tool_guardian, orchestrator check).
PROTECTED_PATHS = (".claude/", ".ai/autonomy.yaml", ".ai/orchestration.yaml", ".ai/roles/", "scripts/orchestrate.py")

BEGIN, END = "<!-- autonomy:begin -->", "<!-- autonomy:end -->"


# ─── Level resolution ────────────────────────────────────────────────────────
def parse_level(text: str | None) -> str | None:
    """Return the level declared in an autonomy.yaml text, or None if absent or invalid."""
    if not text:
        return None
    version = re.search(r"^version:\s*(\S+)\s*(?:#.*)?$", text, flags=re.M)
    if version and version.group(1) != "1":
        return None
    found = re.findall(r"^level:\s*([A-Za-z_-]+)\s*(?:#.*)?$", text, flags=re.M)
    if len(found) != 1 or found[0] not in LEVELS:
        return None
    return found[0]


def base_branch(root: Path) -> str:
    try:
        text = (root / ".ai" / "orchestration.yaml").read_text()
    except OSError:
        return DEFAULT_BASE
    m = re.search(r"^\s*base_branch:\s*([\w./-]+)", text, flags=re.M)
    return m.group(1) if m else DEFAULT_BASE


def _git_show(root: Path, rev_path: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), "show", rev_path], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def declared_level(root: Path) -> str | None:
    try:
        return parse_level((root / LEVEL_FILE).read_text())
    except OSError:
        return None


def effective_level(root: Path) -> tuple[str, str]:
    """(level, reason). Never raises; falls back to supervised."""
    base = base_branch(root)
    local = declared_level(root)
    committed = parse_level(_git_show(root, f"{base}:{LEVEL_FILE}"))
    if local is None:
        return DEFAULT_LEVEL, f"{LEVEL_FILE} missing or invalid in the working tree"
    if committed is None:
        return DEFAULT_LEVEL, f"{LEVEL_FILE} missing or invalid on branch {base}"
    level = min(local, committed, key=LEVELS.index)
    return level, f"working tree={local}, {base}={committed}"


def at_least(level: str, minimum: str) -> bool:
    return LEVELS.index(level) >= LEVELS.index(minimum)


# ─── Policy per level ────────────────────────────────────────────────────────
def deny_rules(level: str) -> list[str]:
    """Claude Code deny rules. Defense in depth: tool_guardian enforces the same policy at run time."""
    rules = [
        "Bash(git rebase:*)",
        "Bash(git pull:*)",
        "Bash(git reset --hard:*)",
        "Bash(git checkout main:*)",
        "Bash(git switch main:*)",
        "Bash(git worktree:*)",
        "Bash(git branch -D:*)",
        "Bash(rm -rf /:*)",
        "Bash(rm -rf ~:*)",
    ]
    if level == "supervised":
        rules[:0] = ["Bash(git push:*)", "Bash(git merge:*)"]
        rules += ["Bash(curl:*)", "Bash(wget:*)", "Bash(gh:*)"]
    else:
        rules[:0] = ["Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push --force-with-lease:*)",
                     "Bash(git push --mirror:*)", "Bash(git push --tags:*)", "Bash(git push origin main:*)",
                     "Bash(gh pr merge --admin:*)", "Bash(gh api -X:*)", "Bash(gh api --method:*)"]
        if level == "branch":
            rules.append("Bash(gh pr merge:*)")
    rules += [
        "Read(./.env)",
        "Read(./.env.*)",
        "Read(**/.env)",
        "Read(**/.env.*)",
        "Edit(./.ai/autonomy.yaml)",
        "Edit(./.ai/orchestration.yaml)",
        "Edit(./.ai/roles/**)",
        "Edit(./.ai/runs/**)",
        "Edit(./.claude/**)",
        "Edit(./scripts/orchestrate.py)",
    ]
    return rules


def disallowed_tools(level: str) -> list[str]:
    """Tools the orchestrator adds to runner.disallowed_tools for this level."""
    return ["WebFetch", "WebSearch"] if level == "supervised" else []


def settings(level: str) -> dict:
    hook = 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{}"'
    return {
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "permissions": {"deny": deny_rules(level)},
        "env": {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        },
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [
                    {"type": "command", "command": hook.format("tool_guardian.py"), "timeout": 30},
                    {"type": "command", "command": hook.format("secrets_scanner.py"), "timeout": 30},
                ]},
                {"matcher": "Write|Edit|MultiEdit|NotebookEdit", "hooks": [
                    {"type": "command", "command": hook.format("secrets_scanner.py"), "timeout": 10},
                ]},
            ]
        },
    }


def settings_text(level: str) -> str:
    return json.dumps(settings(level), indent=2) + "\n"


def claude_line(level: str, base: str = DEFAULT_BASE) -> str:
    """One line for CLAUDE.md (loaded into every session of a 27B model: keep it short)."""
    lines = {
        "supervised": f"- Autonomy `supervised` (`.ai/autonomy.yaml`): commit locally only; never push, merge or "
                      f"rebase; no curl, wget or gh.",
        "branch": f"- Autonomy `branch` (`.ai/autonomy.yaml`): you may push your current non-{base} branch "
                  f"(`git push -u origin <branch>`) and open draft PRs; never push {base}, force-push, merge into "
                  f"{base} or rebase. curl/wget and read-only gh are allowed.",
        "trusted": f"- Autonomy `trusted` (`.ai/autonomy.yaml`): as `branch`, and you may merge your own PR "
                   f"(`gh pr merge <n> --squash`) once all its checks are green; never push {base}, force-push "
                   f"or rebase.",
    }
    return lines[level]


def claude_block(level: str, base: str = DEFAULT_BASE) -> str:
    return f"{BEGIN}\n{claude_line(level, base)}\n{END}"


def agents_block(level: str, base: str = DEFAULT_BASE) -> str:
    rows = {
        "supervised": [
            ("Push", "no — a human pushes", "no — a human pushes after approval"),
            ("Merge", "no", f"a human merges after `orchestrate.py approve`"),
            ("curl / wget", "no", "no"),
            ("gh", "no", "no"),
        ],
        "branch": [
            ("Push", f"current non-`{base}` branch only: `git push -u origin <branch>`",
             "the orchestrator pushes `agent/TASK-*` and opens a draft PR at `READY_FOR_APPROVAL`"),
            ("Merge", f"only `{base}` into your own branch (`git merge origin/{base}`)",
             "a human merges after `orchestrate.py approve`"),
            ("curl / wget", "yes (no pipe to a shell, no file upload)", "DEV: yes"),
            ("gh", "read-only, `pr create --draft`, `pr comment`", "DEV: read-only"),
        ],
        "trusted": [
            ("Push", f"current non-`{base}` branch only: `git push -u origin <branch>`",
             "the orchestrator pushes `agent/TASK-*` and opens a draft PR at `READY_FOR_APPROVAL`"),
            ("Merge", f"`{base}` into your own branch; your own PR once all checks are green "
                      "(`gh pr merge <n> --squash`)",
             "the orchestrator merges after `orchestrate.py approve`, once all checks are green"),
            ("curl / wget", "yes (no pipe to a shell, no file upload)", "DEV: yes"),
            ("gh", "read-only, `pr create --draft`, `pr comment`, `pr merge` (own PR, green)", "DEV: read-only"),
        ],
    }
    table = "\n".join(f"| {a} | {i} | {o} |" for a, i, o in rows[level])
    return f"""{BEGIN}
## Autonomy — level `{level}`

Declared in `.ai/autonomy.yaml` (human-owned, ADR-0008) and enforced by `.claude/settings.json`,
the hooks and `scripts/orchestrate.py`. In orchestrated runs DEV never pushes, at any level.

| Action | Interactive session | Orchestrated run |
|---|---|---|
{table}

Always forbidden: force push, push to / commit on / merge into `{base}`, tag push, `rebase`,
`git pull`, `--no-verify`, reading or committing secrets, editing `.claude/`, `.ai/autonomy.yaml`,
`.ai/orchestration.yaml`, `.ai/roles/` or `scripts/orchestrate.py`.
{END}"""


# ─── Rendering ───────────────────────────────────────────────────────────────
LEGACY_CLAUDE_SENTENCE = " Never push, merge or rebase."
LEGACY_AGENTS_NEVER = "- `git push`, `merge`, `rebase`, `reset --hard`, force operations, or commits on `main`."
AGENTS_NEVER = "- Force operations, `rebase`, `reset --hard`, commits on `main`, or anything **Autonomy** does not allow."


def _replace_block(text: str, block: str) -> str | None:
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), flags=re.S)
    if not pat.search(text):
        return None
    return pat.sub(lambda _m: block, text, count=1)


def update_claude_md(text: str, level: str, base: str) -> tuple[str, str]:
    block = claude_block(level, base)
    new = _replace_block(text, block)
    if new is not None:
        return new, "block"
    for i, line in enumerate(lines := text.split("\n")):
        if line.endswith(LEGACY_CLAUDE_SENTENCE):
            lines[i] = line[: -len(LEGACY_CLAUDE_SENTENCE)]
            lines.insert(i + 1, block)
            return "\n".join(lines), "migrated"
    return text.rstrip("\n") + "\n" + block + "\n", "appended"


def update_agents_md(text: str, level: str, base: str) -> tuple[str, str]:
    block = agents_block(level, base)
    new = _replace_block(text, block)
    if new is not None:
        return new, "block"
    text = text.replace(LEGACY_AGENTS_NEVER, AGENTS_NEVER)
    m = re.search(r"^## Definition of done", text, flags=re.M)
    if m:
        return text[: m.start()] + block + "\n\n" + text[m.start():], "migrated"
    return text.rstrip("\n") + "\n\n" + block + "\n", "appended"


def expected_files(root: Path, level: str) -> dict[str, tuple[str, str]]:
    """{relative path: (new text, how)} for every file the level drives and that exists (or must)."""
    base = base_branch(root)
    out = {".claude/settings.json": (settings_text(level), "rendered")}
    for rel, fn in (("CLAUDE.md", update_claude_md), ("AGENTS.md", update_agents_md)):
        path = root / rel
        if path.is_file():
            out[rel] = fn(path.read_text(), level, base)
    return out


def render(root: Path, level: str | None = None, dry_run: bool = False) -> list[str]:
    """Write the level file (if level is given) and every derived file. Returns messages."""
    msgs = []
    if level is not None:
        if level not in LEVELS:
            raise ValueError(f"unknown level {level!r} (expected one of {', '.join(LEVELS)})")
        text = (root / LEVEL_FILE).read_text() if (root / LEVEL_FILE).is_file() else ""
        if parse_level(text) is not None:
            new = re.sub(r"^level:\s*\S+", f"level: {level}", text, count=1, flags=re.M)
        else:
            new = f"# .ai/autonomy.yaml — agent autonomy level (ADR-0008). Human-owned.\nversion: 1\nlevel: {level}\n"
        if new != text:
            msgs.append(f"{'would write' if dry_run else 'wrote'} {LEVEL_FILE} (level: {level})")
            if not dry_run:
                (root / LEVEL_FILE).parent.mkdir(parents=True, exist_ok=True)
                (root / LEVEL_FILE).write_text(new)
    else:
        level = declared_level(root)
        if level is None:
            level = DEFAULT_LEVEL
            msgs.append(f"{LEVEL_FILE} missing or invalid: rendering `{DEFAULT_LEVEL}`")
    for rel, (text, how) in expected_files(root, level).items():
        path = root / rel
        old = path.read_text() if path.is_file() else None
        if old == text:
            continue
        msgs.append(f"{'would update' if dry_run else 'updated'} {rel} ({how}, level {level})")
        if how == "appended":
            msgs.append(f"warning: {rel} had no autonomy markers nor the legacy line; block appended at the end")
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    return msgs


def check(root: Path) -> list[str]:
    """Problems found; empty list means every derived file matches the declared level."""
    problems = []
    level = declared_level(root)
    if level is None:
        problems.append(f"{LEVEL_FILE} missing or invalid (agents fall back to `{DEFAULT_LEVEL}`)")
        level = DEFAULT_LEVEL
    for rel, (text, how) in expected_files(root, level).items():
        path = root / rel
        if not path.is_file():
            problems.append(f"{rel} missing")
            continue
        current = path.read_text()
        if rel.endswith(".json"):
            try:
                same = json.loads(current) == json.loads(text)
            except json.JSONDecodeError:
                same = False
        else:
            same = how == "block" and current == text
        if not same:
            problems.append(f"{rel} does not match level `{level}` — run sync-governance.sh (or autonomy.py render)")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("effective", "render", "check"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".")
        if name == "render":
            p.add_argument("--level", choices=LEVELS)
            p.add_argument("--dry-run", action="store_true")
    for name in ("agents-block", "claude-line"):
        p = sub.add_parser(name)
        p.add_argument("--level", choices=LEVELS, required=True)
        p.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args(argv)

    if args.cmd == "agents-block":
        print(agents_block(args.level, args.base))
        return 0
    if args.cmd == "claude-line":
        print(claude_line(args.level, args.base))
        return 0
    root = Path(args.root).resolve()
    if args.cmd == "effective":
        level, reason = effective_level(root)
        print(f"{level} ({reason})")
        return 0
    if args.cmd == "render":
        for msg in render(root, args.level, args.dry_run):
            print(msg)
        return 0
    problems = check(root)
    for p in problems:
        print(f"autonomy-check: {p}", file=sys.stderr)
    if not problems:
        print(f"autonomy-check: OK (level {declared_level(root) or DEFAULT_LEVEL})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
