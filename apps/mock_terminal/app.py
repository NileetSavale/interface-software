"""A stand-in for a green-screen core-banking system: a 24x80 character terminal served over telnet-style TCP.

Fake data only (the same members as the web mock). Run with:  uv run interface mock-terminal --port 4300

What a VT100 client sees is what a real screen-oriented host sends: ANSI cursor moves, reverse video for the fields
you can type in, a screen code top left (CB0300), a message line and a function-key line at the bottom. The keys
are the terminal's own: Tab and Backtab between fields, Ctrl-U erases a field, Enter, and F1-F12 as xterm sends
them. No HTML, no accessibility tree, no ids: whatever reads it has to read the characters.

Screens: CB0100 sign on → CB0200 main menu → CB0300 member inquiry → CB0310 member detail; from the menu or the
detail, CB0400 open sub-account → CB0410 review, where F10=Post commits (CB0420).

Faults, armed per server for the next N times they apply (TerminalBank.fault, or --fault at start, every time):
    session_timeout  the next inquiry finds the session expired and returns to sign-on
    server_error     the next inquiry shows a system error screen
    wrong_record     the next detail screen shows another member
    slow             the next detail screen takes 2 s to arrive
    late_fill        the next detail screen arrives with its values blank and fills them 1.5 s later
Variant B, a second bank on the same product: "Member # . ." reads "Mbr Number . .", Enter=Search reads
Enter=Find, and the screen title differs. Every field and value is where it was on the grid.
"""
import asyncio
import re
import threading
from dataclasses import dataclass, field

from apps.mock_core_banking.app import APP_PASSWORD, APP_USER, MEMBERS

ROWS, COLS = 24, 80
KEYS = {b"\r": "Enter", b"\x1bOP": "F1", b"\x1bOQ": "F2", b"\x1bOR": "F3", b"\x1bOS": "F4", b"\x1b[15~": "F5",
        b"\x1b[17~": "F6", b"\x1b[18~": "F7", b"\x1b[19~": "F8", b"\x1b[20~": "F9", b"\x1b[21~": "F10",
        b"\x1b[23~": "F11", b"\x1b[24~": "F12", b"\t": "Tab", b"\x1b[Z": "Backtab", b"\x15": "Erase",
        b"\x7f": "Backspace", b"\x08": "Backspace"}
MINIMUM_DEPOSIT = 25.00
ACCOUNT_TYPES = ("Savings", "Holiday Club", "Certificate")
FAULTS = {"session_timeout", "server_error", "wrong_record", "slow", "late_fill"}


@dataclass
class Field:
    name: str
    row: int
    col: int
    width: int
    hidden: bool = False
    value: str = ""


@dataclass
class Screen:
    code: str
    title: str
    lines: dict[int, str] = field(default_factory=dict)  # row -> text (1-based column 1)
    fields: list[Field] = field(default_factory=list)
    keys: str = ""
    message: str = ""


class TerminalBank:
    """The server: its variant, armed faults and how many sub-accounts were posted (which must stay 0)."""

    def __init__(self, variant: str = "a", always: str | None = None):
        self.variant_name, self.always = variant, always
        self.armed: dict[str, int] = {}
        self.confirmed = 0
        self.server: asyncio.base_events.Server | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def fault(self, name: str | None, times: int = 1):
        if name is None:
            self.armed.clear()
        else:
            assert name in FAULTS, name
            self.armed[name] = times

    def variant(self, name: str):
        self.variant_name = name

    def take(self, name: str) -> bool:
        if self.always == name:
            return True
        left = self.armed.get(name, 0)
        if left == 0:
            return False
        if left > 0:
            self.armed[name] = left - 1
        return True

    async def serve(self, port: int, host: str = "127.0.0.1"):
        self.loop = asyncio.get_running_loop()
        self.server = await asyncio.start_server(lambda r, w: Session(self, r, w).run(), host, port)
        return self.server

    def start_in_thread(self, port: int) -> "TerminalBank":
        """For tests and evals: the server on its own event loop in a daemon thread."""
        ready = threading.Event()

        def main():
            async def go():
                await self.serve(port)
                ready.set()
                try:
                    await self.server.serve_forever()
                except asyncio.CancelledError:
                    pass  # stopped by the test or eval that started it
            asyncio.run(go())
        threading.Thread(target=main, daemon=True).start()
        ready.wait(10)
        return self


