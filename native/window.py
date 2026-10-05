"""Ember's own window on Windows and Linux (and anywhere pywebview runs).

PyInstaller freezes this file, with server.py, into Ember.exe / Ember
(packaging/pyinstaller/build.py). From a checkout it also runs as
    python native/window.py          (needs: pip install pywebview)

Same lifecycle as the macOS app (native/main.swift): start the server if none
answers, check it holds our token, open the dashboard with a one-time login
code, and on close stop the server gracefully — only if this window started
it. With "At launch, open: browser" (palette), or when no web view is
available, the default browser gets the login link instead and the server
keeps running (⏻ in the page stops it); in the latter case the reason is
shown and kept in window.log.

The frozen executable is also the server and the Claude Code tees:
    Ember --server [--port N]          what the window starts
    Ember devtools_hooks.py <cmd>      what installed hooks call
    Ember --install                    Linux: add Ember to the applications menu
"""
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent       # a checkout; unused when frozen
sys.path[:0] = [str(_repo), str(_repo / "tools")]

server = None       # imported in main(): hooks run on every tool call and skip it
PORT = int(os.environ.get("PORT", 3456))
BASE = f"http://127.0.0.1:{PORT}"
FROZEN = bool(getattr(sys, "frozen", False))


def reattach_std():
    """A windowed Windows exe gets sys.stdin/stdout/stderr = None, even when a
    parent (Claude Code running a hook) hands it pipes: rebuild them from the
    OS handles; run from a console (the Add-ons install terminal), attach to
    it; else use os.devnull."""
    attached = None
    for name, std in (("stdin", -10), ("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is not None:
            continue
        mode = "r" if name == "stdin" else "w"
        f = None
        if os.name == "nt":
            import ctypes
            import msvcrt
            k32 = ctypes.windll.kernel32
            k32.GetStdHandle.restype = ctypes.c_void_p
            h = k32.GetStdHandle(std)
            try:
                if h and h != ctypes.c_void_p(-1).value:
                    fd = msvcrt.open_osfhandle(h, os.O_RDONLY if mode == "r" else 0)
                    f = open(fd, mode, encoding="utf-8")
                else:
                    if attached is None:
                        attached = bool(k32.AttachConsole(-1))     # ATTACH_PARENT_PROCESS
                    if attached:
                        f = open("CONIN$" if mode == "r" else "CONOUT$", mode,
                                 encoding="utf-8")
            except OSError:
                f = None
        setattr(sys, name, f or open(os.devnull, mode))


def server_up():
    try:
        with server.local_opener().open(BASE + "/", timeout=1):
            return True
    except OSError:
        return False


def start_server():
    """Start the server detached, its output in the private data dir.
    Returns (process, log path)."""
    argv = ([sys.executable, "--server", "--port", str(PORT)] if FROZEN
            else [sys.executable, "-B", server.__file__, "--port", str(PORT)])
    log = server.APP_DIR / "window-server.log"
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    with open(log, "w", encoding="utf-8") as out:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, **kw)
    for _ in range(60):
        if server_up() or p.poll() is not None:
            break
        time.sleep(0.25)
    return p, log


def shutdown_server():
    """Graceful stop: sessions get up to ~35 s to run their SessionEnd hooks."""
    import urllib.request
    try:
        req = urllib.request.Request(
            BASE + "/api/shutdown", method="POST", data=b"{}",
            headers={"Content-Type": "application/json",
                     "X-Devtools-Token": server.TOKEN_FILE.read_text().strip()})
        server.local_opener().open(req, timeout=40).close()
    except OSError:
        pass


def webview_error():
    """None when pywebview imports, else the exception saying why not."""
    try:
        import webview  # noqa: F401
        return None
    except Exception as e:      # ImportError, or a broken GUI backend
        return e


def have_webview():
    return webview_error() is None


def unblock_bundle():
    """Files extracted from a downloaded zip carry Windows' "from the
    internet" mark (a Zone.Identifier stream), and .NET Framework refuses to
    load a marked assembly (HRESULT 0x80131515): pythonnet's Python.Runtime.dll
    and WebView2's DLLs failed, so the window fell back to the browser. Clear
    the mark on the bundle's own binaries, as Properties → Unblock does; the
    user already chose to run Ember.exe. Returns how many were cleared."""
    if os.name != "nt" or not FROZEN:
        return 0
    n = 0
    for root, _, files in os.walk(Path(sys.executable).parent):
        for f in files:
            if f.lower().endswith((".dll", ".pyd", ".exe")):
                try:
                    os.remove(os.path.join(root, f) + ":Zone.Identifier")
                    n += 1
                except OSError:     # no mark, or a read-only install
                    pass
    return n


def browser_instead(url, err):
    """No window (pywebview missing, no WebView2 runtime, .NET refusing its
    DLLs…): open the browser, and say why: a windowed exe has no stderr, so
    the reason used to vanish. Details go to window.log in the data dir."""
    import traceback
    detail = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    log = server.APP_DIR / "window.log"
    try:
        log.write_text(time.strftime("%Y-%m-%d %H:%M:%S ") + detail, encoding="utf-8")
    except OSError:
        pass
    print(f"no web view ({err}); opening the browser instead", file=sys.stderr)
    webbrowser.open(url)        # the server stays up for the browser tab
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "Ember could not open its own window, so it opened your browser "
            f"instead.\n\n{type(err).__name__}: {err}\n\nDetails: {log}", "Ember", 0x30)
    return 0


