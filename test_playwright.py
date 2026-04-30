import asyncio
from playwright.async_api import async_playwright

async def test_login():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.xiaohongshu.com/explore")
        # Find QR code image
        try:
            img = page.locator("img.qrcode-img")
            await img.wait_for(timeout=10000)
            src = await img.get_attribute("src")
            print("QR SRC:", src[:50] + "...")
        except Exception as e:
            print("Failed to find QR code:", e)
        await browser.close()

asyncio.run(test_login())
