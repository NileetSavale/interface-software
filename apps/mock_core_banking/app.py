"""A stand-in for a legacy core-banking back office: server-rendered, a frameset, layout tables, no ids or test ids.

Fake data only. Run with:  uv run uvicorn apps.mock_core_banking.app:app --port 4100

Faults (docs/ARCHITECTURE.md §8) are armed per server, for the next N member pages:
    POST /__faults?name=session_timeout&times=1      DELETE /__faults      or FAULT=server_error at start (every time)
Some outcomes need no fault, the data produces them: 99999 isn't a member, 45678 is restricted, and a number
starting with 0 is invalid. Variant B and the sub-account flow come later in Pass 2.
"""
import asyncio
import os
import secrets
from html import escape

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

APP_USER = os.environ.get("APP_USER", "teller01")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "demo-only-password")

MEMBERS = {
    "12345": {"name": "Jane Doe", "accounts": [("Savings", "0001-12345-00", "4,210.55"), ("Checking", "0001-12345-10", "812.07")]},
    "23456": {"name": "Rafael Ortiz", "accounts": [("Savings", "0001-23456-00", "15,002.10")]},
    "34567": {"name": "Mei Chen", "accounts": [("Checking", "0001-34567-10", "96.40"), ("Savings", "0001-34567-00", "2,500.00")]},
    "45678": {"name": "Staff Member", "restricted": True, "accounts": [("Savings", "0001-45678-00", "1,000.00")]},
}
FAULTS = {"server_error", "session_timeout", "interstitial", "slow", "unknown_dialog", "wrong_record"}
SLOW_SECONDS = float(os.environ.get("SLOW_SECONDS", "3"))

app = FastAPI()
_sessions: set[str] = set()
_fault = {"name": os.environ.get("FAULT") or None, "times": -1}  # times -1: every time


@app.post("/__faults")
def arm_fault(name: str, times: int = 1):
    if name not in FAULTS:
        return HTMLResponse(f"unknown fault; one of {sorted(FAULTS)}", status_code=400)
    _fault.update(name=name, times=times)
    return {"armed": name, "times": times}


@app.delete("/__faults")
def clear_fault():
    _fault.update(name=None, times=0)
    return {"armed": None}


def take_fault() -> str | None:
    """The armed fault, used up by one page."""
    name = _fault["name"]
    if name and _fault["times"] != 0:
        if _fault["times"] > 0:
            _fault["times"] -= 1
        return name
    return None


def page(body: str, title: str = "CU*Core Teller") -> HTMLResponse:
    # Deliberately dated markup: tables for layout, <font>, bgcolor, no semantics beyond what HTML gives for free
    return HTMLResponse(f"""<HTML><HEAD><TITLE>{title}</TITLE></HEAD>
<BODY BGCOLOR="#FFFFFF" TOPMARGIN=0 LEFTMARGIN=0><FONT FACE="Arial" SIZE=2>{body}</FONT></BODY></HTML>""")


def signed_in(request: Request) -> bool:
    return request.cookies.get("sid") in _sessions


@app.get("/", response_class=HTMLResponse)
def frameset():
    return HTMLResponse("""<HTML><HEAD><TITLE>CU*Core Teller</TITLE></HEAD>
<FRAMESET COLS="180,*" BORDER=1>
  <FRAME NAME="nav" SRC="/nav">
  <FRAME NAME="main" SRC="/main">
</FRAMESET></HTML>""")


@app.get("/nav", response_class=HTMLResponse)
def nav(request: Request):
    if not signed_in(request):
        return page('<TABLE WIDTH="100%" BGCOLOR="#003366"><TR><TD><FONT COLOR="#FFFFFF"><B>CU*Core</B></FONT></TD></TR></TABLE>')
    return page("""<TABLE WIDTH="100%" CELLPADDING=4 BGCOLOR="#003366"><TR><TD><FONT COLOR="#FFFFFF"><B>CU*Core</B></FONT></TD></TR></TABLE>
<TABLE CELLPADDING=3>
  <TR><TD><B>Teller Functions</B></TD></TR>
  <TR><TD><A HREF="/inquiry" TARGET="main">Member Inquiry</A></TD></TR>
  <TR><TD><A HREF="/signoff" TARGET="_top">Sign Off</A></TD></TR>
</TABLE>""")


