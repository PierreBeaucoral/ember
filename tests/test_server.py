"""Regression tests for Ember's server.

Run:  python3 -m pytest tests/ -q
Covers the JSONL parsing invariants (usage dedup, tool pairing, sidechains,
compaction), the 5h-block reconstruction, path-safety guards, and the HTTP
auth/CSRF layer against a live server on an ephemeral port.
"""
import importlib.util
import json
import re
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("cdl_server", HERE.parent / "server.py")
srv = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(srv)


# ---------------------------------------------------------------- fixtures

def rec_user(text, ts="2026-07-28T10:00:00.000Z", sidechain=False, uuid="u1"):
    return {"type": "user", "isSidechain": sidechain, "uuid": uuid, "timestamp": ts,
            "message": {"role": "user", "content": text}, "cwd": "/Users/x/proj",
            "version": "2.1.219", "gitBranch": "main"}


def rec_assistant(blocks, rid="req_1", ts="2026-07-28T10:00:05.000Z",
                  usage=None, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "requestId": rid,
            "timestamp": ts,
            "message": {"role": "assistant", "model": "claude-fable-5",
                        "content": blocks,
                        "usage": usage or {"input_tokens": 10, "output_tokens": 100,
                                           "cache_read_input_tokens": 1000,
                                           "cache_creation_input_tokens": 200}}}


def rec_tool_result(tool_use_id, content, tur=None, ts="2026-07-28T10:00:10.000Z"):
    r = {"type": "user", "isSidechain": False, "timestamp": ts,
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]}}
    if tur is not None:
        r["toolUseResult"] = tur
    return r


