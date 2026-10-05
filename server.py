#!/usr/bin/env python3
"""
Ember — local, read-only inspector for Claude Code sessions.

Reads ~/.claude/projects/<slug>/*.jsonl transcripts (plus subagent transcripts
and project memory) and serves a single-page dashboard on localhost.

Zero dependencies: Python 3.9+ standard library only.
Reads ~/.claude; writes only its own state, a plan checkbox you click, ~/.claude/improve-reports/,
and on a click a session_logs/ entry or an export in ~/Downloads.

Usage:
    python3 server.py [--port 3456] [--root ~/.claude]
"""
import argparse
import base64
import hashlib
import collections
import json
import logging
import logging.handlers
import os
import traceback
import re
import secrets
import select
import shlex
import shutil
import signal
import socketserver
import subprocess
import struct
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath, PureWindowsPath

# POSIX pseudo-terminals power the embedded terminal. macOS/Linux have them;
# Windows does not (ConPTY needs a third-party package), so there the
# dashboard runs fully except for the terminal pane, which reports why.
try:
    import fcntl
    import pty
    import termios
    HAS_PTY = True
except ImportError:                                   # Windows
    fcntl = pty = termios = None
    HAS_PTY = False

# Windows: ConPTY gives the same capability through kernel32 (ctypes only)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import winconpty  # noqa: E402

HAS_TERMINAL = HAS_PTY or winconpty.unsupported_reason() is None


VERSION = "1.3.0"      # single source: build-app.sh and the HTTP header read it
HERE = Path(__file__).resolve().parent
# Inside Ember.app (Contents/Resources) or a PyInstaller build, the code folder
# is replaced wholesale on every update: nothing may be written there.
BUNDLED = (bool(getattr(sys, "frozen", False))
           or ".app/Contents/Resources" in HERE.as_posix())
CLAUDE_ROOT = Path(os.environ.get("CLAUDE_ROOT", str(Path.home() / ".claude")))

# --- private data dir ------------------------------------------------------
# Token and state live OUTSIDE the source folder: the source folder is a git
# repo, and one `git add -f` would publish the token permanently.
if sys.platform == "darwin":
    APP_DIR = Path.home() / "Library" / "Application Support" / "claude-devtools"
elif os.name == "nt":
    APP_DIR = Path(os.environ.get("APPDATA",
                                  Path.home() / "AppData" / "Roaming")) / "claude-devtools"
else:
    APP_DIR = Path(os.environ.get("XDG_CONFIG_HOME",
                                  Path.home() / ".config")) / "claude-devtools"
APP_DIR.mkdir(parents=True, exist_ok=True)

# Log: stderr (the launchers' redirect) + a small rotating file in APP_DIR, so
# the macOS app — whose stderr goes nowhere — still leaves a trace. Only
# non-2xx and slow requests are logged: polls would add ~46k lines a day.
LOG = logging.getLogger("devtools")
LOG.setLevel(logging.INFO)
LOG.propagate = False
STARTED = time.time()
RECENT_ERRORS = collections.deque(maxlen=20)      # for /api/health
SLOW_REQUEST_S = 0.5


def setup_logging():
    fmt = logging.Formatter("%(asctime)s [devtools] %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.append(logging.handlers.RotatingFileHandler(
            APP_DIR / "server.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8"))
        os.chmod(APP_DIR / "server.log", 0o600)
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(fmt)
        LOG.addHandler(h)
try:
    os.chmod(APP_DIR, 0o700)
except OSError:
    pass

# --- auth token: protects every /api endpoint from other local users -------
# Persisted (0600) so bookmarks keep working across restarts. The app exchanges
# it for a same-site cookie at /launch; the UI also sends X-Devtools-Token.
TOKEN_FILE = APP_DIR / "token"
LEGACY_TOKEN_FILE = HERE / ".token"


def load_token():
    for f in (TOKEN_FILE, LEGACY_TOKEN_FILE):
        try:
            tok = f.read_text().strip()
        except OSError:
            continue
        if re.fullmatch(r"[a-f0-9]{32,64}", tok):
            if f is LEGACY_TOKEN_FILE:      # migrate out of the repo
                TOKEN_FILE.write_text(tok)
                os.chmod(TOKEN_FILE, 0o600)
                try:
                    LEGACY_TOKEN_FILE.unlink()
                except OSError:
                    pass
            return tok
    tok = secrets.token_hex(24)
    TOKEN_FILE.write_text(tok)
    os.chmod(TOKEN_FILE, 0o600)
    return tok


SERVER_TOKEN = None   # set in main()

# One-time launch codes: launchers trade the token (sent in a header) for a
# 60 s single-use code, so the long-lived token never lands in a browser's
# argv, shell history or URL bar.
_launch_codes = {}
_launch_lock = threading.Lock()


def launch_code_new():
    code = secrets.token_hex(16)
    with _launch_lock:
        now = time.time()
        for c in [c for c, exp in _launch_codes.items() if exp < now]:
            del _launch_codes[c]
        _launch_codes[code] = now + 60
    return code


def launch_code_take(code):
    with _launch_lock:
        exp = _launch_codes.pop(code, 0)
    return exp >= time.time()


def hello_mac(token, nonce):
    """Proof that this server holds `token`, without revealing it: a launcher
    checks it before sending the token to whatever answers on the port."""
    import hmac
    return hmac.new(token.encode(), b"cdl-hello:" + nonce.encode(), "sha256").hexdigest()
# DNS-rebinding guard: loopback by default; main() adds an explicit --host
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# --- small persistent state (usage baseline cache, layout) -----------------
STATE_FILE = APP_DIR / "state.json"
_LEGACY_STATE = HERE / ".state.json"
if not STATE_FILE.exists() and _LEGACY_STATE.exists():
    try:
        STATE_FILE.write_text(_LEGACY_STATE.read_text())
        _LEGACY_STATE.unlink()
    except OSError:
        pass
_state_lock = threading.Lock()


def state_read():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def state_write(patch):
    with _state_lock:
        st = state_read()
        st.update(patch)
        # atomic: a crash mid-write must not leave a truncated file (which
        # state_read would silently read back as {})
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, STATE_FILE)


# --- update notice -----------------------------------------------------------
# At most once a day, ask GitHub for the latest release; the UI shows a notice
# with a download link. Nothing is installed automatically. The only request
# Ember makes on its own to the internet; off with the palette or CDL_UPDATES=0.
# the web redirect, not the REST API: the API allows 60 unauthenticated calls
# an hour per IP, which a shared (university, office) address can use up
UPDATE_URL = "https://github.com/PierreBeaucoral/ember/releases/latest"
UPDATE_EVERY_S = 86400
_update = {"at": 0.0, "info": None}


def open_in():
    """Where the desktop apps open the dashboard: their own window (default)
    or the default browser. Read at launch by native/main.swift and
    native/window.py; the browser launchers ignore it."""
    return "browser" if state_read().get("open_in") == "browser" else "window"


def updates_enabled():
    if os.environ.get("CDL_UPDATES", "1") == "0":
        return False
    return bool(state_read().get("updates", True))


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])


def latest_release_url():
    """…/releases/latest redirects to …/releases/tag/vX.Y.Z."""
    import urllib.request
    req = urllib.request.Request(UPDATE_URL, method="HEAD",
                                 headers={"User-Agent": "ember/" + VERSION})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.geturl()


def update_info(force=False, fetch=latest_release_url):
    """{current, enabled, latest, url, newer}; latest/url stay None until a
    check succeeded."""
    out = {"current": VERSION, "enabled": updates_enabled(),
           "latest": None, "url": None, "newer": False}
    if not out["enabled"]:
        return out
    now = time.time()
    if force or now - _update["at"] > UPDATE_EVERY_S:
        _update["at"] = now     # also on failure: offline means retry tomorrow
        try:
            url = str(fetch())
            m = re.fullmatch(r"https://github\.com/PierreBeaucoral/ember/releases/tag/"
                             r"v?(\d+(?:\.\d+){0,2})", url)
            if m:
                _update["info"] = {"latest": m.group(1), "url": url}
        except (OSError, ValueError) as e:
            LOG.info("update check failed: %s", e)
    if _update["info"]:
        out.update(_update["info"])
        out["newer"] = version_tuple(out["latest"]) > version_tuple(VERSION)
    return out


MAX_RESULT_CHARS = 20_000     # per tool-result payload sent to the UI
MAX_TEXT_CHARS = 120_000      # per text/thinking block sent to the UI
SEARCH_MAX_RESULTS = 300
LIST_HEAD_BYTES = 512 * 1024  # how much of a big file to scan for list metadata
LIST_TAIL_BYTES = 256 * 1024

_meta_cache = {}              # path -> (mtime, size, meta dict)
_cache_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def projects_dir(root):
    return Path(root) / "projects"


def safe_project_path(root, slug):
    """Resolve a project slug to its directory, refusing path traversal."""
    if "/" in slug or "\\" in slug or slug.startswith("."):
        raise ValueError("bad project slug")
    p = projects_dir(root) / slug
    if not p.is_dir():
        raise FileNotFoundError(slug)
    return p


def iter_jsonl(path, max_bytes=None):
    """Yield parsed objects from a JSONL file, skipping malformed lines."""
    read = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            read += len(line)
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
            if max_bytes is not None and read > max_bytes:
                return


def tail_lines(path, n_bytes):
    """Return the last complete lines within n_bytes of the end of a file."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > n_bytes:
            f.seek(size - n_bytes)
            f.readline()  # drop the partial first line
        return [ln.decode("utf-8", "replace") for ln in f.read().splitlines()]


def truncate(s, limit):
    if s is None:
        return None
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n… [truncated, {len(s):,} chars total]"


def block_text(content):
    """Flatten a message content (str or list of blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "tool_result":
                    parts.append(block_text(b.get("content")))
            elif isinstance(b, str):
                parts.append(b)
        return "\n".join(p for p in parts if p)
    return ""


# ---------------------------------------------------------------- session listing

def session_meta(path):
    """Cheap metadata for the session list: title, times, model, counts."""
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _meta_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]

    meta = {
        "id": path.stem,
        "size": st.st_size,
        "mtime": st.st_mtime,
        "title": None,
        "first_ts": None,
        "last_ts": None,
        "model": None,
        "version": None,
        "cwd": None,
        "gitBranch": None,
        "user_msgs": 0,
        "assistant_msgs": 0,
        "has_subagents": (path.parent / path.stem / "subagents").is_dir(),
        "api_error": None,
    }

    small = st.st_size <= LIST_HEAD_BYTES + LIST_TAIL_BYTES

    def absorb(o):
        t = o.get("type")
        ts = o.get("timestamp")
        if ts:
            if meta["first_ts"] is None or ts < meta["first_ts"]:
                meta["first_ts"] = ts
            if meta["last_ts"] is None or ts > meta["last_ts"]:
                meta["last_ts"] = ts
        if t == "custom-title" and o.get("customTitle"):
            meta["title"] = o["customTitle"]
        elif t == "summary" and o.get("summary") and not meta["title"]:
            meta["title"] = o["summary"]
        elif t == "user" and not o.get("isSidechain"):
            meta["user_msgs"] += 1
            meta["cwd"] = meta["cwd"] or o.get("cwd")
            meta["version"] = meta["version"] or o.get("version")
            meta["gitBranch"] = meta["gitBranch"] or o.get("gitBranch")
            if meta["title"] is None and not o.get("isMeta"):
                txt = block_text(o.get("message", {}).get("content"))
                if txt and not txt.startswith(UNTITLED_PREFIXES):
                    meta["title"] = txt.strip()[:120]
        elif t == "assistant" and not o.get("isSidechain"):
            meta["assistant_msgs"] += 1
            m = o.get("message", {})
            # ended on an API error (rate limit, auth, overload)? a later real
            # reply clears it. Records arrive in file order, head then tail.
            meta["api_error"] = (o.get("error") or "error") if o.get("isApiErrorMessage") else None
            if m.get("model") != "<synthetic>":
                meta["model"] = m.get("model") or meta["model"]

    if small:
        for o in iter_jsonl(path):
            absorb(o)
    else:
        for o in iter_jsonl(path, max_bytes=LIST_HEAD_BYTES):
            absorb(o)
        for ln in tail_lines(path, LIST_TAIL_BYTES):
            try:
                absorb(json.loads(ln))
            except json.JSONDecodeError:
                continue
        # counts are partial for big files; mark that
        meta["counts_partial"] = True

    if not meta["title"]:
        meta["title"] = "(untitled session)"
    with _cache_lock:
        _meta_cache[key] = (st.st_mtime, st.st_size, meta)
    return meta


def list_projects(root):
    out = []
    pdir = projects_dir(root)
    if not pdir.is_dir():
        return out
    for d in sorted(pdir.iterdir()):
        if not d.is_dir():
            continue
        sessions = list(d.glob("*.jsonl"))
        if not sessions and not (d / "memory").is_dir():
            continue
        newest = max((s.stat().st_mtime for s in sessions), default=d.stat().st_mtime)
        # authoritative cwd from the newest session, not the lossy slug
        cwd = None
        for s in sorted(sessions, key=lambda p: p.stat().st_mtime, reverse=True)[:3]:
            for o in iter_jsonl(s, max_bytes=64 * 1024):
                if o.get("cwd"):
                    cwd = o["cwd"]
                    break
            if cwd:
                break
        graph = None
        if cwd:
            g = Path(cwd) / "graphify-out" / "graph.html"
            try:
                if g.is_file():
                    graph = str(g)
            except OSError:
                pass
        out.append({
            "slug": d.name,
            "path": cwd or d.name.replace("-", "/"),
            "sessions": len(sessions),
            "mtime": newest,
            "has_memory": (d / "memory" / "MEMORY.md").is_file(),
            "graph": graph,
        })
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


def project_cwd(root, slug):
    """Real working directory of a project, read from its newest session."""
    d = safe_project_path(root, slug)
    sessions = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for s in sessions[:5]:
        for o in iter_jsonl(s, max_bytes=64 * 1024):
            if o.get("cwd"):
                return o["cwd"]
    return None


# ---------------------------------------------------------------- session parsing

def summarize_tool_result(name, tur, block_content):
    """Produce a compact, human-useful string for a tool result."""
    if isinstance(tur, dict):
        if "stdout" in tur or "stderr" in tur:  # Bash
            parts = []
            if tur.get("stdout"):
                parts.append(tur["stdout"])
            if tur.get("stderr"):
                parts.append("[stderr]\n" + tur["stderr"])
            return "\n".join(parts) or "(no output)"
        if tur.get("type") == "text" and isinstance(tur.get("file"), dict):  # Read
            return tur["file"].get("content", "")
        if "oldString" in tur or "structuredPatch" in tur:  # Edit / Write
            return None  # rendered as a diff from the input side
        if "plan" in tur:
            return tur.get("plan")
        if "results" in tur:  # WebSearch etc.
            try:
                return json.dumps(tur["results"], indent=2, ensure_ascii=False)
            except (TypeError, ValueError):
                pass
        if "result" in tur and isinstance(tur["result"], str):  # WebFetch
            return tur["result"]
    txt = block_text(block_content)
    if txt:
        return txt
    if tur is not None:
        try:
            return json.dumps(tur, indent=2, ensure_ascii=False)[:MAX_RESULT_CHARS]
        except (TypeError, ValueError):
            return str(tur)
    return None


COMMAND_PREFIXES = ("<local-command", "<command-name", "<command-message")
# a title never comes from command output or a built-in (/clear, /model);
# a skill command (<command-message>…) does: "/improve config audit"
UNTITLED_PREFIXES = ("<local-command", "<command-name")
CMD_RE = {k: re.compile(rf"<{k}>(.*?)</{k}>", re.S)
          for k in ("command-name", "command-args", "local-command-stdout", "local-command-stderr")}
AGENT_FROM_RE = re.compile(r'<agent-message from="([^"]+)"')
TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
TASK_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)


