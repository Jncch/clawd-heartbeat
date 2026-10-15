# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pyserial==3.5",
#   "esptool==4.8.1",
# ]
# ///
"""Clawd Heartbeat かんたんセットアップ(macOS / Linux は WiFi・USB、Windows は USB のみ)

    uv run setup.py                # 書き込み → 接続方式(WiFi / USB)の設定 → 疎通確認 → hook 設定
    uv run setup.py --wifi-only    # WiFi だけ設定し直す(ネットワークを変えたとき)
    uv run setup.py --remove-bridge  # USB モードの常駐中継(bridge)を停止して自動起動を解除
    uv run setup.py --build        # (開発者向け)pio でビルドして dist/ に配布用 bin を作る

Atom を USB で PC につないでから実行する。
- WiFi モード: hook → HTTP → Atom。SSID/パスワードは PC が今つないでいるネットワークから取得を試みる
- USB モード: hook → HTTP → bridge.py(PC 上に常駐)→ USB シリアル → Atom。社内ネットワーク等向け
"""
from __future__ import annotations

import argparse
import getpass
import json
import locale
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import NoReturn
from pathlib import Path

import serial
from serial.tools import list_ports

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
FW_DIR = HERE / "firmware"
DIST_BIN = HERE / "dist" / "clawd-heartbeat.bin"
LED_SH = REPO / "led.sh"
PROTO = "1"
BAUD = 115200
HOSTNAME = "atom.local"

# USB モードの常駐中継。リポジトリを移動しても動くよう ~/.claude 配下にコピーして使う
BRIDGE_SRC = HERE / "bridge.py"
BRIDGE_PORT = 47801   # bridge.py の PORT と同じ
BRIDGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}"
BRIDGE_LABEL = "com.clawd-heartbeat.bridge"

# Atom Lite の USB-UART(ロットにより異なる): CH9102 / FTDI / CP210x
KNOWN_VIDS = {0x1A86: "CH9102/CH340", 0x0403: "FTDI", 0x10C4: "CP210x"}

HOOK_EVENTS = [
    # (イベント, 引数, matcher を付けるか)  README の hook 設定と同じ
    ("SessionStart", "idle", False),
    ("UserPromptSubmit", "tool", False),
    ("PreToolUse", "tool", True),
    ("PostToolUse", "tool", True),
    ("PostToolUseFailure", "tool", True),
    ("PermissionRequest", "wait", True),
    ("PermissionDenied", "idle", True),
    ("Stop", "done", False),
    ("StopFailure", "err", False),
    ("SessionEnd", "idle", False),
]


# ---------------------------------------------------------------- 表示・入力

def step(msg: str) -> None:
    print(f"\n==> {msg}")


def info(msg: str) -> None:
    print(f"    {msg}")


def die(msg: str) -> NoReturn:
    print(f"\n[エラー] {msg}", file=sys.stderr)
    sys.exit(1)


def ask_yes(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        a = input(f"    {question} {suffix} ").strip().lower()
        if not a:
            return default
        if a in ("y", "yes"):
            return True
        if a in ("n", "no"):
            return False


def mask(pw: str) -> str:
    # 文字は一切出さない(ログやスクリーンショット経由の漏えいを避ける)
    return f"{'*' * len(pw)} ({len(pw)}文字)" if pw else "(なし)"


def decode(b: bytes) -> str:
    # Windows のコマンドはコードページ 932 の環境でもパイプ経由だと UTF-8 で出すことがある(netsh で実測)ので、
    # UTF-8 で読めなければロケールの文字コードで読み直す
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode(locale.getpreferredencoding(False), "replace")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, **kw)
    return subprocess.CompletedProcess(r.args, r.returncode, decode(r.stdout), decode(r.stderr))


