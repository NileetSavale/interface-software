"""The policy gate every automated action passes through, in discovery and replay (docs/ARCHITECTURE.md §6).

The model only asks; this code decides. Irreversible actions are blocked outright (D1), and when the classifier
isn't sure, it classes up.
"""
import fnmatch
import re
from typing import Literal
from urllib.parse import urlsplit

from interfaze.artifact.schema import Policy

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


def classify(action: str, role: str, name: str) -> Risk:
    if action == "click" and role in COMMIT_ROLES:
        if IRREVERSIBLE.search(name):
            return "irreversible"
        if REVERSIBLE.search(name):
            return "reversible"
    return "read"


def check_action(policy: Policy, action: str, role: str, name: str) -> Risk:
    risk = classify(action, role, name)
    if risk == "irreversible":
        raise PolicyViolation(f"blocked by policy: irreversible action ({role} {name!r}). Automation can reach a "
                              f"commit point but never press it; stop here.")
    if risk == "reversible" and policy.max_risk == "read":
        raise PolicyViolation(f"blocked by policy: {role} {name!r} would change data, and this run is read-only")
    return risk


def check_url(policy: Policy, url: str) -> None:
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in policy.allowed_origins:
        raise PolicyViolation(f"blocked by policy: {origin} isn't an allowed origin")
    if not any(fnmatch.fnmatch(parts.path or "/", pattern) for pattern in policy.allowed_paths):
        raise PolicyViolation(f"blocked by policy: path {parts.path} isn't allowed")
