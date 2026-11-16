"""The web Surface: Playwright over the browser's accessibility tree, frames included.

The model sees Playwright's AI snapshot (refs like f2e11 that work across frames and go stale after the next
snapshot). Replay never sees refs: it resolves recorded strategies with Playwright's role/label/text locators and,
for legacy tables, the in-page helpers in locate.js.
"""
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Literal

from playwright.async_api import BrowserContext, ElementHandle, Error as PlaywrightError, Frame, Locator, Page

from interface.artifact.schema import (
    Action, AnchorStrategy, LabelStrategy, OnlyInRegionStrategy, Policy, Regex, RegionScope, RoleStrategy, Strategy, TableCellStrategy, Target, TextMatch,
    TextStrategy, Visible,
)
from interface.pii import LABELS, PATTERNS
from interface.artifact.templates import bind
from interface.policy import refused
from interface.surface.base import ErrorCode, Observation, Resolved, Surface, SurfaceError, Unresolved

HUMAN_JS = (Path(__file__).parent / "human.js").read_text()
FORM_KEY_JS = """el => { const form = el.form || el.closest('form'); if (!form) return null;
  return el.ownerDocument.location.pathname + '#form' + Array.from(el.ownerDocument.forms).indexOf(form); }"""
KEEP_MASKED_JS = """(() => { const [p, l] = %s;
  const mask = () => window.__ifz && window.__ifz.maskPII(p, l, true);
  document.addEventListener("DOMContentLoaded", mask); document.addEventListener("input", mask, true);
  setInterval(mask, 100); })();"""
LOCATE_JS = (Path(__file__).parent / "locate.js").read_text()
POLL_SECONDS = 0.15
_SNAPSHOT_LINE = re.compile(r"""^\s*-\s+'?(?P<role>[a-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?.*\[ref=(?P<ref>\w+)\]""")
_REF = re.compile(r"\s*\[ref=\w+\]")
_IFRAME = re.compile(r"- iframe ((?:\[[a-z]+\] )*)\[ref=(\w+)\]")


def py_pattern(match: TextMatch) -> str | re.Pattern:
    """For Playwright's locators. Inline (?i) becomes a flag, since Playwright hands the source to JavaScript."""
    if isinstance(match, str):
        return match
    source, flags = match.matches, 0
    if source.startswith("(?i)"):
        source, flags = source[4:], re.IGNORECASE
    return re.compile(source, flags)


def js_pattern(match: TextMatch) -> dict:
    """For locate.js."""
    if isinstance(match, str):
        return {"exact": match}
    source = match.matches
    return {"regex": [source[4:], "i"]} if source.startswith("(?i)") else {"regex": [source, ""]}


def fingerprint(snapshot: str) -> str:
    """The screen's shape without its data: refs dropped, and any quoted text containing a digit (member numbers,
    balances, dates) blanked. Two members' detail pages hash the same; a detail page and an error page don't."""
    shape = _REF.sub("", snapshot)
    shape = re.sub(r'"[^"]*\d[^"]*"', '"#"', shape)
    return hashlib.sha256(shape.encode()).hexdigest()[:16]