class Session:
    def __init__(self, bank: TerminalBank, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.bank, self.reader, self.writer = bank, reader, writer
        self.screen: Screen | None = None
        self.focus = 0
        self.member: str | None = None
        self.draft: dict[str, str] = {}

    @property
    def b(self) -> bool:
        return self.bank.variant_name == "b"

    # ── Screens ────────────────────────────────────────────────────────────────────────────────────────────────

    def sign_on(self, message: str = "") -> Screen:
        return Screen("CB0100", "SIGN ON", {4: "Core Banking System", 6: "Sign on to continue."},
                      [Field("user", 9, 22, 10), Field("password", 11, 22, 20, hidden=True)],
                      "Enter=Sign on  F3=Exit", message) | _labels({9: "User ID . . . . :", 11: "Password . . . . :"})

    def menu(self, message: str = "") -> Screen:
        return Screen("CB0200", "MAIN MENU", {5: "Select one of the following:", 7: "   1. Member inquiry",
                                              8: "   2. Open sub-account", 9: "  90. Sign off"},
                      [Field("option", 12, 22, 2)], "Enter=Select  F3=Sign off", message) | _labels(
            {12: "Option . . . . . :"})

    def inquiry(self, message: str = "") -> Screen:
        label = "Mbr Number . . . :" if self.b else "Member # . . . . :"
        return Screen("CB0300", "MEMBER LOOKUP" if self.b else "MEMBER INQUIRY", {5: "Type a member number."},
                      [Field("member", 8, 22, 5)], f"Enter={'Find' if self.b else 'Search'}  F3=Exit  F12=Cancel",
                      message) | _labels({8: label})

    def detail(self, number: str, blank: bool = False) -> Screen:
        m = MEMBERS[number]
        balance = {kind: amount for kind, _, amount in m["accounts"]}
        value = (lambda v: "") if blank else (lambda v: v)
        rows = {6: f"Member # . . . . :  {value(number)}", 8: f"Name . . . . . . :  {value(m['name'])}",
                9: f"Date of birth . . :  {value(m['dob'])}",
                11: f"Savings balance . :  {value(balance.get('Savings', 'none'))}",
                12: f"Checking balance :  {value(balance.get('Checking', 'none'))}"}
        return Screen("CB0310", "MEMBER DETAIL", rows, [], "F3=Exit  F6=Open sub-account  F12=Cancel")

    def sub_account(self, message: str = "") -> Screen:
        s = Screen("CB0400", "OPEN SUB-ACCOUNT", {5: f"Account type: {', '.join(ACCOUNT_TYPES)}."},
                   [Field("member", 7, 22, 5), Field("type", 9, 22, 14), Field("nickname", 11, 22, 20),
                    Field("deposit", 13, 22, 10)], "Enter=Review  F12=Cancel", message) | _labels(
            {7: "Member # . . . . :", 9: "Account type . . :", 11: "Nickname . . . . :", 13: "Initial deposit . :"})
        for f in s.fields:
            f.value = self.draft.get(f.name, "")
        if self.member and not s.fields[0].value:
            s.fields[0].value = self.member
        return s

    def review(self) -> Screen:
        d = self.draft
        return Screen("CB0410", "REVIEW SUB-ACCOUNT", {
            5: "Check the details, then post to open the sub-account.", 7: f"Member # . . . . :  {d['member']}",
            9: f"Account type . . :  {d['type']}", 11: f"Nickname . . . . :  {d['nickname']}",
            13: f"Initial deposit . :  {d['deposit']}"}, [], "F10=Post  F12=Cancel")

    # ── Keys ───────────────────────────────────────────────────────────────────────────────────────────────────

    async def key(self, name: str):
        s, values = self.screen, {f.name: f.value.strip() for f in self.screen.fields}
        if name == "F3" and s.code in ("CB0100", "CB0200"):
            self.writer.close()
            return
        if name in ("F3", "F12"):
            await self.show(self.menu() if s.code != "CB0410" else self.sub_account())
            return
        match s.code, name:
            case "CB0100", "Enter":
                ok = values["user"] == APP_USER and values["password"] == APP_PASSWORD
                await self.show(self.menu() if ok else self.sign_on("Invalid user ID or password."))
            case "CB0200", "Enter":
                pick = {"1": self.inquiry, "2": self.sub_account}.get(values["option"])
                if values["option"] == "90":
                    await self.show(self.sign_on())
                else:
                    await self.show(pick() if pick else self.menu(f"Option {values['option'] or '(none)'} is not "
                                                                   f"valid."))
            case "CB0300", "Enter":
                await self.look_up(values["member"])
            case "CB0310", "F6":
                self.draft = {"member": self.member or ""}
                await self.show(self.sub_account())
            case "CB0400", "Enter":
                self.draft = values
                await self.show(self.check_sub_account(values))
            case "CB0410", "F10":
                self.bank.confirmed += 1
                await self.show(Screen("CB0420", "SUB-ACCOUNT OPENED", {6: "The sub-account was opened."}, [],
                                       "Enter=Main menu"))
            case "CB0420", "Enter":
                await self.show(self.menu())
            case _:
                s.message = "Function key not allowed."
                await self.show(s)

    async def look_up(self, number: str):
        if self.bank.take("session_timeout"):
            await self.show(self.sign_on("Session has expired. Sign on again."))
            return
        if self.bank.take("server_error"):
            await self.show(Screen("CB9999", "SYSTEM ERROR", {6: "Internal server error. The request was not "
                                                                 "completed.", 8: "Reference: CB-0001"}, [],
                                   "F3=Exit"))
            return
        if not re.fullmatch(r"[1-9]\d{4}", number):
            await self.show(self.inquiry("Invalid member number."))
        elif number not in MEMBERS or number == "99999":
            await self.show(self.inquiry("No member found for that number."))
        elif MEMBERS[number].get("restricted"):
            await self.show(self.inquiry("Not authorized to view this member."))
        else:
            self.member = number
            shown = next(n for n in MEMBERS if n != number) if self.bank.take("wrong_record") else number
            if self.bank.take("slow"):
                await asyncio.sleep(2)
            if self.bank.take("late_fill"):
                await self.show(self.detail(shown, blank=True))
                await asyncio.sleep(1.5)
                if self.screen.code != "CB0310":
                    return
            await self.show(self.detail(shown))

    def check_sub_account(self, v: dict) -> Screen:
        if v["member"] not in MEMBERS:
            return self.sub_account("No member found for that number.")
        if v["type"] not in ACCOUNT_TYPES:
            return self.sub_account(f"Account type must be one of: {', '.join(ACCOUNT_TYPES)}.")
        try:
            deposit = float(v["deposit"].replace(",", ""))
        except ValueError:
            return self.sub_account("Initial deposit must be an amount.")
        if deposit < MINIMUM_DEPOSIT:
            return self.sub_account(f"Initial deposit must be at least {MINIMUM_DEPOSIT:.2f}.")
        return self.review()

    # ── The terminal ───────────────────────────────────────────────────────────────────────────────────────────

    async def show(self, screen: Screen):
        self.screen, self.focus = screen, 0
        out = ["\x1b[0m\x1b[2J"]
        title = f"{screen.code}  {screen.title}"
        bank = "SECOND BANK" if self.b else "FIRST BANK"
        out.append(f"\x1b[1;1H{title}\x1b[1;{COLS - len(bank) + 1}H{bank}")
        for row, text in screen.lines.items():
            out.append(f"\x1b[{row};1H{text}")
        for f in screen.fields:
            shown = ("*" * len(f.value) if f.hidden else f.value).ljust(f.width)
            out.append(f"\x1b[{f.row};{f.col}H\x1b[7m{shown}\x1b[0m")
        if screen.message:
            out.append(f"\x1b[{ROWS - 1};1H{screen.message}")
        out.append(f"\x1b[{ROWS};1H{screen.keys}")
        out.append(self.cursor())
        self.writer.write("".join(out).encode())
        await self.writer.drain()

    def cursor(self) -> str:
        if not self.screen.fields:
            return f"\x1b[{ROWS};{COLS}H"
        f = self.screen.fields[self.focus]
        return f"\x1b[{f.row};{f.col + min(len(f.value), f.width - 1)}H"

    async def echo(self):
        f = self.screen.fields[self.focus]
        shown = ("*" * len(f.value) if f.hidden else f.value).ljust(f.width)
        self.writer.write(f"\x1b[{f.row};{f.col}H\x1b[7m{shown}\x1b[0m{self.cursor()}".encode())
        await self.writer.drain()

    async def run(self):
        await self.show(self.sign_on())
        buffer = b""
        try:
            while not self.writer.is_closing():
                data = await self.reader.read(256)
                if not data:
                    break
                buffer += data
                while buffer:
                    name, size = next(((n, len(k)) for k, n in KEYS.items() if buffer.startswith(k)), (None, 0))
                    if name is None and buffer.startswith(b"\x1b") and len(buffer) < 5:
                        break  # the rest of an escape sequence is on its way
                    if name is None:
                        size = 1
                        await self.typed(buffer[:1].decode(errors="ignore"))
                    elif name in ("Tab", "Backtab") and self.screen.fields:
                        self.focus = (self.focus + (1 if name == "Tab" else -1)) % len(self.screen.fields)
                        self.writer.write(self.cursor().encode())
                    elif name in ("Erase", "Backspace") and self.screen.fields:
                        f = self.screen.fields[self.focus]
                        f.value = "" if name == "Erase" else f.value[:-1]
                        await self.echo()
                    elif name not in ("Tab", "Backtab", "Erase", "Backspace"):
                        await self.key(name)
                    buffer = buffer[size:]
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.writer.close()

    async def typed(self, ch: str):
        if not self.screen.fields or not ch.isprintable():
            return
        f = self.screen.fields[self.focus]
        if len(f.value) < f.width:
            f.value += ch
            await self.echo()


def _labels(labels: dict[int, str]):
    """`screen | _labels({row: "Label :"})`: the protected text in front of each field."""
    class Merge:
        def __ror__(self, screen: Screen) -> Screen:
            screen.lines.update(labels)
            return screen
    return Merge()


def main(port: int = 4300, variant: str = "a", fault: str | None = None):
    async def run():
        bank = TerminalBank(variant, always=fault)
        server = await bank.serve(port)
        print(f"mock terminal bank {variant} on telnet://127.0.0.1:{port}", flush=True)
        await server.serve_forever()
    asyncio.run(run())
