#!/usr/bin/env python3
"""Pi Pad: a touch game controller website for Roblox on a Raspberry Pi running LineageOS.
Runs in Termux, standard library only. It uses the scrcpy server (no root) to inject real multi-touch.

  pkg install -y python android-tools
  PI_TOKEN='long-secret' ADB_SERIAL='localhost:5555' python server.py
The website (index.html in the repo root) is hosted on GitHub Pages or Vercel and connects here.
"""
import asyncio, base64, hashlib, hmac, json, os, random, socket, struct, subprocess, threading, urllib.request
from urllib.parse import urlparse, parse_qs

TOKEN = os.environ.get("PI_TOKEN", "")
SERIAL = os.environ.get("ADB_SERIAL", "")
PORT = int(os.environ.get("PORT", "8080"))
VER = os.environ.get("SCRCPY_VER", "3.1")  # must match the jar below
JAR = os.path.expanduser(f"~/scrcpy-server-v{VER}.jar")
FWD = 27183
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"), "/icon.svg": ("icon.svg", "image/svg+xml")}
if len(TOKEN) < 12 or not SERIAL:
    raise SystemExit("Set PI_TOKEN (12+ characters) and ADB_SERIAL (for example localhost:37123).")


def adb(*a, timeout=20):
    return subprocess.run(["adb", "-s", SERIAL, *a], capture_output=True, timeout=timeout)


class Ctrl:
    sock = None; w = 1920; h = 1080; error = ""
    lock = threading.Lock(); active = {}

    def start(self):
        try:
            if not os.path.exists(JAR):
                urllib.request.urlretrieve(
                    f"https://github.com/Genymobile/scrcpy/releases/download/v{VER}/scrcpy-server-v{VER}", JAR)
            adb("push", JAR, "/data/local/tmp/scrcpy-server.jar")
            scid = "%08x" % random.randint(1, 0x7FFFFFFF)
            subprocess.Popen(["adb", "-s", SERIAL, "shell", "CLASSPATH=/data/local/tmp/scrcpy-server.jar",
                              "app_process", "/", "com.genymobile.scrcpy.Server", VER, f"scid={scid}",
                              "tunnel_forward=true", "video=false", "audio=false", "control=true", "cleanup=false"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            adb("forward", "--remove", f"tcp:{FWD}")
            adb("forward", f"tcp:{FWD}", f"localabstract:scrcpy_{scid}")
            for _ in range(20):
                try:
                    s = socket.create_connection(("127.0.0.1", FWD), timeout=3)
                    if s.recv(1):  # dummy byte means the server is ready
                        s.recv(64)  # device name
                        s.settimeout(None)
                        threading.Thread(target=self.drain, args=(s,), daemon=True).start()
                        self.sock = s; self.error = ""; self.refresh_size()
                        return True
                    s.close()
                except OSError:
                    pass
                threading.Event().wait(0.5)
            self.error = "scrcpy server did not start. Check Wireless debugging and SCRCPY_VER."
        except Exception as e:
            self.error = f"Setup failed: {e}"
        return False

    def drain(self, s):
        try:
            while s.recv(4096):
                pass
        except OSError:
            pass
        self.sock = None

    def refresh_size(self):
        png = adb("exec-out", "screencap", "-p").stdout
        if png[:4] == b"\x89PNG":
            self.w, self.h = struct.unpack(">II", png[16:24])
        return png

    def send(self, data):
        with self.lock:
            if self.sock:
                try:
                    self.sock.sendall(data)
                except OSError:
                    self.sock = None

    def touch(self, action, pid, nx, ny):  # action: 0 down, 1 up, 2 move
        x = min(max(int(nx * self.w), 0), self.w - 1); y = min(max(int(ny * self.h), 0), self.h - 1)
        if action == 1: self.active.pop(pid, None)
        else: self.active[pid] = (nx, ny)
        self.send(struct.pack(">BBQIIHHHII", 2, action, pid, x, y, self.w, self.h, 0 if action == 1 else 0xFFFF, 0, 0))

    def key(self, code, down):
        self.send(struct.pack(">BBIII", 0, 0 if down else 1, code, 0, 0))

    def release_all(self):
        for pid, (x, y) in list(self.active.items()):
            self.touch(1, pid, x, y)


ctrl = Ctrl()
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def frame(obj):
    b = json.dumps(obj).encode()
    return bytes([0x81, len(b)]) + b if len(b) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(b)) + b


async def websocket(r, w):
    while True:
        b1, b2 = await r.readexactly(2)
        op, n = b1 & 15, b2 & 127
        if n == 126: n = struct.unpack(">H", await r.readexactly(2))[0]
        elif n == 127: n = struct.unpack(">Q", await r.readexactly(8))[0]
        mask = await r.readexactly(4) if b2 & 128 else b"\0\0\0\0"
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(await r.readexactly(n)))
        if op == 8: return
        if op == 9: w.write(bytes([0x8A, len(data)]) + data); continue
        if op != 1: continue
        m = json.loads(data)
        t = m.get("t")
        if t in ("down", "move", "up"):
            ctrl.touch({"down": 0, "up": 1, "move": 2}[t], int(m["id"]), float(m["x"]), float(m["y"]))
        elif t == "key":
            ctrl.key(int(m["k"]), bool(m["d"]))
        elif t == "ping":
            w.write(frame({"pong": m.get("ts"), "ok": ctrl.sock is not None, "err": ctrl.error}))
        elif t == "retry" and not ctrl.sock:
            await asyncio.get_running_loop().run_in_executor(None, ctrl.start)
            w.write(frame({"ok": ctrl.sock is not None, "err": ctrl.error}))


async def handle(r, w):
    try:
        head = (await r.readuntil(b"\r\n\r\n")).decode(errors="ignore").split("\r\n")
        _, target, _ = head[0].split(" ", 2)
        h = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in head[1:] if ":" in l}
        u = urlparse(target); q = parse_qs(u.query).get("token", [""])[0]
        if u.path in STATIC and os.path.exists(os.path.join(ROOT, STATIC[u.path][0])):
            body = open(os.path.join(ROOT, STATIC[u.path][0]), "rb").read()
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + STATIC[u.path][1].encode() + b"\r\nCache-Control: no-store\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        elif not hmac.compare_digest(q.encode(), TOKEN.encode()):
            await asyncio.sleep(1); w.write(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        elif u.path == "/screen":
            png = await asyncio.get_running_loop().run_in_executor(None, ctrl.refresh_size)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nCache-Control: no-store\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(png) + png)
        elif u.path == "/ws" and h.get("upgrade", "").lower() == "websocket":
            acc = base64.b64encode(hashlib.sha1(h["sec-websocket-key"].encode() + GUID).digest())
            w.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
            w.write(frame({"ok": ctrl.sock is not None, "err": ctrl.error}))
            try:
                await websocket(r, w)
            finally:
                ctrl.release_all()
        else:
            w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await w.drain()
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
        pass
    finally:
        w.close()





async def main():
    threading.Thread(target=ctrl.start, daemon=True).start()
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"Pi Pad on port {PORT}. Starting the control channel (first run downloads the scrcpy server)...")
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