def fail(msg):
    """Say what went wrong where the user will see it."""
    print(msg, file=sys.stderr)
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "Ember", 0x10)
    elif have_webview():
        import webview
        import html
        webview.create_window("Ember", html=f"<pre style='white-space:pre-wrap;"
                              f"font:14px system-ui;padding:24px'>{html.escape(msg)}</pre>",
                              width=720, height=360)
        webview.start()
    return 1


def open_app():
    started = None
    if not server_up():
        started, log = start_server()
        if not server_up():
            try:
                why = log.read_text(encoding="utf-8", errors="replace").strip()[-1500:]
            except OSError:
                why = ""
            return fail("Ember's server did not start.\n\n" + (why or f"See {log}"))
    try:
        url = server.launch_url(PORT)
    except server.NotOurServer as e:
        return fail(f"Another program answers on port {PORT}: {e}.\n\n"
                    f"Quit it, or set PORT to another value.")
    except (OSError, ValueError, KeyError) as e:
        return fail(f"Could not log in to Ember's server: {e}")

    if server.open_in() == "browser":
        webbrowser.open(url)        # the server stays up for the browser tab
        return 0
    unblock_bundle()
    err = webview_error()
    if err:
        return browser_instead(url, err)

    import webview
    # target=_blank (transcript links, "Open in browser") → default browser
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    # text_select: pywebview blocks selecting text by default (transcripts!)
    webview.create_window("Ember", url, width=1500, height=950, min_size=(900, 600),
                          text_select=True, zoomable=True)
    try:
        # a persistent profile: themes and layout live in localStorage
        webview.start(private_mode=False, storage_path=str(server.APP_DIR / "webview"))
    except Exception as e:      # e.g. no WebView2 runtime, no WebKitGTK
        return browser_instead(server.launch_url(PORT), e)
    if started:
        shutdown_server()
    return 0


def install_desktop_entry():
    """Linux menu entry for this executable (per user, no sudo). File
    managers won't run a binary on double-click; the menu will."""
    share = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    icon = share / "icons" / "hicolor" / "scalable" / "apps" / "ember.svg"
    icon.parent.mkdir(parents=True, exist_ok=True)
    src = Path(server.HERE) / ("claude-devtools.svg" if FROZEN
                               else "launchers/linux/claude-devtools.svg")
    icon.write_bytes(src.read_bytes())
    entry = share / "applications" / "ember.desktop"
    entry.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable] if FROZEN else [sys.executable, str(Path(__file__).resolve())]
    entry.write_text("[Desktop Entry]\nType=Application\nName=Ember\n"
                     "Comment=A workspace for Claude Code\n"
                     "Exec=" + " ".join(f'"{a}"' for a in argv) + "\n"
                     "Icon=ember\nTerminal=false\n"
                     "Categories=Development;Utility;\nStartupNotify=true\n")
    print(f"Added Ember to your applications menu ({entry}).")
    return 0


def main():
    global server
    reattach_std()
    if FROZEN and os.environ.get("APPIMAGE"):
        # inside an AppImage sys.executable is a mount that vanishes on exit;
        # the hooks, the menu entry and the detached server need the file
        sys.executable = os.environ["APPIMAGE"]
    args = sys.argv[1:]
    if args[:1] == ["devtools_hooks.py"]:
        import devtools_hooks
        return devtools_hooks.main([sys.argv[0]] + args[1:])
    import server
    if args[:1] == ["--server"]:
        sys.argv = [sys.argv[0]] + args[1:]
        return server.main()
    if args[:1] == ["--install"]:
        return install_desktop_entry()
    return open_app()


if __name__ == "__main__":
    sys.exit(main())
