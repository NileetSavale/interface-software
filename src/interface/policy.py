"""The policy gate every automated action passes through, in discovery and replay (docs/ARCHITECTURE.md §6).

The model only asks; this code decides. Irreversible actions are blocked outright (D1), and when the classifier
isn't sure, it classes up.
"""
import fnmatch
import re
from typing import Literal
from urllib.parse import urlsplit

from interface.artifact.schema import Policy

Risk = Literal["read", "reversible", "irreversible"]

# Buttons that commit something. Matched on the accessible name of what's being clicked.
IRREVERSIBLE = re.compile(
    r"\b(confirm|submit|post|transfer|send|pay|approve|authori[sz]e|delete|remove|close account|withdraw|disburse|"
    r"open account|finali[sz]e|commit|execute)\b", re.IGNORECASE)
# Reversible changes: saving or continuing a form that can still be backed out of before a commit
REVERSIBLE = re.compile(r"\b(save|continue|next|apply|update|add|edit|create)\b", re.IGNORECASE)
COMMIT_ROLES = {"button", "link", "menuitem"}


class PolicyViolation(Exception):
    pass


def classify(action: str, role: str, name: str, paging: bool = False) -> Risk:
    """paging: a declared turn to the next page of results (D28). A link named "Next" there is a GET that changes
    nothing, so it's read; a button named "Next" (a wizard's) stays reversible, and an irreversible name is always
    irreversible. A page turn is classified like the click it is."""
    if action in ("click", "pageUntil") and role in COMMIT_ROLES:
        if IRREVERSIBLE.search(name):
            return "irreversible"
        if REVERSIBLE.search(name) and not (paging and role == "link"):
            return "reversible"
    return "read"


def check_action(policy: Policy, action: str, role: str, name: str, paging: bool = False) -> Risk:
    risk = classify(action, role, name, paging)
    if risk == "irreversible":
        raise PolicyViolation(f"blocked by policy: irreversible action ({role} {name!r}). Automation can reach a "
                              f"commit point but never press it; stop here.")
    if risk == "reversible" and policy.max_risk == "read":
        raise PolicyViolation(f"blocked by policy: {role} {name!r} would change data, and this run is read-only")
    return risk


def check_url(policy: Policy, url: str) -> None:
    if reason := refused(policy, url, document=True):
        raise PolicyViolation(reason)


def refused(policy: Policy, url: str, document: bool) -> str | None:
    """Why the browser may not request this URL, or None. Every request must stay on an allowed origin; a document
    load or form post must also be on an allowed path and not on a commit path. Enforced in the network layer
    (WebSurface), so it holds for a click on anything: a link to another site, or a commit button labelled "OK"."""
    parts = urlsplit(url)
    if parts.scheme in ("data", "blob") and not document:
        return None  # content the page made itself (an inline image): it never leaves the browser
    if parts.scheme not in ("http", "https"):
        # file: would read the replaying machine's own disk; data:/blob: as a page would be content no app served
        return f"blocked by policy: {parts.scheme or 'scheme-less'} URLs aren't allowed"
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in policy.allowed_origins:
        return f"blocked by policy: {origin} isn't an allowed origin"
    if not document:
        return None
    path = parts.path or "/"
    if not any(fnmatch.fnmatch(path, pattern) for pattern in policy.allowed_paths):
        return f"blocked by policy: path {path} isn't allowed"
    if any(fnmatch.fnmatch(path, pattern) for pattern in policy.commit_paths):
        return (f"blocked by policy: {path} commits a change (irreversible). Automation can reach a commit point "
                f"but never press it; stop here.")
    return None
