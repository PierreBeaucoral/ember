"""Smoke test for a built Ember app (release workflow; runs anywhere).

    python tools/smoke_app.py frozen dist/Ember/Ember[.exe]
    python tools/smoke_app.py macapp dist/Ember.app
    python tools/smoke_app.py window dist/Ember/Ember[.exe]   (needs a display)

Runs the app's own server and tees from the build, in a throwaway home:
the server must answer, serve the page and its vendor files, and prove it
holds the token; the statusline tee must read stdin and write stdout (a
windowed Windows exe gets no stdio by default: native/window.py rebuilds it).
`window` opens the real window for a few seconds instead: it must stay up
without falling back to the browser, on Windows with every file carrying the
"downloaded from the internet" mark a user's unzipped copy has. Exit code 0 = pass.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PORT = 3499


def commands(kind, target):
    t = Path(target).resolve()
    if kind == "frozen":
        return [str(t), "--server"], [str(t), "devtools_hooks.py"]
    res = t / "Contents" / "Resources"
    py = str(res / "python" / "bin" / "python3")
    return [py, "-B", str(res / "server.py")], [py, "-B", str(res / "tools" / "devtools_hooks.py")]


def get(path):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{PORT}{path}", timeout=3) as r:
        return r.status, r.read()


def throwaway_home():
    home = Path(tempfile.mkdtemp(prefix="ember-smoke-"))
    (home / ".claude" / "projects").mkdir(parents=True)
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               APPDATA=str(home / "AppData"), XDG_CONFIG_HOME=str(home / ".config"),
               CLAUDE_ROOT=str(home / ".claude"), CDL_UPDATES="0", PORT=str(PORT))
    return home, env


def mark_downloaded(folder):
    """Give every file the "from the internet" mark that Explorer copies onto
    files extracted from a downloaded zip: the build must open its window
    anyway (Ember.exe.config lets .NET load marked DLLs; 1.4.1 cleared the
    marks instead, which failed in a read-only C:\\Program Files)."""
    for f in Path(folder).rglob("*"):
        if f.is_file():
            with open(str(f) + ":Zone.Identifier", "w") as z:
                z.write("[ZoneTransfer]\r\nZoneId=3\r\n")


def window(target):
    home, env = throwaway_home()
    runtime = None
    if os.name == "nt":
        mark_downloaded(Path(target).resolve().parent)
        runtime = next(Path(target).resolve().parent.rglob("Python.Runtime.dll"), None)
    p = subprocess.Popen([str(Path(target).resolve())], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(15)
        alive = p.poll() is None
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0] or ""
        try:                        # the window started a server: stop it
            tok = next(home.rglob("token")).read_text().strip()
            req = urllib.request.Request(
                f"http://127.0.0.1:{PORT}/api/shutdown", method="POST", data=b"{}",
                headers={"Content-Type": "application/json", "X-Devtools-Token": tok})
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=40)
        except (StopIteration, OSError) as e:
            print(f"could not stop the server: {e!r}")
    print(out[-3000:])
    assert alive and "no web view" not in out, "the window did not stay up"
    if runtime is not None:
        # the window must open with the mark still there, as in a read-only install
        assert os.path.exists(str(runtime) + ":Zone.Identifier"), \
            "the internet mark is gone from Python.Runtime.dll: the test proves nothing"
        print("opened despite the internet mark: ok")
    print("window: ok")
    return 0


def main(kind, target):
    if kind == "window":
        return window(target)
    server, hooks = commands(kind, target)
    home, env = throwaway_home()
    log = home / "server.log"
    with open(log, "w") as out:
        p = subprocess.Popen(server + ["--port", str(PORT)], env=env,
                             stdout=out, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                get("/")
                break
            except OSError:
                if p.poll() is not None:
                    break
                time.sleep(0.25)
        code, page = get("/")
        assert code == 200 and b"<title>Ember" in page, "page not served"
        assert get("/vendor/xterm.min.js")[0] == 200, "vendor files missing"
        mac = json.loads(get("/hello?n=0123456789abcdef")[1])["mac"]
        assert len(mac) == 64, "no identity proof"
        print("server: ok")

        status = {"session_id": "smoke", "model": {"display_name": "Smoke"},
                  "context_window": {"used_percentage": 42}}
        r = subprocess.run(hooks + ["statusline"], env=env, input=json.dumps(status),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and "ctx 42%" in r.stdout, f"tee: {r.returncode} {r.stdout!r} {r.stderr!r}"
        saved = list(home.rglob("smoke.json"))
        assert saved, "tee did not save the status"
        print("tee: ok")
    except Exception:
        print(log.read_text(errors="replace")[-3000:], file=sys.stderr)
        raise
    finally:
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