def driverless_devices() -> list[str]:
    """Windows: USB 機器としては見えているが、ドライバがなく COM ポートになっていない Atom を探す"""
    if platform.system() != "Windows":
        return []
    vids = "|".join(f"{v:04X}" for v in KNOWN_VIDS)
    ps = (f"Get-PnpDevice -PresentOnly | Where-Object {{ $_.InstanceId -match 'VID_({vids})' -and $_.Status -ne 'OK' }}"
          " | ForEach-Object { $_.FriendlyName + ' [' + $_.InstanceId + ']' }")   # " を使わない(引数のエスケープを避ける)
    try:
        return [l.strip() for l in run(["powershell", "-NoProfile", "-Command", ps]).stdout.splitlines() if l.strip()]
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------- ポート検出

def find_port(explicit: str | None) -> str:
    if explicit:
        return explicit
    ports = list(list_ports.comports())
    cands = [p for p in ports if p.vid in KNOWN_VIDS]
    if len(cands) == 1:
        p = cands[0]
        info(f"{p.device}  ({KNOWN_VIDS[p.vid]})")
        return p.device
    if not cands:
        info("Atom らしい USB シリアルが見つかりませんでした。")
        missing = driverless_devices()
        if missing:
            info("USB 機器としては認識されていますが、ドライバが入っていません:")
            for d in missing:
                info(f"  {d}")
            info("- VID_0403(FTDI): 設定 → Windows Update → 詳細オプション → オプションの更新プログラム、")
            info("  または FTDI 公式の VCP ドライバを入れる: https://ftdichip.com/drivers/vcp-drivers/")
            info("- VID_1A86(CH9102): M5Stack 公式のドライバを入れる: https://docs.m5stack.com/en/download")
            info("  (どちらのドライバも M5Stack のページ「USB Driver」欄からも入手できます)")
            die("ドライバを入れてから再実行してください(管理者権限が必要です)")
        info("- 充電専用ケーブルでないか(データ通信対応のケーブルが必要)")
        info("- Windows では USB-シリアルのドライバ(FTDI / CH9102)が必要な場合があります")
        if ports:
            info("見つかったポート: " + ", ".join(p.device for p in ports))
        die("--port でポートを指定して再実行してください")
    for i, p in enumerate(cands, 1):
        info(f"[{i}] {p.device}  ({KNOWN_VIDS[p.vid]}) {p.description}")
    while True:
        a = input("    どのポートを使いますか? 番号: ").strip()
        if a.isdigit() and 1 <= int(a) <= len(cands):
            return cands[int(a) - 1].device


# ---------------------------------------------------------------- シリアル通信

def open_serial(port: str) -> serial.Serial:
    # Atom は DTR/RTS が EN/IO0 につながっており、普通に開くとリセットがかかる。
    # 開く前に両方 False にしておく(docs/NOTES.md と同じ手順)
    s = serial.Serial()
    s.port = port
    s.baudrate = BAUD
    s.timeout = 0.2
    s.dtr = False
    s.rts = False
    s.open()
    # それでも環境によっては(macOS + FTDI 版で実測)開いた瞬間にリセットされる。
    # 起動中(約 0.5 秒)に送ったコマンドは取りこぼされるので、起動が終わるまで待つ
    time.sleep(1.5)
    s.reset_input_buffer()
    return s


def read_until(s: serial.Serial, pred, timeout: float, echo: bool = False):
    """pred(line) が真になった行を返す。タイムアウトで None"""
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        chunk = s.read(256)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").strip()
            if echo and line:
                info(f"[atom] {line}")
            r = pred(line)
            if r:
                return r
    return None


def probe(port: str) -> str | None:
    """CLAWD? に応答すればその行を返す(easy-setup 版ファームが動いている)"""
    try:
        with open_serial(port) as s:
            for _ in range(3):
                s.reset_input_buffer()
                s.write(b"\nCLAWD?\n")
                line = read_until(s, lambda l: l if l.startswith("CLAWD ") else None, 1.5)
                if line:
                    return line
    except serial.SerialException as e:
        die(f"{port} を開けません: {e}\n    (他のアプリ(シリアルモニタ等)が使っていないか確認してください)")
    return None


# ---------------------------------------------------------------- 書き込み

def esptool(*args: str) -> int:
    return subprocess.call([sys.executable, "-m", "esptool", *args])


