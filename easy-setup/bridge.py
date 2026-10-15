# /// script
# requires-python = ">=3.10"
# dependencies = ["pyserial==3.5"]
# ///
"""USB モード用の常駐中継: localhost の HTTP を Atom の USB シリアルに流す

    hook (led.sh) ──HTTP──> bridge.py (127.0.0.1:47801) ──USB シリアル──> Atom

HTTP API はデバイスと同じ(/led /rgb /)なので、led.sh も led-test.sh も宛先を変えるだけで動く。
シリアルポートは開きっぱなしにする(Atom は開くたびに DTR/RTS でリセットされるため、
hook ごとに開閉する方式は使えない。docs/HANDOFF.md 参照)。
USB を抜き差ししても自動で再接続する。setup.py が自動起動に登録する。
"""
from __future__ import annotations

import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import serial
from serial.tools import list_ports

HOST = "127.0.0.1"
PORT = 47801
BAUD = 115200
KNOWN_VIDS = {0x1A86, 0x0403, 0x10C4}   # CH9102/CH340, FTDI, CP210x
STATES = {"idle", "tool", "wait", "done", "err"}
LOG = Path(__file__).resolve().parent / "bridge.log"   # setup.py が配置した場所(--claude-dir に追従)


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def read_line_until(s: serial.Serial, pred, timeout: float) -> str | None:
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        buf += s.read(256)
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if pred(line):
                return line
    return None


class Link:
    """USB モードで動いている Atom とのシリアル接続(切れたら次の要求時に開き直す)"""

    def __init__(self) -> None:
        self.ser: serial.Serial | None = None
        self.lock = threading.Lock()
        self.retry_at = 0.0

    def _open(self) -> serial.Serial | None:
        for p in list_ports.comports():
            if p.vid not in KNOWN_VIDS:
                continue
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = p.device, BAUD, 0.2
            s.dtr = s.rts = False   # 開く前に False にしてリセットを避ける
            try:
                s.open()
                for _ in range(3):   # 開いた拍子に再起動した場合は起動を待つ
                    s.reset_input_buffer()
                    s.write(b"\nCLAWD?\n")
                    line = read_line_until(s, lambda l: l.startswith("CLAWD "), 1.5)
                    if line:
                        break
                if line and line.split()[2:3] == ["usb"]:
                    log(f"connected: {p.device}")
                    return s
                log(f"{p.device}: USB モードの Atom ではありません ({line})")
                s.close()
            except (serial.SerialException, OSError) as e:
                log(f"{p.device}: {e}")
                s.close()
        return None

    def _ensure(self) -> serial.Serial | None:
        if self.ser is None and time.time() >= self.retry_at:
            self.ser = self._open()
            if self.ser is None:
                self.retry_at = time.time() + 3   # 未接続の間は 3 秒に 1 回だけ探す
        return self.ser

    def _drop(self, e: Exception) -> None:
        log(f"disconnected: {e}")
        try:
            self.ser.close()
        except Exception:
            pass
        self.ser = None

    def send(self, line: str) -> bool:
        with self.lock:
            s = self._ensure()
            if s is None:
                return False
            try:
                s.write((line + "\n").encode())
                return True
            except (serial.SerialException, OSError) as e:
                self._drop(e)
                return False

    def status(self) -> str | None:
        with self.lock:
            s = self._ensure()
            if s is None:
                return None
            try:
                s.reset_input_buffer()
                s.write(b"STATUS\n")
                lines: list[str] = []
                end = time.time() + 1.5
                buf = b""
                while time.time() < end:
                    buf += s.read(256)
                    while b"\n" in buf:
                        raw, buf = buf.split(b"\n", 1)
                        l = raw.decode("utf-8", "replace").rstrip("\r")
                        if l == "END":
                            return "\n".join(lines) + "\n"
                        lines.append(l)
                return None
            except (serial.SerialException, OSError) as e:
                self._drop(e)
                return None


link = Link()


class Server(ThreadingHTTPServer):
    # Windows では SO_REUSEADDR を付けると同じポートに 2 つ目のプロセスも bind できてしまい(実測)、
    # 二重起動を検知できない。Windows だけ外す(他の OS では TIME_WAIT 中の再起動に必要なので残す)
    allow_reuse_address = sys.platform != "win32"


class Handler(BaseHTTPRequestHandler):
    def reply(self, code: int, body: str) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/led":
            st = q.get("s", "")
            if st not in STATES:
                return self.reply(400, "unknown state\n")
            sid = re.sub(r"\s", "", q.get("sid", ""))[:47] or "default"
            ts = q.get("ts", "0")
            ts = ts if ts.isdigit() else "0"
            ok = link.send(f"LED {st} {sid} {ts}")
            return self.reply(200 if ok else 503, "ok\n" if ok else "atom not connected\n")
        if u.path == "/rgb":
            try:
                r, g, b = (max(0, min(255, int(q.get(k, "0")))) for k in "rgb")
            except ValueError:
                return self.reply(400, "bad rgb\n")
            ok = link.send(f"RGB {r} {g} {b}")
            return self.reply(200 if ok else 503, "ok\n" if ok else "atom not connected\n")
        if u.path == "/":
            body = link.status()
            return self.reply(200, body) if body else self.reply(503, "atom not connected\n")
        if u.path == "/quit":   # setup.py がシリアルポートを使う前に止める(Windows 用)
            # 応答より先にポートを閉じ、HTTP が止まった時点でポートが空いていることを保証する
            # (Windows ではプロセスの終了とポートの解放が HTTP の停止より遅れ、setup.py が開けなかった。実測)
            with link.lock:
                if link.ser is not None:
                    link.ser.close()
                    link.ser = None
                link.retry_at = float("inf")   # 終了までに来た要求で開き直さない
            self.reply(200, "bye\n")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        self.reply(404, "not found\n")

    def log_message(self, *args) -> None:   # 1 リクエストごとのログは出さない
        pass


def main() -> None:
    try:
        srv = Server((HOST, PORT), Handler)
    except OSError as e:
        log(f"port {PORT} を使えません(既に起動している?): {e}")
        sys.exit(1)
    log(f"listening on http://{HOST}:{PORT}")
    link.send("")   # 起動時に一度つないでおく
    srv.serve_forever()
    log("stopped")


if __name__ == "__main__":
    main()
