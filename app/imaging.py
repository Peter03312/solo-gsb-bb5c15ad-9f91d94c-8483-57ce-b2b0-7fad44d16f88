"""图像处理：解码、EXIF 旋正、不可逆涂实、去元数据输出。"""
from __future__ import annotations

from io import BytesIO
from typing import Any, Iterable, Mapping

from PIL import Image, ImageDraw, ImageOps



SUPPORTED_FORMATS = {"PNG": ".png", "JPEG": ".jpg"}


def scale_rect(
    rect: Mapping[str, Any],
    src_width: int,
    src_height: int,
    dst_width: int,
    dst_height: int,
) -> dict:
    """把源图（旋正后）坐标系中的一个矩形缩放到目标图坐标系。

    宽、高方向分别缩放（适配同版式、不同尺寸），全部使用整数运算：
    左上边界向下取整（``floor``），右下边界向上取整（``ceil``），
    使遮盖区域向外扩一点也不缩——宁可多遮，不可漏遮。结果再夹到
    目标图 ``[0, dst_width/dst_height]`` 内。``reason`` 原样保留。

    源矩形为标注接口接受的合法矩形（w,h > 0 且在源图内），因此
    floor 左/上边界严格小于 ceil 右/下边界，输出 w/h 必然 >= 1。
    """
    src_right = rect["x"] + rect["w"]
    src_bottom = rect["y"] + rect["h"]
    # floor(a*num/den)：非负整数下整除即向下取整
    nx = rect["x"] * dst_width // src_width
    ny = rect["y"] * dst_height // src_height
    # ceil(a*num/den) = (a*num + den - 1) // den
    nr = (src_right * dst_width + src_width - 1) // src_width
    nb = (src_bottom * dst_height + src_height - 1) // src_height
    # 限制在目标图内
    nx = max(0, min(nx, dst_width))
    ny = max(0, min(ny, dst_height))
    nr = max(0, min(nr, dst_width))
    nb = max(0, min(nb, dst_height))
    return {"x": nx, "y": ny, "w": nr - nx, "h": nb - ny, "reason": rect["reason"]}


class ImageDecodeError(Exception):
    """上传字节无法解码为图像。"""


class UnsupportedFormatError(Exception):
    """能解码但不是 PNG/JPEG。"""


def load_oriented(data: bytes) -> tuple[Image.Image, str]:
    """解码图像并按 EXIF Orientation 旋正。

    返回 (旋正后的图像, 原始格式)。标注坐标系即该图像的像素坐标，
    与查看器看到的方向一致。
    """
    try:
        img = Image.open(BytesIO(data))
        fmt = img.format
        img.load()  # 强制解码，尽早暴露截断/损坏数据
    except Exception as exc:
        raise ImageDecodeError(str(exc) or exc.__class__.__name__) from exc
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedFormatError(fmt or "unknown")
    try:
        oriented = ImageOps.exif_transpose(img)
        oriented.load()
    except Exception as exc:
        raise ImageDecodeError(str(exc) or exc.__class__.__name__) from exc
    return oriented, fmt


# 各模式的"纯黑不透明"填充值
_FILLS = {
    "RGB": (0, 0, 0),
    "RGBA": (0, 0, 0, 255),
    "L": 0,
    "LA": (0, 255),
}


def render_publication(
    oriented: Image.Image, rects: Iterable[Mapping[str, Any]]
) -> Image.Image:
    """把敏感矩形涂实为纯黑，并返回不携带任何元数据的干净图像。

    涂实是单向的：输出中矩形区域只含黑色像素，无法还原。
    """
    fill = _FILLS.get(oriented.mode)
    if fill is None:  # P / CMYK / I;16 等统一转 RGB
        oriented = oriented.convert("RGB")
        fill = (0, 0, 0)
    out = oriented.copy()
    draw = ImageDraw.Draw(out)
    for r in rects:
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        draw.rectangle([x, y, x + w - 1, y + h - 1], fill=fill)
    # 重建像素容器，彻底丢弃 EXIF/ICC/文本等元数据
    clean = Image.new(out.mode, out.size)
    clean.paste(out)
    return clean
