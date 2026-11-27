"""生成高分屏清晰的 Win11 托盘与窗口图标（Pillow 矢量绘制）."""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

from src.config import DATA_DIR


def create_app_image(size: int = 128) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    radius = size // 4
    # 现代金融蓝圆角底
    draw.rounded_rectangle(
        [pad, pad, size - pad, size - pad],
        radius=radius,
        fill=(37, 99, 235, 255),
    )
    # 白色走势折线
    pts = [
        (size * 0.22, size * 0.67),
        (size * 0.42, size * 0.48),
        (size * 0.56, size * 0.59),
        (size * 0.78, size * 0.33),
    ]
    width = max(3, size // 12)
    draw.line(pts, fill=(255, 255, 255, 255), width=width, joint="curve")
    # 末端红色护盘亮点
    r = max(4, size // 12)
    cx, cy = pts[-1]
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(252, 165, 165, 255))
    return img


def ensure_ico_file() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ico_path = DATA_DIR / "gjdjk.ico"
    if not ico_path.exists():
        img = create_app_image(128)
        img.save(str(ico_path), format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128)])
    return ico_path
