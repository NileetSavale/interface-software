"""Sets up a fictional customer of our own on ParaBank, Parasoft's public demo bank, for the third-party evidence.

    uv run python scripts/parabank_setup.py

Registers a made-up customer through the public Register page, opens a second account and moves a small amount
between the two, so there's a balance to read and a transaction to find. This is seeding, done by a script like a
mock's fixtures: nothing here is an artifact or a model run. The login goes into .env as PARABANK_USER /
PARABANK_PASSWORD and is never printed. ParaBank resets its database now and then; run this again when sign-on fails.

The customer is fictional: name, address and an SSN in the never-issued 000 range. It's a public demo: no real bank,
no real money, no real person.
"""
import asyncio
import os
import re
import secrets
from pathlib import Path

from playwright.async_api import async_playwright

BASE = "https://parabank.parasoft.com/parabank"
ENV_FILE = Path(".env")
CUSTOMER = {"firstName": "Avery", "lastName": "Quill", "address.street": "1 Example Way", "address.city": "Springfield",
            "address.state": "IL", "address.zipCode": "62701", "phoneNumber": "555-010-0199", "ssn": "000-12-3456"}
TRANSFER = "25.00"  # the amount the evidence searches for


def save(values: dict[str, str]):
    lines = [l for l in (ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else [])
             if l.split("=", 1)[0] not in values]
    ENV_FILE.write_text("\n".join([*lines, *(f"{k}={v}" for k, v in values.items())]) + "\n")


async def register() -> list[str]:
    """Registers the customer, seeds the accounts, saves the login to .env and the process environment; returns the
    account numbers, the original first."""
    username, password = f"ifc{secrets.token_hex(4)}", secrets.token_urlsafe(12)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{BASE}/register.htm")
        for field, value in CUSTOMER.items():
            await page.fill(f'[id="customer.{field}"]', value)
        await page.fill('[id="customer.username"]', username)
        await page.fill('[id="customer.password"]', password)
        await page.fill("#repeatedPassword", password)
        await page.click('input[value="Register"]')
        await page.wait_for_selector("text=Your account was created successfully")

        # A second account, funded from the first, then a transfer back: two accounts and a known transaction
        await page.goto(f"{BASE}/openaccount.htm")
        await page.wait_for_function("document.querySelector('#fromAccountId')?.options.length > 0")
        await page.click('input[value="Open New Account"]')
        await page.wait_for_selector("#newAccountId")
        new_account = (await page.text_content("#newAccountId")).strip()
        await page.goto(f"{BASE}/transfer.htm")
        await page.wait_for_function("document.querySelector('#toAccountId')?.options.length > 1")
        await page.fill("#amount", TRANSFER)
        await page.select_option("#fromAccountId", new_account)
        await page.click('input[value="Transfer"]')
        await page.wait_for_selector("text=Transfer Complete!")
        await page.goto(f"{BASE}/overview.htm")
        await page.wait_for_selector("#accountTable a")
        accounts = [a.strip() for a in await page.locator("#accountTable a").all_text_contents() if re.fullmatch(r"\d+", a.strip())]
        await browser.close()
    save({"PARABANK_USER": username, "PARABANK_PASSWORD": password})
    os.environ.update({"PARABANK_USER": username, "PARABANK_PASSWORD": password})
    return accounts


async def customer_exists() -> bool:
    """Whether the saved login still signs on, outside any replay: after a reset of the public database it doesn't."""
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(f"{BASE}/index.htm", wait_until="load")
        await page.fill("input[name=username]", os.environ.get("PARABANK_USER", ""))
        await page.fill("input[name=password]", os.environ.get("PARABANK_PASSWORD", ""))
        await page.click('input[value="Log In"]')
        await page.wait_for_load_state("load")
        gone = await page.get_by_text("could not be verified").count() > 0
        await browser.close()
    return not gone


if __name__ == "__main__":
    print(f"registered a fictional customer; accounts {', '.join(asyncio.run(register()))}; login saved to {ENV_FILE}")
