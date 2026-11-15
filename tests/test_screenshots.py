"""Evidence screenshots have PII blacked out, by the same detectors as the text (§6)."""
import asyncio

import httpx
from playwright.async_api import async_playwright

from interface.surface.web import WebSurface
from tests.conftest import ENV


async def on_member_page(base_url, check):
    httpx.delete(base_url + "/__faults")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        surface = await WebSurface.open(await browser.new_context())
        await surface.navigate(base_url + "/")
        await surface.observe()
        main = surface.page.frame("main")
        await main.get_by_label("User ID").fill(ENV["APP_USER"])
        await main.get_by_label("Password").fill(ENV["APP_PASSWORD"])
        await main.get_by_role("button", name="Sign On").click()
        await surface.observe()
        await surface.page.frame("main").goto(base_url + "/member?id=12345")
        await surface.observe()
        try:
            return await check(surface)
        finally:
            await browser.close()


def test_screenshot_masks_record_data_and_restores_the_page(base_url, tmp_path):
    async def check(surface):
        masked = await surface.mask_pii(True)
        styled = await surface.page.frame("main").evaluate(
            "() => document.querySelectorAll('[data-ifz-masked]').length")
        await surface.mask_pii(False)
        restored = await surface.page.frame("main").evaluate(
            "() => document.querySelectorAll('[data-ifz-masked]').length")
        shot = await surface.screenshot(str(tmp_path / "member.png"))
        return masked, styled, restored, shot

    masked, styled, restored, shot = asyncio.run(on_member_page(base_url, check))
    for value in ("12345", "Jane Doe", "0001-12345-00", "$4,210.55", "0001-12345-10", "$812.07"):
        assert value in masked, value
    assert "Savings" not in masked and "Member #:" not in masked  # labels and categories stay readable
    assert styled >= 6
    assert restored == 0  # the page is left as it was, so replay isn't affected
    assert shot and (tmp_path / "member.png").stat().st_size > 0