def flash(port: str, image: Path) -> None:
    for baud in ("460800", "115200"):   # 921600 は FTDI 版で失敗した(実測)
        info(f"書き込み中 ({baud} bps)...")
        rc = esptool("--chip", "esp32", "--port", port, "--baud", baud,
                     "write_flash", "0x0", str(image))
        if rc == 0:
            return
        info("失敗しました。速度を落として再試行します。")
    die("書き込みに失敗しました。Atom のボタンを押しながら USB を挿し直してから再実行してください")


def build() -> None:
    """pio でビルドし、ブートローダ等と結合した 1 ファイルの bin を dist/ に作る"""
    if not shutil.which("pio"):
        die("PlatformIO (pio) が見つかりません")
    step("ファームウェアをビルド")
    if subprocess.call(["pio", "run"], cwd=FW_DIR) != 0:
        die("ビルドに失敗しました")
    b = FW_DIR / ".pio" / "build" / "m5stack-atom"
    core = Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio"))
    boot_app0 = core / "packages" / "framework-arduinoespressif32" / "tools" / "partitions" / "boot_app0.bin"
    for f in (b / "bootloader.bin", b / "partitions.bin", boot_app0, b / "firmware.bin"):
        if not f.exists():
            die(f"{f} がありません")
    DIST_BIN.parent.mkdir(exist_ok=True)
    rc = esptool("--chip", "esp32", "merge_bin", "-o", str(DIST_BIN),
                 "0x1000", str(b / "bootloader.bin"),
                 "0x8000", str(b / "partitions.bin"),
                 "0xe000", str(boot_app0),
                 "0x10000", str(b / "firmware.bin"))
    if rc != 0:
        die("bin の結合に失敗しました")
    info(f"作成: {DIST_BIN.relative_to(REPO)}")


# ---------------------------------------------------------------- WiFi 情報の取得

def current_ssid() -> str | None:
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            dev = "en0"
            out = run(["networksetup", "-listallhardwareports"]).stdout
            m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", out)
            if m:
                dev = m.group(1)
            # macOS 14.4 以降は位置情報の許可がないと SSID が伏せられることがある
            out = run(["ipconfig", "getsummary", dev]).stdout
            m = re.search(r"^\s*SSID : (.+)$", out, re.M)
            if m and "redacted" not in m.group(1):
                return m.group(1).strip()
            out = run(["networksetup", "-getairportnetwork", dev]).stdout
            m = re.search(r"Current Wi-Fi Network: (.+)", out)
            if m:
                return m.group(1).strip()
        else:
            out = run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]).stdout
            for line in out.splitlines():
                if line.startswith("yes:"):
                    return line[4:].replace("\\:", ":")
    except FileNotFoundError:
        pass
    return None


def saved_password(ssid: str) -> str | None:
    sysname = platform.system()
    try:
        if sysname == "Darwin":
            # キーチェーンへのアクセス許可ダイアログが出る
            r = run(["security", "find-generic-password", "-D", "AirPort network password", "-a", ssid, "-w"])
            return r.stdout.rstrip("\n") if r.returncode == 0 else None
        r = run(["nmcli", "-s", "-g", "802-11-wireless-security.psk", "connection", "show", ssid])
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None
    except FileNotFoundError:
        return None


def ask_wifi() -> tuple[str, str]:
    ssid = current_ssid()
    if ssid:
        info(f"この PC がつないでいる WiFi: {ssid}")
        if not ask_yes("この WiFi を使いますか?(Atom は 2.4GHz のみ対応)"):
            ssid = None
    else:
        # macOS は位置情報の許可がないと SSID が伏せられる(パスワードは SSID さえ分かれば取れる)
        info("WiFi 名(SSID)を自動取得できませんでした。メニューバーの WiFi アイコン等で確認して入力してください")
    while not ssid:
        ssid = input("    SSID を入力: ").strip()

    pw = None
    if platform.system() == "Darwin":
        hint = "(macOS の確認ダイアログが出ます。ログインパスワードを入れて「許可」)"
    else:
        hint = "(NetworkManager から読み取ります)"
    if ask_yes(f"保存済みのパスワードを PC から取得しますか?{hint}"):
        pw = saved_password(ssid)
        if pw is None:
            info("取得できませんでした。手入力してください。")
    if pw is None:
        pw = getpass.getpass("    パスワードを入力(表示されません。なしなら空のまま Enter): ")
    info(f"SSID: {ssid} / パスワード: {mask(pw)}")
    return ssid, pw


