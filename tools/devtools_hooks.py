#!/usr/bin/env python3
"""Ember's optional Claude Code tees. Stdlib only.

    devtools_hooks.py statusline        statusLine command: saves Claude Code's
                                        status JSON (official 5h / 7-day limit %,
                                        context %, cost) for the Usage pane, then
                                        runs your previous statusline unchanged
    devtools_hooks.py event             async hook: appends one METADATA line per
                                        event (never tool inputs or outputs) so
                                        the dashboard can show what each session
                                        is doing and when one waits for you
    devtools_hooks.py guard             PreToolUse hook: enforces /careful and
                                        /freeze (.claude/state/session-guards.json
                                        in the project) and logs each block as
                                        metadata for the dashboard's guard log
    devtools_hooks.py install-statusline | uninstall-statusline
    devtools_hooks.py install-events     | uninstall-events
    devtools_hooks.py install-guard      | uninstall-guard

The install commands edit ~/.claude/settings.json (a copy is saved next to it
first as settings.json.bak-devtools). The Add-ons pane runs them in a visible
terminal; nothing is installed behind your back.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "claude-devtools"
elif os.name == "nt":
    APP_DIR = Path(os.environ.get("APPDATA",
                                  Path.home() / "AppData" / "Roaming")) / "claude-devtools"
else:
    APP_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                  Path.home() / ".config")) / "claude-devtools"
STATUS_DIR = APP_DIR / "status"
EVENTS = APP_DIR / "events.jsonl"
INNER = APP_DIR / "statusline_inner.json"      # the statusline we wrap
EVENTS_MAX = 5_000_000
GUARDS = APP_DIR / "guards.jsonl"
GUARDS_MAX = 1_000_000
# the profile a terminal belongs to (Ember sets CLAUDE_CONFIG_DIR for one)
SETTINGS = Path(os.environ.get("CLAUDE_ROOT") or os.environ.get("CLAUDE_CONFIG_DIR")
                or Path.home() / ".claude") / "settings.json"
MARK = "devtools_hooks.py"

# events worth a line; the rest would only add noise
EVENT_NAMES = ("SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse",
               "PostToolUse", "PostToolUseFailure", "PermissionRequest", "PermissionDenied",
               "Notification", "SubagentStart", "SubagentStop", "PreCompact",
               "PostCompact", "Stop", "StopFailure")
# metadata only: tool_input / tool_response / prompt are deliberately absent
KEEP = ("hook_event_name", "session_id", "cwd", "tool_name", "agent_type",
        "agent_id", "notification_type", "message", "matcher", "stop_hook_active")


def _atomic_write(path, text):
    if path.is_symlink():       # a dotfiles link: write its target, keep the link
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def statusline(stdin_text):
    """Save the status JSON, then hand the same stdin to the wrapped command."""
    try:
        data = json.loads(stdin_text)
        sid = "".join(c for c in str(data.get("session_id", "")) if c.isalnum() or c in "-_")
        if sid:
            data["_saved_at"] = time.time()
            _atomic_write(STATUS_DIR / f"{sid}.json", json.dumps(data))
    except (ValueError, OSError):
        pass                        # never break the user's statusline
    try:
        inner = json.loads(INNER.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError):
        inner = None
    if inner:
        r = subprocess.run(inner, shell=True, input=stdin_text, text=True,
                           capture_output=True)
        sys.stdout.write(r.stdout)
        return r.returncode
    try:                            # no previous statusline: a minimal one
        d = json.loads(stdin_text)
        five = ((d.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")
        ctx = (d.get("context_window") or {}).get("used_percentage")
        parts = [str((d.get("model") or {}).get("display_name") or "")]
        if ctx is not None:
            parts.append(f"ctx {ctx:.0f}%")
        if five is not None:
            parts.append(f"5h {five:.0f}%")
        print(" · ".join(p for p in parts if p))
    except (ValueError, TypeError, AttributeError):
        pass
    return 0


def event(stdin_text):
    """One compact line per event; rotated at 5 MB (one old copy kept)."""
    if os.environ.get("CDL_IMPROVE_RUN"):
        return 0                    # Ember's own retrospective: not your session
    try:
        d = json.loads(stdin_text)
    except ValueError:
        return 0
    line = {k: d[k] for k in KEEP if k in d}
    if isinstance(line.get("message"), str):
        line["message"] = line["message"][:200]
    line["ts"] = time.time()
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if EVENTS.exists() and EVENTS.stat().st_size > EVENTS_MAX:
            os.replace(EVENTS, EVENTS.with_suffix(".jsonl.1"))
        with open(EVENTS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass
    return 0


# ------------------------------------------------------------------ guards
# /careful and /freeze write .claude/state/session-guards.json in the project;
# this hook enforces them. A guard stays active until "/careful off" or
# "/freeze off" flips it back.

GUARD_TOOLS = "Bash|Edit|Write|MultiEdit|NotebookEdit"
DESTRUCTIVE = [
    (r"\brm\s+-[a-z]*[rf]", "rm with recursive/force flags"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+push\s+(.*\s)?(--force\b|-f\b)", "git push --force"),
    (r"\bgit\s+clean\s+-[a-z]*f", "git clean -f"),
    (r"\bgit\s+checkout\s+--\s+\.", "git checkout -- ."),
    (r"\bgit\s+branch\s+-D\b", "git branch -D"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "DROP TABLE/DATABASE"),
    (r"\bchmod\s+(-R\s+)?777\b", "chmod 777"),
]


def guard_verdict(d):
    """(guard, rule, reason) when the tool call must be blocked, else None."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or d.get("cwd") or ""
    if not root:
        return None
    try:
        guards = json.loads((Path(root) / ".claude" / "state" /
                             "session-guards.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(guards, dict):
        return None
    tool, inp = d.get("tool_name", ""), d.get("tool_input") or {}
    freeze = guards.get("freeze") or {}
    if freeze.get("active") and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        if target:
            f = os.path.normcase(os.path.realpath(os.path.join(root, target)))
            base = os.path.normcase(os.path.realpath(root))
            allowed = [os.path.join(base, os.path.normcase(a)) for a in
                       freeze.get("allowed_paths") or []] + [os.path.join(base, ".claude")]

            def inside(a):
                a = a.rstrip("/\\")
                try:
                    return os.path.commonpath([f, a]) == a
                except ValueError:          # different drives on Windows
                    return False
            if not any(inside(a) for a in allowed):
                return ("freeze", Path(target).name,
                        f"FREEZE ACTIVE: edit blocked, '{Path(target).name}' is outside "
                        f"{freeze.get('allowed_paths') or []}. Run /freeze off to lift it.")
    careful = guards.get("careful") or {}
    if careful.get("active") and tool == "Bash":
        cmd = str(inp.get("command", ""))
        for pat, rule in DESTRUCTIVE:
            if re.search(pat, cmd, re.IGNORECASE):
                return ("careful", rule,
                        f"CAREFUL MODE: blocked '{rule}'. Run /careful off to lift it, "
                        f"or rephrase the command.")
    return None


def guard(stdin_text):
    """Deny the call when a guard says so; log the block (never the command)."""
    try:
        d = json.loads(stdin_text)
    except ValueError:
        return 0
    v = guard_verdict(d)
    if not v:
        return 0
    line = {"ts": time.time(), "session_id": d.get("session_id"), "cwd": d.get("cwd"),
            "tool_name": d.get("tool_name"), "guard": v[0], "rule": v[1]}
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        if GUARDS.exists() and GUARDS.stat().st_size > GUARDS_MAX:
            os.replace(GUARDS, GUARDS.with_suffix(".jsonl.1"))
        with open(GUARDS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
    except OSError:
        pass                        # a failed log must not unblock the call
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": v[2]}}))
    return 0


def _cmd(sub):
    q = lambda s: f'"{s}"' if " " in s else s
    # a frozen Ember.exe / Ember runs us itself: `Ember devtools_hooks.py event`
    # (the literal name keeps MARK in the command)
    me = MARK if getattr(sys, "frozen", False) else q(str(Path(__file__).resolve()))
    return f"{q(sys.executable)} {me} {sub}"


def _ours(command, sub):
    """Is this hook command our `sub` tee? The last word is the subcommand, so
    the event tee and the guard (both on PreToolUse) are told apart."""
    command = str(command or "")
    return MARK in command and command.split()[-1:] == [sub]


def _load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _save_settings(st):
    if SETTINGS.exists():
        backup = SETTINGS.with_name("settings.json.bak-devtools")
        backup.write_text(SETTINGS.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"saved a copy of your settings: {backup}")
    _atomic_write(SETTINGS, json.dumps(st, indent=2) + "\n")


def install_statusline():
    st = _load_settings()
    cur = st.get("statusLine") or {}
    if MARK in str(cur.get("command", "")):
        print("statusline tee already installed")
        return 0
    if cur.get("command"):
        _atomic_write(INNER, json.dumps({"command": cur["command"]}))
        print(f"your statusline keeps working (wrapped): {cur['command']}")
    st["statusLine"] = dict(cur, type="command", command=_cmd("statusline"))
    _save_settings(st)
    print("installed: new Claude Code sessions report their limits to the dashboard")
    return 0


def uninstall_statusline():
    st = _load_settings()
    if MARK not in str((st.get("statusLine") or {}).get("command", "")):
        print("statusline tee not installed")
        return 0
    try:
        inner = json.loads(INNER.read_text(encoding="utf-8")).get("command")
    except (OSError, ValueError):
        inner = None
    if inner:
        st["statusLine"]["command"] = inner
    else:
        st.pop("statusLine", None)
    _save_settings(st)
    print("statusline restored")
    return 0


def install_events():
    st = _load_settings()
    hooks = st.setdefault("hooks", {})
    added = 0
    for ev in EVENT_NAMES:
        entries = hooks.setdefault(ev, [])
        if any(_ours(h.get("command"), "event") for e in entries for h in e.get("hooks", [])):
            continue
        entries.append({"hooks": [{"type": "command", "command": _cmd("event"),
                                   "async": True, "timeout": 5}]})
        added += 1
    _save_settings(st)
    print(f"installed an async event hook on {added} event(s)")
    return 0


def _remove(st, sub):
    hooks = st.get("hooks") or {}
    for ev in list(hooks):
        kept = []
        for e in hooks[ev]:
            hs = [h for h in e.get("hooks", []) if not _ours(h.get("command"), sub)]
            if hs:
                kept.append(dict(e, hooks=hs))
        if kept:
            hooks[ev] = kept
        else:
            del hooks[ev]


def uninstall_events():
    st = _load_settings()
    _remove(st, "event")
    _save_settings(st)
    print("event hook removed")
    return 0


def install_guard():
    st = _load_settings()
    entries = st.setdefault("hooks", {}).setdefault("PreToolUse", [])
    if any(_ours(h.get("command"), "guard") for e in entries for h in e.get("hooks", [])):
        print("session guards already installed")
        return 0
    # synchronous on purpose: it has to answer before the tool runs
    entries.append({"matcher": GUARD_TOOLS,
                    "hooks": [{"type": "command", "command": _cmd("guard"), "timeout": 5}]})
    _save_settings(st)
    print("installed: /careful and /freeze now block in new Claude Code sessions")
    return 0


def uninstall_guard():
    st = _load_settings()
    _remove(st, "guard")
    _save_settings(st)
    print("session guards removed")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "statusline":
        return statusline(sys.stdin.read())
    if cmd == "event":
        return event(sys.stdin.read())
    if cmd == "guard":
        return guard(sys.stdin.read())
    fn = {"install-statusline": install_statusline, "uninstall-statusline": uninstall_statusline,
          "install-events": install_events, "uninstall-events": uninstall_events,
          "install-guard": install_guard, "uninstall-guard": uninstall_guard}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
