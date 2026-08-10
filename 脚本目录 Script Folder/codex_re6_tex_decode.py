from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

from codex_python_runtime_bootstrap import try_import_optional_runtime_module


TEX_FORMAT_MAPPER = {
    2: b"DXT1",
    14: b"",
    19: b"DXT1",
    20: b"DXT1",
    23: b"DXT5",
    24: b"DXT5",
    25: b"DXT1",
    31: b"DXT5",
    32: b"DXT5",
    35: b"DXT5",
    39: b"",
    40: b"",
    43: b"DXT1",
}


def build_dds_header(width: int, height: int, mipmaps: int, compression: bytes, cubemap: bool) -> bytes:
    ddsd_caps = 0x1
    ddsd_height = 0x2
    ddsd_width = 0x4
    ddsd_pixelformat = 0x1000
    ddsd_mipmapcount = 0x20000
    ddsd_linearsize = 0x80000

    ddscaps_complex = 0x8
    ddscaps_texture = 0x1000
    ddscaps_mipmap = 0x400000

    ddscaps2_cubemap = 0x200
    ddscaps2_allfaces = 0xFC00

    ddpf_alphapixels = 0x1
    ddpf_fourcc = 0x4
    ddpf_rgb = 0x40

    flags = ddsd_caps | ddsd_height | ddsd_width | ddsd_pixelformat
    caps = ddscaps_texture
    caps2 = 0
    pitch_or_linear = 0

    if mipmaps > 0:
        flags |= ddsd_mipmapcount
        caps |= ddscaps_complex | ddscaps_mipmap

    if compression:
        flags |= ddsd_linearsize
        pitch_or_linear = calculate_linear_size(width, height, compression)
        pixel_flags = ddpf_fourcc
        rgb_bits = 0
        rmask = 0
        gmask = 0
        bmask = 0
        amask = 0
        fourcc = compression.ljust(4, b"\x00")[:4]
    else:
        pixel_flags = ddpf_rgb | ddpf_alphapixels
        rgb_bits = 32
        rmask = 0x00FF0000
        gmask = 0x0000FF00
        bmask = 0x000000FF
        amask = 0xFF000000
        fourcc = b"\x00\x00\x00\x00"

    if cubemap:
        caps2 |= ddscaps2_cubemap | ddscaps2_allfaces

    header = struct.pack(
        "<4s18I",
        b"DDS ",
        124,
        flags,
        height,
        width,
        pitch_or_linear,
        0,
        mipmaps,
        *([0] * 11),
    )
    pixel_format = struct.pack(
        "<II4sIIIII",
        32,
        pixel_flags,
        fourcc,
        rgb_bits,
        rmask,
        gmask,
        bmask,
        amask,
    )
    caps_blob = struct.pack("<5I", caps, caps2, 0, 0, 0)
    return header + pixel_format + caps_blob


def calculate_linear_size(width: int, height: int, fmt: bytes) -> int:
    block_size = 8 if fmt in (b"DXT1", b"BC1", b"BC4") else 16
    return ((width + 3) >> 2) * ((height + 3) >> 2) * block_size