# ---------------------------------------------------------------- WiFi 設定の送信

FAIL_HINTS = {
    "1": "SSID が見つかりません。5GHz 専用の SSID ではありませんか?(Atom は 2.4GHz のみ)",
    "4": "接続に失敗しました。パスワードを確認してください",
}


def send_wifi(port: str, ssid: str, pw: str) -> str:
    """設定を送り、接続できたら IP を返す"""
    hexs = lambda t: t.encode("utf-8").hex() or "-"
    with open_serial(port) as s:
        s.reset_input_buffer()
        s.write(f"\nWIFI {hexs(ssid)} {hexs(pw)}\n".encode())
        if not read_until(s, lambda l: l == "OK wifi saved" or l.startswith("ERR"), 3):
            die("Atom が応答しません")
        info("接続を待っています(最大 25 秒)...")

        def result(line: str):
            if line.startswith("ready: http://"):
                return ("ok", line.split("http://", 1)[1])
            if line.startswith("wifi: failed"):
                return ("fail", line.rsplit("=", 1)[-1])
            return None

        r = read_until(s, result, 25)
    if r is None:
        die("時間内に接続できませんでした")
    if r[0] == "fail":
        die(FAIL_HINTS.get(r[1], f"WiFi に接続できませんでした(status={r[1]})。SSID・パスワード・2.4GHz かを確認してください"))
    return r[1]


def http_ok(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/", timeout=3) as r:
            return r.read().startswith(b"state=")
    except Exception:
        return False


def mdns_ok() -> bool:
    # getaddrinfo にはタイムアウトがないのでスレッドで待つ。IPv4 のみ(ESP32 は AAAA を返さない)
    res: list = []

    def lookup():
        try:
            res.append(socket.getaddrinfo(HOSTNAME, 80, socket.AF_INET)[0][4][0])
        except OSError:
            pass

    t = threading.Thread(target=lookup, daemon=True)
    t.start()
    t.join(5)
    return bool(res) and http_ok(f"http://{res[0]}")


# ---------------------------------------------------------------- hook 設定

def install_hooks(claude_dir: Path, atom_url: str) -> None:
    claude_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")

    # led.sh: リポジトリ直下のものをコピー(宛先 URL だけ差し替え)
    src = LED_SH.read_text("utf-8")
    if atom_url != f"http://{HOSTNAME}":
        src = src.replace(f'ATOM_URL="http://{HOSTNAME}"', f'ATOM_URL="{atom_url}"')
    dst = claude_dir / "led.sh"
    if dst.exists() and dst.read_text("utf-8") == src:
        info(f"{dst} は最新です")
    else:
        if dst.exists():
            if not ask_yes(f"{dst} が既にあり内容が異なります。上書きしますか?(バックアップを残します)"):
                info("led.sh の更新をスキップしました")
                src = None
            else:
                shutil.copy2(dst, dst.with_name(f"led.sh.bak-{stamp}"))
        if src is not None:
            dst.write_text(src, "utf-8", newline="\n")   # Windows でも LF のまま(bash で動かすため)
            dst.chmod(0o755)
            info(f"{dst} を配置しました")

    # settings.json: 既存の設定を残したまま、led.sh の hook がないイベントにだけ追加する
    settings_path = claude_dir / "settings.json"
    settings: dict = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text("utf-8"))
        except json.JSONDecodeError as e:
            die(f"{settings_path} が JSON として読めません({e})。手で直してから再実行してください")
    hooks = settings.setdefault("hooks", {})
    added = []
    for event, arg, use_matcher in HOOK_EVENTS:
        entries = hooks.setdefault(event, [])
        if any("led.sh" in h.get("command", "") for e in entries for h in e.get("hooks", [])):
            continue
        entry: dict = {"hooks": [{"type": "command", "command": f"$HOME/.claude/led.sh {arg}", "async": True}]}
        if use_matcher:
            entry = {"matcher": "*", **entry}
        entries.append(entry)
        added.append(event)
    if not added:
        info("hook は設定済みです")
        return
    info("追加する hook: " + ", ".join(added))
    if not ask_yes(f"{settings_path} に追加しますか?(バックアップを残します)"):
        info("hook の追加をスキップしました(README の手順で手動設定できます)")
        return
    if settings_path.exists():
        shutil.copy2(settings_path, settings_path.with_name(f"settings.json.bak-{stamp}"))
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", "utf-8")
    info("追加しました。Claude Code を再起動すると有効になります")


