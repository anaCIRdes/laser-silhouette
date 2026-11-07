#!/usr/bin/env python3
"""Convert silhouette images into laser-cutter-optimized SVG or DXF vector files.

Pipeline: load -> threshold (Otsu or manual) -> denoise -> contour extraction
-> polygon simplification -> optional kerf compensation -> cut-order sorting
-> export as SVG and/or DXF (R2000 ASCII, units mm).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

import cv2
import ezdxf
import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

logger = logging.getLogger("laser_silhouette")

SUPPORTED_INPUTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Convert silhouette images to laser-cutter-optimized SVG/DXF."
    )
    p.add_argument("input", type=Path, help="input image (silhouette on background)")
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output file (.svg or .dxf); default: same name as input with .svg",
    )
    p.add_argument(
        "--px-per-mm",
        type=float,
        default=10.0,
        help="pixels per millimeter in the source image (default: 10)",
    )
    p.add_argument(
        "--threshold",
        type=int,
        default=None,
        choices=range(1, 256),
        help="manual binarization threshold 1-255; omit for automatic (Otsu)",
    )
    p.add_argument(
        "--invert",
        action="store_true",
        help="invert: use light shapes on dark background",
    )
    p.add_argument(
        "--blur",
        type=int,
        default=3,
        metavar="N",
        help="median blur kernel size in px to suppress noise (0 to disable, default: 3)",
    )
    p.add_argument(
        "--min-area-mm2",
        type=float,
        default=1.0,
        help="drop shapes smaller than this area in mm^2 (default: 1)",
    )
    p.add_argument(
        "--simplify",
        type=float,
        default=0.1,
        help="polygon simplification tolerance in mm (Douglas-Peucker; default: 0.1)",
    )
    p.add_argument(
        "--kerf",
        type=float,
        default=0.0,
        help="laser kerf compensation in mm; positive grows the part outline so "
        "the finished part keeps nominal size",
    )
    p.add_argument(
        "--smooth",
        type=int,
        default=2,
        metavar="N",
        help="Chaikin smoothing iterations for clean contours (0 to disable, default: 2)",
    )
    p.add_argument(
        "--fit-circles",
        action="store_true",
        help="detect circular parts (e.g. balloons) and export them as perfect "
        "circles instead of the jagged merged outline",
    )
    p.add_argument(
        "--circle-min-r",
        type=float,
        default=2.0,
        help="minimum circle radius in mm for --fit-circles (default: 2)",
    )
    p.add_argument(
        "--holes",
        choices=["keep", "discard"],
        default="keep",
        help="keep or discard interior holes (default: keep)",
    )
    p.add_argument(
        "--merge-overlaps",
        action="store_true",
        help="merge overlapping outlines into single shapes to avoid double-cut lines",
    )
    p.add_argument(
        "--order",
        choices=["topleft", "inside-out", "none"],
        default="topleft",
        help="cut order: topleft (reading order), inside-out (holes first), none",
    )
    p.add_argument("--dpi-hint", type=int, default=96, help="DPI hint written into SVG output")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def load_binary(path: Path, threshold: int | None, invert: bool, blur: int) -> np.ndarray:
    """Return a mask where 255 marks the silhouette material.

    Polarity is auto-detected from the image border: if the border is mostly
    bright, the background is bright and the dark silhouette is the material.
    ``invert`` flips the detected polarity (bright silhouette on dark ground).
    """
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise SystemExit(f"error: cannot read image: {path}")
    if blur and blur > 0:
        if blur % 2 == 0:
            blur += 1
            logger.info("even blur kernel not supported, using %d", blur)
        img = cv2.medianBlur(img, blur)
    if threshold is None:
        _, binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    else:
        _, binary = cv2.threshold(img, threshold, 255, cv2.THRESH_BINARY)

    border = np.concatenate(
        [binary[0, :], binary[-1, :], binary[:, 0], binary[:, -1]]
    )
    background_bright = np.count_nonzero(border > 127) > border.size / 2
    material_bright = not background_bright
    if invert:
        material_bright = not material_bright
    if not material_bright:
        binary = cv2.bitwise_not(binary)
    kernel = np.ones((3, 3), np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    return binary


def upscale_mask(binary: np.ndarray, scale: int = 4) -> np.ndarray:
    """Upscale, blur and re-threshold the mask to get sub-pixel smooth edges."""
    up = cv2.resize(binary, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
    up = cv2.GaussianBlur(up, (0, 0), scale)
    _, up = cv2.threshold(up, 127, 255, cv2.THRESH_BINARY)
    return up


def chaikin(points: np.ndarray, iterations: int) -> np.ndarray:
    """Chaikin corner-cutting smoothing for closed polygons."""
    pts = points.astype(np.float64)
    for _ in range(iterations):
        nxt = np.roll(pts, -1, axis=0)
        a = 0.75 * pts + 0.25 * nxt
        b = 0.25 * pts + 0.75 * nxt
        pts = np.empty((2 * len(pts), 2), np.float64)
        pts[0::2] = a
        pts[1::2] = b
    return pts


def extract_shapes(
    binary: np.ndarray,
    px_per_mm: float,
    min_area_mm2: float,
    simplify_mm: float,
    smooth: int,
) -> list[dict]:
    scale = 4
    mask = upscale_mask(binary, scale)
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]
    simplify_px = max(simplify_mm * px_per_mm * scale, 1.0)
    min_area_px = min_area_mm2 * (px_per_mm * scale) ** 2

    def clean_ring(contour: np.ndarray) -> np.ndarray | None:
        ring = cv2.approxPolyDP(contour, simplify_px, True).reshape(-1, 2)
        if len(ring) < 3:
            return None
        if smooth > 0:
            ring = chaikin(ring, smooth)
            ring = cv2.approxPolyDP(
                ring.reshape(-1, 1, 2).astype(np.float32), max(simplify_px / 8, 1.0), True
            ).reshape(-1, 2)
        return ring / scale

    shapes = []
    for i, contour in enumerate(contours):
        if hierarchy[i][3] != -1:
            continue
        if cv2.contourArea(contour) < min_area_px:
            continue
        outer = clean_ring(contour)
        if outer is None:
            continue
        holes = []
        child = hierarchy[i][2]
        while child != -1:
            if cv2.contourArea(contours[child]) >= min_area_px:
                hole = clean_ring(contours[child])
                if hole is not None:
                    holes.append(hole)
            child = hierarchy[child][0]
        shapes.append({"outer": outer, "holes": holes})
    return shapes


def shapes_to_polygons(shapes: list[dict], px_per_mm: float) -> list[Polygon]:
    polys = []
    for s in shapes:
        outer = [(x / px_per_mm, -y / px_per_mm) for x, y in s["outer"]]
        holes = [
            [(x / px_per_mm, -y / px_per_mm) for x, y in h] for h in s["holes"]
        ]
        try:
            poly = Polygon(outer, holes)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                polys.extend(p for p in poly.geoms if p.area > 0)
            elif not poly.is_empty and poly.area > 0:
                polys.append(poly)
        except Exception:
            logger.debug("skipping invalid polygon", exc_info=True)
    return polys


def apply_kerf(poly: Polygon, kerf_mm: float) -> Polygon:
    # The beam removes material on both sides of the path (kerf/2 each), so the
    # part ends up undersized and holes oversized. Compensate by growing the
    # polygon: outer contour moves outward by kerf/2, holes move inward.
    if kerf_mm == 0:
        return poly
    grown = poly.buffer(kerf_mm / 2.0, join_style=2)
    if grown.is_empty:
        logger.warning("kerf too large: shape vanished, keeping original outline")
        return poly
    return grown


def process(args) -> list[Polygon]:
    binary = load_binary(args.input, args.threshold, args.invert, args.blur)
    shapes = extract_shapes(
        binary, args.px_per_mm, args.min_area_mm2, args.simplify, args.smooth
    )
    logger.info("found %d shape(s)", len(shapes))
    polys = shapes_to_polygons(shapes, args.px_per_mm)

    if not polys:
        raise SystemExit("error: no shapes detected; try --threshold, --invert or --min-area-mm2")

    if args.merge_overlaps:
        merged = unary_union([p.buffer(0.001) for p in polys]).buffer(-0.001)
        polys = list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
        logger.info("merged into %d shape(s)", len(polys))

    if getattr(args, "fit_circles", False):
        binary = load_binary(args.input, args.threshold, args.invert, args.blur)
        circles = detect_circles(binary, args.px_per_mm, args.circle_min_r)
        logger.info("detected %d circle(s)", len(circles))
        polys = apply_circle_fit(polys, circles, args.min_area_mm2)

    polys = [apply_kerf(p, args.kerf) for p in polys]

    if args.holes == "discard":
        polys = [Polygon(p.exterior) for p in polys]

    order_shapes(polys, args.order)
    return polys


def disk(cx: float, cy: float, r: float, segments: int = 72) -> Polygon:
    angles = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    return Polygon(
        [(cx + r * np.cos(a), cy + r * np.sin(a)) for a in angles]
    )


def detect_circles(binary: np.ndarray, px_per_mm: float, min_r_mm: float) -> list[Polygon]:
    """Detect filled circular regions in the material mask and return them
    as polygons in mm coordinates (y flipped like shapes_to_polygons)."""
    h, w = binary.shape
    min_r = max(min_r_mm * px_per_mm, 3.0)
    max_r = min(h, w) / 2.0
    if max_r <= min_r:
        return []
    blur = cv2.GaussianBlur(binary, (9, 9), 2)
    found = cv2.HoughCircles(
        blur,
        cv2.HOUGH_GRADIENT,
        dp=1,
        minDist=int(min_r * 2),
        param1=100,
        param2=35,
        minRadius=int(min_r),
        maxRadius=int(max_r),
    )
    if found is None:
        return []
    circles = []
    for cx, cy, r in np.round(found[0]).astype(int):
        if r <= 0 or r > max_r:
            continue
        mask = np.zeros((h, w), np.uint8)
        cv2.circle(mask, (cx, cy), r, 1, -1)
        coverage = np.count_nonzero(mask & (binary > 0)) / np.count_nonzero(mask)
        if coverage < 0.8:
            continue
        circles.append(disk(cx / px_per_mm, -cy / px_per_mm, r / px_per_mm))
    return circles


def apply_circle_fit(
    polys: list[Polygon], circles: list[Polygon], min_area_mm2: float
) -> list[Polygon]:
    """Cut detected circles out of the shapes and add them as separate parts."""
    result = []
    for poly in polys:
        rest = poly
        for circ in circles:
            rest = rest.difference(circ)
        parts = rest.geoms if rest.geom_type == "MultiPolygon" else [rest]
        result.extend(p for p in parts if p.area >= min_area_mm2)
    result.extend(c for c in circles if c.area >= min_area_mm2)
    return result


def order_shapes(polys: list[Polygon], mode: str) -> None:
    if mode == "none":
        return
    if mode == "topleft":
        polys.sort(key=lambda p: (p.bounds[1], p.bounds[0]))
    elif mode == "inside-out":
        polys.sort(key=lambda p: -p.area)


def poly_rings(poly: Polygon) -> list[list[tuple]]:
    rings = [list(poly.exterior.coords)]
    for hole in poly.interiors:
        ring = list(hole.coords)
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def bounds_mm(polys: list[Polygon]) -> tuple:
    minx = min(p.bounds[0] for p in polys)
    miny = min(p.bounds[1] for p in polys)
    maxx = max(p.bounds[2] for p in polys)
    maxy = max(p.bounds[3] for p in polys)
    return minx, miny, maxx, maxy


def write_svg(polys: list[Polygon], path: Path, dpi_hint: int) -> None:
    minx, miny, maxx, maxy = bounds_mm(polys)
    w = maxx - minx
    h = maxy - miny
    scale = dpi_hint / 25.4
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.3f}mm" height="{h:.3f}mm" '
        f'viewBox="0 0 {w * scale:.3f} {h * scale:.3f}">',
        f'<g transform="scale({scale:.6f})" fill="none" stroke="#000000" '
        f'stroke-width="{1 / scale:.6f}" fill-rule="evenodd">',
    ]
    for poly in polys:
        d_parts = []
        for ring in poly_rings(poly):
            pts = [(x - minx, maxy - y) for x, y in ring]
            d = "M " + " L ".join(f"{x:.3f} {y:.3f}" for x, y in pts) + " Z"
            d_parts.append(d)
        lines.append(f'<path d="{" ".join(d_parts)}"/>')
    lines.append("</g>")
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_dxf(polys: list[Polygon], path: Path) -> None:
    doc = ezdxf.new("R2000", setup=False)
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    minx, miny, _, maxy = bounds_mm(polys)
    for poly in polys:
        for ring in poly_rings(poly):
            pts = [(x - minx, maxy - y) for x, y in ring[:-1]]
            msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "CUT"})
    doc.saveas(path)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    if args.input.suffix.lower() not in SUPPORTED_INPUTS:
        raise SystemExit(f"error: unsupported input type {args.input.suffix}")

    out = args.output or args.input.with_suffix(".svg")
    out.parent.mkdir(parents=True, exist_ok=True)

    polys = process(args)
    minx, miny, maxx, maxy = bounds_mm(polys)
    logger.info(
        "total: %d shape(s), %.1f x %.1f mm", len(polys), maxx - minx, maxy - miny
    )

    if out.suffix.lower() == ".svg":
        write_svg(polys, out, args.dpi_hint)
    elif out.suffix.lower() == ".dxf":
        write_dxf(polys, out)
    else:
        raise SystemExit("error: output must be .svg or .dxf")
    logger.info("written: %s", out)

    alt = out.with_suffix(".dxf" if out.suffix.lower() == ".svg" else ".svg")
    logger.info("hint: also available via -o %s", alt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
