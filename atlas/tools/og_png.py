"""Rasterise the social card: `site/assets/img/og.svg` -> `og.png`.

    python -m atlas.tools.og_png        # or: make og

`og.svg` stays the source of truth — it is what a designer edits — but
it cannot be the social card: X/Twitter, Slack, LinkedIn and iMessage
all refuse SVG for `og:image`, so the `og:image`/`twitter:image` tags
point at a committed 1200x630 PNG instead.

Rendering goes through Chromium (the `e2e` dependency group) rather than
a rasteriser library: the card is drawn with the same engine that renders
the site, so a font fallback or a gradient that works in the browser
works here too, and no new runtime dependency is added for a file that is
regenerated perhaps twice a year. Regenerate and commit the PNG whenever
`og.svg` changes.

One caveat: the card asks for `Helvetica Neue, Helvetica, Arial,
sans-serif`, and the committed PNG was rendered on macOS. Re-rendering on
Linux picks a different face and changes the image -- so re-render on
macOS, or accept (and eyeball) the font change.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from atlas import config

DEFAULT_SVG = config.ROOT / "site" / "assets" / "img" / "og.svg"
DEFAULT_PNG = DEFAULT_SVG.with_suffix(".png")

#: The Open Graph card size every consumer scales from.
WIDTH = 1200
HEIGHT = 630

#: `background` matches the card's own backdrop, so a rounding error at
#: the edge shows card-coloured rather than white.
PAGE_TEMPLATE = """<!doctype html>
<meta charset="utf-8" />
<style>
  html, body {{ margin: 0; padding: 0; background: {background}; }}
  svg {{ display: block; width: {width}px; height: {height}px; }}
</style>
{svg}
"""

BACKGROUND_RE = re.compile(r'<rect[^>]*\bfill="(#[0-9a-fA-F]{3,8})"')


def backdrop(svg: str, fallback: str = "#0d1117") -> str:
    """The fill of the card's first `<rect>` — its background colour."""
    found = BACKGROUND_RE.search(svg)
    return found.group(1) if found else fallback


def render(svg_path: Path, png_path: Path, *, width: int, height: int) -> Path:
    """Screenshot `svg_path` at `width`x`height` into `png_path`."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:  # pragma: no cover - depends on the env
        raise SystemExit(
            "playwright is not installed: `uv sync --group e2e && "
            "uv run playwright install chromium`"
        ) from error

    svg = svg_path.read_text(encoding="utf-8")
    page_html = PAGE_TEMPLATE.format(
        background=backdrop(svg), width=width, height=height, svg=svg
    )

    png_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        )
        try:
            page = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            page.set_content(page_html, wait_until="load")
            page.screenshot(
                path=str(png_path),
                clip={"x": 0, "y": 0, "width": width, "height": height},
            )
        finally:
            browser.close()
    return png_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.tools.og_png",
        description="Render the Open Graph card from its SVG source.",
    )
    parser.add_argument("--svg", type=Path, default=DEFAULT_SVG)
    parser.add_argument("--out", type=Path, default=DEFAULT_PNG)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    args = parser.parse_args(argv)

    if not args.svg.is_file():
        print(f"error: {args.svg} not found", file=sys.stderr)
        return 1

    out = render(args.svg, args.out, width=args.width, height=args.height)
    print(f"og_png: {args.svg.name} -> {out} ({args.width}x{args.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