# ---------------------------------------------------------------- USB モード(bridge)

def bridge_dir(claude_dir: Path) -> Path:
    return claude_dir / "clawd-heartbeat"


def uv_exe(windowless: bool = False) -> str:
    # uv run の中では UV に uv 本体のパスが入っている
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv:
        die("uv が見つかりません")
    if windowless and platform.system() == "Windows":
        uvw = Path(uv).with_name("uvw.exe")   # コンソール窓を出さない版
        if uvw.exists():
            return str(uvw)
    return uv


def launchd_plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{BRIDGE_LABEL}.plist"


def systemd_unit() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "clawd-bridge.service"


def bridge_running() -> bool:
    try:
        with urllib.request.urlopen(BRIDGE_URL + "/", timeout=2):
            return True
    except urllib.error.HTTPError:
        return True    # 503(Atom 未接続)でも bridge 自体は動いている
    except Exception:
        return False


def stop_bridge() -> None:
    """シリアルポートを掴んでいるので、setup.py がポートを使う前に止める"""
    sysname = platform.system()
    if sysname == "Darwin" and launchd_plist().exists():
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{BRIDGE_LABEL}"])
    elif sysname == "Linux" and systemd_unit().exists():
        run(["systemctl", "--user", "stop", "clawd-bridge.service"])
    if bridge_running():   # Windows、または手動起動のもの
        try:
            urllib.request.urlopen(BRIDGE_URL + "/quit", timeout=2).read()
        except Exception:
            pass
    for _ in range(20):
        if not bridge_running():
            return
        time.sleep(0.25)
    die(f"bridge が止まりません。{BRIDGE_URL} を使っているプロセスを終了してから再実行してください")


def install_bridge(claude_dir: Path) -> None:
    d = bridge_dir(claude_dir)
    d.mkdir(parents=True, exist_ok=True)
    script = d / "bridge.py"
    shutil.copy2(BRIDGE_SRC, script)
    info(f"{script} を配置しました")
    sysname = platform.system()

    if sysname == "Darwin":
        import plistlib
        plist = launchd_plist()
        plist.parent.mkdir(parents=True, exist_ok=True)
        with plist.open("wb") as f:
            plistlib.dump({
                "Label": BRIDGE_LABEL,
                "ProgramArguments": [uv_exe(), "run", "--script", str(script)],
                "RunAtLoad": True,
                "KeepAlive": True,          # 落ちても自動で再起動
                "ThrottleInterval": 10,
                "StandardErrorPath": str(d / "bridge-stderr.log"),
            }, f)
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{BRIDGE_LABEL}"])
        r = run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)])
        if r.returncode != 0:
            die(f"launchd への登録に失敗しました: {r.stderr.strip()}")
        info(f"ログイン時に自動起動します({plist})")

    elif sysname == "Windows":
        import winreg
        uvw = uv_exe(windowless=True)
        cmd = f'"{uvw}" run --script "{script}"'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ClawdHeartbeatBridge", 0, winreg.REG_SZ, cmd)
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        subprocess.Popen([uvw, "run", "--script", str(script)], creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        info("ログオン時に自動起動します(レジストリの Run に登録)")

    else:
        unit = systemd_unit()
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            "[Unit]\nDescription=Clawd Heartbeat USB bridge\n\n"
            f"[Service]\nExecStart={uv_exe()} run --script {script}\nRestart=always\nRestartSec=5\n\n"
            "[Install]\nWantedBy=default.target\n", "utf-8")
        run(["systemctl", "--user", "daemon-reload"])
        r = run(["systemctl", "--user", "enable", "--now", "clawd-bridge.service"])
        if r.returncode != 0:
            die(f"systemd への登録に失敗しました: {r.stderr.strip()}")
        info(f"ログイン時に自動起動します({unit})")
        info("シリアルポートの権限エラーが出る場合は dialout グループへの追加が必要です")

    # 初回は uv が依存ライブラリを取ってくるので少し待つ
    info("bridge の起動を待っています...")
    for _ in range(120):
        try:
            with urllib.request.urlopen(BRIDGE_URL + "/", timeout=2) as r:
                if r.read().startswith(b"state="):
                    info(f"bridge が Atom とつながりました({BRIDGE_URL})")
                    return
        except Exception:
            pass
        time.sleep(0.5)
    die(f"bridge が Atom とつながりません。ログを確認してください: {d / 'bridge.log'}")


