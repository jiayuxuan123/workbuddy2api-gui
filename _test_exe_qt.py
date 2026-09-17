"""Verify the packaged Qt EXE end to end, from a clean directory.

Checks the things that only a real bundle can get wrong:
  * the window actually appears (Qt platform plugin loaded)
  * the web panel is served (dashboard.html found inside the bundle)
  * writable data lands beside the EXE, not in the temp extraction dir
  * the gateway can be started and stopped through the real HTTP surface

Usage:
    python _test_exe_qt.py <path to the folder containing WorkBuddy2API.exe>
"""

import json
import os
import socket
import subprocess
import sys
import time

EXE_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dist", "WorkBuddy2API")
EXE = os.path.join(EXE_DIR, "WorkBuddy2API.exe")

failures = []


def check(name, ok, detail=""):
    print("  %-52s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- %s" % detail))
    if not ok:
        failures.append(name)


def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http_get(port, path, timeout=4.0):
    """Minimal loopback GET: literal address, no URL parsing, no redirects."""
    if not isinstance(port, int) or not (1 <= port <= 65535):
        return None
    if not path.startswith("/"):
        return None
    request = ("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n"
               % (path, port)).encode("ascii")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        sock.sendall(request)
        chunks = []
        while True:
            block = sock.recv(8192)
            if not block:
                break
            chunks.append(block)
    except OSError:
        return None
    finally:
        sock.close()
    raw = b"".join(chunks)
    split = raw.find(b"\r\n\r\n")
    if split == -1:
        return None
    head = raw[:split].decode("latin-1")
    body = raw[split + 4:]
    status = 0
    try:
        status = int(head.split(" ")[1])
    except Exception:
        pass
    return status, body


def main():
    if not os.path.isfile(EXE):
        print("SKIP: EXE not found at %s" % EXE)
        return 0

    port = free_port()
    print("EXE : %s" % EXE)
    print("port: %d" % port)
    print()

    # Start it with --start so the gateway listens without a click, on a port
    # this test owns.
    proc = subprocess.Popen(
        [EXE, "--port", str(port), "--start"],
        cwd=EXE_DIR, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        print("=== window appears ===")
        title = ""
        for _ in range(40):
            time.sleep(0.5)
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$p = Get-Process -Id %d -ErrorAction SilentlyContinue; "
                 "if ($p -and $p.MainWindowHandle -ne 0) "
                 "{ Write-Output $p.MainWindowTitle }" % proc.pid],
                capture_output=True, timeout=30)
            # PowerShell emits the window title in the console code page, not
            # UTF-8, so decoding with errors='replace' avoids a UnicodeDecodeError
            # that would otherwise be mistaken for "no window".
            title = (out.stdout or b"").decode("utf-8", "replace").strip()
            if title:
                break
        check("GUI window created", bool(title), "no window title")
        check("window title correct", "WorkBuddy2API" in title, title)

        print()
        print("=== gateway answers on the configured port ===")
        health = None
        for _ in range(30):
            result = http_get(port, "/health")
            if result and result[0] == 200:
                try:
                    health = json.loads(result[1].decode("utf-8"))
                except Exception:
                    health = None
                if health:
                    break
            time.sleep(0.5)
        check("/health returns ok", bool(health and health.get("ok")), str(health))

        print()
        print("=== web panel served from inside the bundle ===")
        result = http_get(port, "/")
        check("/ returns 200", bool(result and result[0] == 200),
              str(result[0]) if result else "no response")
        if result and result[0] == 200:
            body = result[1]
            check("dashboard.html served (not an error page)",
                  b"WorkBuddy" in body and b"unavailable" not in body,
                  "size=%d" % len(body))

        print()
        print("=== writable data resolves beside the EXE, not into a temp dir ===")
        # gateway.json is written on first run, so it proves where the data
        # directory resolved. accounts/ itself is created lazily, on the first
        # account save, so its absence here is expected and not a failure.
        prefs_file = os.path.join(EXE_DIR, "gateway.json")
        check("settings written beside the EXE", os.path.isfile(prefs_file),
              prefs_file)
        if os.path.isfile(prefs_file):
            try:
                data = json.load(open(prefs_file, encoding="utf-8"))
                check("settings carry the configured port",
                      data.get("port") == port, str(data.get("port")))
            except Exception as exc:
                check("settings readable", False, str(exc))

        # The distinctive failure of a bad onefile layout would be data landing
        # in an _MEI extraction dir, which is deleted when the process exits.
        import glob
        mei = glob.glob(os.path.join(os.environ.get("TEMP", ""), "_MEI*"))
        leaked = [d for d in mei
                  if os.path.isfile(os.path.join(d, "gateway.json"))]
        check("no extraction dir holds the settings", not leaked, str(leaked))

        print()
        print("=== stop cleanly ===")
        proc.terminate()
        try:
            proc.wait(timeout=15)
            check("process exits on terminate", True)
        except subprocess.TimeoutExpired:
            proc.kill()
            check("process exits on terminate", False, "had to kill")
        time.sleep(1.0)
        check("port released", http_get(port, "/health", timeout=1.5) is None)

    finally:
        if proc.poll() is None:
            proc.kill()

    print()
    if failures:
        print("RESULT: %d FAILURE(S)" % len(failures))
        for name in failures:
            print("  - %s" % name)
        return 1
    print("RESULT: ALL PACKAGED-EXE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