def write_session(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


@pytest.fixture()
def session_file(tmp_path):
    f = tmp_path / "proj" / "abc123.jsonl"
    write_session(f, [
        {"type": "queue-operation", "operation": "enqueue"},
        rec_user("hello world"),
        rec_assistant([{"type": "thinking", "thinking": "let me think"}], rid="req_1"),
        # same requestId again (multi-block response) — usage must count ONCE
        rec_assistant([{"type": "text", "text": "the answer"}], rid="req_1"),
        rec_assistant([{"type": "tool_use", "id": "tu_1", "name": "Bash",
                        "input": {"command": "echo hi"}}], rid="req_2",
                      ts="2026-07-28T10:01:00.000Z"),
        rec_tool_result("tu_1", "ignored",
                        tur={"stdout": "hi", "stderr": "warn!", "interrupted": False}),
        rec_assistant([{"type": "tool_use", "id": "tu_2", "name": "Edit",
                        "input": {"file_path": "/a.py", "old_string": "x",
                                  "new_string": "y"}}], rid="req_3",
                      ts="2026-07-28T10:02:00.000Z"),
        rec_tool_result("tu_2", "ok",
                        tur={"filePath": "/a.py", "oldString": "x", "newString": "y",
                             "structuredPatch": [{"oldStart": 1, "oldLines": 1,
                                                  "newStart": 1, "newLines": 1,
                                                  "lines": ["-x", "+y"]}]}),
        rec_user("sidechain msg", sidechain=True),
        "{not valid json",
        {"type": "custom-title", "customTitle": "My Test Session", "leafUuid": "u1"},
    ])
    return f


# ---------------------------------------------------------------- parsing

def test_usage_deduped_by_request(session_file):
    s = srv.parse_session(session_file)
    assert s["totals"]["requests"] == 3          # req_1 counted once, not twice
    assert s["totals"]["output_tokens"] == 300
    assert s["totals"]["peak_context"] == 1210


def test_timeline_kinds_and_title(session_file):
    s = srv.parse_session(session_file)
    kinds = [e["kind"] for e in s["entries"]]
    assert kinds == ["user", "thinking", "assistant", "tool", "tool"]
    assert s["title"] == "My Test Session"       # custom-title wins over first prompt
    assert s["model"] == "claude-fable-5"
    assert s["sidechain_msgs"] == 1              # skipped from the main timeline


def test_tool_result_pairing(session_file):
    s = srv.parse_session(session_file)
    bash = next(e for e in s["entries"] if e["kind"] == "tool" and e["name"] == "Bash")
    assert "hi" in bash["result"] and "[stderr]" in bash["result"]
    edit = next(e for e in s["entries"] if e["kind"] == "tool" and e["name"] == "Edit")
    assert edit["patch"][0]["lines"] == ["-x", "+y"]
    assert edit["file_path"] == "/a.py"


def test_sidechain_included_for_subagents(tmp_path):
    f = tmp_path / "agent.jsonl"
    write_session(f, [rec_user("agent prompt", sidechain=True),
                      rec_assistant([{"type": "text", "text": "done"}], sidechain=True)])
    assert len(srv.parse_session(f)["entries"]) == 0            # main view: skipped
    s = srv.parse_session(f, include_sidechain=True)            # subagent view: kept
    assert [e["kind"] for e in s["entries"]] == ["user", "assistant"]
    assert s["title"] == "agent prompt"


def test_title_falls_back_to_first_prompt(tmp_path):
    f = tmp_path / "s.jsonl"
    write_session(f, [rec_user("first prompt here"),
                      rec_assistant([{"type": "text", "text": "hi"}])])
    assert srv.parse_session(f)["title"] == "first prompt here"


def test_compaction_detected_on_context_drop(tmp_path):
    big = {"input_tokens": 10, "output_tokens": 5,
           "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 0}
    small = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 40_000, "cache_creation_input_tokens": 0}
    f = tmp_path / "s.jsonl"
    write_session(f, [
        rec_assistant([{"type": "text", "text": "a"}], rid="r1", usage=big),
        rec_assistant([{"type": "text", "text": "b"}], rid="r2", usage=small,
                      ts="2026-07-28T11:00:00.000Z"),
    ])
    series = srv.parse_session(f)["context_series"]
    assert not series[0].get("compaction") and series[1].get("compaction")


def test_series_points_at_first_entry_of_request(session_file):
    # the context chart jumps to entries[point["entry"]] on click
    s = srv.parse_session(session_file)
    kinds = [(s["entries"][p["entry"]]["kind"]) for p in s["context_series"]]
    assert kinds == ["thinking", "tool", "tool"]
    assert s["entries"][s["context_series"][2]["entry"]]["name"] == "Edit"


def test_malformed_lines_skipped(session_file):
    # the "{not valid json" line must not break anything (implicitly covered
    # above, asserted explicitly here)
    assert srv.parse_session(session_file)["entries"]


# ---------------------------------------------------------------- usage blocks

def rec_attach(att, ts="2026-07-28T10:00:00.500Z"):
    return {"type": "attachment", "isSidechain": False, "timestamp": ts, "attachment": att}


def test_injected_messages_are_not_prompts(tmp_path):
    """Skill bodies, agent reports, commands, hooks, queued prompts and API
    errors each get their own kind; only real prompts stay `user`."""
    f = tmp_path / "p" / "s.jsonl"
    write_session(f, [
        rec_attach({"type": "hook_non_blocking_error", "hookName": "SessionStart:startup",
                    "exitCode": 127, "stderr": "node: command not found", "command": "node x.js"}),
        rec_attach({"type": "instructions", "files": [
            {"path": "/u/.claude/CLAUDE.md", "type": "User", "content": "x" * 400},
            {"path": "/p/CLAUDE.md", "type": "Project", "content": "y" * 80}]}),
        rec_user("<command-message>improve</command-message>\n<command-name>/improve</command-name>\n"
                 "<command-args>config audit</command-args>"),
        dict(rec_user("# Retrospective\n\nReview the conversation"), isMeta=True),
        dict(rec_user("<local-command-caveat>Caveat</local-command-caveat>"), isMeta=True),
        dict(rec_user('Another Claude session sent a message:\n<agent-message from="a1">report'),
             isMeta=True, origin={"kind": "peer", "name": "Explore"}),
        rec_attach({"type": "queued_command", "prompt": "also check X", "origin": {"kind": "human"}}),
        rec_attach({"type": "queued_command", "origin": {"kind": "task-notification"},
                    "prompt": "<task-notification><summary>Agent \"x\" finished</summary></task-notification>"}),
        rec_user("<task-notification><summary>Agent \"y\" finished</summary></task-notification>"),
        rec_attach({"type": "file", "displayPath": "a.py",
                    "content": {"type": "text", "file": {"content": "z" * 40}}}),
        rec_attach({"type": "total_tokens_reminder", "text": "ignored"}),
        {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
         "timestamp": "2026-07-28T10:00:06.000Z",
         "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "Rate limited"}]}},
    ])
    d = srv.parse_session(f)
    E = d["entries"]
    kinds = [(e["kind"], e.get("sub") or e.get("cmd") or e.get("name")) for e in E]
    assert kinds == [("hook", "SessionStart:startup"), ("command", "/improve"), ("meta", "skill"),
                     ("meta", "agent"), ("user", None), ("meta", "agent"), ("meta", "agent"),
                     ("assistant", None)]
    assert E[1]["args"] == "config audit"
    assert E[2]["title"] == "Skill /improve"
    assert E[3]["title"] == "Report from Explore"
    assert E[4]["queued"] is True
    assert E[5]["title"] == 'Agent "x" finished'
    assert E[6]["title"] == 'Agent "y" finished'
    assert E[7]["api_error"] == "rate_limit"
    assert d["model"] is None                     # <synthetic> is not a model
    ctx = [(c["cat"], c["label"], c["tok"], c["entry"]) for c in d["context"]]
    assert ctx == [("claude-md", "/u/.claude/CLAUDE.md", 100, 1), ("claude-md", "/p/CLAUDE.md", 20, 1),
                   ("mentions", "a.py", 10, 7)]


def test_tool_duration_and_token_estimates(tmp_path):
    f = tmp_path / "p" / "s.jsonl"
    write_session(f, [
        rec_user("go"),
        rec_assistant([{"type": "text", "text": "a" * 40},
                       {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}],
                      ts="2026-07-28T10:00:05.000Z",
                      usage={"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
                             "cache_creation_input_tokens": 4,
                             "cache_creation": {"ephemeral_1h_input_tokens": 4}}),
        rec_tool_result("t1", "x", tur={"stdout": "b" * 400}, ts="2026-07-28T10:00:07.500Z"),
    ])
    d = srv.parse_session(f)
    tool = d["entries"][2]
    assert tool["dur"] == 2.5
    assert tool["tok"] == len(json.dumps({"command": "ls"})) // 4 + 100
    assert d["entries"][1]["tok"] == 10
    assert d["totals"]["cache_ttl"] == 3600
    assert d["totals"]["last_request_ts"] == "2026-07-28T10:00:05.000Z"
    assert d["context_series"][0]["model"] == "claude-fable-5"


def test_subagent_stats_before_expanding(tmp_path):
    f = tmp_path / "p" / "s.jsonl"
    write_session(f, [rec_user("go")])
    a = tmp_path / "p" / "s" / "subagents" / "agent-a1.jsonl"
    write_session(a, [
        rec_user("task", sidechain=True, ts="2026-07-28T10:00:00.000Z"),
        rec_assistant([{"type": "tool_use", "id": "x", "name": "Read", "input": {}}], rid="r1",
                      sidechain=True, ts="2026-07-28T10:00:30.000Z"),
        rec_assistant([{"type": "text", "text": "done"}], rid="r2", sidechain=True,
                      ts="2026-07-28T10:01:00.000Z"),
    ])
    a.with_name("agent-a1.meta.json").write_text(json.dumps({"agentType": "Explore"}))
    st = srv.parse_session(f)["subagents"][0]["stats"]
    assert (st["type"], st["model"], st["tools"], st["output"], st["peak"], st["duration"]) == \
           ("Explore", "claude-fable-5", 1, 200, 1210, 60.0)


def test_session_meta_flags_a_trailing_api_error(tmp_path):
    f = tmp_path / "p" / "s.jsonl"
    err = {"type": "assistant", "isApiErrorMessage": True, "error": "authentication_failed",
           "message": {"model": "<synthetic>", "content": []}}
    write_session(f, [rec_user("hi"), rec_assistant([{"type": "text", "text": "x"}]), err])
    assert srv.session_meta(f)["api_error"] == "authentication_failed"
    assert srv.session_meta(f)["model"] == "claude-fable-5"
    write_session(f, [rec_user("hi"), err, rec_assistant([{"type": "text", "text": "x"}], rid="r9")])
    os.utime(f, (time.time() + 5, time.time() + 5))      # bust the meta cache
    assert srv.session_meta(f)["api_error"] is None


def test_usage_history_by_day_project_and_model(tmp_path):
    from datetime import datetime, timedelta
    now = datetime.now()
    iso = lambda d: d.astimezone().isoformat()
    proj = tmp_path / "projects"
    write_session(proj / "-a" / "s1.jsonl", [
        rec_assistant([], rid="r1", ts=iso(now - timedelta(days=1))),
        rec_assistant([], rid="r2", ts=iso(now - timedelta(days=1))),
        rec_assistant([], rid="r3", ts=iso(now - timedelta(days=200)))])     # outside the window
    write_session(proj / "-a" / "s1" / "subagents" / "agent-x.jsonl",
                  [rec_assistant([], rid="r4", ts=iso(now), sidechain=True)])
    write_session(proj / "-b" / "s2.jsonl", [rec_assistant([], rid="r5", ts=iso(now))])
    h = srv.usage_history(tmp_path, days=30)
    assert sum(d["output"] for d in h["days"].values()) == 400
    assert h["days"][(now - timedelta(days=1)).strftime("%Y-%m-%d")] == {"output": 200, "requests": 2}
    assert h["projects"] == [{"slug": "-a", "output": 300}, {"slug": "-b", "output": 100}]
    assert h["by_model"] == {"claude-fable-5": 400}
    assert srv.usage_history(tmp_path, days=9999)["span"] == 366


def test_blocks_split_on_5h_gap():
    h = 3600
    recs = [(1000 * h, 50, "m"), (1000 * h + 2 * h, 30, "m"),   # block 1
            (1000 * h + 9 * h, 20, "m")]                        # >5h later: block 2
    blocks = srv.blocks_from_records(recs)
    assert len(blocks) == 2
    assert blocks[0][2] == 80 and blocks[1][2] == 20
    assert blocks[0][1] - blocks[0][0] == 5 * h


# ---------------------------------------------------------------- path safety

def test_safe_home_path_blocks_escape():
    with pytest.raises(ValueError):
        srv.safe_home_path("/etc")
    with pytest.raises(ValueError):
        srv.safe_home_path(str(Path.home()) + "/../../etc")
    assert srv.safe_home_path(str(Path.home())) == Path.home().resolve()


def test_safe_project_path_blocks_traversal(tmp_path):
    with pytest.raises(ValueError):
        srv.safe_project_path(tmp_path, "../evil")
    with pytest.raises(ValueError):
        srv.safe_project_path(tmp_path, ".hidden")


def test_fs_listing_hides_dotfiles_except_dot_claude(tmp_path, monkeypatch):
    home = Path.home()
    d = home / ".cdl-test-tmp"
    d.mkdir(exist_ok=True)
    try:
        (d / ".secret").write_text("x")
        (d / "visible.txt").write_text("x")
        names = [e["name"] for e in srv.fs_listing(str(d))["entries"]]
        assert "visible.txt" in names and ".secret" not in names
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


# ---------------------------------------------------------------- HTTP layer

@pytest.fixture(scope="module")
def http_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("claude-root")
    (root / "projects" / "-Users-x-proj").mkdir(parents=True)
    write_session(root / "projects" / "-Users-x-proj" / "s1.jsonl",
                  [rec_user("hello"), rec_assistant([{"type": "text", "text": "hi"}])])
    srv.Handler.root = root
    srv.SERVER_TOKEN = "a" * 48
    server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def fetch(url, method="GET", headers=None, body=None):
    req = urllib.request.Request(url, method=method, data=body,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_api_requires_token(http_server):
    code, _ = fetch(http_server + "/api/projects")
    assert code == 401
    code, body = fetch(http_server + "/api/projects",
                       headers={"X-Devtools-Token": "a" * 48})
    assert code == 200 and b"-Users-x-proj" in body


def test_post_requires_json_content_type(http_server):
    # text/plain would be a CSRF-able "simple request" — must be refused even
    # with a valid token
    code, _ = fetch(http_server + "/api/term/start", method="POST",
                    headers={"X-Devtools-Token": "a" * 48,
                             "Content-Type": "text/plain"},
                    body=b'{"kind":"shell"}')
    assert code == 403


def test_post_rejects_foreign_origin(http_server):
    code, _ = fetch(http_server + "/api/term/start", method="POST",
                    headers={"X-Devtools-Token": "a" * 48,
                             "Content-Type": "application/json",
                             "Origin": "https://evil.example"},
                    body=b'{"kind":"shell"}')
    assert code == 403


def test_launch_exchanges_token_for_cookie(http_server):
    req = urllib.request.Request(http_server + "/launch?k=" + "a" * 48)
    # don't follow the redirect: inspect it
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(req, timeout=5)
        assert False, "expected 302"
    except urllib.error.HTTPError as e:
        assert e.code == 302
        assert e.headers["Location"] == "/"
        assert "cdl=" + "a" * 48 in e.headers["Set-Cookie"]
        assert "SameSite=Strict" in e.headers["Set-Cookie"]
    code, _ = fetch(http_server + "/launch?k=wrong")
    assert code == 403


def test_launch_code_is_single_use(http_server):
    H = {"X-Devtools-Token": "a" * 48, "Content-Type": "application/json"}
    code, body = fetch(http_server + "/api/launch-code", "POST", H, b"{}")
    assert code == 200
    c = json.loads(body)["code"]

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(http_server + "/launch?c=" + c, timeout=5)
    assert e.value.code == 302 and "cdl=" + "a" * 48 in e.value.headers["Set-Cookie"]
    code, _ = fetch(http_server + "/launch?c=" + c)      # replay refused
    assert code == 403
    code, _ = fetch(http_server + "/api/launch-code", "POST",
                    {"Content-Type": "application/json"}, b"{}")
    assert code == 401                                   # minting needs the token


def test_hello_proves_token_without_revealing_it(http_server):
    code, body = fetch(http_server + "/hello?n=" + "ab" * 16)
    assert code == 200
    mac = json.loads(body)["mac"]
    assert mac == srv.hello_mac("a" * 48, "ab" * 16) and "a" * 48 not in body.decode()
    assert fetch(http_server + "/hello?n=zz")[0] == 400


def test_cookie_authenticates(http_server):
    code, body = fetch(http_server + "/api/projects",
                       headers={"Cookie": "cdl=" + "a" * 48})
    assert code == 200 and b"-Users-x-proj" in body
    code, _ = fetch(http_server + "/api/projects",
                    headers={"Cookie": "cdl=wrong"})
    assert code == 401


def test_sensitive_filenames_blocked():
    for name in (".env", ".env.local", "credentials", "aws-credentials.json",
                 "hosts.yml", "id_rsa", "server.pem", "my_secret.yaml",
                 ".netrc", "API_TOKEN.txt", "key.p12"):
        assert srv.is_sensitive(name), name
    for name in ("notes.md", "analysis.R", "graph.html", "settings.json",
                 "data.csv", "main.tex"):
        assert not srv.is_sensitive(name), name


def test_page_csp_pins_the_inline_script(http_server):
    # the CSP hash must match the served inline <script>, or the page is blank;
    # and no other inline script may run (an XSS payload would be one)
    import base64, hashlib, re
    with urllib.request.urlopen(http_server + "/", timeout=5) as r:
        body, csp = r.read(), r.headers["Content-Security-Policy"]
        assert r.headers["X-Content-Type-Options"] == "nosniff"
    inline = re.search(rb"<script>(.*?)</script>", body, re.S).group(1)
    inline = inline.replace(b"\r\n", b"\n")      # as the browser sees it
    want = base64.b64encode(hashlib.sha256(inline).digest()).decode()
    assert f"'sha256-{want}'" in csp
    assert "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";")[0]
    assert "frame-ancestors 'none'" in csp


def test_page_csp_hash_ignores_crlf():
    # a Git-for-Windows checkout serves index.html with CRLF; the browser hashes
    # the script after turning CRLF into LF, so a raw-bytes hash blocks the page
    lf = b"<html><script>let a = 1;\nlet b = 2;\r</script></html>"
    assert srv.page_csp(lf.replace(b"\n", b"\r\n")) == srv.page_csp(lf.replace(b"\r", b"\n"))


def test_html_preview_is_sandboxed_even_top_level(http_server):
    home = Path.home()
    f = home / ".cdl-test-preview.html"
    f.write_text("<script>1</script>")
    try:
        req = urllib.request.Request(
            http_server + "/api/fs/file?path=" + urllib.parse.quote(str(f)),
            headers={"X-Devtools-Token": "a" * 48})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")
    finally:
        f.unlink()


def test_sensitive_file_refused_over_http(http_server, tmp_path):
    home = Path.home()
    f = home / ".cdl-test-credentials.json"
    f.write_text('{"api_key":"do-not-serve"}')
    try:
        code, body = fetch(http_server + "/api/fs/file?path=" +
                           urllib.parse.quote(str(f)),
                           headers={"X-Devtools-Token": "a" * 48})
        assert code == 403 and b"secrets" in body
        assert b"do-not-serve" not in body
    finally:
        f.unlink()


def test_sensitive_marked_unviewable_in_listing():
    home = Path.home()
    d = home / ".cdl-test-listing"
    d.mkdir(exist_ok=True)
    try:
        (d / "secret_keys.json").write_text("{}")
        (d / "report.md").write_text("hi")
        entries = {e["name"]: e for e in srv.fs_listing(str(d))["entries"]}
        assert entries["secret_keys.json"]["viewable"] is False
        assert entries["secret_keys.json"]["sensitive"] is True
        assert entries["report.md"]["viewable"] is True
    finally:
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def test_foreign_host_header_refused(http_server):
    # DNS-rebinding guard: a rebound page presents its own hostname
    code, _ = fetch(http_server + "/api/projects",
                    headers={"X-Devtools-Token": "a" * 48, "Host": "evil.example"})
    assert code == 403
    port = http_server.rsplit(":", 1)[1]
    code, _ = fetch(http_server + "/api/projects",
                    headers={"X-Devtools-Token": "a" * 48,
                             "Host": "127.0.0.1:" + port})
    assert code == 200


@pytest.fixture
def log_lines():
    import logging
    lines = []
    h = logging.Handler()
    h.emit = lambda r: lines.append(r.getMessage())
    srv.LOG.addHandler(h)
    yield lines
    srv.LOG.removeHandler(h)


def test_log_redacts_tokens(log_lines):
    h = srv.Handler.__new__(srv.Handler)
    srv.Handler.log_message(h, '"GET /api/viz?token=%s HTTP/1.1"', "a" * 48)
    assert "a" * 48 not in log_lines[-1] and "[redacted]" in log_lines[-1]
    srv.Handler.log_message(h, '"GET /launch?k=%s HTTP/1.1"', "b" * 48)
    srv.Handler.log_message(h, '"GET /launch?c=%s HTTP/1.1"', "c" * 32)
    assert not any("b" * 48 in l or "c" * 32 in l for l in log_lines)


def test_only_errors_and_slow_requests_are_logged(http_server, log_lines):
    fetch(http_server + "/api/projects", headers={"X-Devtools-Token": "a" * 48})
    assert not any("/api/projects" in l for l in log_lines)       # a normal poll
    fetch(http_server + "/api/nope", headers={"X-Devtools-Token": "a" * 48})
    assert any("/api/nope" in l and " 404 " in l for l in log_lines)
    code, body = fetch(http_server + "/api/health", headers={"X-Devtools-Token": "a" * 48})
    h = json.loads(body)
    assert code == 200 and h["version"] == srv.VERSION and "a" * 48 not in body.decode()
    assert any("/api/nope" in e["req"] for e in h["recent_errors"])


def test_token_and_state_live_outside_repo():
    repo = Path(srv.__file__).resolve().parent
    assert repo not in srv.TOKEN_FILE.parents, "token must not sit in the git repo"
    assert repo not in srv.STATE_FILE.parents, "state must not sit in the git repo"
    assert "claude-devtools" in str(srv.APP_DIR)


# ---------------------------------------------------------------- CLI discovery

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX login shell / PATH semantics")


@posix_only
def test_login_path_includes_user_bin_dirs(monkeypatch):
    """An app launched from Finder/.desktop inherits a minimal PATH; the login
    PATH must still surface the usual install dirs."""
    srv._env_cache.clear()
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    p = srv.login_path().split(os.pathsep)
    assert "/usr/bin" in p
    for d in p:
        assert os.path.isdir(d)          # no phantom entries
    assert len(p) == len(set(p))         # deduplicated
    srv._env_cache.clear()


def test_find_claude_prefers_explicit_override(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    srv._env_cache.clear()
    monkeypatch.setenv("CLAUDE_BIN", str(fake))
    assert srv.find_claude() == str(fake)
    srv._env_cache.clear()


def test_claude_kind_never_falls_back_to_a_shell(monkeypatch):
    """Regression: `+ claude` used to open a plain shell when the binary was
    off-PATH, instead of reporting that it wasn't found."""
    srv._env_cache["claude"] = None
    srv._env_cache["path_raw"] = ["/usr/bin", "/bin"]
    try:
        with pytest.raises(FileNotFoundError) as e:
            srv.start_term("claude", None)
        assert "CLAUDE_BIN" in str(e.value)
    finally:
        srv._env_cache.clear()


@posix_only
def test_terminal_env_carries_login_path(monkeypatch):
    srv._env_cache["claude"] = "/nonexistent/claude"
    srv._env_cache["path_raw"] = ["/usr/bin", "/bin"]
    try:
        captured = {}

        class FakeTerm:
            def __init__(self, argv, cwd, env=None, **kw):
                captured["argv"] = argv
                captured["env"] = env
                self.id, self.alive = "x", True

        monkeypatch.setattr(srv, "PosixTerm", FakeTerm)
        monkeypatch.setattr(srv, "WindowsTerm", FakeTerm)
        srv.TERMS.clear()
        srv.start_term("claude", None)
        assert captured["argv"][0] == "/nonexistent/claude"
        assert captured["env"]["PATH"] == "/usr/bin:/bin"
        assert captured["env"]["CLAUDE_DEVTOOLS_UI"] == "1"
    finally:
        srv.TERMS.clear()
        srv._env_cache.clear()


@posix_only
def test_claude_gets_ember_prompt_unless_claude_md_has_it(monkeypatch, tmp_path):
    """Sessions started from Ember learn about Viz/Plan/comments without the
    user editing CLAUDE.md, and nobody pays for the block twice."""
    srv._env_cache["claude"] = "/nonexistent/claude"
    srv._env_cache["path_raw"] = ["/usr/bin", "/bin"]
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path)
    captured = []

    class FakeTerm:
        def __init__(self, argv, cwd, env=None, **kw):
            captured.append(argv)
            self.id, self.alive = str(len(captured)), True

    monkeypatch.setattr(srv, "PosixTerm", FakeTerm)
    try:
        srv.TERMS.clear()
        srv.start_term("claude", None, prompt="/graphify")
        srv.start_term("resume", None, session_id="abc")
        srv.start_term("shell", None)
        new, resume, shell = captured
        assert new[1:3] == ["--append-system-prompt", srv.EMBER_PROMPT]
        assert new[-1] == "/graphify"                 # positional prompt stays last
        assert resume[1:3] == ["--append-system-prompt", srv.EMBER_PROMPT]
        assert resume[-2:] == ["--resume", "abc"]
        assert "--append-system-prompt" not in shell
        assert '"' not in srv.EMBER_PROMPT            # survives ConPTY quoting
        (tmp_path / "CLAUDE.md").write_text("When `CLAUDE_DEVTOOLS_UI=1` is set ...")
        srv.TERMS.clear()
        srv.start_term("claude", None)
        assert "--append-system-prompt" not in captured[-1]
    finally:
        srv.TERMS.clear()
        srv._env_cache.clear()


def test_setup_status_reports_first_run_needs(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(srv, "CLAUDE_ROOT", home / ".claude")
    srv._env_cache["claude"] = None
    srv._env_cache["path_raw"] = ["/nonexistent"]
    try:
        st = srv.setup_status(home / ".claude")
        assert st == {"claude": False, "logged_in": None, "projects": False,
                      "aware": "prompt", "practice": None}
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"x": 1}}))
        (home / ".claude" / "projects" / "-Users-x-proj").mkdir()
        st = srv.setup_status(home / ".claude")
        assert st["logged_in"] is True and st["projects"] is True
    finally:
        srv._env_cache.clear()


