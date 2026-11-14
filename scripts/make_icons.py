"""生成 fnOS 应用图标（无第三方素材）。"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "deploy" / "fnos" / "wy-music-cloud"
UI = APP / "app" / "ui" / "images"


def draw_icon(size: int) -> Image.Image:
    scale = 4
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    radius = int(canvas * 0.18)
    draw.rounded_rectangle((0, 0, canvas, canvas), radius=radius, fill=(194, 12, 12, 255))

    # 白色云朵
    white = (255, 255, 255, 255)
    draw.ellipse((canvas * 0.16, canvas * 0.46, canvas * 0.50, canvas * 0.80), fill=white)
    draw.ellipse((canvas * 0.33, canvas * 0.36, canvas * 0.67, canvas * 0.70), fill=white)
    draw.ellipse((canvas * 0.52, canvas * 0.43, canvas * 0.82, canvas * 0.73), fill=white)
    draw.rounded_rectangle(
        (canvas * 0.20, canvas * 0.61, canvas * 0.78, canvas * 0.80),
        radius=int(canvas * 0.08),
        fill=white,
    )

    # 云朵内的红色音符
    red = (194, 12, 12, 255)
    stem_w = int(canvas * 0.028)
    draw.rectangle((canvas * 0.44, canvas * 0.44, canvas * 0.44 + stem_w, canvas * 0.66), fill=red)
    draw.rectangle((canvas * 0.58, canvas * 0.40, canvas * 0.58 + stem_w, canvas * 0.62), fill=red)
    draw.polygon(
        [
            (canvas * 0.44, canvas * 0.44),
            (canvas * 0.58 + stem_w, canvas * 0.40),
            (canvas * 0.58 + stem_w, canvas * 0.47),
            (canvas * 0.44, canvas * 0.51),
        ],
        fill=red,
    )
    draw.ellipse(
        (canvas * 0.39, canvas * 0.62, canvas * 0.48, canvas * 0.71),
        fill=red,
    )
    draw.ellipse(
        (canvas * 0.53, canvas * 0.58, canvas * 0.62, canvas * 0.67),
        fill=red,
    )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    APP.mkdir(parents=True, exist_ok=True)
    UI.mkdir(parents=True, exist_ok=True)
    draw_icon(64).save(APP / "ICON.PNG", "PNG")
    draw_icon(256).save(APP / "ICON_256.PNG", "PNG")
    draw_icon(64).save(UI / "icon_64.png", "PNG")
    draw_icon(256).save(UI / "icon_256.png", "PNG")
    print(f"wrote icons to {APP} and {UI}")


if __name__ == "__main__":
    main()