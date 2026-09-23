# -*- coding: utf-8 -*-
"""_make_icon.py —— 生成 WorkBuddy2API 的 Windows 图标（app.ico）。

不使用任何第三方库：PNG 由 zlib 直接构造，ICO 由 PNG 逐条打包。
图标是多尺寸的（16/24/32/48/64/128/256），Windows 会在任务栏、
资源管理器和快捷方式上按需要挑合适的那一张。

画的是一个圆角蓝底 + 白色双向箭头（网关的语义：请求与响应对流）。
所有形状都在 4 倍尺寸上计算，再按面积平均降采样，这样边缘是平滑的，
不依赖任何绘图库。

用法：
    python -X utf8 _make_icon.py            # 写入 app.ico
    python -X utf8 _make_icon.py out.ico    # 写入指定文件
"""

import os
import struct
import sys
import zlib

#: 超采样倍率。4 倍足够让 16x16 这种小图也不出锯齿。
SS = 4

#: 图标配色：蓝底白箭头，与界面的主题色一致。
BG = (37, 99, 235, 255)
BG_DARK = (29, 78, 216, 255)
FG = (255, 255, 255, 255)
CLEAR = (0, 0, 0, 0)

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


# --------------------------------------------------------------------------
# PNG / ICO 编码
# --------------------------------------------------------------------------

def _png(width, height, rgba_rows):
    """Encode RGBA rows as a PNG byte string."""
    raw = bytearray()
    for row in rgba_rows:
        raw.append(0)                     # filter type 0 (None)
        for px in row:
            raw += bytes(px)

    def chunk(tag, data):
        out = struct.pack(">I", len(data)) + tag + data
        return out + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def _ico(images):
    """Pack ``[(size, png_bytes), ...]`` into an .ico byte string."""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = bytearray()
    blobs = bytearray()
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(png), offset)
        offset += len(png)
        blobs += png
    return bytes(header + bytes(entries) + bytes(blobs))


# --------------------------------------------------------------------------
# 几何
# --------------------------------------------------------------------------

def _in_round_rect(px, py, size, radius):
    """True when (px, py) falls inside a rounded square of ``size``."""
    cx = min(max(px, radius), size - radius)
    cy = min(max(py, radius), size - radius)
    dx = px - cx
    dy = py - cy
    return dx * dx + dy * dy <= radius * radius


def _in_poly(px, py, points):
    """Even-odd point-in-polygon test."""
    inside = False
    n = len(points)
    j = n - 1
    for i in range(n):
        xi, yi = points[i]
        xj, yj = points[j]
        if (yi > py) != (yj > py):
            x_cross = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_cross:
                inside = not inside
        j = i
    return inside


def _arrow_shapes(s):
    """The two white arrows, in unit coordinates scaled by ``s``.

    Returns a list of polygons: a horizontal bar plus a triangular head,
    mirrored for the lower arrow. The two point in opposite directions so
    the mark reads as "data flowing both ways".
    """
    def poly(pts):
        return [(x * s, y * s) for x, y in pts]

    upper = poly([(0.20, 0.315), (0.66, 0.315), (0.66, 0.245),
                  (0.82, 0.375), (0.66, 0.505), (0.66, 0.435),
                  (0.20, 0.435)])
    lower = poly([(0.80, 0.565), (0.34, 0.565), (0.34, 0.495),
                  (0.18, 0.625), (0.34, 0.755), (0.34, 0.685),
                  (0.80, 0.685)])
    return (upper, lower)


def _render(size):
    """Render one icon size, supersampled, returning RGBA rows."""
    hi = size * SS
    radius = hi * 0.22
    shapes = _arrow_shapes(hi)

    # 先在超采样网格上判定，再按 SS x SS 的块平均成最终像素。
    hi_rows = []
    for y in range(hi):
        py = y + 0.5
        row = []
        for x in range(hi):
            px = x + 0.5
            if not _in_round_rect(px, py, hi, radius):
                row.append(CLEAR)
                continue
            # 底部略深，做出一点点纵向渐变，避免整块死板。
            t = y / max(1, hi - 1)
            bg = tuple(
                int(BG[i] + (BG_DARK[i] - BG[i]) * t) for i in range(3)
            ) + (255,)
            if any(_in_poly(px, py, poly) for poly in shapes):
                row.append(FG)
            else:
                row.append(bg)
        hi_rows.append(row)

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(SS):
                src = hi_rows[y * SS + dy]
                for dx in range(SS):
                    px = src[x * SS + dx]
                    acc[0] += px[0]
                    acc[1] += px[1]
                    acc[2] += px[2]
                    acc[3] += px[3]
            n = SS * SS
            row.append(tuple(int(v / n + 0.5) for v in acc))
        rows.append(row)
    return rows


def build_ico(path):
    images = []
    for size in SIZES:
        rows = _render(size)
        images.append((size, _png(size, size, rows)))
        print("  已渲染 %3dx%-3d  %6d bytes" % (size, size, len(images[-1][1])))
    blob = _ico(images)
    with open(path, "wb") as fh:
        fh.write(blob)
    return blob


def verify(path):
    """Read the ICO back and report its directory, so a bad write is caught."""
    with open(path, "rb") as fh:
        data = fh.read()
    reserved, itype, count = struct.unpack("<HHH", data[:6])
    print("\n回读校验：")
    print("  reserved = %d (应为 0)" % reserved)
    print("  type     = %d (应为 1)" % itype)
    print("  count    = %d" % count)
    ok = reserved == 0 and itype == 1 and count == len(SIZES)
    for i in range(count):
        off = 6 + 16 * i
        w, h, _c, _r, planes, bits, length, offset = struct.unpack(
            "<BBBBHHII", data[off:off + 16])
        dw = w or 256
        dh = h or 256
        head = data[offset:offset + 8]
        is_png = head == b"\x89PNG\r\n\x1a\n"
        print("  [%d] %3dx%-3d  %2d bit  %6d bytes  偏移 %-7d PNG=%s"
              % (i, dw, dh, bits, length, offset, is_png))
        ok = ok and is_png and planes == 1
    return ok


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "app.ico")
    if not os.path.isabs(out):
        out = os.path.join(here, out)

    print("生成图标 -> %s" % out)
    build_ico(out)
    size = os.path.getsize(out)
    print("\n写入完成：%d bytes" % size)
    ok = verify(out)
    print("\n结果：%s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
