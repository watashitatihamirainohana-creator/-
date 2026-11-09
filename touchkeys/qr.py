"""依存なしの最小 QR コード生成 (バイトモード / 誤り訂正 L / バージョン 1〜4、最大 78 バイト)。"""

# バージョン -> (データコード語数, 誤り訂正コード語数)。L レベルの 1〜4 はすべて 1 ブロック。
_SPEC = {1: (19, 7), 2: (34, 10), 3: (55, 15), 4: (80, 20)}

_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _rs(data, n):
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _mul(g, _EXP[i])
        gen = nxt
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(gen[j + 1], f)
    return rem


_MASKS = [
    lambda x, y: (x + y) % 2,
    lambda x, y: y % 2,
    lambda x, y: x % 3,
    lambda x, y: (x + y) % 3,
    lambda x, y: (x // 3 + y // 2) % 2,
    lambda x, y: x * y % 2 + x * y % 3,
    lambda x, y: (x * y % 2 + x * y % 3) % 2,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
]


def _penalty(m):
    size = len(m)
    p = 0
    for lines in (m, list(zip(*m))):
        for line in lines:
            run = 1
            for i in range(1, size):
                if line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        p += run - 2
                    run = 1
            if run >= 5:
                p += run - 2
    for y in range(size - 1):
        for x in range(size - 1):
            if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                p += 3
    dark = sum(map(sum, m))
    p += abs(dark * 20 // (size * size) - 10) * 10
    return p


def make(text):
    """text を QR コードにして、True=黒 の 2 次元リスト (行のリスト) を返す。"""
    raw = text.encode("utf-8")
    for ver in sorted(_SPEC):
        ndata, nec = _SPEC[ver]
        if len(raw) <= ndata - 2:
            break
    else:
        raise ValueError("QR コードにするには長すぎます (最大 78 バイト)")

    bits = [0, 1, 0, 0] + [(len(raw) >> i) & 1 for i in range(7, -1, -1)]
    for b in raw:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, ndata * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    data = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(data) < ndata:
        data.append(pad)
        pad ^= 0xEC ^ 0x11
    code = data + _rs(data, nec)

    size = 17 + 4 * ver
    mod = [[False] * size for _ in range(size)]
    func = [[False] * size for _ in range(size)]

    def put(x, y, v):
        if 0 <= x < size and 0 <= y < size:
            mod[y][x] = bool(v)
            func[y][x] = True

    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):          # 位置検出パターン
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                put(cx + dx, cy + dy, d != 2 and d != 4)
    for i in range(8, size - 8):                                    # タイミング
        put(i, 6, i % 2 == 0)
        put(6, i, i % 2 == 0)
    if ver >= 2:                                                    # 位置合わせ
        c = size - 7
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                put(c + dx, c + dy, max(abs(dx), abs(dy)) != 1)

    def put_format(mask):
        d = (1 << 3) | mask                                         # L = 01
        rem = d
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        f = ((d << 10) | rem) ^ 0x5412
        bit = lambda i: (f >> i) & 1
        for i in range(6):
            put(8, i, bit(i))
        put(8, 7, bit(6))
        put(8, 8, bit(7))
        put(7, 8, bit(8))
        for i in range(9, 15):
            put(14 - i, 8, bit(i))
        for i in range(8):
            put(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            put(8, size - 15 + i, bit(i))
        put(8, size - 8, 1)

    put_format(0)                                                   # 領域を予約

    i = 0
    right = size - 1
    while right >= 1:                                               # データをジグザグに配置
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                x = right - j
                y = size - 1 - vert if (right + 1) & 2 == 0 else vert
                if not func[y][x] and i < len(code) * 8:
                    mod[y][x] = bool((code[i >> 3] >> (7 - (i & 7))) & 1)
                    i += 1
        right -= 2

    base = [row[:] for row in mod]
    best = None
    for mask in range(8):
        for y in range(size):
            for x in range(size):
                if not func[y][x]:
                    mod[y][x] = base[y][x] ^ (_MASKS[mask](x, y) == 0)
        put_format(mask)
        p = _penalty(mod)
        if best is None or p < best[0]:
            best = (p, [row[:] for row in mod])
    return best[1]
