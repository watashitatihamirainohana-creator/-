#!/usr/bin/env python3
"""touchkeys の GUI。ダブルクリックで起動し、QR コードを iPad のカメラで読むだけで接続できる。"""
import json
import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import qr  # noqa: E402
import server  # noqa: E402

CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULTS = {"keys": "d f j k", "slide": False, "retrigger": True,
            "retrigger_ms": 8, "test": sys.platform != "win32", "port": 8765}
IS_WIN = sys.platform == "win32"


def load_conf():
    conf = dict(DEFAULTS)
    try:
        with open(CONF, encoding="utf-8") as f:
            conf.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not IS_WIN:
        conf["test"] = True
    return conf


class App:
    def __init__(self, root):
        self.root = root
        self.conf = load_conf()
        self.lanes = None
        self.keys = []
        root.title("touchkeys")
        root.resizable(False, False)

        top = ttk.Frame(root, padding=12)
        top.grid(row=0, column=0, sticky="nsew")

        # ---- 左: QR コード
        left = ttk.Frame(top)
        left.grid(row=0, column=0, sticky="n", padx=(0, 16))
        self.qr = tk.Canvas(left, width=240, height=240, bg="white", highlightthickness=0)
        self.qr.grid(row=0, column=0)
        self.url = tk.StringVar()
        ttk.Entry(left, textvariable=self.url, state="readonly", width=30,
                  justify="center").grid(row=1, column=0, pady=(8, 2))
        self.ips = server.all_ips()
        self.ip = tk.StringVar(value=self.ips[0])
        if len(self.ips) > 1:
            row = ttk.Frame(left)
            row.grid(row=2, column=0)
            ttk.Label(row, text="つながらないときは別のアドレス:").pack(side="left")
            cb = ttk.Combobox(row, textvariable=self.ip, values=self.ips, state="readonly", width=15)
            cb.pack(side="left")
            cb.bind("<<ComboboxSelected>>", lambda e: self.draw_qr())
        ttk.Label(left, text="iPad のカメラで読み取る(PC と同じ Wi-Fi)",
                  foreground="#555").grid(row=3, column=0, pady=(4, 0))

        # ---- 右: 設定
        right = ttk.Frame(top)
        right.grid(row=0, column=1, sticky="n")
        ttk.Label(right, text="キー(左のレーンから、スペース区切り)").grid(row=0, column=0, sticky="w")
        self.v_keys = tk.StringVar(value=self.conf["keys"])
        e = ttk.Entry(right, textvariable=self.v_keys, width=34)
        e.grid(row=1, column=0, sticky="we", pady=(2, 2))
        e.bind("<Return>", lambda ev: self.apply())
        ttk.Label(right, text="例: s d f space j k l   (shift ctrl left f1 なども可)",
                  foreground="#555").grid(row=2, column=0, sticky="w", pady=(0, 8))

        self.v_retrigger = tk.BooleanVar(value=self.conf["retrigger"])
        self.v_slide = tk.BooleanVar(value=self.conf["slide"])
        self.v_test = tk.BooleanVar(value=self.conf["test"])
        self.v_ms = tk.StringVar(value=str(self.conf["retrigger_ms"]))
        ttk.Checkbutton(right, text="押さえ中のレーンを別の指で叩いたら打ち直す",
                        variable=self.v_retrigger).grid(row=3, column=0, sticky="w")
        row = ttk.Frame(right)
        row.grid(row=4, column=0, sticky="w", padx=(22, 0))
        ttk.Label(row, text="離す→押すの間隔").pack(side="left")
        ttk.Spinbox(row, from_=0, to=50, textvariable=self.v_ms, width=4).pack(side="left", padx=4)
        ttk.Label(row, text="ms").pack(side="left")
        ttk.Checkbutton(right, text="指を滑らせたら隣のレーンに押し替える",
                        variable=self.v_slide).grid(row=5, column=0, sticky="w")
        t = ttk.Checkbutton(right, text="テストモード(PC にキーを送らない)", variable=self.v_test)
        t.grid(row=6, column=0, sticky="w")
        if not IS_WIN:
            t.state(["disabled"])
            ttk.Label(right, text="※ キー送信は Windows のみ対応", foreground="#b3261e").grid(
                row=7, column=0, sticky="w", padx=(22, 0))
        row = ttk.Frame(right)
        row.grid(row=8, column=0, sticky="w", pady=(10, 0))
        ttk.Button(row, text="適用", command=self.apply).pack(side="left")
        self.status = tk.StringVar()
        ttk.Label(row, textvariable=self.status).pack(side="left", padx=10)

        # ---- 下: レーンモニター
        bottom = ttk.Frame(root, padding=(12, 0, 12, 12))
        bottom.grid(row=1, column=0, sticky="we")
        head = ttk.Frame(bottom)
        head.pack(fill="x")
        ttk.Label(head, text="入力モニター(数字は押した回数)").pack(side="left")
        ttk.Button(head, text="回数をリセット", command=self.reset_hits).pack(side="right")
        self.mon = tk.Canvas(bottom, height=90, bg="#111", highlightthickness=0)
        self.mon.pack(fill="x", pady=(4, 0))
        self.mon.bind("<Configure>", lambda e: self.build_monitor())

        if not self.start_server():
            root.destroy()
            return
        self.apply(first=True)
        self.draw_qr()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    # ---- サーバー
    def start_server(self):
        server.Handler.quiet = True
        port = self.conf["port"]
        for p in range(port, port + 10):
            try:
                self.srv = server.ThreadingHTTPServer(("0.0.0.0", p), server.Handler)
                break
            except OSError:
                continue
        else:
            messagebox.showerror("touchkeys", "ポート %d〜%d が使えません。" % (port, port + 9))
            return False
        self.srv.daemon_threads = True
        self.port = p
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return True

    def apply(self, first=False):
        keys = server.parse_keys(self.v_keys.get().split())
        try:
            ms = float(self.v_ms.get())
            if not keys:
                raise ValueError("キーを 1 つ以上入れてください。")
            if len(keys) > 16:
                raise ValueError("レーンは 16 本までです。")
            if not 0 <= ms <= 1000:
                raise ValueError
            backend = server.make_backend(keys, self.v_test.get(), quiet=True)
        except SystemExit as ex:      # 不明なキー名など
            messagebox.showerror("touchkeys", str(ex))
            return
        except ValueError as ex:
            messagebox.showerror("touchkeys", str(ex) or "間隔 ms には 0〜1000 の数値を入れてください。")
            return
        self.keys = keys
        self.lanes = server.Lanes(backend, len(keys), ms / 1000.0)
        server.Handler.cfg = {"keys": keys, "slide": self.v_slide.get(),
                              "retrigger": self.v_retrigger.get()}
        server.Handler.lanes = self.lanes
        server.Handler.kick_all()     # iPad 側は自動で再接続して新しい設定を受け取る
        self.build_monitor()
        self.conf.update(keys=" ".join(keys), slide=self.v_slide.get(),
                         retrigger=self.v_retrigger.get(), retrigger_ms=ms, test=self.v_test.get())
        if not first:
            try:
                with open(CONF, "w", encoding="utf-8") as f:
                    json.dump(self.conf, f, ensure_ascii=False, indent=1)
            except OSError:
                pass

    # ---- 表示
    def draw_qr(self):
        url = "http://%s:%d/" % (self.ip.get(), self.port)
        self.url.set(url)
        m = qr.make(url)
        n = len(m) + 8                      # 余白 4 セルずつ
        cell = 240 // n
        off = (240 - cell * n) // 2 + cell * 4
        self.qr.delete("all")
        for y, line in enumerate(m):
            for x, v in enumerate(line):
                if v:
                    self.qr.create_rectangle(off + x * cell, off + y * cell,
                                             off + (x + 1) * cell, off + (y + 1) * cell,
                                             fill="black", width=0)

    def build_monitor(self):
        self.mon.delete("all")
        self.rects, self.texts = [], []
        if not self.keys:
            return
        w = max(self.mon.winfo_width(), 100) / len(self.keys)
        for i, k in enumerate(self.keys):
            self.rects.append(self.mon.create_rectangle(i * w + 1, 0, (i + 1) * w - 1, 90,
                                                        fill="#1b1b1b", width=0))
            self.mon.create_text((i + .5) * w, 24, text=k.upper(), fill="#999",
                                 font=("TkDefaultFont", 12, "bold"))
            self.texts.append(self.mon.create_text((i + .5) * w, 60, text="0", fill="#fff",
                                                   font=("TkDefaultFont", 16, "bold")))
        self.shown = None

    def reset_hits(self):
        if self.lanes:
            with self.lanes.lock:
                self.lanes.hits = [0] * len(self.lanes.hits)

    def tick(self):
        lanes = self.lanes
        state = (tuple(lanes.down), tuple(lanes.hits))
        if state != self.shown and len(self.rects) == len(lanes.down):
            self.shown = state
            for i, r in enumerate(self.rects):
                self.mon.itemconfigure(r, fill="#2f7cf6" if lanes.down[i] else "#1b1b1b")
                self.mon.itemconfigure(self.texts[i], text=str(lanes.hits[i]))
        n = len(server.Handler.conns)
        mode = "テストモード" if self.conf["test"] else "キー送信中"
        self.status.set(("● iPad 接続中 (%d 台)" % n if n else "○ iPad 未接続") + " / " + mode)
        self.root.after(33, self.tick)

    def close(self):
        server.Handler.kick_all()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
