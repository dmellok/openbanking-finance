"""Capture dashboard screenshots into docs/screenshots/.

Assumes the demo server is running at http://127.0.0.1:8765 (see serve_demo.py)
and uses the system Google Chrome via Playwright.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import Page, async_playwright

URL = "http://127.0.0.1:8765/"
OUT = Path(__file__).resolve().parents[1] / "docs" / "screenshots"
TABS = ["spending", "networth", "cashflow", "insights", "trends", "forecast"]
VIEWPORT = {"width": 1440, "height": 900}


async def wait_for_render(page: Page) -> None:
    await page.wait_for_load_state("networkidle")
    # ECharts initialises after data arrives; give layout/animation a moment.
    await page.wait_for_timeout(900)


async def capture(theme: str) -> None:
    out_dir = OUT / theme
    out_dir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        ctx = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,  # retina-quality PNGs
        )
        await ctx.add_init_script(f"localStorage.setItem('pyfinance.theme', '{theme}');")
        page = await ctx.new_page()
        await page.goto(URL)
        await wait_for_render(page)

        # Expand the date filter to ~12 months so heatmaps/trends fill out.
        await page.fill("#filter-from", "2025-05-12")
        await page.fill("#filter-to", "2026-05-12")
        await page.click("#btn-refresh")
        await wait_for_render(page)

        for tab in TABS:
            await page.click(f'button.tab[data-tab="{tab}"]')
            await wait_for_render(page)
            await page.screenshot(path=str(out_dir / f"{tab}.png"), full_page=True)
            print(f"  [{theme}] {tab}.png")
        await browser.close()


async def main() -> None:
    for theme in ("dark", "light"):
        await capture(theme)


if __name__ == "__main__":
    asyncio.run(main())
