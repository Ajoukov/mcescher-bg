#!/usr/bin/env python3
"""
Filter Escher wallpaper collection:
- Remove images that aren't mostly grayscale
- Remove blurry images
- Remove images with text watermarks (edge-density in border zones)
- Remove images with diagonal watermark grids (shutterstock/getty style)
"""

import cv2
import numpy as np
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

WALLPAPER_DIR = Path.home() / ".escher-wallpapers"
REJECTED_DIR = WALLPAPER_DIR / ".rejected"

BLUR_THRESHOLD = 50.0
MAX_SATURATION_MEAN = 18       # strict grayscale only
MAX_SATURATION_RATIO = 0.08    # almost no colored pixels allowed


def detect_blur(gray):
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def detect_color(img):
    """Reject images that aren't mostly grayscale."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(float)
    sat_mean = np.mean(sat)
    sat_ratio = np.count_nonzero(sat > 50) / sat.size
    if sat_mean > MAX_SATURATION_MEAN:
        return True, f"color (sat_mean={sat_mean:.1f})"
    if sat_ratio > MAX_SATURATION_RATIO:
        return True, f"color (sat_ratio={sat_ratio:.2f})"
    return False, ""


def detect_text_watermark(gray):
    """Detect text in border zones via edge density + horizontal alignment."""
    h, w = gray.shape

    zones = {
        "bottom_strip":  gray[int(h * 0.85):, :],
        "bottom_right":  gray[int(h * 0.82):, int(w * 0.60):],
        "bottom_left":   gray[int(h * 0.82):, :int(w * 0.40)],
        "top_strip":     gray[:int(h * 0.15), :],
        "top_right":     gray[:int(h * 0.15), int(w * 0.60):],
        "top_left":      gray[:int(h * 0.15), :int(w * 0.40)],
        "left_strip":    gray[:, :int(w * 0.10)],
        "right_strip":   gray[:, int(w * 0.90):],
    }

    for name, zone in zones.items():
        if zone.size == 0:
            continue

        edges = cv2.Canny(zone, 80, 180)
        edge_ratio = np.count_nonzero(edges) / edges.size

        # Lower threshold for corner zones (watermark text is small)
        thresh = 0.05 if ("right" in name or "left" in name) else 0.10
        if edge_ratio > thresh:
            row_sums = np.sum(edges > 0, axis=1)
            active_rows = np.count_nonzero(row_sums > max(zone.shape[1] * 0.015, 2))
            row_ratio = active_rows / len(row_sums) if len(row_sums) > 0 else 0
            if 0.03 < row_ratio < 0.75:
                return True, f"text_edges in {name} (edge={edge_ratio:.3f}, rows={row_ratio:.3f})"

        # Bright uniform overlay (white bar watermark)
        zone_mean = np.mean(zone.astype(float))
        zone_std = np.std(zone.astype(float))
        if zone_mean > 215 and zone_std < 30 and "strip" in name:
            return True, f"bright_overlay in {name} (mean={zone_mean:.0f}, std={zone_std:.0f})"

    return False, ""


def detect_color_logo(img):
    """Detect small colored watermark logos in corners of otherwise grayscale images."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    h, w = sat.shape

    # Overall image saturation (if the whole image is colorful, skip this check)
    overall_sat = np.mean(sat.astype(float))
    if overall_sat > MAX_SATURATION_MEAN:
        return False, ""  # Already caught by detect_color

    # Check corners for concentrated color (logo watermarks)
    corners = {
        "bottom_right": sat[int(h * 0.85):, int(w * 0.80):],
        "bottom_left":  sat[int(h * 0.85):, :int(w * 0.20)],
        "top_right":    sat[:int(h * 0.15), int(w * 0.80):],
        "top_left":     sat[:int(h * 0.15), :int(w * 0.20)],
    }

    for name, corner in corners.items():
        if corner.size == 0:
            continue
        # High saturation pixels in corner = colored logo
        color_pixels = np.count_nonzero(corner > 80)
        color_ratio = color_pixels / corner.size
        if color_ratio > 0.005:  # Even tiny colored logo
            return True, f"color_logo in {name} (ratio={color_ratio:.3f})"

    return False, ""