def test_practice_project_creates_and_ticks(monkeypatch, tmp_path):
    home = tmp_path / "home"
    root = home / ".claude"
    (root / "projects").mkdir(parents=True)
    viz = tmp_path / "viz"
    viz.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))   # Path.home() on Windows
    monkeypatch.setattr(srv, "VIZ_DIR", viz)
    st = srv.practice_create()
    d = home / "Ember-practice"
    assert st == {"cwd": str(d), "done": []}
    assert (d / "coffee.csv").read_text().startswith("month,")
    plan = srv.plan_read(str(d))
    assert plan["total"] == len(srv.PRACTICE_STEPS) and plan["done"] == 0

    # a user edit survives a second create
    (d / "coffee.csv").write_text("mine")
    srv.practice_create()
    assert (d / "coffee.csv").read_text() == "mine"

    # a transcript for that folder, a figure, a saved comment: all three tick
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(d))
    (root / "projects" / slug).mkdir()
    (root / "projects" / slug / "s.jsonl").write_text("{}\n")
    (viz / "cups-per-month.png").write_bytes(b"png")
    (viz / ".review").mkdir()
    (viz / ".review" / "cups-per-month.png.json").write_text("{}")
    st = srv.practice_sync(root)
    assert st["done"] == ["asked", "chart", "comment"]
    plan = srv.plan_read(str(d))
    assert plan["done"] == 3
    assert srv.practice_sync(root)["done"] == st["done"]     # idempotent


