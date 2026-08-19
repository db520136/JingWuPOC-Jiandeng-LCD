from pathlib import Path

import numpy as np
from PIL import Image


SOURCE = Path(
    r"C:\Users\WLH\AppData\Local\Temp\codex-clipboard-4ad5692e-a77b-45b9-9eb9-cb091f21fc3e.jpg"
)
OUTPUT_DIR = Path(__file__).resolve().parents[1] / "img"


def smooth_background(image, box, top_rows, bottom_rows):
    """Replace a smooth region by interpolating clean pixels above and below it."""
    x0, y0, x1, y1 = box
    top0, top1 = top_rows
    bottom0, bottom1 = bottom_rows

    top = np.median(image[top0:top1, x0:x1].astype(np.float32), axis=0)
    bottom = np.median(image[bottom0:bottom1, x0:x1].astype(np.float32), axis=0)

    # Remove the real right border from the upper reference. It is restored later.
    border_local_x = 2132 - x0
    left = top[border_local_x - 8 : border_local_x - 4].mean(axis=0)
    right = top[border_local_x + 5 : border_local_x + 9].mean(axis=0)
    for offset, column in enumerate(range(border_local_x - 4, border_local_x + 5)):
        alpha = (offset + 1) / 10.0
        top[column] = left * (1.0 - alpha) + right * alpha

    ys = np.arange(y0, y1, dtype=np.float32)
    top_y = (top0 + top1 - 1) / 2.0
    bottom_y = (bottom0 + bottom1 - 1) / 2.0
    alpha = np.clip((ys - top_y) / (bottom_y - top_y), 0.0, 1.0)
    alpha = alpha[:, None, None]
    fill = top[None, :, :] * (1.0 - alpha) + bottom[None, :, :] * alpha
    image[y0:y1, x0:x1] = np.clip(fill, 0, 255).astype(np.uint8)


def extrapolate_line(image, y, x0, x1, sample_x0, sample_x1):
    """Extend a three-pixel antialiased horizontal border over the repaired area."""
    xs = np.arange(sample_x0, sample_x1, dtype=np.float64)
    target_xs = np.arange(x0, x1, dtype=np.float64)
    for line_y in (y - 1, y, y + 1):
        values = image[line_y, sample_x0:sample_x1].astype(np.float64)
        # A linear fit follows the subtle illumination change without amplifying JPEG noise.
        design = np.column_stack((xs, np.ones_like(xs)))
        coefficients = np.linalg.lstsq(design, values, rcond=None)[0]
        restored = target_xs[:, None] * coefficients[0] + coefficients[1]
        image[line_y, x0:x1] = np.clip(restored, 0, 255).astype(np.uint8)


def restore_vertical_border(image, x, y0, y1):
    """Restore the antialiased right border using the clean segment immediately above."""
    reference = image[y0 - 36 : y0 - 8, x - 2 : x + 3].astype(np.float32)
    colors = np.median(reference, axis=0)
    image[y0:y1, x - 2 : x + 3] = np.clip(colors, 0, 255).astype(np.uint8)


def make_candidate():
    image = np.asarray(Image.open(SOURCE).convert("RGB")).copy()

    # The rectangle contains all watermark glyphs and their JPEG fringes.
    smooth_background(
        image,
        box=(1868, 1674, 2192, 1772),
        top_rows=(1650, 1664),
        bottom_rows=(1778, 1792),
    )

    # Restore the lower and right edges of the original frame.
    extrapolate_line(image, y=1710, x0=1868, x1=2133, sample_x0=1680, sample_x1=1860)
    restore_vertical_border(image, x=2132, y0=1674, y1=1710)

    # Keep a crisp, connected corner while retaining the line's antialiasing.
    image[1709:1712, 2130:2135] = image[1709:1712, 2125:2130].mean(
        axis=1, keepdims=True
    ).astype(np.uint8)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output = OUTPUT_DIR / "intercom_no_watermark.jpg"
    preview = OUTPUT_DIR / "intercom_no_watermark_corner.jpg"
    Image.fromarray(image).save(output, quality=96, subsampling=0)
    Image.fromarray(image[1600:1792, 1750:2208]).save(
        preview, quality=96, subsampling=0
    )
    print(output)
    print(preview)


if __name__ == "__main__":
    make_candidate()