def parse_tex_157(data: bytes, expect_magic: bytes) -> dict:
    if len(data) < 16:
        raise ValueError("file too small for tex157/rtex157")
    if data[:4] != expect_magic:
        raise ValueError(f"unexpected magic: {data[:4]!r}")

    bits = int.from_bytes(data[4:16], "little")
    cursor = 0

    def read_bits(count: int) -> int:
        nonlocal cursor
        mask = (1 << count) - 1
        value = (bits >> cursor) & mask
        cursor += count
        return value

    version = read_bits(8)
    unk = read_bits(8)
    attr = read_bits(8)
    prebias = read_bits(4)
    texture_type = read_bits(4)
    mipmaps = read_bits(6)
    width = read_bits(13)
    height = read_bits(13)
    images = read_bits(8)
    compression_format = read_bits(8)
    depth = read_bits(13)
    auto_resize = bool(read_bits(1))
    render_target = bool(read_bits(1))
    use_vtf = bool(read_bits(1))

    cube_size = 108 if images == 6 else 0
    offset_count = mipmaps * images
    header_size = 16 + cube_size + (offset_count * 4)
    if len(data) < header_size:
        raise ValueError("file too small for tex157 mip offsets")

    mipmap_offsets = list(struct.unpack_from(f"<{offset_count}I", data, 16 + cube_size)) if offset_count > 0 else []
    dds_data = data[header_size:]
    return {
        "header_kind": "157",
        "version": version,
        "unk": unk,
        "attr": attr,
        "prebias": prebias,
        "texture_type": texture_type,
        "num_mipmaps_per_image": mipmaps,
        "width": width,
        "height": height,
        "num_images": images,
        "compression_format": compression_format,
        "depth": depth,
        "auto_resize": auto_resize,
        "render_target": render_target,
        "use_vtf": use_vtf,
        "mipmap_offsets": mipmap_offsets,
        "dds_data": dds_data,
    }


def parse_tex_112(data: bytes, expect_magic: bytes) -> dict:
    if len(data) < 40:
        raise ValueError("file too small for tex112/rtex112")
    if data[:4] != expect_magic:
        raise ValueError(f"unexpected magic: {data[:4]!r}")

    version = struct.unpack_from("<H", data, 4)[0]
    packed = data[6]
    texture_type = packed & 0x0F
    encoded_type = (packed >> 4) & 0x0F
    packed2 = data[7]
    depend_screen = bool(packed2 & 0x01)
    render_target = bool((packed2 >> 1) & 0x01)
    attr = (packed2 >> 2) & 0x3F
    mipmaps = data[8]
    images = data[9]
    padding = struct.unpack_from("<H", data, 10)[0]
    width = struct.unpack_from("<H", data, 12)[0]
    height = struct.unpack_from("<H", data, 14)[0]
    depth = struct.unpack_from("<I", data, 16)[0]
    compression_format_raw = data[20:24]
    compression_format = compression_format_raw.rstrip(b"\x00")
    red, green, blue, alpha = struct.unpack_from("<4f", data, 24)

    cube_size = 108 if images == 6 else 0
    offset_count = mipmaps * images
    header_size = 40 + cube_size + (offset_count * 4)
    if len(data) < header_size:
        raise ValueError("file too small for tex112 mip offsets")

    mipmap_offsets = list(struct.unpack_from(f"<{offset_count}I", data, 40 + cube_size)) if offset_count > 0 else []
    dds_data = data[header_size:]
    return {
        "header_kind": "112",
        "version": version,
        "texture_type": texture_type,
        "encoded_type": encoded_type,
        "depend_screen": depend_screen,
        "render_target": render_target,
        "attr": attr,
        "num_mipmaps_per_image": mipmaps,
        "width": width,
        "height": height,
        "num_images": images,
        "compression_format_raw": compression_format_raw,
        "compression_format": compression_format,
        "padding": padding,
        "depth": depth,
        "rgba": [red, green, blue, alpha],
        "mipmap_offsets": mipmap_offsets,
        "dds_data": dds_data,
    }


def parse_texture_file(data: bytes) -> tuple[str, dict]:
    if len(data) < 8:
        raise ValueError("file too small")

    magic = data[:4]
    if magic == b"TEX\x00":
        version16 = struct.unpack_from("<H", data, 4)[0]
        version8 = data[4]
        if version16 == 112:
            return "tex", parse_tex_112(data, magic)
        return "tex", parse_tex_157(data, magic)
    if magic == b"RTX\x00":
        version16 = struct.unpack_from("<H", data, 4)[0]
        if version16 == 112:
            return "rtex", parse_tex_112(data, magic)
        return "rtex", parse_tex_157(data, magic)
    raise ValueError(f"unsupported magic: {magic!r}")