def test_child_env_scrubs_parent_session_markers():
    """Regression: a dashboard started from inside a Claude Code session leaked
    CLAUDE_CODE_CHILD_SESSION into terminals, which turns transcript saving OFF
    — the sessions this tool exists to display would never be recorded."""
    parent = {
        "HOME": "/Users/x", "PATH": "/usr/bin",
        "CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1",
        "CLAUDE_CODE_SESSION_ID": "abc", "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_AGENT_SDK_VERSION": "1.2", "CLAUDE_PID": "42",
        "CLAUDE_EFFORT": "high", "ANTHROPIC_BASE_URL": "http://internal",
        # genuine user config that must survive
        "CLAUDE_CONFIG_DIR": "/Users/x/.claude",
        "CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_API_KEY": "sk-user",
    }
    env = srv.child_environment(parent, extra={"CLAUDE_DEVTOOLS_UI": "1"})
    for gone in ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                 "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_PID", "CLAUDE_EFFORT",
                 "ANTHROPIC_BASE_URL"):
        assert gone not in env, gone
    assert env["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["ANTHROPIC_API_KEY"] == "sk-user"
    assert env["HOME"] == "/Users/x"
    assert env["CLAUDE_DEVTOOLS_UI"] == "1"


def test_child_env_keeps_user_base_url_when_not_nested():
    """Outside a Claude session, ANTHROPIC_BASE_URL is the user's own setting."""
    env = srv.child_environment({"HOME": "/h", "ANTHROPIC_BASE_URL": "https://proxy"})
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy"


def test_inherited_session_markers_detection():
    assert srv.inherited_session_markers({"CLAUDECODE": "1", "HOME": "/h"}) \
        == ["CLAUDECODE"]
    assert srv.inherited_session_markers({"HOME": "/h"}) == []


# ---------------------------------------------------------------- windows backend

def test_conpty_command_line_quoting():
    import winconpty
    assert winconpty.build_command_line(["claude"]) == "claude"
    line = winconpty.build_command_line(["C:\\Program Files\\claude.exe", "/graphify"])
    assert '"C:\\Program Files\\claude.exe"' in line and "/graphify" in line
    # an argument with quotes must stay one argument
    assert winconpty.build_command_line(["x", 'a "b" c']).count('"') >= 2


def test_conpty_environment_block():
    import winconpty
    blk = winconpty.build_environment_block({"A": "1", "B": "two",
                                             "BAD=KEY": "x", "": "y"})
    text = blk.decode("utf-16-le")
    assert text.endswith("\0\0")
    entries = [e for e in text.rstrip("\0").split("\0") if e]
    assert entries == ["A=1", "B=two"]        # malformed keys dropped, sorted


def test_conpty_reports_unavailable_off_windows():
    import winconpty
    if sys.platform == "win32":               # pragma: no cover
        pytest.skip("this assertion is for non-Windows hosts")
    assert winconpty.AVAILABLE is False
    assert winconpty.unsupported_reason() == "not running on Windows"
    with pytest.raises(NotImplementedError):
        winconpty.ConPtyProcess(["cmd.exe"], None)


def test_terminal_backend_selection():
    # POSIX hosts must keep using the pty implementation
    if srv.HAS_PTY:
        assert issubclass(srv.PosixTerm, srv.Term)
        assert srv.HAS_TERMINAL is True
    assert issubclass(srv.WindowsTerm, srv.Term)
    for hook in ("_spawn", "_read", "_write", "_set_size", "_hangup",
                 "_terminate"):
        assert hasattr(srv.PosixTerm, hook) and hasattr(srv.WindowsTerm, hook)


def test_session_endpoint_roundtrip(http_server):
    code, body = fetch(http_server + "/api/session?project=-Users-x-proj&id=s1",
                       headers={"X-Devtools-Token": "a" * 48})
    assert code == 200
    data = json.loads(body)
    assert data["title"] == "hello"
    assert [e["kind"] for e in data["entries"]] == ["user", "assistant"]


# ------------------------------------------------- terminal scrollback offsets
#
# The stream hands clients an ABSOLUTE byte offset while `buf` keeps only a
# bounded tail. Conflating the two froze the terminal at exactly SCROLLBACK_CAP:
# once a caught-up client's position equalled len(buf), trimming the front kept
# len(buf) pinned at the cap, so `buf[pos:]` stayed empty forever and no further
# output ever reached the browser.

CAP = srv.SCROLLBACK_CAP


def _detached_term(buf, discarded=0):
    """A Term with its buffer state set directly — no PTY, no pump thread."""
    t = srv.Term.__new__(srv.Term)
    t.buf = bytearray(buf)
    t.discarded = discarded
    return t


class _ScriptedTerm(srv.Term):
    """Real Term with the PTY transport replaced by a scripted byte stream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        super().__init__(["/bin/false"], str(HERE), cols=80, rows=24, env={})

    def _spawn(self, argv, cwd, env, cols, rows):
        pass

    def _read(self):
        return self._chunks.pop(0) if self._chunks else b""   # b'' == EOF

    def _write(self, data):
        pass

    def _set_size(self, cols, rows):
        pass

    def _hangup(self):
        pass

    def _terminate(self):
        pass


def test_pump_trims_scrollback_and_accounts_for_it():
    chunks = 8                               # 800 KB, comfortably past the cap
    total = chunks * 100_000
    t = _ScriptedTerm([b"A" * 100_000] * chunks)
    for _ in range(100):
        if not t.alive:
            break
        time.sleep(0.02)
    assert not t.alive, "pump never reached EOF"
    assert len(t.buf) == CAP                 # tail is bounded
    assert t.discarded == total - CAP        # ...and the loss is recorded
    assert t.produced() == total             # absolute count survives trimming


def test_slice_from_delivers_output_produced_after_a_trim():
    """The exact freeze: client caught up at the cap, then the front trims."""
    t = _detached_term(b"B" * CAP, discarded=100)   # 100 bytes aged out
    chunk, pos = t.slice_from(CAP)                  # client had consumed CAP
    assert chunk == b"B" * 100, "post-trim output was not delivered"
    assert pos == CAP + 100
    # and it does not re-deliver on the next poll
    assert t.slice_from(pos) == (b"", CAP + 100)


def test_slice_from_clamps_a_client_that_fell_behind():
    t = _detached_term(b"C" * 1000, discarded=5000)
    chunk, pos = t.slice_from(0)          # asking for bytes that aged out
    assert chunk == b"C" * 1000           # skip the gap, do not stall
    assert pos == 6000


def test_slice_from_tolerates_a_position_past_the_end():
    t = _detached_term(b"D" * 10, discarded=0)
    assert t.slice_from(999) == (b"", 10)          # no negative index, no crash


def test_slice_from_reassembles_the_stream_without_loss():
    t = _detached_term(b"", 0)
    pos, seen = 0, bytearray()
    for i in range(20):
        t.buf.extend(bytes([65 + i]) * 50)
        chunk, pos = t.slice_from(pos)
        seen.extend(chunk)
    assert bytes(seen) == bytes(t.buf)


# ---------------------------------------------------------------- plan pane

@pytest.fixture
def plan_project(tmp_path, monkeypatch):
    """A project directory that looks like $HOME so safe_home_path accepts it."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    return proj


def test_plan_parses_checkboxes_and_skips_comments_and_fences():
    items, done, total = srv.parse_plan_text(
        "# Plan\n"
        "<!-- a note\n     spanning two lines -->\n"
        "Status: DRAFT\n"
        "## Steps\n"
        "- [x] first\n"
        "- [ ] second\n"
        "  - [~] nested\n"
        "1. [ ] numbered\n"
        "```\n- [ ] inside a fence\n```\n")
    kinds = [(i["kind"], i.get("state"), i["text"]) for i in items]
    assert ("task", "done", "first") in kinds
    assert ("task", "doing", "nested") in kinds
    assert ("head", None, "Plan") in kinds
    assert ("text", None, "Status: DRAFT") in kinds
    assert not any("fence" in i["text"] for i in items)     # code blocks ignored
    assert not any("spanning" in i["text"] for i in items)  # comments ignored
    assert (done, total) == (1, 4)


def test_plan_discovery_prefers_the_live_plan_then_dated_plans(plan_project):
    root = plan_project
    (root / "PLAN.md").write_text("- [ ] root\n")
    plans = root / "quality_reports" / "plans"
    plans.mkdir(parents=True)
    (plans / "2026-01-01_old.md").write_text("- [ ] old\n")
    # no .claude/plan.md yet: the dated plan wins over PLAN.md
    assert srv.plan_candidates(root)[0].name == "2026-01-01_old.md"
    (root / ".claude" / "plan.md").write_text("- [ ] live\n")
    cands = srv.plan_candidates(root)
    assert cands[0].name == "plan.md"
    assert [c.name for c in cands[1:]] == ["2026-01-01_old.md", "PLAN.md"]


def test_plan_toggle_rewrites_only_the_marker(plan_project):
    f = plan_project / ".claude" / "plan.md"
    f.write_text("# Plan\n- [ ] alpha\n- [ ] beta\n")
    out = srv.plan_toggle(plan_project, str(f), 1, "alpha", "done")
    assert f.read_text() == "# Plan\n- [x] alpha\n- [ ] beta\n"
    assert (out["done"], out["total"]) == (1, 2)
    srv.plan_toggle(plan_project, str(f), 1, "alpha", "open")
    assert f.read_text() == "# Plan\n- [ ] alpha\n- [ ] beta\n"


def test_plan_toggle_refuses_files_outside_the_project(plan_project):
    victim = Path.home() / "secrets.md"
    victim.write_text("- [ ] do not touch\n")
    with pytest.raises(ValueError):
        srv.plan_toggle(plan_project, str(victim), 0, None, "done")
    assert victim.read_text() == "- [ ] do not touch\n"


def test_plan_toggle_refuses_when_the_line_moved(plan_project):
    f = plan_project / ".claude" / "plan.md"
    f.write_text("- [ ] alpha\n- [ ] beta\n")
    with pytest.raises(ValueError):                 # stale text from the client
        srv.plan_toggle(plan_project, str(f), 0, "beta", "done")
    with pytest.raises(ValueError):                 # not a checkbox line
        f.write_text("plain prose\n")
        srv.plan_toggle(plan_project, str(f), 0, None, "done")
    with pytest.raises(ValueError):                 # out of range
        srv.plan_toggle(plan_project, str(f), 99, None, "done")


def test_plan_toggle_preserves_crlf_and_indentation(plan_project):
    f = plan_project / ".claude" / "plan.md"
    f.write_bytes(b"# Plan\r\n  - [ ] indented\r\n")
    srv.plan_toggle(plan_project, str(f), 1, "indented", "done")
    assert f.read_bytes() == b"# Plan\r\n  - [x] indented\r\n"


# ------------------------------------------------------- end-of-session improve

def test_mangle_cwd_names_report_folders():
    assert srv.mangle_cwd("/Users/x/Docs/Cours MACRO 1/app") == \
        "-Users-x-Docs-Cours-MACRO-1-app"
    assert srv.mangle_cwd("/a/b.c") == "-a-b-c"


@pytest.mark.parametrize("cwd, slug", [
    ("/Users/x/data/session_logs", "-Users-x-data-session-logs"),          # underscore
    ("/Users/x/Thèse", "-Users-x-Th-se"),                                  # non-ASCII
    ("C:\\Users\\x\\proj", "C--Users-x-proj"),                            # Windows
    ("/Users/x/" + "d" * 240, "-Users-x-" + "d" * 190 + "-fi4zme"),          # truncated+hash
])
def test_project_dir_found_whatever_claude_code_named_it(tmp_path, cwd, slug):
    d = tmp_path / "projects" / slug
    d.mkdir(parents=True)
    write_session(d / "s.jsonl", [dict(rec_user("hi"), cwd=cwd)])
    assert srv.project_dir_for_cwd(tmp_path, cwd) == d
    assert srv.project_dir_for_cwd(tmp_path, "/nowhere") is None


def test_improve_never_runs_inside_its_own_retrospective(monkeypatch, tmp_path):
    monkeypatch.setenv("CDL_IMPROVE_RUN", "1")
    monkeypatch.setattr(srv, "find_claude", lambda: "/bin/false")
    assert srv.spawn_improve(str(tmp_path), None) is None


def test_improve_respects_the_kill_switch(monkeypatch, tmp_path):
    monkeypatch.delenv("CDL_IMPROVE_RUN", raising=False)
    monkeypatch.setenv("CDL_IMPROVE", "0")
    assert srv.improve_enabled() is False
    assert srv.spawn_improve(str(tmp_path), None) is None


def test_improve_skips_short_or_missing_transcripts(monkeypatch, tmp_path):
    monkeypatch.delenv("CDL_IMPROVE_RUN", raising=False)
    monkeypatch.delenv("CDL_IMPROVE", raising=False)
    monkeypatch.setattr(srv, "find_claude", lambda: "/bin/false")
    monkeypatch.setattr(srv, "addon_installed", lambda a: True)
    spawned = []
    monkeypatch.setattr(srv.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    short = tmp_path / "s.jsonl"
    short.write_text("{}\n")                              # far under the threshold
    assert srv.spawn_improve(str(tmp_path), short) is None
    assert srv.spawn_improve(str(tmp_path), tmp_path / "gone.jsonl") is None
    assert spawned == []


def test_new_claude_terminal_pins_its_session_id(monkeypatch, tmp_path):
    """The retrospective once analysed a distill hook's transcript instead of
    the session that closed: "newest file in the project" loses to any hook
    that runs its own claude at SessionEnd. The id is now chosen up front."""
    monkeypatch.setattr(srv, "find_claude", lambda: "/usr/bin/claude")
    seen = {}

    class FakeTerm:
        def __init__(self, argv, cwd, **kw):
            seen["argv"] = argv
            self.id, self.alive = "t", True
    monkeypatch.setattr(srv, "PosixTerm", FakeTerm)
    monkeypatch.setattr(srv, "WindowsTerm", FakeTerm)
    monkeypatch.setattr(srv, "TERMS", {})
    srv.start_term("claude", str(tmp_path))
    argv = seen["argv"]
    sid = argv[argv.index("--session-id") + 1]
    assert re.fullmatch(r"[0-9a-f-]{36}", sid)


# ---------------------------------------------------------------- config pane

def test_front_matter_reads_name_and_description(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("---\nname: coder\ndescription: Writes code.\n"
                 "tools: Read, Grep\n---\n\n# body\n")
    fm = srv.read_frontmatter(f)
    assert fm["name"] == "coder" and fm["description"] == "Writes code."
    f.write_text("no front matter\n# Heading\n")
    assert srv.read_frontmatter(f) == {}
    assert srv.first_heading(f) == "Heading"


def test_config_inventory_separates_resident_from_on_demand(tmp_path):
    root = tmp_path / ".claude"
    (root / "rules").mkdir(parents=True)
    (root / "agents").mkdir()
    (root / "skills" / "graphify").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("x" * 4000)
    (root / "rules" / "workflow.md").write_text("# Workflow\n" + "y" * 2000)
    (root / "agents" / "coder.md").write_text(
        "---\nname: coder\ndescription: Writes code.\n---\n" + "z" * 8000)
    (root / "skills" / "graphify" / "SKILL.md").write_text(
        "---\nname: graphify\ndescription: Graphs things.\n---\n" + "w" * 8000)

    inv = srv.config_inventory(root)
    by = {g["key"]: g for g in inv["groups"]}
    assert [i["name"] for i in by["skills"]["items"]] == ["graphify"]
    assert by["memory"]["items"][1]["description"] == "Workflow"   # heading fallback

    # instruction files cost their whole body every turn ...
    claude_md = by["memory"]["items"][0]
    assert claude_md["resident"] == claude_md["tokens"] > 900
    # ... an agent costs only its description until it is dispatched
    coder = by["agents"]["items"][0]
    assert coder["resident"] < 10 < coder["tokens"]
    assert inv["ondemand"] > inv["resident"]


def test_config_inventory_flags_hooks_nothing_points_at(tmp_path):
    root = tmp_path / ".claude"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "orphan.sh").write_text("#!/bin/sh\n")
    (root / "hooks" / "live.sh").write_text("#!/bin/sh\n")
    (root / "settings.json").write_text(json.dumps({"hooks": {"SessionEnd": [
        {"hooks": [{"type": "command",
                    "command": '"' + str(root / "hooks" / "live.sh") + '"'}]}]}}))
    # a settings.local.json without hooks must not erase the registration
    (root / "settings.local.json").write_text(json.dumps({"permissions": {}}))

    hooks = {i["name"]: i for g in srv.config_inventory(root)["groups"]
             if g["key"] == "hooks" for i in g["items"]}
    assert hooks["live"]["event"] == "SessionEnd"
    assert hooks["orphan"]["event"] is None


def test_config_view_skips_a_project_claude_dir_with_nothing_in_it(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    (tmp_path / ".claude").mkdir()
    proj = tmp_path / "proj" / ".claude"
    proj.mkdir(parents=True)
    (proj / "plan.md").write_text("- [ ] a\n")        # not a config component
    assert srv.config_view(str(tmp_path / "proj"))["project"] is None
    (proj / "agents").mkdir()
    (proj / "agents" / "x.md").write_text("---\nname: x\ndescription: d\n---\n")
    assert srv.config_view(str(tmp_path / "proj"))["project"]["count"] == 1


def test_mcp_servers_come_from_claude_json_not_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    (tmp_path / ".claude.json").write_text(json.dumps({
        "mcpServers": {"garmin": {"type": "stdio", "command": "x",
                                  "env": {"TOKEN": "sekrit"}}},
        "projects": {str(tmp_path / "proj"): {
            "mcpServers": {"datagouv": {"type": "http", "url": "https://x"}}}}}))
    assert srv.mcp_servers(tmp_path / ".claude") == {"garmin": "stdio"}
    assert srv.mcp_servers(None, scope=tmp_path / "proj") == {"datagouv": "http"}


def test_config_reports_project_mcp_even_without_a_project_claude_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    (tmp_path / ".claude").mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"projects": {
        str(proj): {"mcpServers": {"datagouv": {"type": "http"}}}}}))
    inv = srv.config_view(str(proj))["project"]
    assert [i["name"] for g in inv["groups"] for i in g["items"]] == ["datagouv"]


