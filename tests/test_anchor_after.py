"""The "after" anchor: a control with no name of its own, found by the text a reader meets just before it. Found on
ParaBank, whose sign-on puts "Username" in a paragraph over the box and whose search page has four identical
"Find Transactions" buttons, each told apart only by the line before it."""
import asyncio

from playwright.async_api import async_playwright

from interface.artifact.schema import AnchorStrategy, Target
from interface.surface.web import WebSurface

PAGE = """<form>
  <p><b>Username</b></p><div><input name="user"></div>
  <p><b>Password</b></p><div><input type="password" name="pw"></div>
  <div><input type="submit" value="Log In"></div>
  <hr><div><b>Find by Date</b>: <input id="date"> ( MM-DD-YYYY )</div>
  <div><button id="byDate">Find Transactions</button></div>
  <hr><div>Between <input id="from"> and <input id="to"> ( MM-DD-YYYY )</div>
  <div><button id="byRange">Find Transactions</button></div>
  <hr><div><b>Find by Amount</b>: <input id="amount"></div>
  <div><button id="byAmount">Find Transactions</button></div>
</form>"""


async def described_and_found(selectors: list[str]) -> dict[str, tuple[str, list[str]]]:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        surface = await WebSurface.open(await browser.new_context())
        await surface.page.set_content(PAGE)
        out = {}
        for sel in selectors:
            handle = await surface.page.query_selector(sel)
            after = [c for c in await handle.evaluate("el => window.__ifz.describeValue(el)") if c["relation"] == "after"]
            text = after[0]["anchorText"] if after else ""
            role = "button" if sel.startswith("#by") or "submit" in sel else "textbox"
            target = Target(frame=[], strategies=[AnchorStrategy(anchor_text=text or "?", relation="after", role=role)])
            hits = await surface._hits(target, target.strategies[0])
            out[sel] = (text, [await h.evaluate("el => el.id || el.name") for h in hits])
        await browser.close()
    return out


def test_controls_are_found_by_the_line_before_them():
    found = asyncio.run(described_and_found(
        ["input[name=user]", "input[name=pw]", "#amount", "#byAmount", "#byDate", "#to", "#byRange"]))
    assert found["input[name=user]"] == ("Username", ["user"])       # a caption in the paragraph above
    assert found["input[name=pw]"] == ("Password", ["pw"])
    assert found["#amount"] == ("Find by Amount:", ["amount"])        # the line it sits on, up to the box
    assert found["#byAmount"] == ("Find by Amount:", ["byAmount"])    # one of four same-named buttons
    assert found["#byDate"] == ("Find by Date: ( MM-DD-YYYY )", ["byDate"])
    assert found["#to"] == ("and", ["to"])                            # only the text since the previous box
    assert found["#byRange"][1] == ["byRange"]


LOGIN = """<form name="f"><p>Username</p><input name="user"><p>Password</p><input type="password" name="pw">
  <input type="submit" value="Log In"></form>"""


def test_text_that_lands_in_another_field_is_put_back_before_submitting():
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            surface = await WebSurface.open(await browser.new_context())
            await surface.page.set_content(LOGIN)
            user, pw = await surface.page.query_selector("[name=user]"), await surface.page.query_selector("[name=pw]")
            await surface.act(user, "type", "teller")
            await surface.act(pw, "type", "s3cret-value")
            # The page changes a typed field before submit. On ParaBank an onload focus move sent part of the password
            # into the username box; whatever moved it, what's sent must be what was typed
            await user.evaluate("el => el.value += 'stray'")
            await surface.page.evaluate("document.f.addEventListener('submit', e => e.preventDefault())")
            await surface.act(await surface.page.query_selector("[type=submit]"), "click")
            values = (await user.input_value(), await pw.input_value())
            await browser.close()
            return values

    assert asyncio.run(run()) == ("teller", "s3cret-value")
