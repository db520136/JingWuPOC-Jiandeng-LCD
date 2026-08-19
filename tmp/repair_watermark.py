from pathlib import Path

import numpy as np
from PIL import Image


SOURCE = Path(r"C:\Users\WLH\AppData\Local\Temp\codex-clipboard-4ad5692e-a77b-45b9-9eb9-cb091f21fc3e.jpg")
OUTPUT = Path(__file__).resolve().parents[1] / "img" / "intercom_no_watermark.jpg"
PREVIEW = Path(__file__).resolve().parent / "intercom_no_watermark_crop.jpg"


def polynomial_features(x, y):
    """Degree-3 surface terms for the smooth background in the repair area."""
    return np.column_stack(
        (
            np.ones_like(x),
            x,
            y,
            x * x,
            x * y,
            y * y,
            x * x * x,
            x * x * y,
            x * y * y,
            y * y * y,
        )
    )


def fit_background(image):
    # Fit on a broad neighborhood. Samples from the watermark and the frame are
    # excluded; the remaining pixels describe the underlying smooth gradient.
    fit_x0, fit_x1 = 1720, image.shape[1]
    fit_y0, fit_y1 = 1550, image.shape[0]
    patch = image[fit_y0:fit_y1, fit_x0:fit_x1].astype(np.float64)

    yy, xx = np.mgrid[fit_y0:fit_y1, fit_x0:fit_x1]
    xn = (xx - 1960.0) / 240.0
    yn = (yy - 1680.0) / 140.0

    brightness = patch.mean(axis=2)
    neutral = patch.max(axis=2) - patch.min(axis=2)
    usable = brightness < 36
    usable &= neutral < 34
    usable &= ~((yy >= 1672) & (xx >= 1855))
    usable &= ~((yy >= 1705) & (yy <= 1715))
    usable &= ~((xx >= 2127) & (xx <= 2137))

    # Regularly subsample to keep the least-squares solve compact.
    usable &= ((xx + yy) % 3 == 0)
    design = polynomial_features(xn[usable], yn[usable])
    coefficients = []
    for channel in range(3):
        coefficients.append(np.linalg.lstsq(design, patch[:, :, channel][usable], rcond=None)[0])
    return coefficients


def evaluate_background(coefficients, x0, x1, y0, y1):
    yy, xx = np.mgrid[y0:y1, x0:x1]
    xn = (xx - 1960.0) / 240.0
    yn = (yy - 1680.0) / 140.0
    design = polynomial_features(xn.ravel(), yn.ravel())
    values = np.column_stack([design @ coefficient for coefficient in coefficients])
    return np.clip(values.reshape(y1 - y0, x1 - x0, 3), 0, 255)


def reconstruct_frame(original, repaired, predicted):
    # The watermark crosses the bottom-right corner of the thin frame. Extend
    # each source scanline profile from the nearest clean segment, modulated by
    # the predicted local background so the join follows the original gradient.
    x0, y0 = 1855, 1668
    horizontal_source_x = np.arange(1790, 1850)
    horizontal_target_x = np.arange(1855, 2133)
    for y in range(1707, 1714):
        source = original[y, horizontal_source_x].astype(np.float64)
        source_bg = np.median(original[np.r_[1697:1705, 1716:1724], :][:, horizontal_source_x], axis=0)
        line_delta = np.median(source - source_bg, axis=0)
        target_bg = predicted[y - y0, horizontal_target_x - x0]
        repaired[y, horizontal_target_x] = np.clip(target_bg + line_delta, 0, 255)

    vertical_source_y = np.arange(1585, 1665)
    vertical_target_y = np.arange(1668, 1711)
    for x in range(2129, 2136):
        source = original[vertical_source_y, x].astype(np.float64)
        source_bg = np.median(original[vertical_source_y][:, np.r_[2117:2125, 2140:2148]], axis=1)
        line_delta = np.median(source - source_bg, axis=0)
        target_bg = predicted[vertical_target_y - y0, x - x0]
        repaired[vertical_target_y, x] = np.clip(target_bg + line_delta, 0, 255)

    # Ensure the line profiles meet cleanly at the true corner, while the area
    # to its lower-right remains background only.
    repaired[1707:1714, 2133:2136] = predicted[1707 - y0 : 1714 - y0, 2133 - x0 : 2136 - x0]
    repaired[1711:1714, 2129:2133] = predicted[1711 - y0 : 1714 - y0, 2129 - x0 : 2133 - x0]


def main():
    original_image = Image.open(SOURCE).convert("RGB")
    original = np.asarray(original_image).astype(np.float64)
    repaired = original.copy()

    coefficients = fit_background(original)
    x0, x1, y0, y1 = 1855, 2208, 1668, 1772
    predicted = evaluate_background(coefficients, x0, x1, y0, y1)

    # Feather a narrow outer band into the untouched image. The central area is
    # fully replaced, removing both the glyph cores and JPEG halo pixels.
    yy, xx = np.mgrid[y0:y1, x0:x1]
    edge_distance = np.minimum.reduce((xx - x0, x1 - 1 - xx, yy - y0, y1 - 1 - yy)).astype(np.float64)
    alpha = np.clip(edge_distance / 12.0, 0.0, 1.0)[..., None]
    current = repaired[y0:y1, x0:x1]
    repaired[y0:y1, x0:x1] = current * (1.0 - alpha) + predicted * alpha

    reconstruct_frame(original, repaired, predicted)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    result = Image.fromarray(np.clip(np.rint(repaired), 0, 255).astype(np.uint8), "RGB")
    result.save(OUTPUT, quality=97, subsampling=0, optimize=True)
    result.crop((1760, 1590, 2208, 1792)).save(PREVIEW, quality=98, subsampling=0)
    print(OUTPUT)
    print(PREVIEW)


if __name__ == "__main__":
    main()