def test_mcp_inventory_never_leaks_args_or_env(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {
        "s": {"type": "stdio", "command": "/bin/x",
              "args": ["--key", "AKIA-DO-NOT-LEAK"],
              "env": {"API_KEY": "sk-do-not-leak"}}}}))
    blob = json.dumps(srv.config_view())
    assert "DO-NOT-LEAK" not in blob and "sk-do-not-leak" not in blob
    assert "/bin/x" not in blob


def test_empty_retrospectives_are_not_offered_as_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "IMPROVE_DIR", tmp_path)
    d = tmp_path / "proj"
    d.mkdir()
    stale = d / "2026-01-01_0900.md"
    stale.write_text("# Retrospective\n")                 # header only
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    real = d / "2026-01-02_0900.md"
    real.write_text("# Retrospective\n\n" + "finding. " * 60)
    names = [r["name"] for r in srv.improve_reports("proj")["reports"]]
    assert names == [real.name]

    fresh = d / "2026-01-03_0900.md"                      # still being written
    fresh.write_text("# Retrospective\n")
    assert fresh.name in [r["name"] for r in srv.improve_reports("proj")["reports"]]


def test_hook_name_resolves_through_an_interpreter(tmp_path):
    assert srv.hook_script_name(["python3", "/a/pre-compact.py"]) == "pre-compact.py"
    assert srv.hook_script_name(["/a/protect-files.sh"]) == "protect-files.sh"
    assert srv.hook_script_name(["node", "--enable-source-maps", "/a/x.mjs"]) == "x.mjs"
    assert srv.hook_script_name(["some-binary"]) == "some-binary"


def test_helper_scripts_in_hooks_are_not_flagged_as_orphans(tmp_path):
    root = tmp_path / ".claude"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "lib.sh").write_text("#!/bin/sh\n# a linter other hooks call\n")
    (root / "hooks" / "guard.sh").write_text(
        "#!/bin/sh\n# reads the payload\ncat | grep tool_input\n")
    (root / "hooks" / "declared.py").write_text('"""Hook Event: PreCompact"""\n')
    (root / "settings.json").write_text("{}")

    got = {i["name"]: i for g in srv.config_inventory(root)["groups"]
           if g["key"] == "hooks" for i in g["items"]}
    assert got["lib"]["orphan"] is False          # helper, not a hook
    assert got["guard"]["orphan"] is True         # consumes a hook payload
    assert got["declared"]["orphan"] is True
    assert "PreCompact" in got["declared"]["description"]


def test_config_inventory_counts_nested_rules(tmp_path):
    """Claude Code loads rules/ recursively, so a subfolder is resident too."""
    root = tmp_path / ".claude"
    (root / "rules" / "pipeline").mkdir(parents=True)
    (root / "rules" / "top.md").write_text("# Top\n" + "x" * 400)
    (root / "rules" / "pipeline" / "workflow.md").write_text("# Flow\n" + "y" * 4000)
    mem = [g for g in srv.config_inventory(root)["groups"] if g["key"] == "memory"][0]
    names = {i["name"]: i for i in mem["items"]}
    assert set(names) == {"top", "pipeline/workflow"}
    assert names["pipeline/workflow"]["always"] is True
    assert names["pipeline/workflow"]["resident"] > names["top"]["resident"]


# ---------------------------------------------------------------- figure review

@pytest.fixture
def figure(plan_project):
    img = plan_project / "fig.png"
    img.write_bytes(b"\x89PNG fake v1")
    return img


def test_review_roundtrip_clamps_and_numbers(figure):
    d = srv.review_write(str(figure), [
        {"type": "point", "x": 0.25, "y": 1.7, "text": "bigger label"},
        {"type": "region", "x": -1, "y": 0.1, "w": 0.5, "h": 0.2,
         "status": "bogus", "text": "drop this band"}], 0)
    assert d["revision"] == 1 and not d["stale"]
    a, b = d["comments"]
    assert (a["n"], a["y"], a["status"]) == (1, 1.0, "open")
    assert (b["n"], b["x"], b["w"], b["status"]) == (2, 0.0, 0.5, "open")
    saved = json.loads((figure.parent / ".review" / "fig.png.json").read_text())
    assert saved["figure"]["content_hash"] == d["content_hash"]
    assert figure.read_bytes() == b"\x89PNG fake v1"          # never rewritten


def test_review_flags_a_regenerated_figure(figure):
    srv.review_write(str(figure), [{"type": "point", "x": .5, "y": .5}], 0)
    figure.write_bytes(b"\x89PNG fake v2")
    assert srv.review_read(str(figure))["stale"] is True


def test_review_refuses_a_stale_revision(figure):
    srv.review_write(str(figure), [], 0)
    with pytest.raises(ValueError, match="changed on disk"):
        srv.review_write(str(figure), [], 0)


def test_pdf_review_keeps_page_numbers(plan_project):
    pdf = plan_project / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    d = srv.review_write(str(pdf), [
        {"type": "region", "x": .1, "y": .2, "w": .3, "h": .1, "page": 4, "text": "table 2 notes"},
        {"type": "point", "x": .5, "y": .5, "text": "no page given"}], 0)
    assert [c.get("page") for c in d["comments"]] == [4, None]
    saved = json.loads((plan_project / ".review" / "paper.pdf.json").read_text())
    assert "page" in saved["coordinates"]
    for bad in (0, -1, "3", 2.5, True):
        with pytest.raises(ValueError):
            srv.review_write(str(pdf), [{"type": "point", "x": 0, "y": 0, "page": bad}], None)


def test_review_rejects_bad_input(figure, plan_project):
    with pytest.raises(ValueError):
        srv.review_write(str(figure), [{"type": "arrow", "x": 0, "y": 0}], 0)
    with pytest.raises(ValueError):
        srv.review_write(str(figure), [{"type": "point", "x": "a", "y": 0}], 0)
    notes = plan_project / "notes.md"
    notes.write_text("x")
    with pytest.raises(ValueError):
        srv.review_read(str(notes))                           # not an image or PDF
    with pytest.raises(ValueError):
        srv.review_read("/etc/hosts.png")                     # outside $HOME


def test_every_kernel32_call_has_a_ctypes_signature():
    """An undeclared kernel32 function gets its arguments as 32-bit C ints, so
    a 64-bit HANDLE or pointer raises "argument 1: OverflowError: int too long
    to convert" on Windows. This runs anywhere: it reads the source."""
    import re
    src = (HERE.parent / "winconpty.py").read_text()
    called = set(re.findall(r"k32\.(\w+)\(", src))
    declared = set(re.findall(r'\(\s*"(\w+)",', src))
    assert called, "no kernel32 calls found — pattern out of date"
    assert called <= declared, f"missing argtypes: {sorted(called - declared)}"


# ---------------------------------------------------------------- add-ons

def test_shipped_addons_manifest_is_complete():
    data = json.loads((HERE.parent / "addons.json").read_text())
    ids = [a["id"] for a in data["addons"]]
    assert len(ids) == len(set(ids))
    for a in data["addons"]:
        assert srv.ADDON_ID_RE.fullmatch(a["id"]), a["id"]
        assert a["check"]["kind"] in ("skill", "command", "plugin", "statusline", "hook", "mcp"), a["id"]
        for plat in ("posix", "windows"):
            assert a["install"][plat], f"{a['id']} has no {plat} commands"
        assert a["source"].startswith("https://"), a["id"]
    used = {a["id"] for a in data["addons"] if a.get("used_by_app")}
    assert used == {"graphify", "improve"}   # the tees edit settings.json: opt-in only


def test_addon_detection_by_kind(tmp_path):
    root = tmp_path / ".claude"
    (root / "skills" / "graphify").mkdir(parents=True)
    (root / "skills" / "graphify" / "SKILL.md").write_text("x")
    (root / "skills" / "empty").mkdir()                     # no SKILL.md
    (root / "commands").mkdir()
    (root / "commands" / "improve.md").write_text("x")
    (root / "plugins").mkdir()
    (root / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"version": 2, "plugins": {"ponytail@ponytail": []}}))
    ok = lambda kind, name: srv.addon_installed({"kind": kind, "name": name}, root)
    assert ok("skill", "graphify") and not ok("skill", "empty")
    assert ok("command", "improve") and not ok("command", "nope")
    assert ok("plugin", "ponytail@ponytail") and not ok("plugin", "codex@openai-codex")
    assert not ok("skill", "../skills/graphify")             # no path games
    assert not ok("weird", "graphify")


def test_addons_status_reports_missing_prerequisites(tmp_path, monkeypatch):
    manifest = tmp_path / "addons.json"
    manifest.write_text(json.dumps({"addons": [
        {"id": "x", "check": {"kind": "skill", "name": "x"},
         "needs": ["definitely-not-a-real-program-cdl"],
         "install": {"posix": ["echo hi"], "windows": ["echo hi"]}},
        {"id": "Bad Id!", "check": {"kind": "skill", "name": "y"}}]}))
    st = srv.addons_status(root=tmp_path / ".claude", manifest=manifest)
    assert [a["id"] for a in st["addons"]] == ["x"]           # invalid id dropped
    a = st["addons"][0]
    assert a["installed"] is False and a["commands"] == ["echo hi"]
    assert a["missing_needs"] == ["definitely-not-a-real-program-cdl"]


def test_improve_is_skipped_when_the_command_is_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "CLAUDE_ROOT", tmp_path / ".claude")
    monkeypatch.setattr(srv, "improve_enabled", lambda: True)
    monkeypatch.delenv("CDL_IMPROVE_RUN", raising=False)
    called = []
    monkeypatch.setattr(srv, "find_claude", lambda: called.append(1) or "/bin/true")
    assert srv.spawn_improve(str(tmp_path), None) is None
    assert called == []                   # bailed out before looking for claude


def _joined_install_lines(plat):
    """Every line the pane can send: install, reinstall, and prerequisites."""
    data = json.loads((HERE.parent / "addons.json").read_text())
    out = {}
    oses = ("windows", "windows-nowinget") if plat == "windows" else ("mac", "linux")
    for key, pr in data.get("prerequisites", {}).items():
        for o in oses:
            if pr["install"].get(o):
                out[f"prereq {key}:{o}"] = " && ".join(pr["install"][o])
    # {hooks} as the server fills it, with a path that needs quoting
    hooks = srv.hooks_cmd(exe="/Apps/My Python/python3", here="/Users/Jean Dupont/ember",
                          frozen=False, windows=plat == "windows")
    for a in data["addons"]:
        for key in ("install", "reinstall"):
            if a.get(key):
                out[f"{a['id']}:{key}"] = " && ".join(a[key][plat]).replace("{hooks}", hooks)
    return out


@posix_only
@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_posix_install_lines_parse_in_real_shells(shell):
    """The pane chains an add-on's commands into one line. `echo (x)` once
    broke ponytail/codex/frontend-design with a syntax error: parse them all."""
    import shutil
    import subprocess
    if not shutil.which(shell):
        pytest.skip(f"{shell} not installed")
    for aid, line in _joined_install_lines("posix").items():
        r = subprocess.run([shell, "-n", "-c", line], capture_output=True, text=True)
        assert r.returncode == 0, f"{aid}: {r.stderr.strip()}"


def test_windows_install_lines_are_safe_to_chain_in_cmd():
    """cmd.exe can't be run here, so lint the two traps that bit us: an `if`
    swallows every `&& …` after it (so /improve never downloaded), and an
    npm-installed `claude` is a .cmd that must be CALLed or it ends the line."""
    import re
    for aid, line in _joined_install_lines("windows").items():
        assert not re.search(r"(^|&&|\(|\|\|)\s*if\s", line), f"{aid}: bare if"
        for m in re.finditer(r"(^|&&|\(|\|\|)\s*(\S+)", line):
            # npm-installed CLIs are .cmd wrappers too
            assert m.group(2) not in ("claude", "npm", "codex"), \
                f"{aid}: `{m.group(2)}` without `call`"
        assert line.count("(") == line.count(")"), aid