def convert_to_dds(parsed: dict) -> bytes:
    compression_value = parsed["compression_format"]
    if isinstance(compression_value, bytes):
        if compression_value in (b"", b"\x15"):
            compression = b""
        elif compression_value in (b"DXT1", b"DXT3", b"DXT5"):
            compression = compression_value
        else:
            raise ValueError(f"unsupported tex112 compression: {compression_value!r}")
    else:
        compression = TEX_FORMAT_MAPPER.get(compression_value)
        if compression is None:
            raise ValueError(f"unsupported tex157 compression format: {compression_value}")

    header = build_dds_header(
        parsed["width"],
        parsed["height"],
        parsed["num_mipmaps_per_image"],
        compression,
        parsed["num_images"] > 1,
    )
    return header + parsed["dds_data"]


def _build_png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)


def _build_placeholder_rgba_rows(width: int, height: int) -> bytes:
    step = max(8, min(width, height) // 8)
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            checker_on = ((x // step) + (y // step)) % 2 == 0
            shade = 140 if checker_on else 96
            r = g = b = shade
            if x < 2 or y < 2 or x >= (width - 2) or y >= (height - 2):
                r, g, b = 255, 96, 32
            rows.extend((r, g, b, 255))
    return bytes(rows)


def _try_import_pillow_for_placeholder():
    if try_import_optional_runtime_module("PIL", repair=False) is None:
        return None
    try:
        from PIL import Image, ImageDraw

        return Image, ImageDraw
    except Exception:
        return None


def write_placeholder_png(output_path: Path, width: int, height: int) -> None:
    width = max(1, int(width))
    height = max(1, int(height))
    pillow_types = _try_import_pillow_for_placeholder()
    if pillow_types is not None:
        Image, ImageDraw = pillow_types
        try:
            image = Image.new("RGBA", (width, height), (96, 96, 96, 255))
            draw = ImageDraw.Draw(image)
            step = max(8, min(width, height) // 8)
            for x in range(0, width, step):
                for y in range(0, height, step):
                    if ((x // step) + (y // step)) % 2 == 0:
                        draw.rectangle((x, y, min(width - 1, x + step - 1), min(height - 1, y + step - 1)), fill=(140, 140, 140, 255))
            draw.text((8, 8), "RTEX", fill=(255, 96, 32, 255))
            image.save(output_path)
            return
        except Exception:
            # Pillow is optional. The pure-Python PNG path below owns fallback.
            pass
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    image_rows = _build_placeholder_rgba_rows(width, height)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        + _build_png_chunk(b"IHDR", ihdr)
        + _build_png_chunk(b"IDAT", zlib.compress(image_rows, level=9))
        + _build_png_chunk(b"IEND", b"")
    )
    output_path.write_bytes(png_bytes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--allow-rtex-placeholder", action="store_true")
    args = parser.parse_args(argv)

    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        data = input_path.read_bytes()
        kind, parsed = parse_texture_file(data)

        if kind == "tex":
            out_bytes = convert_to_dds(parsed)
            output_path.write_bytes(out_bytes)
            payload = {
                "status": "ok",
                "kind": "tex",
                "output": str(output_path),
                "width": parsed["width"],
                "height": parsed["height"],
                "compression_format": parsed["compression_format"],
                "header_kind": parsed["header_kind"],
            }
        else:
            if not args.allow_rtex_placeholder:
                raise RuntimeError("rtex has no embedded image payload; placeholder disabled")
            if output_path.suffix.lower() != ".png":
                output_path = output_path.with_suffix(".png")
            write_placeholder_png(output_path, parsed["width"], parsed["height"])
            payload = {
                "status": "ok",
                "kind": "rtex",
                "output": str(output_path),
                "width": parsed["width"],
                "height": parsed["height"],
                "header_kind": parsed["header_kind"],
                "placeholder": True,
            }
    except Exception as exc:
        payload = {
            "status": "error",
            "error": str(exc),
            "input": str(input_path),
            "output": str(output_path),
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False))
        else:
            print(payload["error"], file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(payload["output"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