def est_tok(n_chars):
    """~4 characters per token: an estimate, always shown with a `~`."""
    return max(1, n_chars // 4) if n_chars else 0


def parse_command(txt):
    """<command-name>/x</command-name><command-args>…</command-args> → fields;
    local-command output keeps its text. Values are the tag contents."""
    out = {}
    for tag, key in (("command-name", "cmd"), ("command-args", "args"),
                     ("local-command-stdout", "out"), ("local-command-stderr", "out")):
        m = CMD_RE[tag].search(txt)
        if m and m.group(1).strip():
            out[key] = m.group(1).strip()
    return out


def first_heading_text(txt):
    for line in txt.splitlines()[:40]:
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip()[:80]
    return None


def meta_entry(txt, o, prev):
    """A message Claude Code injected as 'user' (isMeta): a skill's body, a
    subagent's report, a compaction summary, an image note. Never a prompt."""
    origin = o.get("origin") if isinstance(o.get("origin"), dict) else {}
    if o.get("isCompactSummary"):
        sub, title = "summary", "Compaction summary"
    elif origin.get("kind") == "peer" or txt.startswith("Another Claude session"):
        m = AGENT_FROM_RE.search(txt[:400])
        sub, title = "agent", "Report from " + (origin.get("name") or (m and m.group(1)) or "agent")
    elif txt.startswith("Base directory for this skill:"):
        path = txt.split("\n", 1)[0].split(":", 1)[1].strip().rstrip("/\\")
        sub, title = "skill", "Skill " + re.split(r"[\\/]", path)[-1]
    elif prev and prev.get("kind") == "command" and prev.get("cmd"):
        sub, title = "skill", "Skill " + prev["cmd"]
    elif txt.startswith("[Image"):
        sub, title = "note", txt.split("\n", 1)[0][:100]
    else:
        sub, title = "note", first_heading_text(txt) or txt.strip().split("\n", 1)[0][:100]
    return {"kind": "meta", "sub": sub, "title": title}


def context_item(a, n_entries):
    """What an `attachment` record put into the context window, as one or more
    {entry, cat, label, tok}. Unknown types give nothing: the schema is
    Claude Code's, undocumented, and changes between versions."""
    t = a.get("type")
    size = lambda v: len(v) if isinstance(v, str) else len(json.dumps(v, ensure_ascii=False))
    item = lambda cat, label, chars, **kw: dict(entry=n_entries, cat=cat, label=label,
                                                tok=est_tok(chars), **kw)
    if t == "instructions":
        return [item("claude-md", f.get("path") or "?", size(f.get("content") or ""),
                     scope=f.get("type") or "")
                for f in a.get("files") or [] if isinstance(f, dict)]
    if t == "prompt_snapshot" and a.get("systemPrompt"):
        return [item("system", "System prompt", size(a["systemPrompt"]))]
    if t == "skill_listing":
        return [item("skills", f"Skill list ({a.get('skillCount') or len(a.get('names') or [])})",
                     size(a.get("content") or ""))]
    if t == "agent_listing_delta":
        return [item("agents", "Agent list", size(a.get("addedLines") or ""))]
    if t == "mcp_instructions_delta":
        names = ", ".join(a.get("addedNames") or [])[:80]
        return [item("mcp", "MCP instructions" + (f": {names}" if names else ""),
                     size(a.get("addedBlocks") or ""))]
    if t == "deferred_tools_delta":
        return [item("mcp", "Deferred tool list", size(a.get("addedLines") or ""))]
    if t == "hook_additional_context":
        return [item("hooks", a.get("hookName") or "hook", size(a.get("content") or ""))]
    if t == "file":
        c = a.get("content")
        body = (c.get("file") or {}).get("content") if isinstance(c, dict) else c
        return [item("mentions", a.get("displayPath") or a.get("filename") or "file",
                     size(body or ""))]
    if t == "edited_text_file":
        return [item("mentions", (a.get("filename") or "file") + " (changed)",
                     size(a.get("snippet") or ""))]
    return []


AGENT_STATS = {}            # path -> ((mtime_ns, size), stats)
AGENT_STATS_MAX = 400


def agent_stats(f):
    """Cheap one-pass summary of a subagent transcript, cached per file, so a
    Task row shows the agent's cost before anyone expands it."""
    st = f.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = AGENT_STATS.get(f)
    if hit and hit[0] == key:
        return hit[1]
    seen, out, peak, tools, first, last, model = set(), 0, 0, 0, None, None, None
    for o in iter_jsonl(f):
        ts = o.get("timestamp")
        if ts:
            first = first or ts
            last = ts
        if o.get("type") != "assistant":
            continue
        msg = o.get("message") or {}
        if msg.get("model") and msg["model"] != "<synthetic>":
            model = msg["model"]
        tools += sum(1 for b in msg.get("content") or []
                     if isinstance(b, dict) and b.get("type") == "tool_use")
        rid, u = o.get("requestId"), msg.get("usage")
        if rid and u and rid not in seen:
            seen.add(rid)
            out += u.get("output_tokens", 0)
            peak = max(peak, u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0))
    a, b = _ts_epoch(first), _ts_epoch(last)
    stats = {"model": model, "tools": tools, "output": out, "peak": peak,
             "duration": round(b - a, 1) if a and b else None, "mtime": st.st_mtime}
    try:
        meta = json.loads(f.with_name(f.stem + ".meta.json").read_text(encoding="utf-8"))
        stats["type"] = meta.get("agentType")
    except (OSError, ValueError, AttributeError):
        pass
    if len(AGENT_STATS) >= AGENT_STATS_MAX:
        AGENT_STATS.clear()
    AGENT_STATS[f] = (key, stats)
    return stats


def parse_session(path, include_sidechain=False):
    """Parse a transcript into UI-ready timeline entries + usage series.

    Subagent transcripts mark every record isSidechain=true, so those are
    parsed with include_sidechain=True; main transcripts skip sidechain
    records (they live in their own files in recent Claude Code versions).

    Besides the timeline it returns `context`: what attachment records put into
    the context window (CLAUDE.md files, skill list, @-files…), each tagged
    with the timeline index it precedes, so the UI can attribute it to a turn.
    """
    entries = []            # ordered timeline
    tool_index = {}         # tool_use_id -> entry
    tool_ep = {}            # tool_use_id -> epoch of the call (for durations)
    usage_by_request = {}   # requestId -> usage (dedupe multi-block responses)
    context_series = []     # one point per API request
    context = []            # attachment-injected context, see docstring
    seen_requests = set()
    title = None
    model = None
    cache_ttl = None
    sidechain_count = 0

    for o in iter_jsonl(path):
        t = o.get("type")
        ts = o.get("timestamp")

        if t == "custom-title" and o.get("customTitle"):
            title = o["customTitle"]
            continue
        if t == "summary" and o.get("summary"):
            title = title or o["summary"]
            continue
        if o.get("isSidechain") and not include_sidechain:
            sidechain_count += 1
            continue
        if t == "attachment":
            a = o.get("attachment") if isinstance(o.get("attachment"), dict) else {}
            at = a.get("type")
            if at in ("hook_non_blocking_error", "hook_cancelled"):
                err = (a.get("stderr") or "").strip() or (
                    "timed out" if a.get("timedOut") else "cancelled")
                entries.append({"kind": "hook", "ts": ts, "name": a.get("hookName") or "hook",
                                "exit": a.get("exitCode"), "text": truncate(err, 4000),
                                "command": a.get("command")})
            elif at == "queued_command":
                origin = a.get("origin") if isinstance(a.get("origin"), dict) else {}
                txt = a.get("prompt") if isinstance(a.get("prompt"), str) else block_text(a.get("prompt"))
                if not txt:
                    continue
                if origin.get("kind") == "task-notification" or txt.startswith("<task-notification>"):
                    m = TASK_SUMMARY_RE.search(txt)
                    entries.append({"kind": "meta", "sub": "agent", "ts": ts,
                                    "title": (m.group(1).strip() if m else "Agent notification")[:120],
                                    "text": truncate(txt, MAX_TEXT_CHARS), "tok": est_tok(len(txt))})
                elif origin.get("kind") in (None, "human"):
                    # typed while Claude was working: a prompt, but not a new turn
                    entries.append({"kind": "user", "queued": True, "ts": ts,
                                    "text": truncate(txt, MAX_TEXT_CHARS), "tok": est_tok(len(txt)),
                                    "system_reminder": False})
            else:
                context.extend(context_item(a, len(entries)))
            continue
        if t in ("queue-operation", "last-prompt", "file-history-snapshot"):
            continue

        if t == "user":
            msg = o.get("message", {})
            content = msg.get("content")
            # tool results come back as user messages
            if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                        continue
                    entry = tool_index.get(b.get("tool_use_id"))
                    if entry is None:
                        continue
                    tur = o.get("toolUseResult")
                    if isinstance(tur, dict) and "structuredPatch" in tur:
                        entry["patch"] = tur.get("structuredPatch")
                        entry["file_path"] = tur.get("filePath")
                    res = summarize_tool_result(entry["name"], tur, b.get("content"))
                    entry["result"] = truncate(res, MAX_RESULT_CHARS)
                    entry["is_error"] = bool(b.get("is_error"))
                    entry["tok"] = entry.get("tok", 0) + est_tok(len(res or ""))
                    t0, t1 = tool_ep.get(b.get("tool_use_id")), _ts_epoch(ts)
                    if t0 is not None and t1 is not None:
                        entry["dur"] = round(max(0.0, t1 - t0), 2)
                    if isinstance(tur, dict) and tur.get("agentId"):
                        entry["agent_id"] = tur.get("agentId")
                continue
            txt = block_text(content)
            if not txt:
                continue
            if txt.startswith("<task-notification>"):          # older layouts: a plain user message
                m = TASK_SUMMARY_RE.search(txt)
                entries.append({"kind": "meta", "sub": "agent", "ts": ts,
                                "title": (m.group(1).strip() if m else "Agent notification")[:120],
                                "text": truncate(txt, MAX_TEXT_CHARS), "tok": est_tok(len(txt))})
                continue
            if o.get("isMeta") or o.get("isCompactSummary"):
                if txt.startswith("<local-command-caveat>"):
                    continue                # "the next message ran locally": noise
                e = meta_entry(txt, o, entries[-1] if entries else None)
                e.update(ts=ts, text=truncate(txt, MAX_TEXT_CHARS), tok=est_tok(len(txt)))
                entries.append(e)
                continue
            kind = "command" if txt.startswith(COMMAND_PREFIXES) else "user"
            sysrem = "<system-reminder>" in txt
            if title is None and not sysrem and not txt.startswith(UNTITLED_PREFIXES):
                title = txt.strip()[:120]
            e = {"kind": kind, "ts": ts, "text": truncate(txt, MAX_TEXT_CHARS),
                 "system_reminder": sysrem, "uuid": o.get("uuid"), "tok": est_tok(len(txt))}
            if kind == "command":
                e.update(parse_command(txt))
            entries.append(e)

        elif t == "assistant":
            msg = o.get("message", {})
            if o.get("isApiErrorMessage"):
                entries.append({"kind": "assistant", "ts": ts, "api_error": o.get("error") or "error",
                                "text": truncate(block_text(msg.get("content")) or "API error",
                                                 MAX_TEXT_CHARS)})
                continue
            if msg.get("model") and msg["model"] != "<synthetic>":
                model = msg["model"]
            rid = o.get("requestId")
            usage = msg.get("usage")
            if rid and usage and rid not in seen_requests:
                seen_requests.add(rid)
                usage_by_request[rid] = usage
                ctx = (usage.get("input_tokens", 0)
                       + usage.get("cache_read_input_tokens", 0)
                       + usage.get("cache_creation_input_tokens", 0))
                cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
                if cc.get("ephemeral_1h_input_tokens"):
                    cache_ttl = 3600
                elif cc.get("ephemeral_5m_input_tokens"):
                    cache_ttl = 300
                context_series.append({
                    "ts": ts, "context": ctx,
                    "output": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_creation": usage.get("cache_creation_input_tokens", 0),
                    "input": usage.get("input_tokens", 0),
                    "entry": len(entries),   # first timeline row of this request
                    "model": msg.get("model"),
                })
            for b in msg.get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    entries.append({"kind": "assistant", "ts": ts, "tok": est_tok(len(b["text"])),
                                    "text": truncate(b["text"], MAX_TEXT_CHARS)})
                elif bt == "thinking" and b.get("thinking"):
                    entries.append({"kind": "thinking", "ts": ts, "tok": est_tok(len(b["thinking"])),
                                    "text": truncate(b["thinking"], MAX_TEXT_CHARS)})
                elif bt == "tool_use":
                    entry = {"kind": "tool", "ts": ts, "name": b.get("name", "?"),
                             "input": b.get("input", {}), "id": b.get("id"),
                             "result": None, "is_error": False}
                    try:  # keep giant inputs (Write content) bounded
                        raw = json.dumps(entry["input"], ensure_ascii=False)
                        entry["tok"] = est_tok(len(raw))
                        if len(raw) > MAX_RESULT_CHARS:
                            entry["input_truncated"] = True
                            entry["input"] = {
                                k: (truncate(v, 4000) if isinstance(v, str) else v)
                                for k, v in entry["input"].items()}
                    except (TypeError, ValueError):
                        entry["input"] = {}
                    entries.append(entry)
                    if b.get("id"):
                        tool_index[b["id"]] = entry
                        tool_ep[b["id"]] = _ts_epoch(ts)

        elif t == "system":
            txt = o.get("content") or o.get("text") or ""
            sub = o.get("subtype", "")
            if "compact" in sub or "compact" in str(txt)[:200].lower():
                entries.append({"kind": "compact", "ts": ts,
                                "text": "Context compaction boundary"})
            elif txt:
                entries.append({"kind": "system", "ts": ts,
                                "text": truncate(block_text(txt) or str(txt), 4000)})

    # mark probable compactions from context drops (>35% between requests)
    for i in range(1, len(context_series)):
        prev, cur = context_series[i - 1], context_series[i]
        if prev["context"] > 60_000 and cur["context"] < prev["context"] * 0.65:
            cur["compaction"] = True

    totals = {
        "requests": len(usage_by_request),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage_by_request.values()),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage_by_request.values()),
        "cache_read": sum(u.get("cache_read_input_tokens", 0) for u in usage_by_request.values()),
        "cache_creation": sum(u.get("cache_creation_input_tokens", 0)
                              for u in usage_by_request.values()),
        "peak_context": max((p["context"] for p in context_series), default=0),
        # prompt cache: warm until the last request + its TTL (5 min or 1 h)
        "last_request_ts": context_series[-1]["ts"] if context_series else None,
        "cache_ttl": cache_ttl,
    }

    # tool call histogram
    tool_counts = {}
    for e in entries:
        if e["kind"] == "tool":
            tool_counts[e["name"]] = tool_counts.get(e["name"], 0) + 1

    # subagent transcripts on disk, with their cost (agent-<id>.jsonl)
    subagents = []
    subdir = path.parent / path.stem / "subagents"
    if subdir.is_dir():
        for f in sorted(subdir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime):
            try:
                stats = agent_stats(f)
            except OSError:
                stats = {}
            subagents.append({"file": f.name, "size": f.stat().st_size,
                              "mtime": f.stat().st_mtime, "stats": stats})

    return {
        "id": path.stem,
        "title": title,
        "model": model,
        "entries": entries,
        "context_series": context_series,
        "context": context,
        "totals": totals,
        "tool_counts": tool_counts,
        "subagents": subagents,
        "sidechain_msgs": sidechain_count,
    }


# ---------------------------------------------------------------- search

_session_cache = {}              # path -> ((mtime_ns, size), encoded JSON)
_session_lock = threading.Lock()
SESSION_CACHE_MAX = 3


def session_json(path):
    """parse_session(path) as encoded JSON, cached on (mtime, size): reopening
    a session, or coming back from a subagent, skips a re-parse that costs
    ~0.7 s on a 100 MB transcript. A live session changes size, so it misses."""
    st = path.stat()
    key = (st.st_mtime_ns, st.st_size)
    with _session_lock:
        hit = _session_cache.get(path)
        if hit and hit[0] == key:
            _session_cache[path] = _session_cache.pop(path)      # most recent
            return hit[1]
    body = json.dumps(parse_session(path), ensure_ascii=False).encode("utf-8")
    with _session_lock:
        _session_cache[path] = (key, body)
        while len(_session_cache) > SESSION_CACHE_MAX:           # evict oldest
            del _session_cache[next(iter(_session_cache))]
    return body


STATUS_TTL = 600          # official limits older than this are not shown