def remove_bridge() -> None:
    stop_bridge()
    sysname = platform.system()
    if sysname == "Darwin":
        launchd_plist().unlink(missing_ok=True)
    elif sysname == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, "ClawdHeartbeatBridge")
        except FileNotFoundError:
            pass
    elif systemd_unit().exists():
        run(["systemctl", "--user", "disable", "clawd-bridge.service"])
        systemd_unit().unlink()
        run(["systemctl", "--user", "daemon-reload"])


def bridge_installed() -> bool:
    sysname = platform.system()
    if sysname == "Darwin":
        return launchd_plist().exists()
    if sysname == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
                winreg.QueryValueEx(k, "ClawdHeartbeatBridge")
                return True
        except FileNotFoundError:
            return False
    return systemd_unit().exists()


def set_usb_mode(port: str) -> None:
    with open_serial(port) as s:
        s.reset_input_buffer()
        s.write(b"\nMODE usb\n")
        if not read_until(s, lambda l: l == "OK usb mode", 3):
            die("Atom が USB モードに切り替わりません")
    info("Atom を USB モードにしました(WiFi は使いません)")


# ---------------------------------------------------------------- main

def ask_mode() -> str:
    info("[1] WiFi — Atom を WiFi につなぐ。PC と USB でつながっていなくても使える")
    info("[2] USB  — Atom を PC の USB につないだまま使う。社内ネットワーク等で WiFi が使えない場合向け")
    info("           (PC に小さな常駐プログラムを置きます)")
    while True:
        a = input("    どちらにしますか? 番号: ").strip()
        if a in ("1", "2"):
            return "wifi" if a == "1" else "usb"