@app.get("/main", response_class=HTMLResponse)
def main(request: Request):
    if signed_in(request):
        return page("<TABLE CELLPADDING=8><TR><TD>Select a teller function from the menu.</TD></TR></TABLE>")
    return page("""<FORM METHOD="POST" ACTION="/login" TARGET="_top">
<TABLE CELLPADDING=6 BORDER=0>
  <TR><TD COLSPAN=2><B>Operator Sign On</B></TD></TR>
  <TR><TD><LABEL FOR="u">User ID</LABEL></TD><TD><INPUT TYPE="text" NAME="user" ID="u" SIZE=12></TD></TR>
  <TR><TD><LABEL FOR="p">Password</LABEL></TD><TD><INPUT TYPE="password" NAME="password" ID="p" SIZE=12></TD></TR>
  <TR><TD></TD><TD><INPUT TYPE="submit" VALUE="Sign On"></TD></TR>
</TABLE></FORM>""")


@app.post("/login")
def login(user: str = Form(""), password: str = Form("")):
    if user != APP_USER or password != APP_PASSWORD:
        return page("""<TABLE CELLPADDING=8><TR><TD><FONT COLOR="#CC0000">Sign on failed.</FONT>
<A HREF="/" TARGET="_top">Try again</A></TD></TR></TABLE>""")
    sid = secrets.token_hex(16)
    _sessions.add(sid)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie("sid", sid, httponly=True)
    return response


@app.get("/signoff")
def signoff(request: Request):
    _sessions.discard(request.cookies.get("sid"))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie("sid")
    return response


@app.get("/inquiry", response_class=HTMLResponse)
def inquiry(request: Request):
    if not signed_in(request):
        return session_expired()
    # Two "Search" buttons on one page: only the fieldset tells them apart (scope, not index)
    return page("""<FORM METHOD="GET" ACTION="/find" NAME="quick"><FIELDSET><LEGEND>Quick Find</LEGEND>
<TABLE CELLPADDING=6 BORDER=0>
  <TR><TD>Last name</TD><TD><INPUT TYPE="text" NAME="last" SIZE=16></TD><TD><INPUT TYPE="submit" VALUE="Search"></TD></TR>
</TABLE></FIELDSET></FORM>
<FORM METHOD="GET" ACTION="/member" NAME="inq"><FIELDSET><LEGEND>Member Inquiry</LEGEND>
<TABLE CELLPADDING=6 BORDER=0>
  <TR><TD><LABEL FOR="m">Member #</LABEL></TD><TD><INPUT TYPE="text" NAME="id" ID="m" SIZE=8 MAXLENGTH=5></TD></TR>
  <TR><TD></TD><TD><INPUT TYPE="submit" VALUE="Search"></TD></TR>
</TABLE></FIELDSET></FORM>""")


@app.get("/find", response_class=HTMLResponse)
def find(request: Request, last: str = ""):
    if not signed_in(request):
        return session_expired()
    hits = [(n, m) for n, m in MEMBERS.items() if last and m["name"].lower().split()[-1].startswith(last.lower())]
    rows = "".join(f'<TR><TD>{n}</TD><TD>{m["name"]}</TD><TD><A HREF="/member?id={n}">Select</A></TD></TR>' for n, m in hits)
    return page(f"""<B>Search Results</B>
<TABLE CELLPADDING=4 BORDER=1 CELLSPACING=0><TR BGCOLOR="#DDDDDD"><TH>Member #</TH><TH>Name</TH><TH></TH></TR>{rows}</TABLE>
<BR><A HREF="/inquiry">New search</A>""")


