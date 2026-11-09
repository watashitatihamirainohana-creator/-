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
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ---------------------------------------------------------------- キー注入

class DryRunBackend:
    """キーを押さずにログだけ出す (動作確認用)。"""

    def __init__(self, keys):
        self.keys = keys

    def press(self, i):
        print("down %s" % self.keys[i], flush=True)

    def release(self, i):
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

    def __init__(self, backend, n, gap):
        self.backend = backend
        self.gap = gap
        self.lock = threading.Lock()
        self.want = [False] * n      # クライアントが押していたい状態
        self.down = [False] * n      # 実際に PC へ送ったキー状態
        self.pending = [False] * n   # 再打鍵の「押す」待ち

    def handle(self, op, i):
        with self.lock:
            if op == "u":
                self.want[i] = False
                if self.down[i]:
                    self.backend.release(i)
                    self.down[i] = False
                return
            self.want[i] = True
            if op == "r" and self.down[i]:
                self.backend.release(i)
                self.down[i] = False
                if self.gap > 0:
                    self.pending[i] = True
                    threading.Timer(self.gap, self._fire, (i,)).start()
                    return
            if not self.down[i] and not self.pending[i]:
                self.backend.press(i)
                self.down[i] = True

    def _fire(self, i):
        with self.lock:
            self.pending[i] = False
            if self.want[i] and not self.down[i]:
                self.backend.press(i)
                self.down[i] = True


# ---------------------------------------------------------------- HTTP / WebSocket

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = None
    lanes = None

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
        print("[接続] %s" % peer, flush=True)
        held = set()
        try:
            self.ws_send(json.dumps(self.cfg))
            while True:
                op, data = self.ws_read()
                if op is None or op == 8:
                    break
                if op == 9:
                    self.wfile.write(b"\x8a" + bytes([len(data)]) + data)
                    continue
                if op != 1:
                    continue
                # 1 フレームに複数イベント可: "d0 d2 u1" (d=押す u=離す r=再打鍵)
                for tok in data.decode("ascii", "ignore").split():
                    c, num = tok[0], tok[1:]
                    if c not in "dur" or not num.isdigit():
                        continue
                    i = int(num)
                    if i >= len(self.cfg["keys"]):
                        continue
                    (held.discard if c == "u" else held.add)(i)
                    self.lanes.handle(c, i)
        except OSError:
            pass
        finally:
            for i in held:  # 切断時に押しっぱなしを残さない
                self.lanes.handle("u", i)
            print("[切断] %s" % peer, flush=True)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # 実際には何も送信しない
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


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
    ap.add_argument("--dry-run", action="store_true",
                    help="キーを押さずログだけ出す (動作確認用)")
    a = ap.parse_args()

    keys = a.keys
    if len(keys) == 1 and len(keys[0]) > 1 and keys[0].lower() not in WindowsBackend.NAMED:
        keys = list(keys[0])  # --keys dfjk も許可

    if a.dry_run:
        backend = DryRunBackend(keys)
    elif sys.platform == "win32":
        backend = WindowsBackend(keys)
    else:
        raise SystemExit("キー入力の注入は Windows のみ対応です。--dry-run で動作確認だけできます。")

    Handler.cfg = {"keys": keys, "slide": a.slide, "retrigger": not a.no_retrigger}
    Handler.lanes = Lanes(backend, len(keys), a.retrigger_ms / 1000.0)
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    srv.daemon_threads = True
    print("レーン: %s" % " | ".join(keys))
    print("iPad の Safari でここを開く →  http://%s:%d/" % (lan_ip(), a.port))
    print("終了は Ctrl+C", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