def test_reinstall_falls_back_to_install_and_node_hooks_need_node(tmp_path):
    manifest = tmp_path / "addons.json"
    manifest.write_text(json.dumps({"addons": [
        {"id": "a", "check": {"kind": "skill", "name": "a"},
         "install": {"posix": ["i"], "windows": ["i"]}},
        {"id": "b", "check": {"kind": "skill", "name": "b"},
         "install": {"posix": ["i"], "windows": ["i"]},
         "reinstall": {"posix": ["r"], "windows": ["r"]}}]}))
    st = {a["id"]: a for a in srv.addons_status(root=tmp_path, manifest=manifest)["addons"]}
    assert st["a"]["reinstall"] == ["i"] and st["b"]["reinstall"] == ["r"]
    shipped = {a["id"]: a for a in json.loads((HERE.parent / "addons.json").read_text())["addons"]}
    for aid in ("ponytail", "codex"):          # their hooks are `node …`
        assert "node" in shipped[aid]["needs"], aid


def test_every_need_has_a_prerequisite_recipe():
    data = json.loads((HERE.parent / "addons.json").read_text())
    pre = data["prerequisites"]
    for a in data["addons"]:
        for n in a.get("needs", []):
            assert n in pre, f"{a['id']} needs {n}, but addons.json has no recipe"
    for key, pr in pre.items():
        for n in pr.get("needs", []):
            assert n in pre, f"prerequisite {key} needs unknown {n}"
        for o in ("mac", "linux", "windows"):
            assert pr["install"].get(o) or pr.get("note", {}).get(o), \
                f"{key}: no command and no note for {o}"


def test_addons_status_lists_prerequisites_for_this_os(tmp_path):
    manifest = tmp_path / "addons.json"
    manifest.write_text(json.dumps({"addons": [], "prerequisites": {
        "definitely-not-a-real-program-cdl": {
            "name": "X", "install": {"mac": ["m"], "linux": ["l"], "windows": ["w"]},
            "needs": ["node"]}}}))
    st = srv.addons_status(root=tmp_path, manifest=manifest)
    x = st["prerequisites"]["definitely-not-a-real-program-cdl"]
    want = {"windows": "w", "mac": "m", "linux": "l"}[st["os"]]
    assert x["installed"] is False and x["commands"] == [want]
    assert x["needs"] == ["node"]


def test_every_winget_recipe_has_a_no_winget_fallback():
    """winget isn't on every Windows PC ("winget n'est pas reconnu")."""
    pre = json.loads((HERE.parent / "addons.json").read_text())["prerequisites"]
    for key, pr in pre.items():
        win = " ".join(pr["install"].get("windows", []))
        if "winget" in win:
            fb = " ".join(pr["install"].get("windows-nowinget", []))
            assert fb and "winget" not in fb, f"{key}: no fallback without winget"


def test_windows_without_winget_gets_the_fallback(tmp_path, monkeypatch):
    manifest = tmp_path / "addons.json"
    manifest.write_text(json.dumps({"addons": [], "prerequisites": {"zz": {
        "install": {"windows": ["winget install zz"],
                    "windows-nowinget": ["powershell zz"]}}}}))
    monkeypatch.setattr(srv, "host_os", lambda: "windows")
    monkeypatch.setattr(srv, "program_on_path", lambda n: False)
    st = srv.addons_status(root=tmp_path, manifest=manifest)
    assert st["prerequisites"]["zz"]["commands"] == ["powershell zz"]
    monkeypatch.setattr(srv, "program_on_path", lambda n: n == "winget")
    st = srv.addons_status(root=tmp_path, manifest=manifest)
    assert st["prerequisites"]["zz"]["commands"] == ["winget install zz"]


# ---------------------------------------------------------------- term lifecycle

def test_term_cap_refuses_before_spawning(monkeypatch):
    spawned = []

    class Fake:
        def __init__(self, *a, **k):
            spawned.append(1)
            self.id, self.alive = str(len(spawned)), True
    monkeypatch.setattr(srv, "HAS_TERMINAL", True)
    monkeypatch.setattr(srv, "HAS_PTY", True)
    monkeypatch.setattr(srv, "PosixTerm", Fake)
    monkeypatch.setattr(srv, "TERMS", {})
    for _ in range(srv.MAX_TERMS):
        srv.start_term("shell", None)
    with pytest.raises(RuntimeError):
        srv.start_term("shell", None)
    assert len(spawned) == srv.MAX_TERMS       # the refused one never forked


@pytest.mark.skipif(not getattr(srv, "HAS_PTY", False), reason="POSIX pty only")
def test_child_that_exits_by_itself_is_reaped():
    t = srv.PosixTerm(["/bin/sh", "-c", "echo bye"], "/tmp")
    deadline = time.time() + 5
    while not t._finished and time.time() < deadline:
        time.sleep(0.05)
    assert t._finished and not t.alive
    with pytest.raises(ChildProcessError):     # already reaped: no zombie
        os.waitpid(t.pid, os.WNOHANG)


def test_viz_missing_override_dir_returns_empty():
    d = Path.home() / ".cdl-test-gone-viz"
    files, where = srv.viz_list(str(d))
    assert files == [] and where == d


def test_viz_retries_interrupted_scan_without_duplicates(tmp_path, monkeypatch):
    """An interruption after one result restarts with an empty accumulator."""
    f = tmp_path / "figure.png"
    f.write_bytes(b"image")
    monkeypatch.setattr(srv, "VIZ_DIR", tmp_path)
    calls = []

    def interrupted(path):
        calls.append(path)
        yield f
        if len(calls) == 1:
            raise InterruptedError(4, "Interrupted system call")

    monkeypatch.setattr(Path, "iterdir", interrupted)
    files, where = srv.viz_list()
    assert where == tmp_path and len(calls) == 2
    assert [x["name"] for x in files] == ["figure.png"]


def test_viz_persistent_interruption_stops_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "VIZ_DIR", tmp_path)
    calls = []

    def interrupted(path):
        calls.append(path)
        raise InterruptedError(4, "Interrupted system call")

    monkeypatch.setattr(Path, "iterdir", interrupted)
    with pytest.raises(InterruptedError):
        srv.viz_list()
    assert len(calls) == 2


@posix_only
def test_terminal_backpressure_has_deadline(monkeypatch):
    """A real PTY with nobody draining input must not hold an HTTP thread."""
    import tty
    master, slave = srv.pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    t = srv.PosixTerm.__new__(srv.PosixTerm)
    t.fd, t.alive, t._finished = master, True, False
    t.cond, t._write_lock = threading.Condition(), threading.Lock()
    t.WRITE_TIMEOUT = 0.1
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            t.write(b"x" * 2_000_000)
        assert time.monotonic() - start < 1.0
        assert not t._write_lock.locked()
    finally:
        os.close(master)
        os.close(slave)


@posix_only
def test_terminal_slow_reader_gets_whole_paste():
    """The deadline counts time without progress, not total time: a child
    draining slowly must receive a paste that takes longer than the timeout."""
    import tty
    master, slave = srv.pty.openpty()
    tty.setraw(slave)
    os.set_blocking(master, False)
    t = srv.PosixTerm.__new__(srv.PosixTerm)
    t.fd, t.alive, t._finished = master, True, False
    t.cond, t._write_lock = threading.Condition(), threading.Lock()
    t.WRITE_TIMEOUT = 0.3
    paste, got = b"y" * 16_384, bytearray()

    def drain():                    # ~1 KB per 50 ms: ~0.8 s for the whole paste
        while len(got) < len(paste):
            got.extend(os.read(slave, 1024))
            time.sleep(0.05)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    start = time.monotonic()
    try:
        t.write(paste)
        assert time.monotonic() - start > t.WRITE_TIMEOUT
        reader.join(2)
        assert bytes(got) == paste
    finally:
        os.close(master)
        os.close(slave)


@posix_only
def test_terminal_partial_writes_and_transient_readiness(monkeypatch):
    t = srv.PosixTerm.__new__(srv.PosixTerm)
    t.fd, t.alive, t._finished = 123, True, False
    t.cond, t._write_lock = threading.Condition(), threading.Lock()
    got, calls = bytearray(), []
    monkeypatch.setattr(srv.select, "select", lambda *args: ([], [123], []))

    def partial(fd, data):
        calls.append(1)
        if len(calls) == 2:
            raise BlockingIOError()
        if len(calls) == 3:
            raise InterruptedError()
        got.extend(data[:2])
        return min(2, len(data))

    monkeypatch.setattr(srv.os, "write", partial)
    t.write(b"abcdefg")
    assert got == b"abcdefg"
    t._finished = True
    with pytest.raises(BrokenPipeError):
        t.write(b"must not reach a recycled fd")
    assert got == b"abcdefg"


@posix_only
def test_nonblocking_terminal_still_exchanges_input_and_output():
    """Exercise the real reader pump after making the PTY nonblocking."""
    t = srv.PosixTerm(["/bin/sh", "-c", 'read line; printf "received:%s\\n" "$line"'], "/tmp")
    try:
        t.write(b"hello\n")
        deadline = time.monotonic() + 3
        with t.cond:
            while b"received:hello" not in t.buf and time.monotonic() < deadline:
                t.cond.wait(0.05)
            assert b"received:hello" in t.buf
    finally:
        t.close_gracefully(grace=0.1)


@pytest.mark.parametrize("error, status", [(TimeoutError("blocked"), 504),
                                          (BrokenPipeError("closed"), 410)])
def test_terminal_input_http_reports_failure(http_server, monkeypatch, error, status):
    class FailedTerm:
        def write(self, data):
            raise error

    monkeypatch.setattr(srv, "TERMS", {"failed": FailedTerm()})
    code, body = fetch(http_server + "/api/term/input", method="POST",
                       headers={"X-Devtools-Token": "a" * 48,
                                "Content-Type": "application/json"},
                       body=json.dumps({"id": "failed", "data": "eA=="}).encode())
    assert code == status and str(error) in json.loads(body)["error"]


def test_non_command_hooks_are_named_by_type():
    reg = srv.hook_registrations({"hooks": {
        "PermissionRequest": [{"hooks": [{"type": "http", "url": "https://hooks.example/x"}]}],
        "Stop": [{"hooks": [{"type": "prompt", "prompt": "check"}]}]}})
    assert reg == {"PermissionRequest": ["http: hooks.example"], "Stop": ["prompt"]}
    assert "PermissionRequest" in srv.HOOK_EVENTS and len(srv.HOOK_EVENTS) == 33


def test_one_version_everywhere():
    import re
    cff = (HERE.parent / "CITATION.cff").read_text()
    assert re.search(r'^version: "([^"]+)"', cff, re.M).group(1) == srv.VERSION
    assert srv.Handler.server_version.endswith("/" + srv.VERSION)


# ---------------------------------------------------------------- search

@pytest.fixture
def search_root(tmp_path):
    d = tmp_path / "projects" / "-Users-x-proj"
    write_session(d / "s1.jsonl", [rec_user('say "normal mode" please'),
                                   rec_assistant([{"type": "text", "text": "Thèse chapter done"}])])
    write_session(d / "s1" / "subagents" / "agent-a1.jsonl",
                  [rec_assistant([{"type": "text", "text": "subagent found the needle"}])])
    write_session(tmp_path / "projects" / "-Users-x-other" / "s2.jsonl", [rec_user("needle elsewhere")])
    return tmp_path


def test_search_finds_quoted_text_accents_and_subagents(search_root):
    assert [r["session"] for r in srv.search_all(search_root, '"normal mode"')] == ["s1"]
    assert srv.search_all(search_root, "THÈSE")[0]["type"] == "assistant"      # non-ASCII path
    hits = srv.search_all(search_root, "needle")
    assert {(r["session"], r.get("agent")) for r in hits} == {("s1", "agent-a1.jsonl"), ("s2", None)}
    scoped = srv.search_all(search_root, "needle", project="-Users-x-proj")
    assert [r.get("agent") for r in scoped] == ["agent-a1.jsonl"]


def test_search_precheck_spans_chunk_boundaries(search_root, monkeypatch):
    monkeypatch.setattr(srv, "SEARCH_CHUNK", 7)      # "needle" straddles every chunk
    assert len(srv.search_all(search_root, "needle")) == 2
    assert srv.search_all(search_root, "absent-word") == []