@app.get("/member", response_class=HTMLResponse)
async def member(request: Request, id: str = "", ack: str = ""):
    if not signed_in(request):
        return session_expired()
    key = id.strip()
    shown = escape(key[:10])
    if key.startswith("0"):
        return page(f"""<TABLE CELLPADDING=8><TR><TD><FONT COLOR="#CC0000">Invalid member number "{shown}".</FONT></TD></TR>
<TR><TD><A HREF="/inquiry">New search</A></TD></TR></TABLE>""")
    found = MEMBERS.get(key)
    if not found:
        return page(f"""<TABLE CELLPADDING=8><TR><TD><FONT COLOR="#CC0000">No members found for "{shown}".</FONT></TD></TR>
<TR><TD><A HREF="/inquiry">New search</A></TD></TR></TABLE>""")
    if found.get("restricted"):
        return page("""<TABLE CELLPADDING=8><TR><TD><FONT COLOR="#CC0000">You are not authorized to view this member.</FONT>
</TD></TR></TABLE>""")

    fault = None if ack else take_fault()
    if fault == "server_error":
        return HTMLResponse("<HTML><BODY><H1>Internal Server Error</H1><P>An unexpected error occurred.</P></BODY></HTML>",
                            status_code=500)
    if fault == "session_timeout":
        _sessions.discard(request.cookies.get("sid"))
        return session_expired()
    if fault == "interstitial":
        return page(f"""<FORM METHOD="GET" ACTION="/member"><TABLE CELLPADDING=8>
<TR><TD><B>Notice</B></TD></TR>
<TR><TD>Branch hours change on the first of next month. Please acknowledge to continue.</TD></TR>
<TR><TD><INPUT TYPE="hidden" NAME="id" VALUE="{shown}"><INPUT TYPE="hidden" NAME="ack" VALUE="1">
<INPUT TYPE="submit" VALUE="Acknowledge"></TD></TR></TABLE></FORM>""")
    if fault == "slow":
        await asyncio.sleep(SLOW_SECONDS)
    if fault == "wrong_record":
        # A legacy bug: a stale cache serves the neighbouring member. Only the identity checkpoint catches it.
        keys = list(MEMBERS)
        key = keys[(keys.index(key) + 1) % len(keys)]
        found = MEMBERS[key]

    rows = "".join(f'<TR><TD>{t}</TD><TD>{n}</TD><TD ALIGN=RIGHT>${b}</TD><TD><A HREF="/account?no={n}">View</A></TD></TR>'
                   for t, n, b in found["accounts"])
    dialog = """<DIALOG OPEN><B>Compliance hold</B><P>This member is under review. Contact your supervisor before
continuing.</P><FORM METHOD="dialog"><INPUT TYPE="submit" VALUE="OK"></FORM></DIALOG>""" if fault == "unknown_dialog" else ""
    return page(f"""{dialog}<TABLE CELLPADDING=4 BORDER=0>
  <TR><TD COLSPAN=2><B>Member Detail</B></TD></TR>
  <TR><TD>Member #:</TD><TD>{key}</TD></TR>
  <TR><TD>Name:</TD><TD>{found["name"]}</TD></TR>
</TABLE>
<BR><B>Account Summary</B>
<TABLE CELLPADDING=4 BORDER=1 CELLSPACING=0>
  <TR BGCOLOR="#DDDDDD"><TH>Type</TH><TH>Account</TH><TH>Balance</TH><TH>Action</TH></TR>
  {rows}
</TABLE>
<BR><A HREF="/inquiry">New search</A>""")


@app.get("/account", response_class=HTMLResponse)
def account(request: Request, no: str = ""):
    if not signed_in(request):
        return session_expired()
    for m in MEMBERS.values():
        for t, n, b in m["accounts"]:
            if n == no:
                return page(f"""<TABLE CELLPADDING=4 BORDER=0>
  <TR><TD COLSPAN=2><B>Account Detail</B></TD></TR>
  <TR><TD>Type:</TD><TD>{t}</TD></TR>
  <TR><TD>Account:</TD><TD>{n}</TD></TR>
  <TR><TD>Balance:</TD><TD>${b}</TD></TR>
</TABLE><BR><A HREF="javascript:history.back()">Back</A>""")
    return page("""<TABLE CELLPADDING=8><TR><TD><FONT COLOR="#CC0000">No such account.</FONT></TD></TR></TABLE>""")


def session_expired() -> HTMLResponse:
    return page("""<TABLE CELLPADDING=8><TR><TD><H3>Session expired</H3>Please <A HREF="/" TARGET="_top">sign on</A> again.</TD></TR></TABLE>""")
