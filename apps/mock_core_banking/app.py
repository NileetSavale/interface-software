"""A stand-in for a legacy core-banking back office: server-rendered, a frameset, layout tables, no ids or test ids.

Fake data only. Run with:  uv run uvicorn apps.mock_core_banking.app:app --port 4100

Pass 1 has the lookup flow only. Faults, the second tenant variant and the sub-account flow come in Pass 2.
"""
import os
import secrets

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

APP_USER = os.environ.get("APP_USER", "teller01")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "demo-only-password")

MEMBERS = {
    "12345": {"name": "Jane Doe", "accounts": [("Savings", "0001-12345-00", "4,210.55"), ("Checking", "0001-12345-10", "812.07")]},
    "23456": {"name": "Rafael Ortiz", "accounts": [("Savings", "0001-23456-00", "15,002.10")]},
    "34567": {"name": "Mei Chen", "accounts": [("Checking", "0001-34567-10", "96.40"), ("Savings", "0001-34567-00", "2,500.00")]},
}

app = FastAPI()
_sessions: set[str] = set()


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
    return page("""<FORM METHOD="GET" ACTION="/member" NAME="inq"><FIELDSET><LEGEND>Member Inquiry</LEGEND>
<TABLE CELLPADDING=6 BORDER=0>
  <TR><TD><LABEL FOR="m">Member #</LABEL></TD><TD><INPUT TYPE="text" NAME="id" ID="m" SIZE=8 MAXLENGTH=5></TD></TR>
  <TR><TD></TD><TD><INPUT TYPE="submit" VALUE="Search"></TD></TR>
</TABLE></FIELDSET></FORM>""")


@app.get("/member", response_class=HTMLResponse)
def member(request: Request, id: str = ""):
    if not signed_in(request):
        return session_expired()
    found = MEMBERS.get(id.strip())
    if not found:
        return page(f"""<TABLE CELLPADDING=8><TR><TD><FONT COLOR="#CC0000">No members found for "{id[:10]}".</FONT></TD></TR>
<TR><TD><A HREF="/inquiry">New search</A></TD></TR></TABLE>""")
    rows = "".join(f"<TR><TD>{t}</TD><TD>{n}</TD><TD ALIGN=RIGHT>${b}</TD></TR>" for t, n, b in found["accounts"])
    return page(f"""<TABLE CELLPADDING=4 BORDER=0>
  <TR><TD COLSPAN=2><B>Member Detail</B></TD></TR>
  <TR><TD>Member #:</TD><TD>{id.strip()}</TD></TR>
  <TR><TD>Name:</TD><TD>{found["name"]}</TD></TR>
</TABLE>
<BR><B>Account Summary</B>
<TABLE CELLPADDING=4 BORDER=1 CELLSPACING=0>
  <TR BGCOLOR="#DDDDDD"><TH>Type</TH><TH>Account</TH><TH>Balance</TH></TR>
  {rows}
</TABLE>
<BR><A HREF="/inquiry">New search</A>""")


def session_expired() -> HTMLResponse:
    return page("""<TABLE CELLPADDING=8><TR><TD><H3>Session expired</H3>Please <A HREF="/" TARGET="_top">sign on</A> again.</TD></TR></TABLE>""")