def test_session_json_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    f = tmp_path / "s.jsonl"
    write_session(f, [rec_user("hello")])
    calls = []
    real = srv.parse_session
    monkeypatch.setattr(srv, "parse_session", lambda p, **k: calls.append(p) or real(p, **k))
    monkeypatch.setattr(srv, "_session_cache", {})
    a = srv.session_json(f)
    assert srv.session_json(f) is a and len(calls) == 1          # hit
    with open(f, "a") as fh:                                      # live session grows
        fh.write(json.dumps(rec_user("more", uuid="u2")) + "\n")
    assert b"more" in srv.session_json(f) and len(calls) == 2
    assert json.loads(a)["entries"] == real(tmp_path / "s.jsonl")["entries"][:1]


# ---------------------------------------------------------------- tees (tools/devtools_hooks.py)

def _hooks_mod(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cdl_hooks", HERE.parent / "tools" / "devtools_hooks.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    for k, v in {"APP_DIR": tmp_path, "STATUS_DIR": tmp_path / "status",
                 "EVENTS": tmp_path / "events.jsonl", "INNER": tmp_path / "inner.json",
                 "GUARDS": tmp_path / "guards.jsonl",
                 "SETTINGS": tmp_path / "settings.json"}.items():
        monkeypatch.setattr(m, k, v)
    return m


def test_event_tee_keeps_metadata_never_inputs(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    m.event(json.dumps({"hook_event_name": "PreToolUse", "session_id": "s1", "tool_name": "Bash",
                        "tool_input": {"command": "export API_KEY=sk-secret"}}))
    line = (tmp_path / "events.jsonl").read_text()
    assert "Bash" in line and "sk-secret" not in line and "tool_input" not in line


def test_statusline_tee_saves_status_and_runs_the_wrapped_line(tmp_path, monkeypatch, capsys):
    m = _hooks_mod(tmp_path, monkeypatch)
    (tmp_path / "inner.json").write_text(json.dumps({"command": "cat"}))
    payload = json.dumps({"session_id": "abc", "rate_limits": {"five_hour": {"used_percentage": 42}}})
    assert m.statusline(payload) == 0
    assert capsys.readouterr().out == payload                    # passed through unchanged
    assert json.loads((tmp_path / "status" / "abc.json").read_text())["rate_limits"]


def test_tee_install_wraps_and_uninstall_restores(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    (tmp_path / "settings.json").write_text(json.dumps(
        {"statusLine": {"type": "command", "command": "my-line"}, "hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
    m.install_statusline(); m.install_events(); m.install_events()      # idempotent
    st = json.loads((tmp_path / "settings.json").read_text())
    assert "devtools_hooks.py" in st["statusLine"]["command"]
    assert srv.addon_installed({"kind": "statusline", "name": "devtools_hooks.py"}, tmp_path)
    assert srv.addon_installed({"kind": "hook", "name": "devtools_hooks.py"}, tmp_path)
    assert sum("devtools_hooks.py" in json.dumps(e) for e in st["hooks"]["Stop"]) == 1
    m.uninstall_statusline(); m.uninstall_events()
    st = json.loads((tmp_path / "settings.json").read_text())
    assert st == {"statusLine": {"type": "command", "command": "my-line"},
                  "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine.sh"}]}]}}
    assert (tmp_path / "settings.json.bak-devtools").exists()


def test_official_limits_and_events_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "APP_DIR", tmp_path)
    monkeypatch.setattr(srv, "EVENTS_FILE", tmp_path / "events.jsonl")
    assert srv.official_limits() is None
    (tmp_path / "status").mkdir()
    f = tmp_path / "status" / "s.json"
    f.write_text(json.dumps({"session_id": "s", "rate_limits": {"five_hour": {"used_percentage": 42}}}))
    assert srv.official_limits()["five_hour"]["used_percentage"] == 42
    old = time.time() - srv.STATUS_TTL - 5
    os.utime(f, (old, old))
    assert srv.official_limits() is None                          # stale is ignored
    assert srv.events_since(0)["enabled"] is False
    (tmp_path / "events.jsonl").write_text('{"a":1}\n{"a":2}\n{"a":')   # last line incomplete
    first = srv.events_since(-1)
    assert first["events"] == [] and first["offset"] > 0          # fresh page starts live
    r = srv.events_since(0)
    assert [e["a"] for e in r["events"]] == [1, 2]
    assert srv.events_since(10**9)["events"] == r["events"]      # rotated: restart at 0


