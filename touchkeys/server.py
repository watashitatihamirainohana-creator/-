#!/usr/bin/env python3
"""touchkeys - iPad (ブラウザ) を PC 音ゲーの鍵盤コントローラーにする。

PC でこのスクリプトを起動し、表示された URL を iPad の Safari で開く。
依存ライブラリなし (Python 3.8+ 標準ライブラリのみ)。キー入力の注入は Windows のみ対応。

    python server.py --keys d f j k
    python server.py --keys s d f space j k l
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import socketserver
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------- キー注入

class DryRunBackend:
    """キーを押さずにログだけ出す (動作確認用)。"""

    def __init__(self, keys, quiet=False):
        self.keys = keys
        self.quiet = quiet

    def press(self, i):
        if not self.quiet:
            print("down %s" % self.keys[i], flush=True)

    def release(self, i):
        if not self.quiet:
            print("up   %s" % self.keys[i], flush=True)


class WindowsBackend:
    """SendInput + スキャンコード。DirectInput / RawInput のゲームにも届く。"""

    NAMED = {
        "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "esc": 0x1B,
        "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1,
        "ctrl": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3,
        "alt": 0xA4, "lalt": 0xA4, "ralt": 0xA5,
        "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    }
    EXTENDED = {0xA3, 0xA5, 0x25, 0x26, 0x27, 0x28}

    def __init__(self, keys):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        ulong_ptr = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ulong_ptr)]

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ulong_ptr)]

        class _U(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self.INPUT = INPUT
        self.KEYBDINPUT = KEYBDINPUT
        self.user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        self.user32.SendInput.restype = wintypes.UINT
        self.user32.VkKeyScanW.argtypes = (wintypes.WCHAR,)
        self.user32.VkKeyScanW.restype = ctypes.c_short
        self.codes = [self._resolve(k) for k in keys]

    def _resolve(self, name):
        n = name.lower()
        if n in self.NAMED:
            vk = self.NAMED[n]
        elif len(n) > 1 and n[0] == "f" and n[1:].isdigit() and 1 <= int(n[1:]) <= 12:
            vk = 0x70 + int(n[1:]) - 1
        elif len(name) == 1:
            r = self.user32.VkKeyScanW(name)
            if r == -1:
                raise SystemExit("このキーボード配列では入力できないキーです: %r" % name)
            vk = r & 0xFF
        else:
            raise SystemExit("不明なキー名です: %r" % name)
        scan = self.user32.MapVirtualKeyW(vk, 0)
        if not scan:
            raise SystemExit("スキャンコードを取得できません: %r" % name)
        return scan, (1 if vk in self.EXTENDED else 0)

    def _send(self, i, up):
        scan, ext = self.codes[i]
        flags = 0x0008 | ext | (0x0002 if up else 0)  # SCANCODE | EXTENDED | KEYUP
        inp = self.INPUT(type=1)
        inp.u.ki = self.KEYBDINPUT(0, scan, flags, 0, 0)
        self.user32.SendInput(1, self.ctypes.byref(inp), self.ctypes.sizeof(inp))

    def press(self, i):
        self._send(i, False)

    def release(self, i):
        self._send(i, True)


# ---------------------------------------------------------------- レーン状態

class Lanes:
    """レーンごとのキー状態。再打鍵 (離す→少し待つ→押す) で押しっぱなしにならないよう管理する。"""

    def __init__(self, backend, n, gap, log=None):
        self.backend = backend
        self.gap = gap
        self.log = log               # 診断ログ用 (文字列を受け取る関数)
        self.lock = threading.Lock()
        self.want = [False] * n      # クライアントが押していたい状態
        self.down = [False] * n      # 実際に PC へ送ったキー状態
        self.pending = [False] * n   # 再打鍵の「押す」待ち
        self.hits = [0] * n          # 押下回数 (GUI の表示用)

    def _press(self, i):
        self.backend.press(i)
        self.down[i] = True
        self.hits[i] += 1
        if self.log:
            self.log("key down %d" % i)

    def _release(self, i):
        self.backend.release(i)
        self.down[i] = False
        if self.log:
            self.log("key up   %d" % i)

    def handle(self, op, i):
        with self.lock:
            if op == "u":
                self.want[i] = False
                if self.down[i]:
                    self._release(i)
                return
            self.want[i] = True
            if op == "r" and self.down[i]:
                self._release(i)
                if self.gap > 0:
                    self.pending[i] = True
                    threading.Timer(self.gap, self._fire, (i,)).start()
                    return
            if not self.down[i] and not self.pending[i]:
                self._press(i)

    def _fire(self, i):
        with self.lock:
            self.pending[i] = False
            if self.want[i] and not self.down[i]:
                self._press(i)


# ---------------------------------------------------------------- HTTP / WebSocket

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = None
    lanes = None
    quiet = False
    log = None                       # 診断ログ用 (文字列を受け取る関数)
    conns = set()
    conns_lock = threading.Lock()

    @classmethod
    def kick_all(cls):
        """全クライアントを切断する (設定変更後、自動再接続で新しい設定を配る)。"""
        with cls.conns_lock:
            for c in list(cls.conns):
                try:
                    c.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self.websocket()
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self.send_error(404)
            return
        with open(os.path.join(HERE, "index.html"), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def ws_send(self, text):
        data = text.encode()
        head = b"\x81" + (bytes([len(data)]) if len(data) < 126
                          else b"\x7e" + struct.pack(">H", len(data)))
        self.wfile.write(head + data)

    def ws_read(self):
        h = self.rfile.read(2)
        if len(h) < 2:
            return None, b""
        op, n = h[0] & 0x0F, h[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", self.rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self.rfile.read(8))[0]
        if n > 4096:
            return None, b""
        mask = self.rfile.read(4) if h[1] & 0x80 else b"\0\0\0\0"
        data = self.rfile.read(n)
        if len(data) < n:
            return None, b""
        return op, bytes(b ^ mask[k & 3] for k, b in enumerate(data))

    def websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.close_connection = True
        peer = self.client_address[0]
        if not self.quiet:
            print("[接続] %s" % peer, flush=True)
        held = set()
        cfg, lanes = self.cfg, self.lanes   # この接続の間は固定
        with self.conns_lock:
            self.conns.add(self.connection)
        try:
            self.ws_send(json.dumps(cfg))
            while True:
                op, data = self.ws_read()
                if op is None or op == 8:
                    break
                if op == 9:
                    self.wfile.write(b"\x8a" + bytes([len(data)]) + data)
                    continue
                if op != 1:
                    continue
                if self.log:
                    self.log("rx " + data.decode("ascii", "replace"))
                if data[:1] == b"#":      # iPad 側の生イベント (診断用)。キー操作はしない
                    continue
                # 1 フレームに複数イベント可: "d0 d2 u1" (d=押す u=離す r=再打鍵)
                for tok in data.decode("ascii", "ignore").split():
                    c, num = tok[0], tok[1:]
                    if c not in "dur" or not num.isdigit():
                        continue
                    i = int(num)
                    if i >= len(cfg["keys"]):
                        continue
                    (held.discard if c == "u" else held.add)(i)
                    lanes.handle(c, i)
        except OSError:
            pass
        finally:
            with self.conns_lock:
                self.conns.discard(self.connection)
            for i in held:  # 切断時に押しっぱなしを残さない
                lanes.handle("u", i)
            if not self.quiet:
                print("[切断] %s" % peer, flush=True)


class LineHandler(socketserver.StreamRequestHandler):
    """ネイティブアプリ (Swift Playgrounds 版) 用。素の TCP で 1 行 1 メッセージ。

    接続直後にサーバーが設定を JSON 1 行で送り、以後クライアントが "d0 u3" のような行を送る。
    """

    def handle(self):
        H = Handler
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = self.client_address[0]
        if not H.quiet:
            print("[接続] %s (アプリ)" % peer, flush=True)
        held = set()
        cfg, lanes = H.cfg, H.lanes
        with H.conns_lock:
            H.conns.add(self.connection)
        try:
            self.wfile.write(json.dumps(cfg).encode() + b"\n")
            for line in self.rfile:
                if len(line) > 4096:
                    break
                if H.log:
                    H.log("rx " + line.decode("ascii", "replace").strip())
                if line[:1] == b"#":
                    continue
                for tok in line.decode("ascii", "ignore").split():
                    c, num = tok[0], tok[1:]
                    if c not in "dur" or not num.isdigit():
                        continue
                    i = int(num)
                    if i >= len(cfg["keys"]):
                        continue
                    (held.discard if c == "u" else held.add)(i)
                    lanes.handle(c, i)
        except OSError:
            pass
        finally:
            with H.conns_lock:
                H.conns.discard(self.connection)
            for i in held:
                lanes.handle("u", i)
            if not H.quiet:
                print("[切断] %s (アプリ)" % peer, flush=True)


class LineServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_line_server(port):
    """アプリ用の TCP サーバーを別スレッドで起動して返す。"""
    srv = LineServer(("0.0.0.0", port), LineHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # 実際には何も送信しない
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def all_ips():
    """この PC の IPv4 アドレス候補。既定の経路のものを先頭にする。"""
    ips = [lan_ip()]
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def parse_keys(keys):
    if len(keys) == 1 and len(keys[0]) > 1 and keys[0].lower() not in WindowsBackend.NAMED \
            and not (keys[0][0] in "fF" and keys[0][1:].isdigit()):
        return list(keys[0])  # "dfjk" のような連結表記も許可
    return keys


def make_backend(keys, dry_run, quiet=False):
    if dry_run:
        return DryRunBackend(keys, quiet)
    if sys.platform == "win32":
        return WindowsBackend(keys)
    raise SystemExit("キー入力の注入は Windows のみ対応です。--dry-run で動作確認だけできます。")


def main():
    ap = argparse.ArgumentParser(description="iPad を PC 音ゲーの鍵盤コントローラーにする")
    ap.add_argument("--keys", nargs="+", default=["d", "f", "j", "k"],
                    help="左のレーンから順に割り当てるキー (例: --keys s d f space j k l)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--slide", action="store_true",
                    help="指を滑らせて隣のレーンに移ったら押し替える")
    ap.add_argument("--no-retrigger", action="store_true",
                    help="押さえ中のレーンを別の指で叩いても打ち直さない")
    ap.add_argument("--retrigger-ms", type=float, default=8,
                    help="打ち直し時に離す→押すの間へ入れる間隔ミリ秒 (既定 8)")
    ap.add_argument("--input", choices=["touch", "pointer"], default="touch",
                    help="iPad 側で使うイベントの種類 (既定 touch)")
    ap.add_argument("--dry-run", action="store_true",
                    help="キーを押さずログだけ出す (動作確認用)")
    a = ap.parse_args()

    keys = parse_keys(a.keys)
    backend = make_backend(keys, a.dry_run)

    Handler.cfg = {"keys": keys, "slide": a.slide, "retrigger": not a.no_retrigger,
                   "input": a.input, "debug": False}
    Handler.lanes = Lanes(backend, len(keys), a.retrigger_ms / 1000.0)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    start_line_server(a.port + 1)
    print("レーン: %s" % " | ".join(keys))
    print("iPad の Safari でここを開く →  http://%s:%d/" % (lan_ip(), a.port))
    print("アプリ版 (Swift Playgrounds) に入れるアドレス →  %s:%d" % (lan_ip(), a.port + 1))
    print("終了は Ctrl+C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