def detect_simple(gray):
    """Reject images that are too simple — mostly solid black/white, not enough detail."""
    # Histogram: what fraction of pixels are near-black or near-white?
    bw_pixels = np.count_nonzero((gray < 30) | (gray > 225))
    bw_ratio = bw_pixels / gray.size
    if bw_ratio > 0.85:
        return True, f"too_simple (bw_ratio={bw_ratio:.2f})"

    # Gray-range richness: count how many of the 256 bins are actually used
    hist = cv2.calcHist([gray], [0], None, [64], [0, 256]).flatten()
    active_bins = np.count_nonzero(hist > gray.size * 0.001)
    if active_bins < 10:
        return True, f"too_simple (active_bins={active_bins})"

    # Edge density over whole image — very low = boring/empty image
    edges = cv2.Canny(gray, 50, 150)
    edge_ratio = np.count_nonzero(edges) / edges.size
    if edge_ratio < 0.02:
        return True, f"too_simple (edge_ratio={edge_ratio:.3f})"

    return False, ""


def detect_photo_of_art(gray):
    """Detect photos of art hanging on walls — large uniform borders around content."""
    h, w = gray.shape
    # Check if edges have large uniform (wall-like) regions
    borders = {
        "top":    gray[:int(h * 0.15), :],
        "bottom": gray[int(h * 0.85):, :],
        "left":   gray[:, :int(w * 0.15)],
        "right":  gray[:, int(w * 0.85):],
    }
    uniform_count = 0
    for name, border in borders.items():
        if border.size == 0:
            continue
        std = np.std(border.astype(float))
        if std < 20:  # Very uniform = likely a wall/background
            uniform_count += 1
    if uniform_count >= 2:
        return True, f"photo_of_art (uniform_borders={uniform_count})"
    return False, ""


def detect_diagonal_watermark(gray):
    """Detect diagonal watermark grids (shutterstock/getty/alamy)."""
    h, w = gray.shape
    center = gray[int(h * 0.3):int(h * 0.7), int(w * 0.3):int(w * 0.7)]
    if center.size == 0:
        return False, ""

    edges = cv2.Canny(center, 50, 150)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=80,
                            minLineLength=min(center.shape) // 3,
                            maxLineGap=10)
    if lines is not None and len(lines) > 20:
        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            angles.append(abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))))
        angles = np.array(angles)
        diagonal = np.sum((angles > 20) & (angles < 70))
        if diagonal > len(lines) * 0.5:
            return True, f"diagonal_watermark (lines={len(lines)}, diagonal={diagonal})"
    return False, ""


def analyze_image(path):
    try:
        img = cv2.imread(str(path))
        if img is None:
            return path, True, ["unreadable"]

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        reasons = []

        blur_score = detect_blur(gray)
        if blur_score < BLUR_THRESHOLD:
            reasons.append(f"blurry (score={blur_score:.1f})")

        is_color, color_reason = detect_color(img)
        if is_color:
            reasons.append(color_reason)

        is_simple, simple_reason = detect_simple(gray)
        if is_simple:
            reasons.append(simple_reason)

        has_text, text_reason = detect_text_watermark(gray)
        if has_text:
            reasons.append(f"watermark: {text_reason}")

        is_photo, photo_reason = detect_photo_of_art(gray)
        if is_photo:
            reasons.append(photo_reason)

        has_logo, logo_reason = detect_color_logo(img)
        if has_logo:
            reasons.append(f"watermark: {logo_reason}")

        has_diag, diag_reason = detect_diagonal_watermark(gray)
        if has_diag:
            reasons.append(f"watermark: {diag_reason}")

        return path, len(reasons) > 0, reasons

    except Exception as e:
        return path, True, [f"error: {e}"]


def main():
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)

    images = [p for p in WALLPAPER_DIR.iterdir()
              if p.suffix in (".jpg", ".jpeg", ".png", ".webp") and p.is_file()]

    print(f"Scanning {len(images)} images...")
    print()

    counts = {"kept": 0, "blurry": 0, "color": 0, "simple": 0, "watermark": 0, "other": 0}

    with ProcessPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(analyze_image, p): p for p in images}
        for i, future in enumerate(as_completed(futures), 1):
            path, bad, reasons = future.result()
            if bad:
                dest = REJECTED_DIR / path.name
                path.rename(dest)
                reason_str = "; ".join(reasons)
                if "blurry" in reason_str:
                    counts["blurry"] += 1
                if "color" in reason_str:
                    counts["color"] += 1
                if "simple" in reason_str:
                    counts["simple"] += 1
                if "watermark" in reason_str:
                    counts["watermark"] += 1
                if "unreadable" in reason_str or "error" in reason_str:
                    counts["other"] += 1
                print(f"  REJECT: {path.name} - {reason_str}")
            else:
                counts["kept"] += 1

            if i % 200 == 0:
                print(f"  Progress: {i}/{len(images)} ...")

    print(f"\nDone!")
    for k, v in counts.items():
        print(f"  {k:>10}: {v}")
    print(f"  Rejected images: {REJECTED_DIR}")


if __name__ == "__main__":
    main()