def official_limits():
    """Newest statusline snapshot (tools/devtools_hooks.py statusline), if
    fresh: Claude Code's own 5-hour / 7-day usage %, reset times, and the
    context % and cost of that session. None → the P90 estimate stands."""
    best = None
    try:
        for f in (APP_DIR / "status").glob("*.json"):
            m = f.stat().st_mtime
            if time.time() - m < STATUS_TTL and (best is None or m > best[0]):
                best = (m, f)
    except OSError:
        return None
    if not best:
        return None
    try:
        d = json.loads(best[1].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rl = d.get("rate_limits") or {}
    cw = d.get("context_window") or {}
    return {"at": best[0], "session_id": d.get("session_id"),
            "five_hour": rl.get("five_hour"), "seven_day": rl.get("seven_day"),
            "context_pct": cw.get("used_percentage"),
            "cost_usd": (d.get("cost") or {}).get("total_cost_usd"),
            "model": (d.get("model") or {}).get("display_name")}


EVENTS_FILE = APP_DIR / "events.jsonl"
GUARDS_FILE = APP_DIR / "guards.jsonl"      # blocks by the Session guards tee


def events_since(since, limit=500, path=None):
    """Hook events appended after byte offset `since` (tools/devtools_hooks.py
    event). since < 0 → just the current end, so a fresh page starts live.
    A file smaller than `since` was rotated: start again from 0."""
    path = path or EVENTS_FILE
    try:
        size = path.stat().st_size
    except OSError:
        return {"events": [], "offset": 0, "enabled": False}
    if since < 0:
        return {"events": [], "offset": size, "enabled": True}
    if since > size:
        since = 0
    out = []
    with open(path, "rb") as fh:
        fh.seek(since)
        data = fh.read(2_000_000)
    end = data.rfind(b"\n") + 1                 # only complete lines
    for line in data[:end].splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return {"events": out[-limit:], "offset": since + end, "enabled": True}


TAIL_OVERLAP = 30         # re-send the last N entries: their tool results may have landed


def session_tail(path, key, since):
    """Live-follow: `key` is the (mtime_ns:size) the client last saw. Unchanged
    → a stat, nothing more. Changed → entries from `since - TAIL_OVERLAP` on
    (earlier tool calls get their results late), which the client splices in
    at `start`, plus the fresh totals."""
    st = path.stat()
    now_key = f"{st.st_mtime_ns}:{st.st_size}"
    if key == now_key:
        return {"key": now_key, "unchanged": True}
    # ponytail: re-parses the whole file on each change (~0.8 s per 100 MB);
    # a resumable parse from a byte offset if huge live sessions feel slow
    d = parse_session(path)
    start = max(0, min(int(since), len(d["entries"])) - TAIL_OVERLAP)
    return {"key": now_key, "start": start, "entries": d["entries"][start:],
            "total": len(d["entries"]), "totals": d["totals"],
            "tool_counts": d["tool_counts"], "context_series": d["context_series"],
            "subagents": d["subagents"], "title": d["title"], "context": d["context"]}


SEARCH_CHUNK = 8 * 1024 * 1024


def _file_may_contain(path, needles):
    """Cheap byte-level pre-check (ASCII queries only): lower-cased 8 MB
    chunks with an overlap, so a big transcript that cannot match is skipped
    without decoding a single line."""
    keep = max(len(n) for n in needles) - 1
    tail = b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(SEARCH_CHUNK)
                if not chunk:
                    return False
                buf = tail + chunk.lower()
                if any(n in buf for n in needles):
                    return True
                tail = buf[-keep:] if keep else b""
    except OSError:
        return False


def _search_file(path, q, needles):
    """(record, text) for each user/assistant record whose text contains q."""
    if needles and not _file_may_contain(path, needles):
        return
    # the raw line holds JSON-escaped text: `"x"` is stored as `\"x\"`, and
    # some writers store `è` as `\u00e8`
    raw = {q, json.dumps(q, ensure_ascii=False)[1:-1].lower(), json.dumps(q)[1:-1].lower()}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                low = line.lower()
                if not any(r in low for r in raw):
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("type") not in ("user", "assistant"):
                    continue
                txt = block_text(o.get("message", {}).get("content"))
                if txt and q in txt.lower():
                    yield o, txt
    except OSError:
        return


def search_all(root, query, project=None, limit=SEARCH_MAX_RESULTS):
    """Full-text search over sessions AND their subagent transcripts, newest
    first; `project` scopes it to one project (much faster)."""
    q = query.lower()
    needles = None
    if q.isascii():
        needles = {q.encode(), json.dumps(q)[1:-1].lower().encode()}
    results = []
    pdir = projects_dir(root)
    dirs = [safe_project_path(root, project)] if project else \
        sorted((d for d in pdir.iterdir() if d.is_dir()),
               key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs:
        for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
            files = [(f, None)] + [(a, a.name) for a in
                                   sorted((d / f.stem / "subagents").glob("*.jsonl"))]
            for path, agent in files:
                for o, txt in _search_file(path, q, needles):
                    i = txt.lower().find(q)
                    lo, hi = max(0, i - 120), min(len(txt), i + len(q) + 160)
                    hit = {
                        "project": d.name,
                        "session": f.stem,
                        "type": o.get("type"),
                        "ts": o.get("timestamp"),
                        "snippet": ("…" if lo else "") + txt[lo:hi] + ("…" if hi < len(txt) else ""),
                    }
                    if agent:
                        hit["agent"] = agent
                    results.append(hit)
                    if len(results) >= limit:
                        return results
    return results


# ---------------------------------------------------------------- memory

def read_memory(root, slug):
    d = safe_project_path(root, slug) / "memory"
    if not d.is_dir():
        return {"files": []}
    files = []
    for f in sorted(d.glob("*.md")):
        try:
            files.append({"name": f.name,
                          "content": truncate(f.read_text(encoding="utf-8", errors="replace"),
                                              MAX_TEXT_CHARS)})
        except OSError:
            continue
    files.sort(key=lambda x: (x["name"] != "MEMORY.md", x["name"]))
    return {"files": files}


# ---------------------------------------------------------------- usage aggregation
#
# Token usage across ALL projects, computed from the transcripts themselves.
# Claude limits work in ~5-hour blocks anchored to first activity, so we
# reconstruct the current block ccusage-style (new block after a >5h-from-
# block-start request, start floored to the hour).

from datetime import datetime, timezone

_usage_cache = {}  # path -> (mtime, size, [(epoch, out, model), ...])
BLOCK_HOURS = 5


def _ts_epoch(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def file_usage_records(path):
    st = path.stat()
    key = str(path)
    with _cache_lock:
        hit = _usage_cache.get(key)
        if hit and hit[0] == st.st_mtime and hit[1] == st.st_size:
            return hit[2]
    recs, seen = [], set()
    for o in iter_jsonl(path):
        if o.get("type") != "assistant":
            continue
        rid = o.get("requestId")
        u = o.get("message", {}).get("usage")
        if not rid or not u or rid in seen or o.get("isApiErrorMessage"):
            continue
        seen.add(rid)
        ep = _ts_epoch(o.get("timestamp"))
        if ep is None:
            continue
        recs.append((ep, u.get("output_tokens", 0),
                     o.get("message", {}).get("model") or "?"))
    with _cache_lock:
        _usage_cache[key] = (st.st_mtime, st.st_size, recs)
    return recs


def blocks_from_records(recs):
    """Group sorted (epoch, out, model) records into 5h blocks; return
    [(start, end, output_total), ...]."""
    blocks = []
    start = end = None
    total = 0
    for ep, out, _ in recs:
        if start is None or ep >= end:
            if start is not None:
                blocks.append((start, end, total))
            start = ep - (ep % 3600)
            end = start + BLOCK_HOURS * 3600
            total = 0
        total += out
    if start is not None:
        blocks.append((start, end, total))
    return blocks


def compute_baseline(root):
    """Scan ALL history once (background thread) for the Usage-Monitor-style
    baseline: max and P90 of output tokens per 5h block. Cached in .state.json
    keyed on the newest transcript mtime, and ratcheted up over time."""
    recs = []
    for f in projects_dir(root).rglob("*.jsonl"):
        try:
            recs.extend(file_usage_records(f))
        except OSError:
            continue
    recs.sort(key=lambda r: r[0])
    totals = sorted(b[2] for b in blocks_from_records(recs) if b[2] > 0)
    if not totals:
        return
    p90 = totals[max(0, int(len(totals) * 0.9) - 1)]
    prev = state_read()
    state_write({"baseline_max": max(totals[-1], prev.get("baseline_max", 0)),
                 "baseline_p90": max(p90, prev.get("baseline_p90", 0)),
                 "baseline_blocks": len(totals),
                 "baseline_at": time.time()})


def usage_summary(root):
    now = time.time()
    cutoff = now - 8 * 86400
    recs = []
    pdir = projects_dir(root)
    for f in pdir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        recs.extend(r for r in file_usage_records(f) if r[0] >= cutoff)
    recs.sort(key=lambda r: r[0])

    # reconstruct 5h blocks over the last 8 days; keep the one containing `now`
    block_start = block_end = None
    for ep, _, _ in recs:
        if block_start is None or ep >= block_end:
            block_start = ep - (ep % 3600)          # floor to the hour
            block_end = block_start + BLOCK_HOURS * 3600
    block = None
    if block_start is not None and now < block_end:
        brecs = [r for r in recs if block_start <= r[0] < block_end]
        bymodel = {}
        for _, out, model in brecs:
            bymodel[model] = bymodel.get(model, 0) + out
        block = {"start": block_start, "end": block_end,
                 "output": sum(r[1] for r in brecs), "requests": len(brecs),
                 "by_model": bymodel}

    midnight = datetime.now().replace(hour=0, minute=0, second=0,
                                      microsecond=0).timestamp()
    today = [r for r in recs if r[0] >= midnight]
    week = [r for r in recs if r[0] >= now - 7 * 86400]
    wmodel = {}
    for _, out, model in week:
        wmodel[model] = wmodel.get(model, 0) + out
    hourly = [0] * 24                                # last 24h sparkline
    for ep, out, _ in recs:
        if ep >= now - 86400:
            # min(): a live session can write a record stamped after `now`
            hourly[min(23, int((ep - (now - 86400)) // 3600))] += out
    st = state_read()
    return {
        "block": block,
        "today": {"output": sum(r[1] for r in today), "requests": len(today)},
        "week": {"output": sum(r[1] for r in week), "requests": len(week)},
        "week_by_model": wmodel,
        "hourly": hourly,
        "baseline": {"max": st.get("baseline_max"), "p90": st.get("baseline_p90"),
                     "blocks": st.get("baseline_blocks")} if st.get("baseline_max") else None,
        "generated": now,
    }


def usage_history(root, days=90):
    """Output tokens per local day over the last `days` days, with the
    split by project and by model, for the heatmap. Reuses the per-file
    record cache (the baseline scan has usually warmed it already)."""
    days = max(7, min(366, int(days)))
    now = time.time()
    cutoff = now - days * 86400
    pdir = projects_dir(root)
    per_day, per_proj, per_model = {}, {}, {}
    for f in pdir.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            recs = file_usage_records(f)
        except OSError:
            continue
        slug = f.relative_to(pdir).parts[0]      # subagent files count for their project
        for ep, out, model in recs:
            if ep < cutoff:
                continue
            d = datetime.fromtimestamp(ep).strftime("%Y-%m-%d")      # local day
            day = per_day.setdefault(d, {"output": 0, "requests": 0})
            day["output"] += out
            day["requests"] += 1
            per_proj[slug] = per_proj.get(slug, 0) + out
            per_model[model] = per_model.get(model, 0) + out
    return {"days": per_day, "since": cutoff, "generated": now, "span": days,
            "projects": sorted(({"slug": k, "output": v} for k, v in per_proj.items()),
                               key=lambda x: -x["output"])[:15],
            "by_model": per_model}


# ---------------------------------------------------------------- viz inbox
#
# A watched folder Claude Code sessions can write into to "show" you output.
# Lives in this tool's own directory (never inside ~/.claude); a bundled app
# keeps it in its private data dir, since an update replaces the bundle.

VIZ_DIR = (APP_DIR if BUNDLED else HERE) / "viz"
VIZ_TYPES = {".html": "text/html", ".htm": "text/html", ".svg": "image/svg+xml",
             ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf",
             ".md": "text/plain", ".txt": "text/plain", ".json": "text/plain",
             ".csv": "text/plain"}


FS_TEXT = {".r", ".py", ".jl", ".do", ".tex", ".bib", ".qmd", ".rmd", ".yml",
           ".yaml", ".toml", ".js", ".ts", ".sh", ".zsh", ".log", ".sql",
           ".org", ".ini", ".cfg", ".gitignore", ".env"}
SERVER_PORT = 3456  # overwritten in main()


def safe_home_path(raw):
    """Resolve a filesystem path, refusing anything outside $HOME."""
    home = Path.home().resolve()
    p = Path(raw or str(home)).expanduser().resolve()
    if p != home and home not in p.parents:
        raise ValueError("path outside home directory")
    return p


# Never serve credential-shaped files, even when their extension is otherwise
# previewable (.json/.yml/.toml/…). Deliberately over-broad: a blocked
# "tokens_analysis.json" is a smaller cost than a leaked API key.
SENSITIVE_SUBSTRINGS = ("credential", "secret", "password", "passwd", "apikey",
                        "api_key", "private_key", "privatekey", "token",
                        "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
SENSITIVE_EXACT = (".netrc", ".npmrc", ".pypirc", ".git-credentials",
                   ".htpasswd", "hosts.yml", "hosts.yaml", "auth.json",
                   "credentials", ".env")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks",
                      ".asc", ".gpg", ".kdbx")


def is_sensitive(path):
    """True if this filename looks like it holds secrets."""
    name = Path(path).name.lower()
    if name in SENSITIVE_EXACT or name.startswith(".env"):
        return True
    if name.endswith(SENSITIVE_SUFFIXES):
        return True
    return any(s in name for s in SENSITIVE_SUBSTRINGS)


# ---------------------------------------------------------------- graph fit
# graphify's graph.html docks a fixed 280px sidebar (search + node info +
# community legend) next to a flex:1 canvas. That is fine full-screen, but the
# viz pane here is ~320px wide by default, so the sidebar swallows the panel
# and the graph itself becomes a sliver. We inject a responsive override on the
# way out: below ~900px the sidebar becomes an off-canvas drawer behind a ☰
# button and the canvas gets the whole pane. Injected at serve time rather than
# patched into the file so it survives `graphify` regenerating/upgrading.
#
# The stylesheet must land in <head>: vis-network sizes its camera to the
# container it is built in, so the layout has to be wide *before* the inline
# init script runs, or the graph opens absurdly zoomed in.
GRAPH_FIT_CSS = b"""
<style id="cdl-graph-fit">
  #cdl-drawer-btn { display: none; position: fixed; top: 8px; right: 8px;
                    z-index: 30; align-items: center; justify-content: center;
                    width: 30px; height: 30px; padding: 0;
                    border: 1px solid #3a3a5e; border-radius: 7px;
                    background: rgba(26,26,46,.92); color: #e0e0e0;
                    font-size: 14px; line-height: 1; cursor: pointer;
                    transition: right .18s ease; }
  #cdl-drawer-btn:hover { border-color: #4E79A7; }
  @media (max-width: 900px) {
    #sidebar { position: fixed; top: 0; right: 0; bottom: 0;
               width: min(280px, 84vw); z-index: 20;
               transform: translateX(101%); transition: transform .18s ease;
               box-shadow: -10px 0 26px rgba(0,0,0,.6); }
    body.cdl-drawer #sidebar { transform: translateX(0); }
    #graph { width: 100%; }
    #cdl-drawer-btn { display: flex; }
    body.cdl-drawer #cdl-drawer-btn { right: calc(min(280px, 84vw) + 8px); }
  }
</style>
"""

GRAPH_FIT_JS = b"""
<script>
(function () {
  var b = document.createElement('button');
  b.id = 'cdl-drawer-btn';
  b.type = 'button';
  b.textContent = '\\u2630';
  b.title = 'show / hide search, node info and legend';
  b.addEventListener('click', function () {
    var open = document.body.classList.toggle('cdl-drawer');
    b.textContent = open ? '\\u2715' : '\\u2630';
    // the canvas width changes under vis-network, which only re-measures on
    // a window resize; fire one once the drawer transition has finished
    setTimeout(function () {
      window.dispatchEvent(new Event('resize'));
    }, 220);
  });
  document.body.appendChild(b);
})();
</script>
"""


def fit_graph_html(body):
    """Make a graphify graph.html usable inside a narrow preview pane."""
    if b'id="sidebar"' not in body or b'id="graph"' not in body:
        return body
    if b"Search nodes" not in body or b"cdl-graph-fit" in body:
        return body
    if b"</head>" in body:
        body = body.replace(b"</head>", GRAPH_FIT_CSS + b"</head>", 1)
    else:
        body = GRAPH_FIT_CSS + body
    if b"</body>" in body:
        return body.replace(b"</body>", GRAPH_FIT_JS + b"</body>", 1)
    return body + GRAPH_FIT_JS


def viz_list(dir_override=None):
    """List previews, retrying one interrupted scan without duplicating entries."""
    d = safe_home_path(dir_override) if dir_override else VIZ_DIR
    if not d.is_dir():
        return [], d        # a watched folder that was deleted: empty, not a crash
    for attempt in range(2):
        try:
            return _scan_viz(d), d
        except InterruptedError:
            if attempt:
                raise


def _scan_viz(d):
    """Take one scan; files removed during the scan are harmless."""
    out = []
    for f in d.iterdir():
        try:
            if f.is_file() and f.suffix.lower() in VIZ_TYPES:
                st = f.stat()
                out.append({"name": f.name, "size": st.st_size,
                            "mtime": st.st_mtime,
                            "kind": VIZ_TYPES[f.suffix.lower()]})
        except FileNotFoundError:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:50]


def fs_listing(raw_path):
    d = safe_home_path(raw_path)
    if not d.is_dir():
        raise FileNotFoundError(raw_path)
    dirs, files = [], []
    for f in sorted(d.iterdir(), key=lambda p: p.name.lower()):
        if f.name.startswith(".") and f.name not in (".claude",):
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if f.is_dir():
            dirs.append({"name": f.name, "dir": True})
        else:
            ext = f.suffix.lower()
            sens = is_sensitive(f.name)
            files.append({"name": f.name, "dir": False, "size": st.st_size,
                          "mtime": st.st_mtime, "sensitive": sens,
                          "viewable": (not sens)
                                      and (ext in VIZ_TYPES or ext in FS_TEXT)})
    home = str(Path.home().resolve())
    return {"path": str(d), "home": home,
            "parent": str(d.parent) if str(d) != home else None,
            "entries": (dirs + files)[:600]}


# ---------------------------------------------------------------- plan / checklist
#
# The plan pane reads a markdown checklist out of the selected project and
# renders it with live checkboxes. Ticking one rewrites the `- [ ]` marker in
# the file itself, so the plan is a shared artefact: Claude Code writes it,
# you tick it, the next Claude Code session reads the ticks back.
#
# Discovery order (first hit wins, the rest stay selectable in the UI):
#   1. .claude/plan.md                 — the live plan the pane owns
#   2. quality_reports/plans/*.md      — newest first (research-workflow layout)
#   3. PLAN.md / TODO.md / TASKS.md / ROADMAP.md at the project root
#   4. docs/plan.md

PLAN_MAX_BYTES = 512 * 1024
PLAN_ROOT_NAMES = ("PLAN.md", "TODO.md", "TASKS.md", "ROADMAP.md")
PLAN_LIVE = (".claude", "plan.md")

# "- [ ] text", "* [x] text", "1. [~] text" — indentation preserved
PLAN_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+\[([ xX~/\-])\]\s?(.*)$")
PLAN_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")

PLAN_TEMPLATE = """# Plan

<!-- Ember reads this file into its PLAN pane.
     Keep one task per line as a markdown checkbox; tick them off as you go. -->

Status: DRAFT

## Steps

- [ ] First step
- [ ] Second step
"""


def plan_candidates(cwd):
    """Ordered, de-duplicated list of plan files for a project directory."""
    try:
        root = safe_home_path(cwd)
    except (ValueError, OSError):
        return []
    if not root.is_dir():
        return []
    out = []

    def add(f):
        try:
            if f.is_file() and f.stat().st_size <= PLAN_MAX_BYTES and f not in out:
                out.append(f)
        except OSError:
            pass

    add(root.joinpath(*PLAN_LIVE))
    plans = root / "quality_reports" / "plans"
    if plans.is_dir():
        try:
            md = [p for p in plans.glob("*.md") if p.is_file()]
            md.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            md = []
        for f in md[:12]:
            add(f)
    for n in PLAN_ROOT_NAMES:
        add(root / n)
    add(root / "docs" / "plan.md")
    return out


def parse_plan_text(text):
    """Markdown -> a flat list of headings and checkbox items."""
    items, done, total = [], 0, 0
    in_comment = in_fence = False
    for i, raw in enumerate(text.splitlines()):
        stripped = raw.strip()
        if in_comment:
            in_comment = "-->" not in stripped
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("<!--"):
            in_comment = "-->" not in stripped
            continue
        m = PLAN_ITEM_RE.match(raw)
        if m:
            mark = m.group(3)
            state = ("done" if mark in "xX"
                     else "doing" if mark in "~/" else "open")
            total += 1
            if state == "done":
                done += 1
            items.append({"kind": "task", "line": i, "state": state,
                          "depth": min(len(m.group(1)) // 2, 4),
                          "text": truncate(m.group(4).strip(), 400)})
            continue
        h = PLAN_HEAD_RE.match(raw)
        if h:
            items.append({"kind": "head", "line": i,
                          "level": len(h.group(1)),
                          "text": truncate(h.group(2).strip(), 200)})
            continue
        if stripped and not stripped.startswith("|") and len(items) < 400:
            # keep a little prose for context (status lines, one-line rationale)
            items.append({"kind": "text", "line": i,
                          "text": truncate(stripped, 300)})
    return items[:800], done, total


def plan_entry(f, root):
    try:
        rel = str(f.relative_to(root))
    except ValueError:
        rel = f.name
    return {"path": str(f), "rel": rel, "name": f.name,
            "mtime": f.stat().st_mtime}


def plan_read(cwd, which=None):
    """The plan pane's payload: the chosen file, its items, and the alternatives."""
    cands = plan_candidates(cwd)
    root = safe_home_path(cwd)
    out = {"dir": str(root), "candidates": [plan_entry(f, root) for f in cands],
           "file": None, "items": [], "done": 0, "total": 0}
    if not cands:
        return out
    chosen = cands[0]
    if which:
        w = safe_home_path(which)
        if w in cands:
            chosen = w
    try:
        text = chosen.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        out["error"] = str(e)
        return out
    items, done, total = parse_plan_text(text)
    out.update(plan_entry(chosen, root))
    out["file"] = str(chosen)
    out["items"], out["done"], out["total"] = items, done, total
    return out


def plan_toggle(cwd, path, line, expect, state):
    """Flip one checkbox in place. Refuses anything that is not a discovered
    plan file, and refuses if the line moved or its text changed under us."""
    f = safe_home_path(path)
    if f not in plan_candidates(cwd):
        raise ValueError("not a plan file for this project")
    raw = f.read_bytes()
    if len(raw) > PLAN_MAX_BYTES:
        raise ValueError("plan file too large")
    lines = raw.decode("utf-8", errors="replace").splitlines(keepends=True)
    if not isinstance(line, int) or not 0 <= line < len(lines):
        raise ValueError("line out of range")
    body = lines[line]
    eol = ""
    while body.endswith(("\n", "\r")):
        eol = body[-1] + eol
        body = body[:-1]
    m = PLAN_ITEM_RE.match(body)
    if not m:
        raise ValueError("line is not a checkbox")
    if expect is not None and m.group(4).strip() != expect:
        raise ValueError("plan changed on disk — refresh and try again")
    mark = {"done": "x", "doing": "~", "open": " "}.get(state)
    if mark is None:
        raise ValueError("bad state")
    lines[line] = f"{m.group(1)}{m.group(2)} [{mark}] {m.group(4)}" + eol
    tmp = f.with_name(f.name + ".cdl-tmp")
    tmp.write_bytes("".join(lines).encode("utf-8"))  # bytes: keep EOLs as-is on Windows
    os.replace(tmp, f)
    return plan_read(cwd, str(f))


def plan_create(cwd):
    """Create .claude/plan.md so the pane has somewhere to live."""
    root = safe_home_path(cwd)
    if not root.is_dir():
        raise FileNotFoundError(cwd)
    f = root.joinpath(*PLAN_LIVE)
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(PLAN_TEMPLATE, encoding="utf-8")
    return plan_read(cwd, str(f))


# ---------------------------------------------------------------- practice project
# A hands-on tutorial for people coming from the Claude app: a folder with a
# small made-up dataset and a checklist in the Plan pane. Steps Ember can see on
# disk tick themselves; the others the user ticks, which teaches the pane.
PRACTICE_MARK = PurePosixPath(".claude", "ember-practice.json")
PRACTICE_STEPS = (
    (None, "Tick this box by clicking it: you and Claude share this checklist"),
    ("asked", "In the terminal below, ask Claude: what is in this folder?"),
    ("chart", "Ask Claude: make a chart of cups sold per month. It appears in the Viz pane"),
    ("comment", "In Viz, click 💬 Comment, click the chart, write a change, then Send to Claude"),
    (None, "In the sidebar, open this conversation and look at each step Claude took"),
    (None, "Look at the Token use pane: how much of your 5-hour allowance this used"),
)
PRACTICE_CSV = """month,espresso,latte,tea
Jan,412,388,150
Feb,398,371,162
Mar,431,402,140
Apr,455,436,121
May,470,451,98
Jun,502,480,77
Jul,540,515,64
Aug,533,498,70
Sep,489,470,101
Oct,451,433,133
Nov,420,401,158
Dec,465,477,190
"""
PRACTICE_README = """# Ember practice

Made-up data for learning Ember: cups sold per month in an imaginary café
(`coffee.csv`). Nothing here is real, and you can delete this folder at any time.

Follow the checklist in Ember's Plan pane. Claude works on the files in this
folder; it asks before it runs a command or changes a file.
"""
PRACTICE_IMAGES = (".png", ".svg", ".jpg", ".jpeg", ".html", ".pdf")


def practice_dir():
    return Path.home() / "Ember-practice"


def practice_create():
    """Write the practice folder; never overwrite a file the user changed."""
    d = practice_dir()
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    plan = "# Ember practice\n\n<!-- Ember ticks some of these for you when it sees them happen. -->\n\n"
    plan += "".join(f"- [ ] {text}\n" for _, text in PRACTICE_STEPS)
    for name, body in (("coffee.csv", PRACTICE_CSV), ("README.md", PRACTICE_README),
                       (str(PurePosixPath(*PLAN_LIVE)), plan),
                       (str(PRACTICE_MARK), json.dumps({"created": time.time()}))):
        f = d / name
        if not f.exists():
            f.write_text(body, encoding="utf-8")
    return practice_sync(None)


def _newer_files(folders, since, pick):
    for folder in folders:
        try:
            for f in folder.iterdir():
                if pick(f) and f.stat().st_mtime >= since:
                    return True
        except OSError:
            continue
    return False


def practice_sync(root):
    """Tick the steps that already happened. Idempotent; returns the state."""
    d = practice_dir()
    try:
        created = json.loads((d / PRACTICE_MARK).read_text(encoding="utf-8"))["created"]
    except (OSError, ValueError, KeyError, TypeError):
        return {"cwd": None, "done": []}
    done = []
    pdir = project_dir_for_cwd(root, d) if root else None
    if pdir and any(pdir.glob("*.jsonl")):
        done.append("asked")
    figure = lambda f: f.suffix.lower() in PRACTICE_IMAGES and not f.name.startswith(".")
    if _newer_files((VIZ_DIR, d), created, figure):
        done.append("chart")
    if _newer_files((VIZ_DIR / ".review", d / ".review"), created,
                    lambda f: f.suffix == ".json"):
        done.append("comment")
    f = d.joinpath(*PLAN_LIVE)
    try:
        text = f.read_text(encoding="utf-8")
        new = text
        for key, step in PRACTICE_STEPS:
            if key in done:
                new = new.replace(f"- [ ] {step}", f"- [x] {step}")
        if new != text:
            f.write_text(new, encoding="utf-8")
    except OSError:
        pass
    return {"cwd": str(d), "done": done}


# ---------------------------------------------------------------- figure review
#
# Spatial comments on a rendered figure, after paulgp/exhibit-review: click a
# point or drag a region on an image in the Viz pane, type what should change.
# The review lives next to the figure as plain JSON so a Claude Code session
# can read it, regenerate the figure from its script, and mark comments
# resolved — the figure itself is never written.
#
#   <dir>/wealth.png
#   <dir>/.review/wealth.png.json   {schema_version, figure{file,content_hash},
#                                    revision, updated_at, comments[...]}
#
# Coordinates are fractions of the image (0–1, origin top-left), so they
# survive a re-render at a different size. On a PDF each comment also has
# `page` (1-based) and the fractions are of that page. content_hash is the sha256 of the
# image bytes when the review was saved: a mismatch means the figure was
# regenerated since, and the UI says so.

REVIEW_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf"}
REVIEW_MAX_COMMENTS = 200
REVIEW_STATUSES = ("open", "resolved", "wontfix")


def review_paths(raw):
    img = safe_home_path(raw)
    if img.suffix.lower() not in REVIEW_TYPES or is_sensitive(img.name):
        raise ValueError("not a reviewable image or PDF")
    if not img.is_file():
        raise FileNotFoundError(raw)
    return img, img.parent / ".review" / (img.name + ".json")


def _sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def review_read(raw):
    img, f = review_paths(raw)
    cur = _sha256(img)
    try:
        doc = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {"revision": 0, "comments": [], "figure": {}}
    saved = (doc.get("figure") or {}).get("content_hash")
    return {"image": str(img), "file": str(f), "revision": doc.get("revision", 0),
            "comments": doc.get("comments") or [], "content_hash": cur,
            "stale": bool(saved and saved != cur and doc.get("comments"))}


def _unit(v):
    if not isinstance(v, (int, float)) or v != v:        # rejects NaN
        raise ValueError("coordinate must be a number")
    return round(min(1.0, max(0.0, float(v))), 5)


def review_clean(c, n):
    kind = c.get("type")
    if kind not in ("point", "region"):
        raise ValueError("comment type must be point or region")
    out = {"id": str(c.get("id") or uuid.uuid4())[:64], "n": n, "type": kind,
           "x": _unit(c.get("x")), "y": _unit(c.get("y")),
           "text": str(c.get("text") or "")[:4000],
           "status": c.get("status") if c.get("status") in REVIEW_STATUSES else "open"}
    if kind == "region":
        out["w"] = _unit(c.get("w"))
        out["h"] = _unit(c.get("h"))
    page = c.get("page")
    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= 100000:
            raise ValueError("page must be a page number")
        out["page"] = page
    return out


def review_write(raw, comments, expect_revision):
    """Replace the review. Refuses when someone (e.g. Claude) saved a newer
    revision since the page loaded it, so neither side silently loses edits."""
    img, f = review_paths(raw)
    if not isinstance(comments, list) or len(comments) > REVIEW_MAX_COMMENTS:
        raise ValueError("comments must be a list of at most %d" % REVIEW_MAX_COMMENTS)
    cur = review_read(raw)
    if expect_revision is not None and expect_revision != cur["revision"]:
        raise ValueError("review changed on disk — reload the figure and try again")
    clean = [review_clean(c, i + 1) for i, c in enumerate(comments)
             if isinstance(c, dict)]
    doc = {"schema_version": 1,
           "figure": {"file": img.name, "content_hash": cur["content_hash"]},
           "revision": cur["revision"] + 1,
           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "coordinates": ("fractions of the page given by `page` (1-based), origin top-left"
                           if img.suffix.lower() == ".pdf"
                           else "fractions of the image, origin top-left"),
           "comments": clean}
    f.parent.mkdir(exist_ok=True)
    tmp = f.with_name(f.name + ".cdl-tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, f)
    return review_read(raw)


# ---------------------------------------------------------------- config inventory
#
# What is actually installed in ~/.claude — agents, skills, commands, rules,
# hooks, plugins, MCP servers — and roughly what each costs you in context.
#
# The distinction that matters: CLAUDE.md and rules/ are pasted into EVERY
# request, and so are the one-line descriptions of every agent, skill and
# command. Their bodies are not: those load only when dispatched or invoked.
# So a 40 kB skill is nearly free until you use it, while a 40 kB rules file
# is a tax on every single turn. The pane separates the two.
#
# Metadata only — never file contents, and never MCP server args or env, which
# routinely hold API keys.

TOKENS_PER_BYTE = 0.25          # ~4 chars per token; good enough to rank by
CONFIG_MAX_ITEMS = 400


def read_frontmatter(path, limit=8192):
    """name/description out of a YAML front-matter block, without a YAML parser."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(limit)
    except OSError:
        return out
    if not head.startswith("---"):
        return out
    end = head.find("\n---", 3)
    if end == -1:
        return out
    key = None
    for line in head[3:end].splitlines():
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            key = m.group(1).lower()
            val = m.group(2).strip().strip("'\"")
            if key in ("name", "description", "model", "argument-hint"):
                out[key] = val
            continue
        # folded/continued value (description: >- style)
        if key in out and line.startswith((" ", "\t")):
            out[key] = (out[key] + " " + line.strip()).strip()
    return out


def first_heading(path, limit=4096):
    """A markdown file's first `# heading`, used when it has no front matter."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh.read(limit).splitlines():
                if line.startswith("#"):
                    return line.lstrip("#").strip()
    except OSError:
        pass
    return ""


def est_tokens(n_bytes):
    return int(n_bytes * TOKENS_PER_BYTE)


def config_item(path, always_loaded, desc=None, extra=None):
    st = path.stat()
    d = desc if desc is not None else ""
    item = {"name": path.stem if path.name != "SKILL.md" else path.parent.name,
            "path": str(path), "bytes": st.st_size, "mtime": st.st_mtime,
            "description": truncate(d, 300),
            # always-loaded components cost their whole body every turn;
            # on-demand ones only cost their description until invoked
            "tokens": est_tokens(st.st_size),
            "resident": est_tokens(st.st_size if always_loaded else len(d)),
            "always": always_loaded}
    item.update(extra or {})
    return item


def _md_group(root, sub, always, glob="*.md", nested=None):
    d = root / sub
    items = []
    if not d.is_dir():
        return items
    paths = sorted(d.glob(nested) if nested else d.glob(glob))
    for f in paths[:CONFIG_MAX_ITEMS]:
        if not f.is_file():
            continue
        fm = read_frontmatter(f)
        # nested files keep their subfolder so pipeline/workflow ≠ workflow
        rel = f.relative_to(d).with_suffix("")
        items.append(config_item(f, always,
                                 fm.get("description") or first_heading(f),
                                 {"name": rel.as_posix()} if len(rel.parts) > 1
                                 and f.name != "SKILL.md" else None))
    return items


# A file in hooks/ is not necessarily a Claude Code hook. Some are libraries
# other hooks call, some are git hooks. Only flag the ones that read a hook
# payload on stdin or say which event they want — the rest are just files.
# Claude Code's documented events (code.claude.com/docs/en/hooks, 2026-09).
HOOK_EVENTS = (
    "SessionStart", "Setup", "UserPromptSubmit", "UserPromptExpansion",
    "PreToolUse", "PermissionRequest", "PermissionDenied", "PostToolUse",
    "PostToolUseFailure", "PostToolBatch", "Notification", "MessageDisplay",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "Stop",
    "StopFailure", "TeammateIdle", "InstructionsLoaded", "ConfigChange",
    "CwdChanged", "DirectoryAdded", "FileChanged", "WorktreeCreate",
    "WorktreeRemove", "PreCompact", "PostCompact", "PreModelSwitch",
    "PostModelSwitch", "Elicitation", "ElicitationResult", "SessionEnd")


def declared_hook_event(path, limit=4096):
    """`Hook Event: PreCompact` in a file's own header comment, if present."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(limit)
    except OSError:
        return None
    m = re.search(r"hook\s+event\s*:\s*([A-Za-z]+)", head, re.I)
    if m:
        for ev in HOOK_EVENTS:
            if m.group(1).lower() == ev.lower():
                return ev
    return None


def looks_like_a_hook(path, limit=4096):
    """Does this file consume a hook payload, or name its own event?"""
    if declared_hook_event(path):
        return True
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(limit)
    except OSError:
        return False
    return ("tool_input" in head or "hook_event_name" in head
            or "tool_name" in head)


HOOK_SCRIPT_SUFFIXES = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".rb", ".pl")


def hook_script_name(argv):
    """The script a hook command runs — `python3 /a/b/pre-compact.py` is the
    hook `pre-compact.py`, not `python3`."""
    for a in argv:
        if a.startswith("-"):
            continue
        if a.lower().endswith(HOOK_SCRIPT_SUFFIXES):
            return Path(a).name
    return Path(argv[0]).name if argv else "?"


def hook_registrations(settings):
    """event -> [command basenames], from a settings.json hooks block."""
    out = {}
    for event, entries in (settings.get("hooks") or {}).items():
        cmds = []
        for entry in entries if isinstance(entries, list) else []:
            for h in (entry.get("hooks") or []):
                c = str(h.get("command", "")).strip()
                if not c:
                    # http / prompt / agent / mcp_tool hooks run no command
                    kind = str(h.get("type") or "?")
                    detail = h.get("url") or h.get("tool") or h.get("server") or ""
                    if kind == "http" and detail:
                        detail = urllib.parse.urlparse(str(detail)).netloc or detail
                    cmds.append(f"{kind}: {detail}" if detail else kind)
                    continue
                # commands are quoted shell strings; paths contain spaces
                try:
                    argv = shlex.split(c)
                except ValueError:
                    argv = [c]
                cmds.append(hook_script_name(argv))
        out[event] = cmds
    return out


def mcp_servers(root, scope=None):
    """name -> transport, from the places Claude Code actually keeps them.

    Global servers live in ~/.claude.json's `mcpServers`; project ones under
    `projects[<cwd>].mcpServers` in the same file, or in a project .mcp.json.
    Names and transports only — the entries hold args and env, i.e. API keys."""
    out = {}

    def take(d):
        for name, spec in (d or {}).items():
            if isinstance(spec, dict):
                out[name] = str(spec.get("type") or "stdio")

    try:
        conf = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        conf = {}
    if scope is None:                       # the global ~/.claude inventory
        take(conf.get("mcpServers"))
    else:                                   # a project's own .claude
        take(((conf.get("projects") or {}).get(str(scope)) or {}).get("mcpServers"))
        try:
            take(json.loads((Path(scope) / ".mcp.json").read_text()).get("mcpServers"))
        except (OSError, ValueError):
            pass
    return out


def config_inventory(root, scope=None):
    """One .claude directory's worth of installed components.

    `scope` is the project directory when this is a project-level .claude, and
    None for the global one — it decides which MCP servers apply."""
    root = Path(root)
    if not root.is_dir() and scope is None:
        return None
    # a project can have no .claude/ at all and still have MCP servers wired to
    # it in ~/.claude.json — that is worth a row, so keep going
    groups = []

    # --- always resident: the instruction files themselves
    memory = []
    for name in ("CLAUDE.md",):
        f = root / name
        if f.is_file():
            memory.append(config_item(f, True, "project/global instructions"))
    # rules/ is loaded recursively — a flat glob hid whole subfolders
    memory += _md_group(root, "rules", True, nested="**/*.md")
    if memory:
        groups.append({"key": "memory", "label": "Instructions (every turn)",
                       "always": True, "items": memory})

    # --- descriptions resident, bodies on demand
    for key, sub, label, glob, nested in (
            ("agents", "agents", "Agents", "*.md", None),
            ("skills", "skills", "Skills", None, "*/SKILL.md"),
            ("commands", "commands", "Commands", "*.md", None)):
        items = _md_group(root, sub, False, glob or "*.md", nested)
        if items:
            groups.append({"key": key, "label": label, "always": False,
                           "items": items})

    # --- hooks: files on disk, annotated with the events they are wired to
    settings = {}
    for name in ("settings.json", "settings.local.json"):
        try:
            loaded = json.loads((root / name).read_text())
        except (OSError, ValueError):
            continue
        # shallow-merge, but never let a file without a `hooks` key erase one
        for k, v in loaded.items():
            if k == "hooks" and isinstance(v, dict):
                settings.setdefault("hooks", {}).update(v)
            else:
                settings[k] = v
    regs = hook_registrations(settings)
    wired = {c: ev for ev, cmds in regs.items() for c in cmds}
    hooks = []
    hd = root / "hooks"
    if hd.is_dir():
        for f in sorted(hd.iterdir()):
            if not f.is_file() or f.name.startswith("."):
                continue
            event = wired.get(f.name)
            if event:
                desc, orphan = event, False
            elif looks_like_a_hook(f):
                want = declared_hook_event(f)
                desc = ("expects " + want + ", not registered" if want
                        else "hook-shaped, but no settings.json event points at it")
                orphan = True
            else:
                # a helper the other hooks call, or a git hook that happens to
                # live here — not something settings.json should point at
                desc, orphan = "helper script (not a Claude Code hook)", False
            hooks.append(config_item(f, False, desc,
                                     {"event": event, "orphan": orphan}))
    # hooks registered from elsewhere on the filesystem still deserve a row
    for ev, cmds in regs.items():
        for c in cmds:
            if not any(h["name"] == Path(c).stem for h in hooks):
                hooks.append({"name": Path(c).stem, "path": None, "bytes": 0,
                              "mtime": 0, "description": ev + " · registered from "
                              "outside " + str(hd),
                              "tokens": 0, "resident": 0, "always": False,
                              "event": ev, "orphan": False})
    if hooks:
        groups.append({"key": "hooks", "label": "Hooks", "always": False,
                       "items": hooks, "events": regs})

    # --- plugins and MCP servers: names only, never args or env
    ext = []
    for name, on in (settings.get("enabledPlugins") or {}).items():
        ext.append({"name": name, "path": None, "bytes": 0, "mtime": 0,
                    "description": "plugin · " + ("enabled" if on else "disabled"),
                    "tokens": 0, "resident": 0, "always": False})
    for name, kind in sorted(mcp_servers(root, scope).items()):
        ext.append({"name": name, "path": None, "bytes": 0, "mtime": 0,
                    "description": kind + " MCP server — its tool schemas are "
                                   "resident once connected",
                    "tokens": 0, "resident": 0, "always": False})
    if ext:
        groups.append({"key": "ext", "label": "Plugins & MCP", "always": False,
                       "items": ext})

    resident = sum(i["resident"] for g in groups for i in g["items"])
    ondemand = sum(i["tokens"] - i["resident"] for g in groups for i in g["items"])
    return {"root": str(root), "groups": groups,
            "resident": resident, "ondemand": ondemand,
            "count": sum(len(g["items"]) for g in groups)}


def config_view(cwd=None):
    """Global config, plus the project's own .claude when it has one."""
    out = {"user": config_inventory(CLAUDE_ROOT), "project": None}
    if cwd:
        try:
            p = safe_home_path(cwd) / ".claude"
        except (ValueError, OSError):
            p = None
        if p and p.resolve() != Path(CLAUDE_ROOT).resolve():
            inv = config_inventory(p, scope=p.parent)
            out["project"] = inv if inv and inv["count"] else None
    return out


# ---------------------------------------------------------------- optional add-ons
#
# addons.json (next to this file) lists Claude Code add-ons the dashboard can
# offer: graphify and /improve power features here; the rest are suggestions.
# This only DETECTS what is installed. Installing happens in a visible
# terminal tab, with commands taken from that file and shown to the user
# first — never from anything a request supplies.

ADDONS_FILE = HERE / "addons.json"
ADDON_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,40}")


def installed_plugins(root=None):
    try:
        data = json.loads((Path(root or CLAUDE_ROOT) / "plugins" /
                           "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    return set(plugins) if isinstance(plugins, dict) else set()


def addon_installed(check, root=None, plugins=None):
    root = Path(root or CLAUDE_ROOT)
    kind, name = (check or {}).get("kind"), str((check or {}).get("name") or "")
    if not name or "/" in name or "\\" in name or ".." in name:
        return False
    if kind == "skill":
        return (root / "skills" / name / "SKILL.md").is_file()
    if kind == "command":
        return (root / "commands" / (name + ".md")).is_file()
    if kind == "plugin":
        return name in (installed_plugins(root) if plugins is None else plugins)
    if kind in ("statusline", "hook"):      # our own tees, found in settings.json
        try:
            st = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if kind == "statusline":
            return name in str((st.get("statusLine") or {}).get("command", ""))
        # `arg` tells our tees apart: the event tee and the guard share a file
        arg = (check or {}).get("arg")
        return any(name in cmd and (not arg or cmd.split()[-1:] == [arg])
                   for entries in (st.get("hooks") or {}).values()
                   if isinstance(entries, list)
                   for e in entries for h in (e.get("hooks") or [])
                   for cmd in [str(h.get("command", ""))])
    if kind == "mcp":           # `claude mcp add` writes ~/.claude.json (next to ~/.claude)
        try:
            conf = json.loads((root.parent / ".claude.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        scopes = [conf.get("mcpServers")] + [p.get("mcpServers") for p in
                                             (conf.get("projects") or {}).values()
                                             if isinstance(p, dict)]
        return any(isinstance(m, dict) and name in m for m in scopes)
    return False


def program_on_path(name):
    if name == "claude":
        return bool(find_claude())
    return bool(shutil.which(name, path=login_path()))


def host_os():
    return ("windows" if os.name == "nt"
            else "mac" if sys.platform == "darwin" else "linux")


def hooks_cmd(exe=None, here=None, frozen=None, windows=None):
    """The shell words that run tools/devtools_hooks.py with the interpreter
    running Ember (a frozen Ember.exe runs it itself: `Ember.exe
    devtools_hooks.py …`), so no system Python is needed. Quoted for the
    install terminal's shell."""
    exe = exe or sys.executable
    here = str(here or HERE)       # a string: a host Path would flip the separators
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    windows = os.name == "nt" if windows is None else windows
    if "/AppTranslocation/" in here.replace("\\", "/"):
        # macOS runs a downloaded app from a random read-only copy until it
        # is moved: hooks pointing there would break on the next launch.
        # `false` stops the && chain without closing the terminal.
        return "echo Move Ember to the Applications folder, reopen it, then retry && false"
    if not frozen:
        # the target OS's path flavour, not the host's (tests build both)
        flavour = PureWindowsPath if windows else PurePosixPath
        argv = [exe, str(flavour(here, "tools", "devtools_hooks.py"))]
    elif windows:
        # Ember.exe is a windowed program: an interactive cmd would not wait
        # for it, so the && chain would run on (and lose its exit code)
        return 'start "" /wait ' + subprocess.list2cmdline([exe, "devtools_hooks.py"])
    else:
        argv = [exe, "devtools_hooks.py"]
    return (subprocess.list2cmdline(argv) if windows
            else " ".join(shlex.quote(a) for a in argv))


def addons_status(root=None, manifest=None):
    """Every add-on in addons.json with: installed?, missing prerequisites,
    and the install commands for this OS."""
    try:
        data = json.loads(Path(manifest or ADDONS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"platform": None, "addons": []}
    osname = host_os()
    plat = "windows" if osname == "windows" else "posix"
    plugins = installed_plugins(root)
    # {hooks} = how to run our own tees; {app} = this checkout (older lists)
    app = f'"{HERE}"' if " " in str(HERE) else str(HERE)
    subst = {"{hooks}": hooks_cmd(), "{app}": app}

    def fill(c):
        for k, v in subst.items():
            c = c.replace(k, v)
        return c
    out = []
    for a in data.get("addons", []):
        if not ADDON_ID_RE.fullmatch(str(a.get("id", ""))):
            continue
        cmds = [fill(str(c)) for c in (a.get("install") or {}).get(plat) or []]
        # ticking an installed add-on reinstalls it: a clean redo for when an
        # install was interrupted (app closed mid-way) or left it half-working
        redo = [fill(str(c))
                for c in (a.get("reinstall") or {}).get(plat) or []] or cmds
        out.append({"id": a["id"], "name": a.get("name", a["id"]),
                    "used_by_app": bool(a.get("used_by_app")),
                    "unlocks": a.get("unlocks", ""), "source": a.get("source", ""),
                    "installed": addon_installed(a.get("check"), root, plugins),
                    "missing_needs": [n for n in a.get("needs", [])
                                      if not program_on_path(n)],
                    "commands": [str(c) for c in cmds],
                    "reinstall": [str(c) for c in redo]})
    # winget is missing on older Windows 10 and many managed PCs: fall back to
    # each program's official installer (install["windows-nowinget"])
    variant = osname
    if osname == "windows" and not program_on_path("winget"):
        variant = "windows-nowinget"
    prereqs = {}
    for key, pr in (data.get("prerequisites") or {}).items():
        if not ADDON_ID_RE.fullmatch(str(key)):
            continue
        prereqs[key] = {"name": pr.get("name", key), "url": pr.get("url", ""),
                        "installed": program_on_path(key),
                        "needs": [n for n in pr.get("needs", [])],
                        "commands": [str(c) for c in
                                     (pr.get("install") or {}).get(variant)
                                     or (pr.get("install") or {}).get(osname) or []],
                        "note": (pr.get("note") or {}).get(osname, "")}
    return {"platform": plat, "os": osname, "addons": out,
            "prerequisites": prereqs}


# ---------------------------------------------------------------- improve reports
#
# The /improve retrospective (github.com/TerenceBristol/claude-improve) runs
# from a SessionEnd hook and drops a dated markdown report per project. The
# plan pane links the newest one; previews go through /api/fs/file.

IMPROVE_DIR = Path.home() / ".claude" / "improve-reports"


def improve_reports(slug, limit=8):
    d = IMPROVE_DIR / re.sub(r"[^A-Za-z0-9._-]", "-", slug or "")
    if not d.is_dir():
        return {"dir": str(d), "reports": []}
    try:
        files = [p for p in d.glob("*.md") if p.is_file()]
    except OSError:
        files = []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    now = time.time()
    out = []
    for f in files:
        st = f.stat()
        # a finished run that wrote nothing past the header is not a report
        if st.st_size < IMPROVE_MIN_REPORT and now - st.st_mtime > 600:
            continue
        out.append({"path": str(f), "name": f.name,
                    "mtime": st.st_mtime, "size": st.st_size})
        if len(out) >= limit:
            break
    return {"dir": str(d), "reports": out}


# The retrospective itself runs detached, so it survives the server exiting
# (that is the whole point: it fires when you quit the app).

IMPROVE_MIN_TRANSCRIPT = 20 * 1024      # don't analyse a three-message session
IMPROVE_MIN_INTERVAL = 3 * 3600         # at most one retrospective per project
IMPROVE_MIN_REPORT = 400                # a header and nothing else
IMPROVE_TIMEOUT = "1800"                # seconds; enforced by a watchdog

IMPROVE_PROMPT = """/improve

This run was started automatically by Ember when a Claude Code
session closed. There is no live conversation to review, so analyse the
transcript of the session that just ended instead:

  transcript: {transcript}
  project:    {cwd}

Skip the scope question — use "current conversation only" scope, reading that
transcript as the conversation. REPORT ONLY: do not modify CLAUDE.md, settings,
skills, rules, memory or learnings files; you have read-only tools on purpose.
Write the findings list to stdout as markdown, most important first, each with
the file it would change and the exact edit you would propose. If nothing in
this session is worth changing, say so in one line and stop.
"""


def improve_enabled():
    if os.environ.get("CDL_IMPROVE", "1") == "0":
        return False
    return bool(state_read().get("improve", True))


def project_dir_for_cwd(root, cwd):
    """Claude Code's transcript folder for `cwd`. Its naming rule (every
    non-alphanumeric → "-", long paths truncated + hashed) is undocumented
    and drifts between versions, so guess first, then look the cwd up in
    the sessions themselves — a lookup cannot drift."""
    pdir = projects_dir(root)
    for guess in (re.sub(r"[^A-Za-z0-9]", "-", str(cwd)), mangle_cwd(cwd)):
        if (pdir / guess).is_dir():
            return pdir / guess
    for p in list_projects(root):             # authoritative cwd per project
        if p["path"] == str(cwd):
            return pdir / p["slug"]
    return None


def mangle_cwd(cwd):
    """Folder name for OUR improve-reports/ (not Claude Code's rule — see
    project_dir_for_cwd for finding its transcripts)."""
    return re.sub(r"[/ .]", "-", str(cwd))


def improve_stamp_ok(slug):
    """Rate-limit: one retrospective per project per IMPROVE_MIN_INTERVAL."""
    stamp = IMPROVE_DIR / re.sub(r"[^A-Za-z0-9._-]", "-", slug) / ".last-run"
    try:
        return time.time() - stamp.stat().st_mtime >= IMPROVE_MIN_INTERVAL
    except OSError:
        return True                   # never run for this project


def improve_stamp_write(slug):
    stamp = IMPROVE_DIR / re.sub(r"[^A-Za-z0-9._-]", "-", slug) / ".last-run"
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()))
    except OSError:
        pass


def spawn_improve(cwd, transcript):
    """Kick off a /improve retrospective for a just-closed session, detached.

    Read-only by construction (Read/Grep/Glob only), rate-limited, and skipped
    entirely for short sessions. Never runs inside a retrospective's own
    session — CDL_IMPROVE_RUN stops the obvious recursion."""
    if not cwd or not improve_enabled() or os.environ.get("CDL_IMPROVE_RUN"):
        return None
    # without the command, `claude -p "/improve …"` would burn a request on
    # an unknown slash command every time a session closes
    if not (addon_installed({"kind": "command", "name": "improve"})
            or (Path(cwd) / ".claude" / "commands" / "improve.md").is_file()):
        return None
    claude = find_claude()
    if not claude:
        return None
    try:
        if not transcript or transcript.stat().st_size < IMPROVE_MIN_TRANSCRIPT:
            return None
    except OSError:
        return None
    slug = mangle_cwd(cwd)
    if not improve_stamp_ok(slug):
        return None
    d = IMPROVE_DIR / re.sub(r"[^A-Za-z0-9._-]", "-", slug)
    d.mkdir(parents=True, exist_ok=True)
    out = d / (time.strftime("%Y-%m-%d_%H%M") + ".md")
    prompt = IMPROVE_PROMPT.format(transcript=transcript, cwd=cwd)
    env = child_environment(extra={"CDL_IMPROVE_RUN": "1", "PATH": login_path()})
    header = (f"# Retrospective — {Path(cwd).name}\n\n"
              f"*{time.strftime('%Y-%m-%d %H:%M')} · session "
              f"`{transcript.stem}` · read-only run started by "
              f"Ember when the session closed.*\n\n---\n\n")
    try:
        fh = open(out, "w", encoding="utf-8")
        fh.write(header)
        fh.flush()
        run = [claude, "-p", prompt, "--allowedTools", "Read", "Grep", "Glob"]
        kw = {}
        if os.name == "nt":
            # no fork, no sh: detach so quitting the app does not take it down.
            # There is no watchdog here — `claude -p` exits on its own.
            kw["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
            argv = run
        else:
            # a tiny sh wrapper gives us a timeout without depending on
            # `timeout`, which macOS does not ship
            kw["start_new_session"] = True
            argv = ["/bin/sh", "-c",
                    '"$0" "$@" & p=$!; '
                    '(sleep ' + IMPROVE_TIMEOUT + '; kill $p 2>/dev/null) & '
                    'w=$!; wait $p; kill $w 2>/dev/null'] + run
        subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                         stdout=fh, stderr=subprocess.STDOUT, close_fds=True, **kw)
        fh.close()
        improve_stamp_write(slug)     # only burn the rate-limit window on success
        return str(out)
    except OSError:
        try:
            fh.close()
            out.unlink()
        except OSError:
            pass
        return None


# ---------------------------------------------------------------- embedded terminal
#
# Runs a real PTY (claude CLI or your shell) and streams it to the browser
# over Server-Sent Events; input comes back via POST. Localhost only.
#
# Offsets are ABSOLUTE: every byte the child has ever produced counts, and a
# client's position only ever moves forward. `buf` holds a bounded tail of that
# stream, so a position must be translated (`pos - discarded`) before it can
# index into it. Treating a position as a plain index into `buf` breaks the
# moment the front gets trimmed.

# ---------------------------------------------------------------- session-end card
#
# When a Claude terminal ends, the page shows what the session did: files
# changed since it started (git), tokens and cost, plan items ticked, and the
# retrospective. Snapshot at start, summary at exit, kept for the last few.

SUMMARIES = {}                   # term id -> summary dict
SUMMARIES_MAX = 20
SUMMARIES_LOCK = threading.Lock()


def git_out(cwd, *args):
    """stdout of a read-only git command in cwd, or None (no git, not a repo)."""
    git = shutil.which("git", path=login_path())
    if not git:
        return None
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW
    try:
        r = subprocess.run([git, "-C", str(cwd), *args], capture_output=True,
                           text=True, timeout=5, stdin=subprocess.DEVNULL, **kw)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def session_snapshot(cwd, resume=None):
    head = (git_out(cwd, "rev-parse", "HEAD") or "").strip() or None
    try:
        plan0 = plan_read(cwd)["done"]
    except Exception:
        plan0 = None
    return {"started": time.time(), "head": head, "plan_done": plan0, "resume": resume}


def session_transcript(cwd, since, resume=None):
    """The transcript this terminal wrote. A resumed session names its own;
    a new one is the newest file CREATED since the tab opened (so another
    session running in the same project is not mistaken for it)."""
    d = project_dir_for_cwd(CLAUDE_ROOT, cwd)
    if d is None:
        return None
    if resume:
        f = d / (resume + ".jsonl")
        return f if f.is_file() else None
    # ponytail: two NEW sessions started in one project within seconds of each
    # other can still swap; pass the session id to claude if that ever matters
    best = None
    for f in d.glob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        born = getattr(st, "st_birthtime", None)          # macOS, Windows
        if born is None:                                   # Linux: first record's time
            born = transcript_start(f)
        if born is not None and born >= since - 5 and (best is None or st.st_mtime > best[0]):
            best = (st.st_mtime, f)
    return best[1] if best else None


def transcript_start(f):
    """Timestamp of a transcript's first timestamped record, as epoch seconds."""
    try:
        with open(f, encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(50), fh):
                ts = (json.loads(line) or {}).get("timestamp")
                if ts:
                    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (OSError, ValueError, AttributeError):
        pass
    return None


def session_summary(cwd, snap, ended=None):
    ended = ended or time.time()
    out = {"cwd": cwd, "project": Path(cwd).name, "started": snap["started"],
           "ended": ended, "session_id": None, "title": None, "tokens": None,
           "cost_usd": None, "tools": 0, "git": None, "plan": None}
    f = session_transcript(cwd, snap["started"], snap.get("resume"))
    if f:
        try:
            d = parse_session(f)
            out.update(session_id=f.stem, title=d["title"], tokens=d["totals"],
                       tools=sum(d["tool_counts"].values()))
        except Exception:
            pass
        try:            # the Live limits tee saves the session's own cost
            st = json.loads((APP_DIR / "status" / (f.stem + ".json")).read_text(encoding="utf-8"))
            out["cost_usd"] = (st.get("cost") or {}).get("total_cost_usd")
        except (OSError, ValueError, AttributeError):
            pass
    if snap.get("head"):
        # everything since the start: commits made in the session + uncommitted work
        files = []
        for line in (git_out(cwd, "diff", "--numstat", snap["head"]) or "").splitlines()[:200]:
            parts = line.split("\t", 2)
            if len(parts) == 3:
                files.append({"path": parts[2], "added": parts[0], "removed": parts[1]})
        commits = (git_out(cwd, "log", "--format=%h %s", f"{snap['head']}..HEAD") or "").splitlines()
        out["git"] = {"files": files, "commits": commits[:50]}
    try:
        pl = plan_read(cwd)
        if pl.get("file"):
            out["plan"] = {"file": pl["file"], "done": pl["done"], "total": pl["total"],
                           "ticked": (pl["done"] - snap["plan_done"]
                                      if snap.get("plan_done") is not None else None),
                           "open": [i["text"] for i in pl["items"]
                                    if i["kind"] == "task" and i["state"] != "done"][:8]}
    except Exception:
        pass
    return out


def store_summary(tid, summary):
    with SUMMARIES_LOCK:
        SUMMARIES[tid] = summary
        while len(SUMMARIES) > SUMMARIES_MAX:
            del SUMMARIES[next(iter(SUMMARIES))]


def summary_with_improve(tid):
    with SUMMARIES_LOCK:
        s = SUMMARIES.get(tid)
    if s is None:
        return None
    reps = [r for r in improve_reports(mangle_cwd(s["cwd"]))["reports"]
            if r["mtime"] >= s["ended"] - 5]
    return dict(s, improve=reps[0] if reps else None)


def _fmt_int(n):
    return f"{n:,}" if isinstance(n, int) else "—"


def session_log_entry(s):
    """A session_logs/ entry in the project's format, from facts only."""
    day = time.strftime("%Y-%m-%d", time.localtime(s["started"]))
    mins = max(1, round((s["ended"] - s["started"]) / 60))
    tok = s.get("tokens") or {}
    lines = [f"## [{day}] Session — {s.get('title') or s['project']}",
             "Status: completed", "",
             f"Recorded by Ember from the terminal that closed at "
             f"{time.strftime('%H:%M', time.localtime(s['ended']))} ({mins} min"
             + (f", session `{s['session_id']}`" if s.get("session_id") else "") + ").", "",
             "### Changes", "| File | Added | Removed |", "|---|---|---|"]
    files = (s.get("git") or {}).get("files") or []
    lines += [f"| `{f['path']}` | {f['added']} | {f['removed']} |" for f in files] or \
             ["| (no file changes recorded) | | |"]
    commits = (s.get("git") or {}).get("commits") or []
    if commits:
        lines += ["", "### Commits"] + [f"- {c}" for c in commits]
    lines += ["", "### Usage",
              f"- Output tokens: {_fmt_int(tok.get('output_tokens'))}; "
              f"peak context: {_fmt_int(tok.get('peak_context'))}; tool calls: {s.get('tools', 0)}"]
    if s.get("cost_usd") is not None:
        lines.append(f"- Cost (Claude Code's own figure): ${s['cost_usd']:.2f}")
    pl = s.get("plan")
    if pl:
        gained = f" (+{pl['ticked']} this session)" if pl.get("ticked") else ""
        lines.append(f"- Plan: {pl['done']}/{pl['total']} done{gained}")
    lines += ["", "### Design Decisions", "| Decision | Rationale | Alternatives |",
              "|---|---|---|", "| | | |", "", "### LEARN entries", "", "### Next steps"]
    lines += [f"- {t}" for t in (pl or {}).get("open", [])] or ["- "]
    return "\n".join(lines) + "\n"


def append_session_log(tid):
    """Append the entry for terminal `tid` to <project>/session_logs/<day>.md.
    Only summaries this server made are accepted: the client names a terminal,
    never a path."""
    with SUMMARIES_LOCK:
        s = SUMMARIES.get(tid)
    if s is None:
        raise KeyError(tid)
    day = time.strftime("%Y-%m-%d", time.localtime(s["started"]))
    f = Path(s["cwd"]) / "session_logs" / f"{day}.md"
    f.parent.mkdir(exist_ok=True)
    prefix = "\n" if f.exists() and f.stat().st_size else ""
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(prefix + session_log_entry(s))
    return str(f)



# ---------------------------------------------------------------- session export
# The page renders a session into one self-contained HTML file; the server
# only saves it, always into ~/Downloads (the macOS app's WKWebView has no
# blob downloads). The page picks nothing but a file name.

EXPORT_MAX = 50 * 1024 * 1024


def save_export(name, html):
    if not isinstance(html, str) or not html or len(html) > EXPORT_MAX:
        raise ValueError("nothing to save, or the export is over 50 MB")
    # letters in any script (French titles keep their accents); never a separator
    stem = re.sub(r"[^\w .()-]+", "-", str(name or "session"))[:80].strip(" .-") or "session"
    d = Path.home() / "Downloads"
    d.mkdir(exist_ok=True)
    f = d / f"{stem}.html"
    n = 2
    while f.exists():
        f = d / f"{stem} ({n}).html"
        n += 1
    f.write_text(html, encoding="utf-8")
    return str(f)


SCROLLBACK_CAP = 512 * 1024        # bytes of terminal output kept for replay
# One output stream serves every terminal of a page: a browser allows only 6
# connections per host, so a stream per tab left none for keystrokes once 6
# tabs were open. Every pump bumps TERMS_SEQ; the stream waits on it.
TERMS_COND = threading.Condition()
TERMS_SEQ = 0


def terms_changed():
    global TERMS_SEQ
    with TERMS_COND:
        TERMS_SEQ += 1
        TERMS_COND.notify_all()


class Term:
    """Terminal session: shared scrollback + streaming; the transport
    (POSIX pty or Windows ConPTY) is supplied by a subclass."""

    def __init__(self, argv, cwd, cols=100, rows=30, extra_env=None, env=None):
        self.id = secrets.token_hex(8)
        self.label = Path(argv[0]).name + " · " + (Path(cwd).name or "/")
        self.argv, self.cwd = argv, cwd
        self.is_claude = "claude" in Path(argv[0]).name.lower()
        resume = next((argv[i + 1] for i, a in enumerate(argv[:-1])
                       if a in ("--resume", "--session-id")), None)
        self.snap = session_snapshot(cwd, resume) if self.is_claude else None
        self.buf = bytearray()      # scrollback so re-attaching clients catch up
        self.discarded = 0          # bytes trimmed off the front of buf, ever
        self.cond = threading.Condition()
        self.alive = True
        self._finished = False      # _finish() ran: fds closed, child reaped
        self.cols, self.rows = cols, rows
        # `env` is the COMPLETE child environment (already scrubbed of the
        # parent session's markers); extra_env only adds on top of it
        full = dict(env if env is not None else os.environ)
        full.setdefault("TERM", "xterm-256color")
        full.setdefault("COLORTERM", "truecolor")
        full.update(extra_env or {})
        self._spawn(argv, cwd, full, cols, rows)
        threading.Thread(target=self._pump, daemon=True).start()

    # -- transport hooks -------------------------------------------------
    def _spawn(self, argv, cwd, env, cols, rows):
        raise NotImplementedError

    def _read(self):
        """Blocking read; b'' means the terminal closed."""
        raise NotImplementedError

    def _write(self, data):
        raise NotImplementedError

    def _set_size(self, cols, rows):
        raise NotImplementedError

    def _hangup(self):
        """Ask the child to exit cleanly so its shutdown hooks run."""
        raise NotImplementedError

    def _terminate(self):
        raise NotImplementedError

    def _cleanup(self):
        pass

    # -- shared ----------------------------------------------------------
    def _pump(self):
        while True:
            try:
                data = self._read()
            except OSError:
                break
            if data is None:            # idle tick
                continue
            if not data:
                break
            with self.cond:
                self.buf.extend(data)
                if len(self.buf) > SCROLLBACK_CAP:      # cap scrollback
                    drop = len(self.buf) - SCROLLBACK_CAP
                    del self.buf[:drop]
                    # every index into buf just shifted down by `drop`; record
                    # it so absolute positions stay translatable
                    self.discarded += drop
                self.cond.notify_all()
            terms_changed()
        with self.cond:
            self.alive = False
            self.cond.notify_all()
        terms_changed()
        # a child that exits by itself (`exit`, /quit) gets the same ending as
        # a closed tab: reaped, fds closed, retrospective considered
        self._finish()

    def _finish(self):
        """Release the transport exactly once — closing an fd twice could
        close an unrelated file that reused the number — then hand a Claude
        session to the retrospective."""
        with self.cond:
            if self._finished:
                return
            self._finished = True
        try:
            self._cleanup()
        except OSError:
            pass
        # the session has written its transcript and run its own hooks by
        # now — hand it to the retrospective (detached, so quitting the app
        # does not cut it short)
        if self.is_claude and self.snap:
            try:
                store_summary(self.id, session_summary(self.cwd, self.snap))
            except Exception:
                LOG.exception("session summary failed")
        if self.is_claude and self.snap:  # also on quit: that is the retrospective's point
            try:
                spawn_improve(self.cwd, session_transcript(
                    self.cwd, self.snap["started"], self.snap.get("resume")))
            except Exception:
                pass

    def produced(self):
        """Absolute count of bytes the child has emitted. Call under `cond`."""
        return self.discarded + len(self.buf)

    def slice_from(self, pos):
        """Everything after absolute offset `pos`, as (chunk, new_pos).

        `pos` is clamped into the window `buf` still holds: below `discarded`
        those bytes have aged out (a client that fell that far behind skips the
        gap rather than stalling forever), above `produced` the child has not
        emitted them yet. Call under `cond`.
        """
        pos = min(max(pos, self.discarded), self.produced())
        return bytes(self.buf[pos - self.discarded:]), self.produced()

    def write(self, data: bytes):
        """Report transport failures to the caller instead of claiming success."""
        if not self.alive or self._finished:
            raise BrokenPipeError("Terminal has closed")
        self._write(data)

    def resize(self, cols, rows):
        try:
            cols, rows = max(2, int(cols)), max(2, int(rows))
            self._set_size(cols, rows)
            self.cols, self.rows = cols, rows      # what the child believes
        except (OSError, ValueError):
            pass

    def close_gracefully(self, grace=25.0):
        """Hang up first — Claude Code treats it as the terminal closing and
        runs its Stop/SessionEnd hooks — escalating only if it outlives the
        grace period. Blocks until the child is gone."""
        try:
            if self.alive:
                self._hangup()
                deadline = time.time() + grace
                while self.alive and time.time() < deadline:
                    time.sleep(0.15)
            if self.alive:
                self._terminate()
                deadline = time.time() + 3.0
                while self.alive and time.time() < deadline:
                    time.sleep(0.15)
        finally:
            self._finish()

    def kill(self):
        """Non-blocking graceful close (used by the per-tab close button)."""
        threading.Thread(target=self.close_gracefully, daemon=True).start()


class PosixTerm(Term):
    """macOS / Linux: fork a real pty."""

    WRITE_TIMEOUT = 2.0

    def _spawn(self, argv, cwd, env, cols, rows):
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(cwd)
            except OSError:
                pass
            os.environ.clear()          # env is the complete child environment
            os.environ.update(env)
            try:
                os.execvp(argv[0], argv)
            except OSError as e:
                os.write(2, f"exec failed: {e}\r\n".encode())
                os._exit(127)
        self.pid, self.fd = pid, fd
        self._write_lock = threading.Lock()
        os.set_blocking(fd, False)
        self._set_size(cols, rows)

    def _read(self):
        try:
            r, _, _ = select.select([self.fd], [], [], 1.0)
            if r:
                return os.read(self.fd, 65536)
        except (BlockingIOError, InterruptedError):
            pass
        return None                         # idle / readiness changed

    def _write(self, data):
        """Serialize complete writes; give up only after WRITE_TIMEOUT with no
        progress, so a slow-but-reading child can take a large paste."""
        deadline = time.monotonic() + self.WRITE_TIMEOUT
        if not self._write_lock.acquire(timeout=self.WRITE_TIMEOUT):
            raise TimeoutError("Terminal input is busy; queued input was not sent")
        try:
            pending = memoryview(data)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Terminal stopped accepting input; some text may have been sent")
                try:
                    _, ready, _ = select.select([], [self.fd], [], remaining)
                    if not ready:
                        continue
                    # _finish marks the transport closed under this same lock;
                    # never write to an fd that cleanup may have closed/reused.
                    with self.cond:
                        if self._finished or not self.alive:
                            raise BrokenPipeError("Terminal has closed")
                        n = os.write(self.fd, pending)
                    if not n:
                        raise BrokenPipeError("Terminal stopped accepting input")
                    pending = pending[n:]
                    deadline = time.monotonic() + self.WRITE_TIMEOUT
                except (BlockingIOError, InterruptedError):
                    continue
        finally:
            self._write_lock.release()

    def _set_size(self, cols, rows):
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ,
                    struct.pack("HHHH", rows, cols, 0, 0))

    def _signal(self, sig):
        try:
            os.killpg(self.pid, sig)        # child is its own session leader
            return True
        except (AttributeError, OSError, ProcessLookupError):
            try:
                os.kill(self.pid, sig)
                return True
            except (OSError, ProcessLookupError):
                return False

    def _hangup(self):
        self._signal(getattr(signal, "SIGHUP", signal.SIGTERM))

    def _terminate(self):
        self._signal(signal.SIGTERM)
        time.sleep(0.3)
        if self.alive:
            self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def _cleanup(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
        # reap, or it stays a zombie: the child may take a moment to exit
        # after its pty closes, so poll briefly rather than a single WNOHANG
        deadline = time.time() + 3.0
        while True:
            try:
                if os.waitpid(self.pid, os.WNOHANG)[0] or time.time() > deadline:
                    break
            except (ChildProcessError, OSError):
                break
            time.sleep(0.05)


class WindowsTerm(Term):
    """Windows 10 1809+: attach the child to a ConPTY pseudo-console."""

    def _spawn(self, argv, cwd, env, cols, rows):
        self.proc = winconpty.ConPtyProcess(argv, cwd, env, cols, rows)
        self.pid = self.proc.pid

    def _read(self):
        data = self.proc.read()
        if not data and not self.proc.alive():
            return b""
        return data or None

    def _write(self, data):
        self.proc.write(data)

    def _set_size(self, cols, rows):
        self.proc.set_size(cols, rows)

    def _hangup(self):
        # CTRL_CLOSE_EVENT via closing the pseudo-console: the child gets a
        # chance to run cleanup handlers, like SIGHUP on POSIX
        self.proc.request_close()

    def _terminate(self):
        self.proc.terminate()

    def _cleanup(self):
        self.proc.request_close()
        self.proc.close_handles()


TERMS = {}
TERMS_LOCK = threading.Lock()
MAX_TERMS = 6


def clamp_dim(v, fallback):
    """Terminal dimensions are packed into unsigned shorts by TIOCSWINSZ."""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return fallback
    return max(2, min(2000, n))


def default_shell():
    if os.name == "nt":
        return os.environ.get("COMSPEC") or "powershell.exe"
    return os.environ.get("SHELL", "/bin/bash")


_env_cache = {}

# Where `claude` commonly lives, for hosts whose login shell we can't query
CLAUDE_CANDIDATES = (
    "~/.claude/local/claude", "~/.local/bin/claude", "~/bin/claude",
    "/opt/homebrew/bin/claude", "/usr/local/bin/claude",
    "~/.npm-global/bin/claude", "~/.volta/bin/claude",
    "~/AppData/Roaming/npm/claude.cmd", "~/AppData/Local/claude/claude.exe",
)


def _windows_registry_path():
    """PATH as Windows will give the NEXT process: machine + user values read
    from the registry. Our own os.environ PATH is frozen at launch, so without
    this a `winget install` done from the add-ons pane stays invisible."""
    try:
        import winreg
    except ImportError:
        return []
    out = []
    for hive, key in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, key) as k:
                val, _ = winreg.QueryValueEx(k, "Path")
            out += os.path.expandvars(str(val)).split(os.pathsep)
        except OSError:
            continue
    return out


def login_path():
    """PATH as the user's *login shell* sees it.

    An app started from Finder/LaunchServices (or a .desktop entry) inherits a
    minimal PATH — typically /usr/bin:/bin:/usr/sbin:/sbin — which hides
    ~/.local/bin, Homebrew, nvm, volta … i.e. exactly where `claude` lives.
    Ask the shell once, cache it, and always append the usual suspects.
    """
    if "path_raw" in _env_cache:
        return _existing_dirs(_windows_registry_path() + _env_cache["path_raw"])
    parts = []
    if os.name != "nt":
        shell = default_shell()
        for flags in (["-lic"], ["-lc"]):     # -i picks up PATH set in .zshrc
            try:
                r = subprocess.run([shell, *flags, 'printf %s "$PATH"'],
                                   capture_output=True, text=True, timeout=8)
                lines = [ln.strip() for ln in (r.stdout or "").splitlines()
                         if "/" in ln]
                if lines:
                    parts = lines[-1].split(os.pathsep)
                    break
            except (OSError, subprocess.SubprocessError):
                continue
    parts += (os.environ.get("PATH") or "").split(os.pathsep)
    parts += [str(Path(p).expanduser().parent) for p in CLAUDE_CANDIDATES]
    parts += ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin",
              str(Path("~/.local/bin").expanduser())]   # uv / pipx tools
    # cache the (slow) shell query, but re-check which folders exist on every
    # call: `uv tool install` creates ~/.local/bin after the app started, and
    # a cached list would hide it from new terminals until a restart
    _env_cache["path_raw"] = parts
    return _existing_dirs(_windows_registry_path() + parts)


def _existing_dirs(parts):
    seen, ordered = set(), []
    for p in parts:
        if p and p not in seen and os.path.isdir(p):
            seen.add(p)
            ordered.append(p)
    return os.pathsep.join(ordered)


# --- child session hygiene -------------------------------------------------
# If this server was started from inside a Claude Code session (a terminal
# running claude, or an app launched from one), its environment carries that
# session's markers. Children would inherit them, Claude Code would consider
# itself a *nested child session*, and it would DISABLE TRANSCRIPT SAVING —
# so sessions started from this dashboard would never be recorded, i.e. never
# show up in this dashboard. Scrub them so every terminal starts clean.
SESSION_MARKER_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_")
SESSION_MARKER_EXACT = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT",
                        "CLAUDE_PLUGIN_DATA", "CLAUDE_PREVIEW_CLASSIFIER_FLOOR"}
# genuine user configuration that must survive the scrub
SESSION_MARKER_KEEP = {"CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                       "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
                       "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
                       "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CONFIG_DIR",
                       "CLAUDE_BIN"}


def is_session_marker(name):
    if name in SESSION_MARKER_KEEP:
        return False
    return (name in SESSION_MARKER_EXACT
            or name.startswith(SESSION_MARKER_PREFIXES))


def inherited_session_markers(environ=None):
    env = os.environ if environ is None else environ
    return sorted(k for k in env if is_session_marker(k))


def child_environment(environ=None, extra=None):
    """A complete environment for a spawned terminal: the server's, minus the
    parent session's markers, plus our own additions."""
    env = dict(os.environ if environ is None else environ)
    nested = "CLAUDECODE" in env or "CLAUDE_CODE_SESSION_ID" in env
    for k in list(env):
        if is_session_marker(k):
            del env[k]
    if nested:
        # injected by the parent session rather than the user's own profile
        env.pop("ANTHROPIC_BASE_URL", None)
    env.update(extra or {})
    return env


def find_claude():
    """Locate the Claude Code CLI regardless of how this server was started."""
    if "claude" in _env_cache:
        hit = _env_cache["claude"]
        if hit is None:
            # re-check a miss (cheap PATH lookup only): the add-ons pane may
            # have just installed Claude Code
            hit = shutil.which("claude", path=login_path())
            if hit:
                _env_cache["claude"] = hit
        return hit
    found = None
    override = os.environ.get("CLAUDE_BIN")
    if override and os.path.exists(os.path.expanduser(override)):
        found = os.path.expanduser(override)
    if not found:
        found = shutil.which("claude", path=login_path())
    if not found:
        for c in CLAUDE_CANDIDATES:
            p = Path(c).expanduser()
            if p.exists():
                found = str(p)
                break
    if not found and os.name != "nt":       # last resort: ask the shell itself
        try:
            r = subprocess.run([default_shell(), "-lic", "command -v claude"],
                               capture_output=True, text=True, timeout=8)
            for ln in (r.stdout or "").splitlines():
                ln = ln.strip()
                if ln.startswith("/") and os.path.exists(ln):
                    found = ln
                    break
        except (OSError, subprocess.SubprocessError):
            pass
    _env_cache["claude"] = found
    return found


# What sessions started from Ember are told about it, so Viz, Plan and figure
# comments work without anyone editing ~/.claude/CLAUDE.md. No double quotes:
# it travels as one argument through POSIX argv and the ConPTY command line.
EMBER_PROMPT = """This session runs inside Ember, a local workspace around Claude Code \
(CLAUDE_DEVTOOLS_UI=1). To show the user a visual output (figure, chart, HTML report, \
table), also write a self-contained file into the folder in $CLAUDE_DEVTOOLS_VIZ_DIR: it \
appears in Ember's Viz pane within seconds. Prefer .html with inline CSS/JS only (no \
network), .png or .svg, with descriptive file names.

Keep the working plan in .claude/plan.md (relative to the project folder) as markdown \
checkboxes (- [ ] step, - [x] done). Ember shows it as a live checklist and writes the \
user's ticks back, so read it before planning and update it as steps complete.

The user can pin comments on a figure or PDF; they are saved in .review/<file>.json next \
to it (coordinates are fractions of the image, origin top-left; PDFs add a 1-based page). \
Before regenerating that file, read its open comments; after applying one, set its \
status to resolved in that JSON."""


def ember_prompt_args():
    """--append-system-prompt for a claude launched here, unless the user's
    CLAUDE.md already carries the README's Ember block (no duplicate tax)."""
    try:
        own = (CLAUDE_ROOT / "CLAUDE.md").read_text(encoding="utf-8", errors="replace")
    except OSError:
        own = ""
    return [] if "CLAUDE_DEVTOOLS_UI" in own else ["--append-system-prompt", EMBER_PROMPT]


def logged_in():
    """True/False when we can tell, None when we can't (keychain-only setups).
    Not a blocker either way: claude itself walks a new user through login."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        conf = json.loads((CLAUDE_ROOT.parent / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return bool(conf.get("oauthAccount")) if isinstance(conf, dict) else None


def setup_status(root):
    """The welcome checklist: what a first-time user still needs."""
    try:
        has_projects = any(projects_dir(root).iterdir())
    except OSError:
        has_projects = False
    pr = practice_dir()
    return {"claude": bool(find_claude()), "logged_in": logged_in(),
            "projects": has_projects,
            "aware": "prompt" if ember_prompt_args() else "claude-md",
            "practice": str(pr) if (pr / PRACTICE_MARK).is_file() else None}


def start_term(kind, cwd, session_id=None, prompt=None, cols=100, rows=30):
    if not HAS_TERMINAL:
        raise NotImplementedError(
            "no pseudo-terminal available: " + (winconpty.unsupported_reason()
                                                or "unknown reason"))
    cwd = cwd if cwd and os.path.isdir(cwd) else str(Path.home())
    if kind == "shell":
        argv = [default_shell()] + ([] if os.name == "nt" else ["-l"])
    else:
        claude = find_claude()
        if not claude:
            # never silently open a plain shell instead — say what's wrong
            raise FileNotFoundError(
                "Claude Code CLI not found. Install it, or set CLAUDE_BIN to "
                "its full path and restart the dashboard. Looked on the login "
                "shell PATH and in " + ", ".join(CLAUDE_CANDIDATES[:4]) + " …")
        if kind == "resume" and session_id:
            argv = [claude, "--resume", session_id]
        else:
            argv = [claude, "--session-id", str(uuid.uuid4())]
            if prompt:                  # e.g. "/graphify" from the viz pane
                argv.append(str(prompt)[:2000])
        argv[1:1] = ember_prompt_args()
    # complete child environment: scrubbed of the parent session's markers (so
    # transcripts get saved), with the login PATH (an app-launched server has a
    # minimal one), telling Claude Code it runs inside this dashboard
    env = child_environment(extra={
        "PATH": login_path(),
        "CLAUDE_DEVTOOLS_UI": "1",
        "CLAUDE_DEVTOOLS_VIZ_DIR": str(VIZ_DIR),
        "CLAUDE_DEVTOOLS_URL": f"http://127.0.0.1:{SERVER_PORT}"})
    impl = PosixTerm if HAS_PTY else WindowsTerm
    # check the cap BEFORE spawning: refusing after the fork left the new
    # child running with no tab to close it from
    with TERMS_LOCK:
        for tid in [tid for tid, tt in TERMS.items() if not tt.alive]:
            del TERMS[tid]                   # exited ones were _finish()ed
        if len(TERMS) >= MAX_TERMS:
            raise RuntimeError("too many open terminals — close one first")
    # spawn at the client's real viewport size: a child that starts at the
    # wrong width emits wrapped output that stays wrong after the SIGWINCH
    t = impl(argv, cwd, cols=clamp_dim(cols, 100), rows=clamp_dim(rows, 30),
             env=env)
    with TERMS_LOCK:
        TERMS[t.id] = t     # ponytail: two racing starts can reach MAX_TERMS+1
    return t


# ---------------------------------------------------------------- HTTP

def page_csp(body):
    """CSP for the dashboard page: scripts only from /vendor and the one inline
    <script> (pinned by hash, recomputed per serve, so no build step). Inline
    styles stay allowed: KaTeX and xterm set them, and they cannot run code."""
    m = re.search(rb"<script>(.*?)</script>", body, re.S)
    # hash what the browser hashes: HTML parsing turns CRLF and lone CR into LF,
    # and a Git-for-Windows checkout (autocrlf) serves index.html with CRLF
    src = m.group(1).replace(b"\r\n", b"\n").replace(b"\r", b"\n") if m else b""
    h = base64.b64encode(hashlib.sha256(src).digest()).decode() if m else ""
    return ("default-src 'self'; script-src 'self' 'sha256-%s'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "font-src 'self'; frame-src 'self'; object-src 'self'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'" % h)


class Handler(BaseHTTPRequestHandler):
    server_version = "ember/" + VERSION
    root = CLAUDE_ROOT  # overridden in main()

    def log_message(self, fmt, *args):
        # never let a token reach the log: query-string tokens would otherwise
        # persist wherever stderr is redirected
        line = fmt % args
        line = re.sub(r"([?&](?:token|k|c)=)[A-Za-z0-9]+", r"\1[redacted]", line)
        LOG.info(line)

    def parse_request(self):
        self._t0 = time.time()
        return super().parse_request()

    def log_request(self, code="-", size="-"):
        ms = (time.time() - getattr(self, "_t0", time.time())) * 1000
        try:
            ok = 200 <= int(code) < 400
        except (TypeError, ValueError):
            ok = False
        if ok and ms < SLOW_REQUEST_S * 1000:
            return                                  # quiet: a normal poll
        self.log_message('"%s" %s %s %.0fms', self.requestline, code, size, ms)
        if not ok:
            RECENT_ERRORS.append({"t": time.time(), "code": code,
                                  "req": re.sub(r"([?&](?:token|k|c)=)[A-Za-z0-9]+",
                                                r"\1[redacted]", self.requestline)})

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def _host_ok(self):
        """Reject foreign Host headers (DNS-rebinding guard)."""
        h = self.headers.get("Host")
        if not h:
            return True                      # non-browser clients may omit it
        return h.rsplit(":", 1)[0].strip("[]").lower() in ALLOWED_HOSTS

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json_bytes(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self._json({"error": msg}, code)

    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cdl":
                return v
        return ""

    def _authed(self, qs):
        tok = (self.headers.get("X-Devtools-Token") or qs.get("token", [""])[0]
               or self._cookie_token())
        return bool(tok) and secrets.compare_digest(tok, SERVER_TOKEN or "")

    def do_GET(self):
        try:
            if not self._host_ok():
                self._err(403, "bad Host header")
                return
            u = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(u.query)
            p = u.path

            if p.startswith("/api/") and not self._authed(qs):
                self._err(401, "missing or bad token — open the dashboard from a login link")
                return

            if p == "/hello":
                n = qs.get("n", [""])[0]
                if not re.fullmatch(r"[0-9a-f]{16,64}", n):
                    self._err(400, "bad nonce")
                    return
                self._json({"mac": hello_mac(SERVER_TOKEN or "", n)})
                return

            if p == "/launch":
                # the app launcher's entry point: exchange the token (query)
                # for a browser cookie, then land on the dashboard. A real
                # navigation — immune to fragment-only tab-reuse races.
                k = qs.get("k", [""])[0]
                c = qs.get("c", [""])[0]
                ok = (c and launch_code_take(c)) or (
                    k and secrets.compare_digest(k, SERVER_TOKEN or ""))
                if not ok:
                    self._err(403, "bad launch token")
                    return
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 f"cdl={SERVER_TOKEN}; Path=/; SameSite=Strict; "
                                 f"Max-Age=31536000")
                self.end_headers()
                return

            if p in ("/", "/index.html"):
                body = (HERE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", page_csp(body))
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)
                return

            if p.startswith("/vendor/"):
                name = p[len("/vendor/"):]
                if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
                    self._err(404, "not found")
                    return
                f = HERE / "vendor" / name
                if not f.is_file():
                    self._err(404, "not found")
                    return
                if name.endswith(".css"):
                    ctype = "text/css"
                elif name.endswith(".woff2"):
                    ctype = "font/woff2"
                elif name.endswith(".txt"):
                    ctype = "text/plain; charset=utf-8"
                else:
                    ctype = "application/javascript"
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/api/term/stream":
                self.stream_term(qs)
                return

            if p == "/api/usage/history":
                try:
                    days = int(qs.get("days", ["90"])[0])
                except ValueError:
                    days = 90
                self._json(usage_history(self.root, days))
                return

            if p == "/api/usage":
                u = usage_summary(self.root)
                u["official"] = official_limits()
                self._json(u)
                return

            if p == "/api/events":
                try:
                    since = int(qs.get("since", ["-1"])[0])
                except ValueError:
                    since = -1
                self._json(events_since(since))
                return

            if p == "/api/guards":
                try:
                    since = int(qs.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                self._json(events_since(since, limit=100, path=GUARDS_FILE))
                return

            if p == "/api/term/summary":
                s = summary_with_improve(qs.get("id", [""])[0])
                if s is None:
                    self._err(404, "no summary (yet)")
                    return
                self._json(s)
                return

            if p == "/api/state":
                self._json({"layout": state_read().get("layout"),
                            "improve": improve_enabled(),
                            "open_in": open_in(),
                            "updates": updates_enabled(),
                            "has_terminal": HAS_TERMINAL,
                            "terminal_blocked": (None if HAS_TERMINAL
                                                 else winconpty.unsupported_reason()),
                            "platform": sys.platform})
                return

            if p == "/api/viz":
                files, d = viz_list(qs.get("dir", [None])[0])
                self._json({"dir": str(d), "default_dir": str(VIZ_DIR), "files": files})
                return

            if p == "/api/update":
                self._json(update_info(force=qs.get("force", [""])[0] == "1"))
                return

            if p == "/api/addons":
                self._json(addons_status())
                return

            if p == "/api/review":
                self._json(review_read(qs.get("path", [""])[0]))
                return

            if p == "/api/fs":
                self._json(fs_listing(qs.get("path", [None])[0]))
                return

            if p == "/api/fs/file":
                raw = qs.get("path", [""])[0]
                f = safe_home_path(raw)
                ext = f.suffix.lower()
                if is_sensitive(f.name):
                    self._err(403, "refused: file looks like it contains secrets")
                    return
                if not f.is_file() or (ext not in VIZ_TYPES and ext not in FS_TEXT):
                    self._err(404, "not previewable")
                    return
                if f.stat().st_size > 20 * 1024 * 1024:
                    self._err(413, "file too large to preview")
                    return
                ctype = VIZ_TYPES.get(ext, "text/plain")
                body = f.read_bytes()
                if ext in (".html", ".htm"):
                    body = fit_graph_html(body)
                self.send_response(200)
                self.send_header("Content-Type", ctype + "; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                if ext in (".html", ".htm"):
                    # previews render in a sandboxed iframe (opaque origin: no
                    # access to this server's API or cookies). Allow https so
                    # CDN-based pages (e.g. graphify's vis-network) render.
                    self.send_header("Content-Security-Policy",
                                     "sandbox allow-scripts; "
                                     "default-src 'unsafe-inline' data: blob: https:")
                self.end_headers()
                self.wfile.write(body)
                return

            if p == "/api/setup":
                self._json(setup_status(self.root))
                return

            if p == "/api/health":
                with TERMS_LOCK:
                    terms = [{"id": t.id, "label": t.label, "alive": t.alive}
                             for t in TERMS.values()]
                self._json({"version": VERSION, "pid": os.getpid(),
                            "uptime_s": round(time.time() - STARTED),
                            "python": sys.version.split()[0], "os": host_os(),
                            "terminals": terms,
                            "caches": {"meta": len(_meta_cache), "usage": len(_usage_cache),
                                       "sessions": len(_session_cache)},
                            "recent_errors": list(RECENT_ERRORS)})
                return

            if p == "/api/term/list":
                # live terminals, so a reloaded page can re-attach to them
                with TERMS_LOCK:
                    self._json([{"id": t.id, "label": t.label, "cwd": t.cwd,
                                 "alive": t.alive, "cols": t.cols, "rows": t.rows,
                                 "kind": "claude" if t.is_claude else "shell"}
                                for t in TERMS.values() if t.alive])
                return

            if p == "/api/projects":
                self._json(list_projects(self.root))
                return

            if p == "/api/sessions":
                slug = qs.get("project", [""])[0]
                d = safe_project_path(self.root, slug)
                metas = [session_meta(f) for f in d.glob("*.jsonl")]
                metas.sort(key=lambda m: m["mtime"], reverse=True)
                self._json(metas)
                return

            if p == "/api/session":
                slug = qs.get("project", [""])[0]
                sid = qs.get("id", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or ""):
                    self._err(400, "bad session id")
                    return
                f = safe_project_path(self.root, slug) / f"{sid}.jsonl"
                if not f.is_file():
                    self._err(404, "session not found")
                    return
                self._send_json_bytes(session_json(f))
                return

            if p == "/api/session/tail":
                slug = qs.get("project", [""])[0]
                sid = qs.get("id", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or ""):
                    self._err(400, "bad session id")
                    return
                f = safe_project_path(self.root, slug) / f"{sid}.jsonl"
                if not f.is_file():
                    self._err(404, "session not found")
                    return
                try:
                    since = int(qs.get("since", ["0"])[0])
                except ValueError:
                    since = 0
                self._json(session_tail(f, qs.get("key", [""])[0], since))
                return

            if p == "/api/subagent":
                slug = qs.get("project", [""])[0]
                sid = qs.get("session", [""])[0]
                agent = qs.get("agent", [""])[0]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", sid or "") or \
                   not re.fullmatch(r"[A-Za-z0-9_.-]+\.jsonl", agent or ""):
                    self._err(400, "bad id")
                    return
                f = safe_project_path(self.root, slug) / sid / "subagents" / agent
                if not f.is_file():
                    self._err(404, "subagent not found")
                    return
                self._json(parse_session(f, include_sidechain=True))
                return

            if p == "/api/plan":
                slug = qs.get("project", [""])[0]
                cwd = qs.get("cwd", [None])[0] or project_cwd(self.root, slug)
                if not cwd:
                    self._json({"dir": None, "candidates": [], "items": [],
                                "done": 0, "total": 0, "file": None})
                    return
                self._json(plan_read(cwd, qs.get("file", [None])[0]))
                return

            if p == "/api/config":
                self._json(config_view(qs.get("cwd", [None])[0]))
                return

            if p == "/api/improve":
                cwd = qs.get("cwd", [None])[0]
                slug = (mangle_cwd(cwd) if cwd
                        else qs.get("project", [""])[0])
                out = improve_reports(slug)
                out["enabled"] = improve_enabled()
                self._json(out)
                return

            if p == "/api/memory":
                slug = qs.get("project", [""])[0]
                self._json(read_memory(self.root, slug))
                return

            if p == "/api/search":
                q = qs.get("q", [""])[0]
                if len(q) < 2:
                    self._err(400, "query too short")
                    return
                proj = qs.get("project", [None])[0]
                self._json(search_all(self.root, q, proj))
                return

            self._err(404, "not found")
        except (FileNotFoundError, ValueError) as e:
            self._err(404, str(e))
        except BrokenPipeError:
            pass
        except Exception as e:  # keep the server alive; report the error
            LOG.error("GET %s failed\n%s", self.path.split("?")[0], traceback.format_exc())
            self._err(500, f"{type(e).__name__}: {e}")

    def do_POST(self):
        try:
            if not self._host_ok():
                self._err(403, "bad Host header")
                return
            # CSRF guard: browsers can fire cross-origin "simple" POSTs at
            # localhost without preflight. Requiring a JSON content type forces
            # a preflight (which we never approve), and any Origin header must
            # be our own host. curl/local scripts just set the JSON header.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype != "application/json":
                self._err(403, "Content-Type must be application/json")
                return
            origin = self.headers.get("Origin")
            if origin:
                host = urllib.parse.urlparse(origin).netloc.split(":")[0]
                if host not in ("127.0.0.1", "localhost", "[::1]"):
                    self._err(403, "cross-origin request refused")
                    return
            u = urllib.parse.urlparse(self.path)
            if not self._authed(urllib.parse.parse_qs(u.query)):
                self._err(401, "missing or bad token — open the dashboard from a login link")
                return
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            p = u.path

            if p == "/api/launch-code":
                self._json({"code": launch_code_new()})
                return

            if p == "/api/state":
                layout = body.get("layout")
                if isinstance(layout, dict):
                    clean = {k: float(v) for k, v in layout.items()
                             if k in ("col2", "rowL", "rowR", "rowP", "sidebar", "headH")
                             and isinstance(v, (int, float))}
                    state_write({"layout": clean})
                if isinstance(body.get("improve"), bool):
                    state_write({"improve": body["improve"]})
                if isinstance(body.get("updates"), bool):
                    state_write({"updates": body["updates"]})
                if body.get("open_in") in ("window", "browser"):
                    state_write({"open_in": body["open_in"]})
                self._json({"ok": True, "improve": improve_enabled(),
                            "updates": updates_enabled(), "open_in": open_in()})
                return

            if p in ("/api/plan/toggle", "/api/plan/create"):
                slug = body.get("project") or ""
                cwd = body.get("cwd") or project_cwd(self.root, slug)
                if not cwd:
                    self._err(404, "project has no working directory")
                    return
                if p.endswith("create"):
                    self._json(plan_create(cwd))
                else:
                    self._json(plan_toggle(cwd, body.get("file"),
                                           body.get("line"),
                                           body.get("expect"),
                                           body.get("state", "done")))
                return

            if p == "/api/review":
                self._json(review_write(body.get("path") or "",
                                        body.get("comments"),
                                        body.get("revision")))
                return

            if p == "/api/shutdown":
                # close every terminal session gracefully IN PARALLEL so
                # Claude Code sessions get to run their SessionEnd hooks,
                # then exit once they are all down (or after the timeout).
                with TERMS_LOCK:
                    terms = list(TERMS.values())
                    TERMS.clear()
                for t in terms:
                    t.snap = None           # no one is left to read an end card
                threads = [threading.Thread(target=t.close_gracefully, daemon=True)
                           for t in terms]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(timeout=30.0)
                self._json({"ok": True, "bye": True, "closed": len(terms)})
                threading.Timer(0.4, os._exit, [0]).start()
                return

            if p == "/api/term/start":
                kind = body.get("kind", "claude")
                cwd = None
                if body.get("project"):
                    cwd = project_cwd(self.root, body["project"])
                if body.get("cwd") and os.path.isdir(body["cwd"]):
                    cwd = body["cwd"]
                sid = body.get("session")
                if sid and not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
                    self._err(400, "bad session id")
                    return
                prompt = body.get("prompt")
                if prompt is not None and not isinstance(prompt, str):
                    prompt = None
                t = start_term(kind, cwd, sid, prompt=prompt,
                               cols=body.get("cols", 100),
                               rows=body.get("rows", 30))
                self._json({"id": t.id, "label": t.label, "cwd": t.cwd,
                            "argv": t.argv})
                return

            if p == "/api/practice/create":
                self._json(practice_create())
                return

            if p == "/api/practice":
                self._json(practice_sync(self.root))
                return

            if p == "/api/export":
                self._json({"ok": True, "path": save_export(body.get("name"), body.get("html"))})
                return

            if p == "/api/term/sessionlog":
                try:
                    self._json({"ok": True, "path": append_session_log(str(body.get("id", "")))})
                except KeyError:
                    self._err(404, "no summary for that terminal")
                except OSError as e:
                    self._err(500, f"could not write the session log: {e}")
                return

            t = None
            tid = body.get("id", "")
            with TERMS_LOCK:
                t = TERMS.get(tid)
            if t is None:
                self._err(404, "terminal not found")
                return

            if p == "/api/term/input":
                try:
                    t.write(base64.b64decode(body.get("data", "")))
                except TimeoutError as e:
                    self._err(504, str(e))
                    return
                except OSError as e:
                    self._err(410, f"Terminal input failed: {e}")
                    return
                self._json({"ok": True})
                return
            if p == "/api/term/resize":
                t.resize(clamp_dim(body.get("cols"), 100),
                         clamp_dim(body.get("rows"), 30))
                self._json({"ok": True, "cols": t.cols, "rows": t.rows})
                return
            if p == "/api/term/kill":
                t.kill()
                with TERMS_LOCK:
                    TERMS.pop(tid, None)
                self._json({"ok": True})
                return

            self._err(404, "not found")
        except NotImplementedError as e:
            self._err(501, str(e))
        except FileNotFoundError as e:
            self._err(424, str(e))
        except RuntimeError as e:
            self._err(429, str(e))
        except ValueError as e:
            self._err(409, str(e))
        except BrokenPipeError:
            pass
        except Exception as e:
            LOG.error("POST %s failed\n%s", self.path.split("?")[0], traceback.format_exc())
            self._err(500, f"{type(e).__name__}: {e}")

    def stream_term(self, qs):
        """SSE stream of several terminals' output: ?id=a&from=0&id=b&from=0.
        Frames: `data: <id> <offset> <base64>`, where offset is the absolute
        position just past the chunk, and `event: exit / data: <id>`."""
        ids, froms = qs.get("id", []), qs.get("from", [])
        pos = {}
        for i, tid in enumerate(ids):
            try:
                pos[tid] = max(0, int(froms[i]))
            except (IndexError, ValueError):
                pos[tid] = 0
        if not pos:
            self._err(400, "no terminal id")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while pos:
                with TERMS_COND:
                    seen = TERMS_SEQ
                wrote = False
                for tid in list(pos):
                    with TERMS_LOCK:
                        t = TERMS.get(tid)
                    if t is None:               # killed, or gone after a restart
                        chunk, alive, done = b"", False, True
                    else:
                        with t.cond:
                            # `pos` is absolute — translated, never an index
                            chunk, pos[tid] = t.slice_from(pos[tid])
                            alive = t.alive
                            done = not alive and pos[tid] >= t.produced()
                    if chunk:
                        b64 = base64.b64encode(chunk).decode()
                        self.wfile.write(f"data: {tid} {pos[tid]} {b64}\n\n".encode())
                        wrote = True
                    if done:
                        self.wfile.write(f"event: exit\ndata: {tid}\n\n".encode())
                        del pos[tid]
                        wrote = True
                if wrote:
                    self.wfile.flush()
                    continue
                with TERMS_COND:
                    changed = TERMS_COND.wait_for(lambda: TERMS_SEQ != seen, 15.0)
                if not changed:
                    self.wfile.write(b": keepalive\n\n")    # comment frame
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class NotOurServer(Exception):
    pass


def local_opener():
    # no proxy: Windows' system proxy (ProxyOverride "<local>" skips only dotless
    # hosts) would otherwise receive 127.0.0.1 requests — and the token
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def launch_url(port):
    """Check the server on `port` holds our token, trade the token for a
    one-time code, return the /launch URL. Raises NotOurServer, OSError,
    ValueError or KeyError."""
    import hmac
    import urllib.request
    base = f"http://127.0.0.1:{port}"
    opener = local_opener()
    tok = TOKEN_FILE.read_text().strip()
    nonce = secrets.token_hex(16)
    with opener.open(f"{base}/hello?n={nonce}", timeout=3) as r:
        mac = json.loads(r.read())["mac"]
    if not hmac.compare_digest(mac, hello_mac(tok, nonce)):
        raise NotOurServer(f"the server on port {port} is not this user's "
                           f"Ember — refusing to send it the token")
    req = urllib.request.Request(
        f"{base}/api/launch-code", method="POST", data=b"{}",
        headers={"Content-Type": "application/json", "X-Devtools-Token": tok})
    with opener.open(req, timeout=3) as r:
        code = json.loads(r.read())["code"]
    return f"{base}/launch?c={code}"


def print_launch_url(port):
    """Launcher helper: print a one-time /launch URL. Returns an exit code."""
    try:
        print(launch_url(port))
    except NotOurServer as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as e:
        print(f"error: could not get a login code from http://127.0.0.1:{port}: {e}",
              file=sys.stderr)
        return 1
    return 0


def quick_edit_off():
    """Windows: a mouse click in the console starts a QuickEdit selection,
    which blocks every write to it — and each request logs a line, so the
    whole server hangs until Esc. Turn QuickEdit off for this console."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetStdHandle.restype = wintypes.HANDLE
    k32.GetStdHandle.argtypes = [wintypes.DWORD]
    k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    h = k32.GetStdHandle(wintypes.DWORD(-10).value)         # STD_INPUT_HANDLE
    mode = wintypes.DWORD()
    if k32.GetConsoleMode(h, ctypes.byref(mode)):           # False: no console
        # clear ENABLE_QUICK_EDIT_MODE; ENABLE_EXTENDED_FLAGS makes it stick
        k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)


def kill_stale_server(port):
    """A wedged previous instance still holding the port blocks startup until
    reboot; kill it — but only if it is one of our own server runs."""
    if shutil.which("lsof") is None or shutil.which("ps") is None:   # Windows
        return
    try:
        out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                             capture_output=True, text=True, timeout=5).stdout
    except subprocess.SubprocessError:
        return
    for pid_s in out.split():
        pid = int(pid_s)
        if pid == os.getpid():
            continue
        try:
            # -ww: Linux ps cuts the command at 80 columns without a tty
            cmd = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True,
                                 timeout=5).stdout.strip()
        except subprocess.SubprocessError:
            continue
        # "server.py" = this file; "--server" = the frozen window build
        if "server.py" not in cmd and "--server" not in cmd:
            sys.exit(f"error: port {port} is held by another program "
                     f"(pid {pid}: {cmd[:100]}) — pass --port to pick another")
        print(f"killing stale Ember server on port {port} (pid {pid})",
              file=sys.stderr)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class Server(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind does a reverse-DNS getfqdn() between bind and
        # listen: seconds of refused connections where DNS is slow (CI Macs)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main():
    ap = argparse.ArgumentParser(description="Ember — a workspace for Claude Code")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3456)))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--root", default=str(CLAUDE_ROOT))
    ap.add_argument("--launch-url", action="store_true",
                    help="print a one-time login URL for a running server, then exit")
    args = ap.parse_args()
    if args.launch_url:
        sys.exit(print_launch_url(args.port))

    global SERVER_PORT, SERVER_TOKEN, ALLOWED_HOSTS
    setup_logging()
    SERVER_PORT = args.port
    SERVER_TOKEN = load_token()
    ALLOWED_HOSTS = ALLOWED_HOSTS | {args.host.lower()}
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding beyond loopback exposes a shell to your "
              "network — anyone with the token gets code execution.",
              file=sys.stderr)
    Handler.root = Path(args.root).expanduser()
    if not projects_dir(Handler.root).is_dir():
        sys.exit(f"error: {Handler.root}/projects not found — is this a Claude Code machine?")
    VIZ_DIR.mkdir(exist_ok=True)
    threading.Thread(target=compute_baseline, args=(Handler.root,), daemon=True).start()

    markers = inherited_session_markers()
    if markers:
        print(f"note: started from inside a Claude Code session; scrubbing "
              f"{len(markers)} session marker(s) from spawned terminals so "
              f"their transcripts are saved", file=sys.stderr)

    try:
        srv = Server((args.host, args.port), Handler)
    except OSError:
        kill_stale_server(args.port)
        srv = None
        for _ in range(20):                 # up to 10s for SIGTERM to land
            try:
                srv = Server((args.host, args.port), Handler)
                break
            except OSError:
                time.sleep(0.5)
        if srv is None:
            raise
    quick_edit_off()
    # never print the token: this output is often redirected to a log file.
    # ASCII only: a redirected Windows stdout is cp1252 and can't encode "→"
    py = "python" if os.name == "nt" else "python3"
    print(f"Ember running on http://{args.host}:{args.port}/ "
          f"- leave this window open (Ctrl+C stops it)")
    print(f"  to log in, run in another terminal and open the link it prints:\n"
          f"    {py} server.py --launch-url --port {args.port}")
    print(f"  (root: {Handler.root}; token file: {TOKEN_FILE})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
