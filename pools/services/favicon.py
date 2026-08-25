"""The site's icon, in the two forms search engines look for.

`favicon.png` in `static/` (live site) and `offseason/` (static build) is the one
source image. Nothing here keeps a second icon file on disk: the .ico is built
from those same bytes on demand, so the two can't drift apart when the icon is
swapped.
"""

import struct

# Google's favicon crawler makes a separate, infrequent pass from the page crawl
# and goes for /favicon.ico, so both the live site and the offseason build have to
# answer there — see `favicon_ico` in pools/views.py and `render_static_site`.
ICO_CONTENT_TYPE = "image/x-icon"


def png_to_ico(png_bytes, size):
    """Wrap a square PNG in an ICO container, without re-encoding it.

    An .ico is a small header plus one or more images, and since Vista those
    images are allowed to be PNGs stored verbatim — every browser Google's
    crawler included reads that form. So this is 22 bytes of header in front of
    the file we already have, not an image conversion.

    Why bother, when the live site could just serve the PNG under an .ico URL:
    Cloudflare Pages types files by extension, so in the offseason build PNG
    bytes named .ico would go out labeled as an icon and be a lie. Producing a
    real .ico keeps one answer at /favicon.ico across both ways we host.
    """
    if not 1 <= size <= 256:
        raise ValueError(f"icon side must be 1-256 px, got {size}")
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type 1 = icon, one image
    entry = struct.pack(
        "<BBBBHHII",
        size % 256,  # 256 is stored as 0; anything smaller is itself
        size % 256,
        0,  # palette size, 0 for a truecolour image
        0,  # reserved
        1,  # color planes
        32,  # bits per pixel
        len(png_bytes),
        len(header) + 16,  # the image starts right after this entry
    )
    return header + entry + png_bytes


def png_side_length(png_bytes):
    """Width of a square PNG, read from its IHDR — the one field the ICO needs."""
    width, height = struct.unpack(">II", png_bytes[16:24])
    if width != height:
        raise ValueError(f"icon must be square, got {width}x{height}")
    return width


def ico_from_png(png_bytes):
    return png_to_ico(png_bytes, png_side_length(png_bytes))