def test_session_tail_splices_to_a_full_parse(tmp_path):
    f = tmp_path / "s.jsonl"
    use = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}
    write_session(f, [rec_user("go"), rec_assistant([use])])
    first = srv.session_tail(f, "", 0)
    ents = first["entries"]
    r = srv.session_tail(f, first["key"], len(ents))
    assert r == {"key": first["key"], "unchanged": True}          # nothing moved: just a stat
    with open(f, "a") as fh:                                     # the tool result lands later
        fh.write(json.dumps({"type": "user", "uuid": "u9", "timestamp": "2026-07-28T10:00:09.000Z",
                             "message": {"role": "user", "content": [
                                 {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]}}) + "\n")
    r = srv.session_tail(f, first["key"], len(ents))
    spliced = ents[:r["start"]] + r["entries"]
    assert spliced == srv.parse_session(f)["entries"] and r["total"] == len(spliced)


@pytest.mark.skipif(not getattr(srv, "HAS_PTY", False), reason="POSIX pty only")
def test_one_stream_carries_every_terminal(http_server, monkeypatch):
    """A browser allows 6 connections per host: one stream per tab starved
    keystrokes. One stream must carry all terminals, output and exits."""
    import base64
    a = srv.PosixTerm(["/bin/sh", "-c", "echo AAA"], "/tmp")
    b = srv.PosixTerm(["/bin/sh", "-c", "sleep 0.3; echo BBB"], "/tmp")
    monkeypatch.setattr(srv, "TERMS", {a.id: a, b.id: b})
    code, body = fetch(f"{http_server}/api/term/stream?id={a.id}&from=0"
                       f"&id={b.id}&from=0&id=gone&from=0",
                       headers={"X-Devtools-Token": "a" * 48})
    assert code == 200
    out, exits = {a.id: b"", b.id: b""}, set()
    for frame in body.decode().split("\n\n"):
        if frame.startswith("event: exit"):
            exits.add(frame.split("data: ")[1])
        elif frame.startswith("data: "):
            tid, _, b64 = frame[6:].split(" ")
            out[tid] += base64.b64decode(b64)
    assert b"AAA" in out[a.id] and b"BBB" in out[b.id]
    assert exits == {a.id, b.id, "gone"}


def test_quick_edit_off_is_safe_without_a_console():
    # a no-op off Windows; on the Windows CI runner stdin is not a console,
    # so this exercises the ctypes signatures without touching a real window
    srv.quick_edit_off()


# ---------------------------------------------------------------- session guards

def _guarded_project(tmp_path, guards):
    proj = tmp_path / "proj"
    (proj / ".claude" / "state").mkdir(parents=True)
    (proj / ".claude" / "state" / "session-guards.json").write_text(json.dumps(guards))
    return proj


def test_careful_blocks_destructive_commands_only(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    proj = _guarded_project(tmp_path, {"careful": {"active": True}})
    bash = lambda c: {"cwd": str(proj), "tool_name": "Bash", "tool_input": {"command": c}}
    for bad in ("rm -rf build", "rm -Rf x", "git push --force", "git push origin main -f",
                "git reset --hard HEAD~1", "git clean -fd", "chmod -R 777 ."):
        assert m.guard_verdict(bash(bad)), bad
    for ok in ("rm notes.txt", "git status", "git push origin main", "ls -rf"):
        assert m.guard_verdict(bash(ok)) is None, ok
    (proj / ".claude" / "state" / "session-guards.json").write_text('{"careful": {"active": false}}')
    assert m.guard_verdict(bash("rm -rf /")) is None


def test_freeze_allows_only_named_folders(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    proj = _guarded_project(tmp_path, {"freeze": {"active": True, "allowed_paths": ["paper/"]}})
    edit = lambda f: {"cwd": str(proj), "tool_name": "Edit", "tool_input": {"file_path": f}}
    assert m.guard_verdict(edit(str(proj / "paper" / "main.tex"))) is None
    assert m.guard_verdict(edit("paper/sub/x.tex")) is None                 # relative
    assert m.guard_verdict(edit(str(proj / ".claude" / "plan.md"))) is None  # never frozen out
    assert m.guard_verdict(edit(str(proj / "code" / "a.R")))[0] == "freeze"
    assert m.guard_verdict(edit(str(proj / "paper2" / "x.tex")))             # not a prefix match
    assert m.guard_verdict(edit(str(proj / "paper" / ".." / "code.R")))      # no escaping
    assert m.guard_verdict({"cwd": str(proj), "tool_name": "Read",
                            "tool_input": {"file_path": "/etc/hosts"}}) is None


def test_guard_denies_and_logs_metadata_never_the_command(tmp_path, monkeypatch, capsys):
    m = _hooks_mod(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    proj = _guarded_project(tmp_path, {"careful": {"active": True}})
    m.guard(json.dumps({"cwd": str(proj), "session_id": "s1", "tool_name": "Bash",
                        "tool_input": {"command": "rm -rf secret-dir-name"}}))
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and "/careful off" in out["permissionDecisionReason"]
    line = (tmp_path / "guards.jsonl").read_text()
    assert json.loads(line)["guard"] == "careful" and "secret-dir-name" not in line
    m.guard(json.dumps({"cwd": str(proj), "tool_name": "Bash", "tool_input": {"command": "ls"}}))
    assert capsys.readouterr().out == ""                                     # allowed: silent
    monkeypatch.setattr(srv, "GUARDS_FILE", tmp_path / "guards.jsonl")
    assert srv.events_since(0, path=srv.GUARDS_FILE)["events"][0]["rule"]


def test_event_tee_and_guard_install_independently(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    m.install_guard(); m.install_guard()                                    # idempotent
    assert not srv.addon_installed({"kind": "hook", "name": "devtools_hooks.py", "arg": "event"}, tmp_path)
    m.install_events()                  # the guard on PreToolUse must not hide a missing tee
    st = json.loads((tmp_path / "settings.json").read_text())
    pre = json.dumps(st["hooks"]["PreToolUse"])
    assert pre.count("devtools_hooks.py") == 2 and "Bash|Edit" in pre
    assert srv.addon_installed({"kind": "hook", "name": "devtools_hooks.py", "arg": "guard"}, tmp_path)
    m.uninstall_events()
    assert srv.addon_installed({"kind": "hook", "name": "devtools_hooks.py", "arg": "guard"}, tmp_path)
    assert not srv.addon_installed({"kind": "hook", "name": "devtools_hooks.py", "arg": "event"}, tmp_path)
    m.uninstall_guard()
    assert "hooks" not in json.loads((tmp_path / "settings.json").read_text()) or \
        not json.loads((tmp_path / "settings.json").read_text())["hooks"]


def test_event_tee_skips_embers_own_retrospective(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    monkeypatch.setenv("CDL_IMPROVE_RUN", "1")
    m.event(json.dumps({"hook_event_name": "Stop", "cwd": "/p"}))
    assert not (tmp_path / "events.jsonl").exists()


def test_mcp_addon_detection(tmp_path):
    root = tmp_path / ".claude"
    root.mkdir()
    check = {"kind": "mcp", "name": "context7"}
    assert not srv.addon_installed(check, root)
    (tmp_path / ".claude.json").write_text(json.dumps(
        {"projects": {"/p": {"mcpServers": {"playwright": {}}}}}))
    assert srv.addon_installed({"kind": "mcp", "name": "playwright"}, root)  # project scope
    assert not srv.addon_installed(check, root)
    (tmp_path / ".claude.json").write_text(json.dumps({"mcpServers": {"context7": {}}}))
    assert srv.addon_installed(check, root)


def test_new_addons_have_commands_for_both_platforms():
    by_id = {a["id"]: a for a in json.loads((HERE.parent / "addons.json").read_text())["addons"]}
    for aid in ("session-guards", "anthropic-skills", "playwright", "context7"):
        assert by_id[aid]["install"]["posix"] and by_id[aid]["install"]["windows"], aid


# ---------------------------------------------------------------- session-end card

def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                    "-c", "commit.gpgsign=false", *args], check=True, capture_output=True)


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="needs git")
def test_session_summary_counts_changes_since_the_tab_opened(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    _git(proj, "init", "-q")
    (proj / "a.R").write_text("x <- 1\n")
    (proj / ".claude" / "plan.md").write_text("- [x] one\n- [ ] two\n- [ ] three\n")
    _git(proj, "add", "-A"); _git(proj, "commit", "-qm", "start")
    monkeypatch.setattr(srv, "login_path", lambda: os.environ.get("PATH", ""))
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path))  # plans live under $HOME
    snap = srv.session_snapshot(str(proj))
    assert snap["head"] and snap["plan_done"] == 1
    (proj / "a.R").write_text("x <- 1\ny <- 2\n")
    _git(proj, "commit", "-qam", "add y")
    (proj / "b.R").write_text("z <- 3\n"); _git(proj, "add", "b.R")     # staged, not committed
    (proj / ".claude" / "plan.md").write_text("- [x] one\n- [x] two\n- [ ] three\n")
    s = srv.session_summary(str(proj), snap)
    paths = {f["path"] for f in s["git"]["files"]}
    assert {"a.R", "b.R"} <= paths and len(s["git"]["commits"]) == 1
    assert s["plan"]["done"] == 2 and s["plan"]["ticked"] == 1 and s["plan"]["open"] == ["three"]
    entry = srv.session_log_entry(s)
    assert "| `a.R` | 1 | 0 |" in entry and "- three" in entry and "Plan: 2/3 done (+1" in entry

    srv.store_summary("t1", s)
    f = Path(srv.append_session_log("t1"))
    srv.append_session_log("t1")
    assert f.parent == proj / "session_logs" and f.read_text().count("## [") == 2
    with pytest.raises(KeyError):
        srv.append_session_log("../../etc")          # only known terminals, never a path


def test_session_summary_without_git_or_transcript(tmp_path):
    s = srv.session_summary(str(tmp_path), {"started": time.time(), "head": None, "plan_done": None})
    assert s["git"] is None and s["tokens"] is None and "(no file changes recorded)" in srv.session_log_entry(s)


# ---------------------------------------------------------------- export

def test_export_saves_into_downloads_with_a_safe_name(tmp_path, monkeypatch):
    monkeypatch.setattr(srv.Path, "home", classmethod(lambda cls: tmp_path))
    a = Path(srv.save_export("../../evil/<name>", "<html>1</html>"))
    b = Path(srv.save_export("../../evil/<name>", "<html>2</html>"))
    assert a.parent == b.parent == tmp_path / "Downloads" and a != b
    assert ".." not in a.name and "/" not in a.name and a.read_text() == "<html>1</html>"
    assert Path(srv.save_export("Tu reçois le digest", "x")).name == "Tu reçois le digest.html"
    with pytest.raises(ValueError):
        srv.save_export("x", "")


def test_session_transcript_ignores_older_sessions_in_the_same_project(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "project_dir_for_cwd", lambda root, cwd: tmp_path)
    old = tmp_path / "old.jsonl"
    write_session(old, [rec_user("earlier")])            # created before the tab opened
    t0 = time.time() + 30
    os.utime(old, (t0 + 5, t0 + 5))                      # ...but written to since
    monkeypatch.setattr(srv, "transcript_start", lambda f: 0.0)
    if not hasattr(os.stat_result, "st_birthtime"):
        assert srv.session_transcript("/p", t0) is None
    new = tmp_path / "new.jsonl"
    write_session(new, [rec_user("mine")])
    monkeypatch.setattr(srv, "transcript_start", lambda f: t0 + 1 if f == new else 0.0)
    if not hasattr(os.stat_result, "st_birthtime"):
        assert srv.session_transcript("/p", t0) == new
    assert srv.session_transcript("/p", t0, resume="old") == old      # resume names its own
    assert srv.transcript_start(new) is not None


# ---------------------------------------------------------------- desktop apps

def test_hooks_cmd_uses_the_running_interpreter_and_quotes_it():
    posix = srv.hooks_cmd(exe="/Apps/My Py/python3", here="/Users/Jean Dupont/ember",
                          frozen=False, windows=False)
    assert posix == "'/Apps/My Py/python3' '/Users/Jean Dupont/ember/tools/devtools_hooks.py'"
    win = srv.hooks_cmd(exe=r"C:\Program Files\Py\python.exe", here=r"C:\Users\Jean\ember",
                        frozen=False, windows=True)
    assert win.startswith(r'"C:\Program Files\Py\python.exe" ') and win.endswith("devtools_hooks.py")


def test_hooks_cmd_frozen_runs_the_app_itself():
    # the literal name keeps addon_installed()/devtools_hooks._ours() working
    # start /wait: an interactive cmd does not wait for a windowed exe
    assert srv.hooks_cmd(exe=r"C:\Users\Jean Dupont\Ember\Ember.exe", here="x",
                         frozen=True, windows=True) == \
        r'start "" /wait "C:\Users\Jean Dupont\Ember\Ember.exe" devtools_hooks.py'
    assert srv.hooks_cmd(exe="/opt/Ember/Ember", here="x", frozen=True,
                         windows=False) == "/opt/Ember/Ember devtools_hooks.py"


def test_hooks_cmd_refuses_a_translocated_app():
    line = srv.hooks_cmd(exe="/p/python3", frozen=False, windows=False,
                         here="/private/var/folders/x/AppTranslocation/ABC/d/Ember.app/Contents/Resources")
    assert "Applications" in line and line.endswith("&& false") and "devtools_hooks" not in line


def test_frozen_tee_command_names_the_exe(tmp_path, monkeypatch):
    m = _hooks_mod(tmp_path, monkeypatch)
    monkeypatch.setattr(m.sys, "frozen", True, raising=False)
    monkeypatch.setattr(m.sys, "executable", r"C:\Apps\Ember\Ember.exe")
    cmd = m._cmd("guard")
    assert cmd == r"C:\Apps\Ember\Ember.exe devtools_hooks.py guard" and m._ours(cmd, "guard")


def test_update_info_compares_versions(monkeypatch):
    monkeypatch.setattr(srv, "updates_enabled", lambda: True)
    monkeypatch.setattr(srv, "VERSION", "1.2.0")
    tag = "https://github.com/PierreBeaucoral/ember/releases/tag/"
    info = srv.update_info(force=True, fetch=lambda: tag + "v1.10.0")
    assert info["newer"] and info["latest"] == "1.10.0" and info["url"] == tag + "v1.10.0"
    assert not srv.update_info(force=True, fetch=lambda: tag + "v1.2.0")["newer"]


def test_update_info_ignores_foreign_urls_and_failures(monkeypatch):
    monkeypatch.setattr(srv, "updates_enabled", lambda: True)
    monkeypatch.setattr(srv, "_update", {"at": 0.0, "info": None})
    assert srv.update_info(force=True, fetch=lambda: "https://evil.example/tag/v9.0.0")["url"] is None

    def offline():
        raise OSError("no network")
    assert srv.update_info(force=True, fetch=offline)["latest"] is None
    # and a failed check is not retried on every call
    assert srv.update_info(fetch=lambda: 1 / 0)["latest"] is None


def test_update_info_off_makes_no_request(monkeypatch):
    monkeypatch.setenv("CDL_UPDATES", "0")
    info = srv.update_info(force=True, fetch=lambda: 1 / 0)
    assert info["enabled"] is False and info["newer"] is False


def test_open_in_and_updates_are_validated(http_server, monkeypatch, tmp_path):
    monkeypatch.setattr(srv, "STATE_FILE", tmp_path / "state.json")
    hdr = {"X-Devtools-Token": "a" * 48, "Content-Type": "application/json"}
    post = lambda b: json.loads(fetch(http_server + "/api/state", "POST", hdr,
                                      json.dumps(b).encode())[1])
    assert post({"open_in": "browser", "updates": False}) == \
        {"ok": True, "improve": True, "updates": False, "open_in": "browser"}
    assert post({"open_in": "javascript:alert(1)"})["open_in"] == "browser"   # ignored
    assert post({"open_in": "window"})["open_in"] == "window"
    # the native apps read the file directly: keep its shape
    assert json.loads((tmp_path / "state.json").read_text())["open_in"] == "window"


def test_usage_survives_a_record_written_after_the_scan_started(tmp_path):
    """A live session appends while usage_summary() runs: a record stamped a
    moment after `now` once raised IndexError (HTTP 500 in the Usage pane)."""
    future = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 30))
    write_session(tmp_path / "projects" / "-Users-x-proj" / "s.jsonl",
                  [rec_assistant([{"type": "text", "text": "hi"}], ts=future)])
    u = srv.usage_summary(tmp_path)
    assert sum(u["hourly"]) == 100


# ---------------------------------------------------------------- native/window.py

def _window_module():
    spec = importlib.util.spec_from_file_location("ember_window", HERE.parent / "native" / "window.py")
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    return w


def test_window_failure_opens_the_browser_and_keeps_the_reason(tmp_path, monkeypatch):
    """A windowed Ember.exe has no stderr: the reason the window failed must
    land in window.log (and a message box on Windows), not vanish."""
    import types
    import webbrowser
    w = _window_module()
    opened = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    if os.name == "nt":
        import ctypes
        monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", lambda *a: 1)
    w.server = types.SimpleNamespace(APP_DIR=tmp_path)
    try:
        raise OSError("Could not load file or assembly 'Python.Runtime' (0x80131515)")
    except OSError as e:
        assert w.browser_instead("http://127.0.0.1:1/launch?c=x", e) == 0
    assert opened == ["http://127.0.0.1:1/launch?c=x"]
    assert "0x80131515" in (tmp_path / "window.log").read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate data streams")
def test_window_clears_the_internet_mark_on_its_binaries(tmp_path, monkeypatch):
    """Explorer marks files unzipped from a download; .NET then refuses the
    window's DLLs (0x80131515). The frozen app clears the mark on its own."""
    w = _window_module()
    (tmp_path / "_internal").mkdir()
    dll, txt = tmp_path / "_internal" / "Python.Runtime.dll", tmp_path / "notes.txt"
    for f in (dll, txt):
        f.write_bytes(b"x")
        with open(str(f) + ":Zone.Identifier", "w") as z:
            z.write("[ZoneTransfer]\nZoneId=3\n")
    monkeypatch.setattr(w, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Ember.exe"))
    assert w.unblock_bundle() == 1
    assert not os.path.exists(str(dll) + ":Zone.Identifier")
    assert os.path.exists(str(txt) + ":Zone.Identifier")       # only binaries
    assert dll.read_bytes() == b"x"                            # the file itself stays


# ---------------------------------------------------------------- port guard

def _wait_listening(port, proc, timeout=20):
    """Alive is not listening: a cold interpreter on a CI runner can take
    seconds to bind."""
    end = time.time() + timeout
    while time.time() < end:
        assert proc.poll() is None, f"exited with {proc.returncode}"
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.1)
    pytest.fail(f"nothing listening on {port} after {timeout}s")


@pytest.mark.skipif(sys.platform == "win32", reason="no lsof/ps: the guard is a no-op on Windows")
def test_port_guard_kills_stale_server_and_refuses_foreign(tmp_path):
    """Startup kills a previous server.py still holding the port, but
    refuses to kill (and exits on) a foreign process."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (tmp_path / "projects").mkdir()   # CI runners have no ~/.claude/projects

    # foreign holder: refused, never killed
    holder = subprocess.Popen(
        [sys.executable, "-m", "http.server", "--bind", "127.0.0.1", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_listening(port, holder)
        with pytest.raises(SystemExit, match="held by another program"):
            srv.kill_stale_server(port)
        assert holder.poll() is None
    finally:
        holder.terminate()
        holder.wait()

    # stale Ember server: killed so the next start can bind
    stale = subprocess.Popen(
        [sys.executable, str(HERE.parent / "server.py"), "--port", str(port),
         "--root", str(tmp_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _wait_listening(port, stale)
        srv.kill_stale_server(port)
        stale.wait(timeout=10)
        assert stale.returncode != 0
    finally:
        if stale.poll() is None:
            stale.terminate()
            stale.wait()
