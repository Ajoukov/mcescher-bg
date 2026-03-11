#!/usr/bin/env python3
"""
Filter out watermarked and blurry images from the Escher wallpaper collection.

Detects:
- Blurriness via Laplacian variance (low variance = blurry)
- Watermarks via:
  - Text-like regions in bottom/corner areas (common watermark placement)
  - Semi-transparent overlays (brightness anomalies in edges/corners)
  - Common watermark color bands (white/gray text regions)
"""

import cv2
import numpy as np
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

WALLPAPER_DIR = Path.home() / ".escher-wallpapers"
REJECTED_DIR = WALLPAPER_DIR / ".rejected"

# Thresholds (tuned conservatively to avoid false positives)
BLUR_THRESHOLD = 50.0          # Laplacian variance below this = blurry
WATERMARK_TEXT_THRESH = 0.15   # Ratio of high-contrast pixels in watermark zones


def detect_blur(gray):
    """Return Laplacian variance. Lower = blurrier."""
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def detect_watermark(img, gray):
    """Check common watermark zones for text-like patterns. Returns (bool, reason)."""
    h, w = gray.shape

    # Define watermark zones: bottom strip, bottom-right corner, bottom-left corner
    zones = {
        "bottom_strip": gray[int(h * 0.88):, :],
        "bottom_right":  gray[int(h * 0.85):, int(w * 0.65):],
        "bottom_left":   gray[int(h * 0.85):, :int(w * 0.35)],
        "top_right":     gray[:int(h * 0.12), int(w * 0.65):],
    }

    for name, zone in zones.items():
        if zone.size == 0:
            continue

        # Method 1: Edge density in watermark zones
        # Watermark text creates sharp edges against the background
        edges = cv2.Canny(zone, 100, 200)
        edge_ratio = np.count_nonzero(edges) / edges.size

        if edge_ratio > WATERMARK_TEXT_THRESH:
            # Verify it's text-like: check for horizontal alignment
            # (watermark text tends to have horizontal structure)
            row_sums = np.sum(edges > 0, axis=1)
            active_rows = np.count_nonzero(row_sums > w * 0.02)
            row_ratio = active_rows / len(row_sums)

            # Text watermarks: moderate number of active rows (not full area)
            if 0.05 < row_ratio < 0.7:
                return True, f"text_edges in {name} (edge={edge_ratio:.3f}, rows={row_ratio:.3f})"

        # Method 2: Check for uniform semi-transparent overlay
        # Watermarks often have a narrow brightness range in a strip
        zone_std = np.std(zone.astype(float))
        zone_mean = np.mean(zone.astype(float))

        # Very bright uniform strip at bottom (white watermark overlay)
        if zone_mean > 220 and zone_std < 25 and name == "bottom_strip":
            return True, f"bright_overlay in {name} (mean={zone_mean:.0f}, std={zone_std:.0f})"

    # Method 3: Check for "shutterstock" / "getty" / "alamy" style diagonal watermarks
    # These create periodic patterns across the image
    # Use frequency analysis on the full image
    center_crop = gray[int(h*0.3):int(h*0.7), int(w*0.3):int(w*0.7)]
    if center_crop.size > 0:
        edges_center = cv2.Canny(center_crop, 50, 150)
        # Detect grid-like repetitive patterns (diagonal watermarks)
        lines = cv2.HoughLinesP(edges_center, 1, np.pi/180, threshold=80,
                                minLineLength=min(center_crop.shape)//3,
                                maxLineGap=10)
        if lines is not None and len(lines) > 15:
            # Many long lines in the center = likely diagonal watermark grid
            angles = []
            for line in lines:
                x1, y1, x2, y2 = line[0]
                angle = abs(np.degrees(np.arctan2(y2-y1, x2-x1)))
                angles.append(angle)
            angles = np.array(angles)
            # Diagonal watermarks cluster around 30-60 degrees
            diagonal = np.sum((angles > 20) & (angles < 70))
            if diagonal > len(lines) * 0.5 and len(lines) > 20:
                return True, f"diagonal_watermark (lines={len(lines)}, diagonal={diagonal})"

    return False, ""


def analyze_image(path):
    """Analyze a single image for blur and watermarks. Returns (path, dominated, reasons)."""
    try:
        img = cv2.imread(str(path))
        if img is None:
            return path, True, ["unreadable"]

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        reasons = []

        # Check blur
        blur_score = detect_blur(gray)
        if blur_score < BLUR_THRESHOLD:
            reasons.append(f"blurry (score={blur_score:.1f})")

        # Check watermark
        has_wm, wm_reason = detect_watermark(img, gray)
        if has_wm:
            reasons.append(f"watermark: {wm_reason}")

        return path, len(reasons) > 0, reasons

    except Exception as e:
        return path, True, [f"error: {e}"]


def main():
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)

    images = [p for p in WALLPAPER_DIR.iterdir()
              if p.suffix in (".jpg", ".jpeg", ".png", ".webp") and p.is_file()]

    print(f"Scanning {len(images)} images for watermarks and blur...")
    print(f"Blur threshold: Laplacian variance < {BLUR_THRESHOLD}")
    print()

    rejected_blur = 0
    rejected_wm = 0
    rejected_other = 0
    kept = 0

    with ProcessPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(analyze_image, p): p for p in images}
        for i, future in enumerate(as_completed(futures), 1):
            path, bad, reasons = future.result()
            if bad:
                # Move to rejected dir
                dest = REJECTED_DIR / path.name
                path.rename(dest)
                reason_str = "; ".join(reasons)
                if "blurry" in reason_str:
                    rejected_blur += 1
                if "watermark" in reason_str:
                    rejected_wm += 1
                if "unreadable" in reason_str or "error" in reason_str:
                    rejected_other += 1
                if i <= 20 or "watermark" in reason_str:
                    print(f"  REJECT: {path.name} - {reason_str}")
            else:
                kept += 1

            if i % 100 == 0:
                print(f"  Progress: {i}/{len(images)} "
                      f"(kept: {kept}, blur: {rejected_blur}, "
                      f"watermark: {rejected_wm}, other: {rejected_other})")

    total_rejected = rejected_blur + rejected_wm + rejected_other
    print(f"\nDone!")
    print(f"  Kept:       {kept}")
    print(f"  Blurry:     {rejected_blur}")
    print(f"  Watermark:  {rejected_wm}")
    print(f"  Unreadable: {rejected_other}")
    print(f"  Total rejected: {total_rejected}")
    print(f"  Rejected images moved to: {REJECTED_DIR}")


if __name__ == "__main__":
    main()