class WebSurface(Surface):
    def __init__(self, page: Page):
        self.page = page
        self._snapshot = ""
        self._snapshots = 0
        self.blocked: list[str] = []  # requests the policy refused, in order; the caller checks after each action
        self._documents = [0, 0]  # page loads and posts: [sent, decided by the policy gate]

    @classmethod
    async def open(cls, context: BrowserContext, on_human: Callable[[dict], Awaitable[None]] | None = None,
                   policy: Policy | None = None, masked_view: bool = False) -> "WebSurface":
        """on_human receives what a person does in this browser, already described as a recorded target. The page
        reports every click and change; the callback decides whether a person was holding control at the time.

        With a policy, every request the browser makes goes through it first (policy.refused): the guard sits below
        anything a page can say, so a misleading label or a link elsewhere can't get past it. A person holding
        control is held to it too."""
        blocked: list[str] = []
        documents = [0, 0]
        if policy:
            def is_document(request) -> bool:
                return request.is_navigation_request() or request.method != "GET"

            async def gate(route):
                request = route.request
                document = is_document(request)
                try:
                    if reason := refused(policy, request.url, document):
                        blocked.append(reason)
                        await route.abort("blockedbyclient")
                    else:
                        await route.continue_()
                finally:
                    documents[1] += document
            context.on("request", lambda request: documents.__setitem__(0, documents[0] + is_document(request)))
            await context.route("**/*", gate)
        await context.add_init_script(script=LOCATE_JS)
        if masked_view:
            # For a screen recording: PII stays blacked out in every frame for the whole run, re-applied as pages
            # load and as fields are filled, so the video never shows a record's data
            await context.add_init_script(script=KEEP_MASKED_JS % json.dumps(
                [[p for _, p in PATTERNS], [l for _, l in LABELS]]))
        if on_human:
            async def relay(payload: dict):
                await on_human(human_action(payload))
            await context.expose_function("__ifzHuman", relay)
            await context.add_init_script(script=HUMAN_JS)
        surface = cls(await context.new_page())
        surface.blocked, surface._documents = blocked, (documents if policy else None)
        return surface

    def refusals_since(self, mark: int) -> list[str]:
        return self.blocked[mark:]

    # ── Observing ──────────────────────────────────────────────────────────────────────────────────────────────

    async def observe(self, *, screenshot: bool = False, screenshot_path: str | None = None) -> Observation:
        text = await self._stable_snapshot()
        text = await self._name_frames(text)
        self._snapshot = text
        self._snapshots += 1
        shot = await self.screenshot(screenshot_path) if screenshot and screenshot_path else None
        return Observation(snapshot_id=f"s{self._snapshots}", url=self.page.url, text=text,
                           fingerprint=fingerprint(text), screenshot_path=shot)

    async def _stable_snapshot(self, timeout_s: float = 5.0) -> str:
        """A snapshot taken once the screen has stopped changing. A click in one frame often loads another a moment
        later, and legacy apps give no signal for "done", so: wait for every frame to load, then for two snapshots
        in a row to agree (ignoring refs, which are renumbered each time)."""
        deadline = time.monotonic() + timeout_s
        previous = None
        while True:
            await asyncio.sleep(0.25)
            for frame in self.page.frames:
                try:
                    await frame.wait_for_load_state("domcontentloaded", timeout=max(1, int((deadline - time.monotonic()) * 1000)))
                except PlaywrightError:
                    pass
            try:
                text = await self.page.aria_snapshot(mode="ai")
            except PlaywrightError:
                text = None  # a frame navigated mid-snapshot
            if text is not None and (_REF.sub("", text) == previous or time.monotonic() >= deadline):
                return text
            previous = _REF.sub("", text) if text else None

    async def _name_frames(self, text: str) -> str:
        """Frames show up as anonymous iframes in the snapshot. Name them, so the model (and a reader) can tell the
        menu from the content, and so recorded targets can say which frame they're in."""
        def label(match: re.Match) -> str:
            return f'- iframe "{names.get(match.group(2), "")}" {match.group(1)}[ref={match.group(2)}]'
        names = {}
        for _, ref in _IFRAME.findall(text):
            try:
                names[ref] = await self.page.locator(f"aria-ref={ref}").get_attribute("name", timeout=1_000) or ""
            except PlaywrightError:
                names[ref] = ""
        return _IFRAME.sub(label, text)

    async def screenshot(self, path: str) -> str | None:
        """A screenshot for evidence, with PII blacked out in every frame first. Fails closed: if any frame can't be
        masked, no screenshot is written. (The operator's live view is unmasked and never stored.)"""
        try:
            await self.mask_pii(True)
            await self.page.screenshot(path=path, full_page=True)
            return path
        except PlaywrightError:
            return None
        finally:
            await self.mask_pii(False, strict=False)

    async def mask_pii(self, on: bool, strict: bool = True) -> list[str]:
        masked = []
        for frame in self.page.frames:
            try:
                masked += await frame.evaluate("([p, l, on]) => window.__ifz.maskPII(p, l, on)",
                                               [[p for _, p in PATTERNS], [l for _, l in LABELS], on])
            except PlaywrightError:
                if strict:
                    raise
        return masked

    # ── Discovery: acting on refs from the latest snapshot ─────────────────────────────────────────────────────

    def _ref_locator(self, ref: str) -> Locator:
        if not re.fullmatch(r"\w+", ref):
            raise SurfaceError("STALE_REF", f"{ref!r} isn't a ref from the snapshot")
        return self.page.locator(f"aria-ref={ref}")

    async def _ref_handle(self, ref: str) -> ElementHandle:
        try:
            handle = await self._ref_locator(ref).element_handle(timeout=2_000)
        except PlaywrightError as e:
            raise SurfaceError("STALE_REF", f"ref {ref} isn't on the current screen; use a ref from the latest snapshot",
                               retry="safe") from e
        if handle is None:
            raise SurfaceError("STALE_REF", f"ref {ref} isn't on the current screen", retry="safe")
        return handle

    def ref_line(self, ref: str) -> tuple[str, str]:
        """(role, name) of a ref, read from the snapshot the model chose it from."""
        for line in self._snapshot.splitlines():
            m = _SNAPSHOT_LINE.match(line)
            if m and m.group("ref") == ref:
                return m.group("role"), (m.group("name") or "").replace('\\"', '"')
        raise SurfaceError("STALE_REF", f"ref {ref} isn't in the latest snapshot", retry="safe")

    async def act_on_ref(self, ref: str, action: Action, value: str | None = None,
                         guard: Callable[[], None] | None = None) -> None:
        await self.act(await self._ref_handle(ref), action, value, guard)

    async def read_ref(self, ref: str) -> str:
        return await self.read_text(await self._ref_handle(ref))

    async def frame_of(self, ref: str) -> list[str]:
        return frame_path(await (await self._ref_handle(ref)).owner_frame())

    async def form_of(self, ref: str) -> str | None:
        """Which form a control belongs to, as document path + the form's place among that document's forms; None
        outside a form."""
        return await (await self._ref_handle(ref)).evaluate(FORM_KEY_JS)

    async def path_of(self, ref: str) -> str:
        """The URL path of the document the ref is in (its frame's, not the top page's)."""
        return await (await self._ref_handle(ref)).evaluate("el => el.ownerDocument.location.pathname")

    async def describe(self, ref: str, purpose: Literal["act", "read"] = "act",
                       inputs: dict[str, str] | None = None, first_result: bool = False) -> Target:
        role, name = self.ref_line(ref)
        handle = await self._ref_handle(ref)
        frame = await handle.owner_frame()
        path = frame_path(frame)
        candidates: list[Strategy] = []
        # A name with a digit in it is data (a member number on a results link), not a label: never record it. Unless
        # it's exactly what the caller asked for (the link to account 13899 when accountId=13899): then it's recorded
        # as the input, {{inputs.accountId}}, and filled in with whatever account the next run asks for
        inputs = inputs or {}
        given = next((n for n, v in inputs.items() if v and v.strip() == name.strip()), None) if name else None
        if given:
            name = f"{{{{inputs.{given}}}}}"
        if purpose == "act" and name and (given or not re.search(r"\d", name)):
            candidates.append(RoleStrategy(role=role, name=name))
            if role in {"textbox", "combobox", "checkbox", "radio", "spinbutton"}:
                candidates.append(LabelStrategy(label=name))
            if role in {"link", "button"}:
                candidates.append(TextStrategy(text=name))
        found = await frame.evaluate("([el, first]) => window.__ifz.describeValue(el, first)",
                                     [handle, purpose == "read" and first_result])
        as_control = {"role": role} if purpose == "act" else {}
        candidates += [TableCellStrategy.model_validate({**c, **as_control}) if c["kind"] == "tableCell" else
                       AnchorStrategy.model_validate({**c, **as_control}) for c in found]

        # Keep only strategies that find exactly this element, checked against the live page. When none does on the
        # whole frame (two "Search" buttons), try again inside the element's labelled region.
        target = Target(frame=path, strategies=candidates or [RoleStrategy(role=role, name=name)])
        kept = await self._unique(target, candidates, handle, inputs)
        # "The <role> after this text" is the weakest way to find a control: when that's all that's unique on the
        # frame, a labelled region with the control's own name in it is preferred, the "after" kept below it
        only_after = all(isinstance(k, AnchorStrategy) and k.relation == "after" for k in kept)
        if only_after and (region := await frame.evaluate("el => window.__ifz.regionOf(el)", handle)):
            target = target.model_copy(update={"within": RegionScope(role=region["role"], name=region["name"])})
            # Inside a region, "its only <role>" is a fallback that survives a tenant relabelling the control
            if purpose == "act":
                candidates.append(OnlyInRegionStrategy(role=role))
            scoped = await self._unique(target, candidates, handle, inputs)
            if scoped:
                kept = scoped
            else:
                target = target.model_copy(update={"within": None})
        if not kept:
            raise SurfaceError("TARGET_UNRESOLVED", f"no stable way to find {role} {name!r} again; pick a labelled "
                                                    f"control or a value in a labelled row or table")
        return target.model_copy(update={"strategies": kept})

    async def _unique(self, target: Target, candidates: list[Strategy], handle: ElementHandle,
                      inputs: dict[str, str]) -> list[Strategy]:
        kept = []
        for strategy in candidates:
            # Checked the way replay will find it: with this run's inputs filled in
            bound = bind(target.model_copy(update={"strategies": [strategy]}), inputs)
            hits = await self._hits(bound, bound.strategies[0])
            if len(hits) == 1 and await hits[0].evaluate("(a, b) => a === b", handle):
                kept.append(strategy)
        return kept

    # ── Replay: resolving recorded targets ─────────────────────────────────────────────────────────────────────

    async def resolve(self, target: Target, timeout_ms: int = 10_000) -> Resolved | Unresolved:
        """Try the strategies in rank order until one finds exactly one element, polling while the page loads.
        A strategy that finds several is ambiguous and is never resolved by picking one."""
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            attempts: list[tuple[str, ErrorCode]] = []
            for rank, strategy in enumerate(target.strategies):
                hits = await self._hits(target, strategy)
                if len(hits) == 1:
                    if rank > 0:
                        # A fallback hit while the frame was still loading isn't drift: the better strategies may
                        # simply have run a moment too early. Check them once more on the settled page.
                        await asyncio.sleep(POLL_SECONDS)
                        for better, retry in enumerate(target.strategies[:rank]):
                            if len(again := await self._hits(target, retry)) == 1:
                                return Resolved(handle=again[0], rank=better, strategy=retry.kind)
                    return Resolved(handle=hits[0], rank=rank, strategy=strategy.kind)
                attempts.append((strategy.kind, "AMBIGUOUS_TARGET" if hits else "TARGET_UNRESOLVED"))
            if time.monotonic() >= deadline:
                return Unresolved(attempts=attempts)
            await asyncio.sleep(POLL_SECONDS)

    async def _hits(self, target: Target, strategy: Strategy) -> list[ElementHandle]:
        frame = self._frame(target.frame)
        if frame is None:
            return []
        try:
            await frame.wait_for_load_state("domcontentloaded", timeout=2_000)
        except PlaywrightError:
            return []
        hits = await self._find(frame, strategy)
        return await self._inside(frame, target.within, hits) if hits and target.within else hits

    async def _inside(self, frame: Frame, scope: RegionScope, hits: list[ElementHandle]) -> list[ElementHandle]:
        """Keep the hits inside the one region the scope names. No region, or several, means no hits: a scope that
        doesn't pin down one region can't be trusted to disambiguate."""
        regions = await frame.get_by_role(scope.role, name=py_pattern(scope.name), exact=True).element_handles()
        if len(regions) != 1:
            return []
        return [h for h in hits if await frame.evaluate("([r, h]) => r.contains(h)", [regions[0], h])]

    def _frame(self, path: list[str]) -> Frame | None:
        frame = self.page.main_frame
        for name in path:
            frame = next((f for f in frame.child_frames if f.name == name), None)
            if frame is None:
                return None
        return frame

    async def _find(self, frame: Frame, strategy: Strategy) -> list[ElementHandle]:
        try:
            match strategy:
                case RoleStrategy():
                    return await frame.get_by_role(strategy.role, name=py_pattern(strategy.name), exact=True).element_handles()
                case LabelStrategy():
                    return await frame.get_by_label(py_pattern(strategy.label), exact=True).element_handles()
                case TextStrategy():
                    return await frame.get_by_text(py_pattern(strategy.text), exact=True).element_handles()
                case OnlyInRegionStrategy():
                    # Narrowed to the target's region by _inside; alone it matches every control of the role
                    return await frame.get_by_role(strategy.role).element_handles()
                case AnchorStrategy():
                    found = await frame.evaluate_handle("([a, r, role]) => window.__ifz.anchor(a, r, role)",
                                                        [js_pattern(strategy.anchor_text), strategy.relation, strategy.role])
                case TableCellStrategy():
                    where = strategy.row_where
                    found = await frame.evaluate_handle(
                        "([t, c, rc, re, role, first]) => window.__ifz.tableCell(t, c, rc, re, role, first)",
                        [js_pattern(strategy.table_anchor), strategy.column, where.column if where else None,
                         where.equals if where else None, strategy.role, strategy.row == "first"])
                case _:
                    return []  # structural and bbox: not implemented on the web surface yet
            items = await found.get_properties()
            return [h for h in (v.as_element() for v in items.values()) if h is not None]
        except PlaywrightError:
            return []  # the frame is navigating; the caller polls again

    # ── Acting and checking ────────────────────────────────────────────────────────────────────────────────────

    async def act(self, handle: object, action: Action, value: str | None = None,
                  guard: Callable[[], None] | None = None) -> None:
        """guard runs at the last moment before the input is dispatched, after Playwright's actionability waits
        (that's what the trial click is for), and raises to drop the action: the control-token epoch check."""
        assert isinstance(handle, ElementHandle)
        try:
            if guard:
                if action in ("click", "check", "uncheck"):
                    await handle.click(trial=True, timeout=5_000)
                guard()
            if action in ("click", "press"):
                await self._keep_typed()
            match action:
                case "click":
                    sent = self._documents[0] if self._documents else 0
                    await handle.click(timeout=5_000)
                    await self._settle(sent)
                case "type":
                    await self._type(handle, value or "")
                case "select":
                    await handle.select_option(label=value, timeout=5_000)
                case "check":
                    await handle.check(timeout=5_000)
                case "uncheck":
                    await handle.uncheck(timeout=5_000)
                case "press":
                    await handle.press(value or "Enter", timeout=5_000)
                case _:
                    raise SurfaceError("NOT_ACTIONABLE", f"{action} isn't an action on an element")
        except PlaywrightError as e:
            message = str(e).splitlines()[0]
            code: ErrorCode = "TIMEOUT" if "Timeout" in message else "NOT_ACTIONABLE"
            raise SurfaceError(code, message, retry="safe") from e

    async def _type(self, handle: ElementHandle, value: str):
        """Fill, then check the field holds exactly that. A legacy page's onload script can move focus while text is
        going in (ParaBank focuses the username box on load), and the rest lands in another field: a secret in the
        wrong place. One more try, then a failure that names no value."""
        for _ in range(2):
            await handle.fill(value, timeout=5_000)
            if await handle.input_value() == value:
                kept = []
                for h, v in getattr(self, "_typed", []):
                    try:
                        if not await h.evaluate("(a, b) => a === b", handle):
                            kept.append((h, v))
                    except PlaywrightError:
                        pass
                self._typed = [*kept, (handle, value)]
                return
        raise SurfaceError("NOT_ACTIONABLE", "the field didn't keep what was typed into it", retry="safe")

    async def _keep_typed(self):
        """Before a click submits anything, every field typed on this page must still hold what was typed: text that
        moved into another field is put back, so it's never sent."""
        kept = []
        for handle, value in getattr(self, "_typed", []):
            try:
                if await handle.input_value() != value:
                    await handle.fill(value, timeout=5_000)
                kept.append((handle, value))
            except PlaywrightError:
                pass  # its page is gone; nothing of it can be sent now
        self._typed = kept

    async def _settle(self, sent: int, window_s: float = 0.3):
        """A click's page load or form post can leave after click() returns. Wait briefly for one to start, and then
        for the policy gate to decide it, so a refusal is reported against the click that caused it."""
        if self._documents is None:
            return
        deadline = time.monotonic() + window_s
        while time.monotonic() < deadline:
            started, decided = self._documents
            if started > sent and decided >= started:
                return
            await asyncio.sleep(0.02)

    async def read_text(self, handle: object) -> str:
        assert isinstance(handle, ElementHandle)
        tag = await handle.evaluate("el => el.tagName")
        if tag in {"INPUT", "TEXTAREA", "SELECT"}:
            return (await handle.input_value()).strip()
        return " ".join((await handle.inner_text()).split())

    def _visible_locator(self, frame: Frame, condition: Visible) -> Locator:
        if condition.role:
            name = condition.name or condition.label
            loc = frame.get_by_role(condition.role, **({"name": py_pattern(name), "exact": True} if name else {}))
            return loc.filter(has_text=py_pattern(condition.text)) if condition.text else loc
        if condition.label:
            return frame.get_by_label(py_pattern(condition.label), exact=True)
        return frame.get_by_text(py_pattern(condition.text or condition.name), exact=isinstance(condition.text or condition.name, str))

    async def headings(self) -> list[Visible]:
        """The visible headings that hold no data: what a screen is called, whatever record it shows."""
        out = []
        for frame in self.page.frames:
            try:
                for heading in await frame.get_by_role("heading").all():
                    name = (await heading.inner_text()).strip()
                    if name and len(name) <= 60 and not re.search(r"\d", name) and await heading.is_visible():
                        out.append(Visible(frame=frame_path(frame), role="heading", name=name))
            except PlaywrightError:
                continue
        return out

    async def holds(self, condition: Visible) -> bool:
        frames = self.page.frames if condition.frame is None else [f for f in [self._frame(condition.frame)] if f]
        for frame in frames:
            try:
                loc = self._visible_locator(frame, condition)
                for i in range(await loc.count()):
                    if await loc.nth(i).is_visible():
                        return True
            except PlaywrightError:
                continue
        return False

    async def wait_any(self, conditions: list[Visible], timeout_ms: int, frame: list[str] | None = None,
                       load_state: str | None = None) -> int | None:
        deadline = time.monotonic() + timeout_ms / 1000
        if load_state:
            target = self._frame(frame or [])
            if target:
                try:
                    await target.wait_for_load_state(load_state, timeout=timeout_ms)
                except PlaywrightError:
                    pass
        if not conditions:
            return None
        while True:
            for i, condition in enumerate(conditions):
                if await self.holds(condition):
                    return i
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(POLL_SECONDS)

    async def navigate(self, url: str) -> None:
        try:
            # "load", not just parsed: onload scripts (focus moves, form resets) have run before anything is typed
            await self.page.goto(url, wait_until="load")
        except PlaywrightError as e:
            raise SurfaceError("TIMEOUT", str(e).splitlines()[0], retry="safe") from e