def main() -> None:
    ap = argparse.ArgumentParser(description="Clawd Heartbeat かんたんセットアップ")
    ap.add_argument("--port", help="シリアルポート(省略時は自動検出)")
    ap.add_argument("--firmware", type=Path, default=DIST_BIN, help="書き込む bin(既定: dist/clawd-heartbeat.bin)")
    ap.add_argument("--mode", choices=["wifi", "usb"], help="接続方式(省略時は質問する)")
    ap.add_argument("--wifi-only", action="store_true", help="WiFi だけ設定し直す")
    ap.add_argument("--no-hooks", action="store_true", help="hook 設定をしない")
    ap.add_argument("--claude-dir", type=Path, default=Path.home() / ".claude", help="hook の設定先(既定: ~/.claude)")
    ap.add_argument("--remove-bridge", action="store_true", help="USB モードの常駐中継を停止して自動起動を解除")
    ap.add_argument("--build", action="store_true", help="(開発者向け)ビルドして dist/ に bin を作るだけ")
    a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)   # esptool の出力と順序が入れ替わらないように

    if a.build:
        build()
        return
    if a.remove_bridge:
        remove_bridge()
        info("bridge を停止し、自動起動を解除しました")
        return

    print("Clawd Heartbeat かんたんセットアップ")
    mode = "wifi" if a.wifi_only else a.mode
    if platform.system() == "Windows":
        # Windows の WiFi モードは検証で接続まで確認できなかったため対象外(USB モードのみ)
        if mode == "wifi":
            die("Windows では WiFi モードに対応していません。USB モード(--mode usb)を使ってください")
        mode = "usb"
    if mode is None:
        step("接続方式を選んでください")
        mode = ask_mode()

    # USB モードの bridge がシリアルポートを掴んでいると書き込みも設定もできないので先に止める
    had_bridge = bridge_installed() or bridge_running()
    if had_bridge:
        step("常駐中の bridge を一時停止")
        stop_bridge()

    step("Atom を探しています")
    port = find_port(a.port)

    # 1. 書き込み
    reply = probe(port)
    if a.wifi_only:
        if not reply:
            die("easy-setup 版のファームウェアが動いていません。--wifi-only を外して実行してください")
    else:
        step("ファームウェアの書き込み")
        do_flash = True
        if reply and reply.split()[1] == PROTO:
            info("easy-setup 版のファームウェアが既に入っています")
            do_flash = ask_yes("書き込み直しますか?", default=False)
        if do_flash:
            if not a.firmware.exists():
                die(f"{a.firmware} がありません(開発者は `uv run setup.py --build` で作れます)")
            info("Atom のファームウェアは上書きされます")
            if not ask_yes("書き込みますか?"):
                die("中止しました")
            flash(port, a.firmware)
            time.sleep(2)   # 再起動待ち
            reply = probe(port)
            if not reply:
                die("書き込み後に Atom が応答しません。USB を挿し直して再実行してください")

    if mode == "usb":
        # 2. USB モード: Atom を切り替えて bridge を常駐させる
        step("USB モードの設定")
        set_usb_mode(port)
        install_bridge(a.claude_dir)
        atom_url = BRIDGE_URL
    else:
        if had_bridge:
            info("USB モードの bridge は WiFi モードでは不要です")
            if ask_yes("bridge の自動起動を解除しますか?"):
                remove_bridge()

        # 2. WiFi
        step("WiFi の設定")
        ip = None
        parts = reply.split()
        if len(parts) >= 4 and parts[2] == "connected":
            info(f"すでに WiFi に接続済みです({parts[3]})")
            if not ask_yes("設定し直しますか?", default=a.wifi_only):
                ip = parts[3]
        if ip is None:
            ssid, pw = ask_wifi()
            ip = send_wifi(port, ssid, pw)
            info(f"接続しました: {ip}  (LED が青になります)")

        # 3. 疎通確認
        step("PC から Atom に届くか確認")
        if not http_ok(f"http://{ip}"):
            die(f"http://{ip} に届きません。PC と Atom が同じネットワークにいるか確認してください"
                "(ゲスト WiFi や AP アイソレーションでは端末同士が通信できません。USB モードを検討してください)")
        if mdns_ok():
            atom_url = f"http://{HOSTNAME}"
            info(f"{atom_url} で届きました")
        else:
            atom_url = f"http://{ip}"
            info(f"{HOSTNAME} は名前解決できませんでした。IP({ip})を使います")
            info("IP が変わると届かなくなるので、ルーターの DHCP 予約で固定するのがおすすめです")

    # 4. hook(led.sh の宛先をモードに合わせる)
    if not a.no_hooks and not a.wifi_only:
        step("Claude Code の hook 設定")
        install_hooks(a.claude_dir, atom_url)
    elif a.wifi_only and atom_url != f"http://{HOSTNAME}":
        info(f"~/.claude/led.sh の ATOM_URL を {atom_url} に合わせてください")

    step("完了")
    info("Claude Code で何か作業させると、LED が白(作業中)→ 緑(完了)→ 青(待機)と変わります")
    if mode == "usb":
        info("Atom は PC の USB につないだままにしてください(抜き差ししても自動で再接続します)")
        info(f"手動テスト: ATOM={BRIDGE_URL} ../led-test.sh status")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n中止しました")
        sys.exit(130)