def frame_path(frame: Frame) -> list[str]:
    path = []
    while frame.parent_frame is not None:
        path.insert(0, frame.name)
        frame = frame.parent_frame
    return path


def human_action(payload: dict) -> dict:
    """A person's click or change, as the recorder would have described it: the same ranked strategies, built from
    what the page reported at the moment of the event. Not verified for uniqueness (the page has usually moved on),
    which is one reason human steps always need review."""
    role, name = payload["role"], payload.get("name") or ""
    strategies: list[Strategy] = []
    if name and not re.search(r"\d", name):
        strategies.append(RoleStrategy(role=role, name=name))
        if role in {"textbox", "combobox", "checkbox", "radio"}:
            strategies.append(LabelStrategy(label=name))
        if role in {"link", "button"}:
            strategies.append(TextStrategy(text=name))
    for place in payload.get("places") or []:
        place = {**place, "role": role}
        strategies.append(TableCellStrategy.model_validate(place) if place["kind"] == "tableCell"
                          else AnchorStrategy.model_validate(place))
    region = payload.get("region")
    target = Target(frame=payload.get("frame") or [], strategies=strategies,
                    within=RegionScope(**region) if region else None) if strategies else None
    action = "click" if payload["kind"] == "click" else ("select" if role == "combobox" else "type")
    return {"action": action, "role": role, "name": name, "target": target, "value": payload.get("value")}
