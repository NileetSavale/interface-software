"""The page map: what one bank's app looks like, learned from verified runs (docs/ARCHITECTURE.md §7a, D3–D7).

    nodes  screens, by a fingerprint of their structure with every data value taken out (two members' detail pages
           are one node), plus the static labels on them
    edges  actions: (screen, action, surface-neutral target, value template) → the screen it led to, with every
           observation kept: which kind of run saw it, when, and whether it worked

A library, not a service: one JSON file per bank, never shared across banks (D5), and holding structure only. Labels
pass through the same PII masker the model sees through, and a value is kept only if it's a template
({{inputs.x}}, {{secrets.X}}), so no name, number or balance can end up in it.

Write rules (D7): only runs whose success checkpoint passed add edges; a failed run can only lower the weight of
edges it used; screens that matched an error outcome are never learned. Weights are by source (an approved replay
counts for more than an unreviewed discovery, a person's one-off step for least) and decay with age.

Who reads it: discovery, as known actions it can run several of in one turn (follow_known), and replay, as a bounded
fallback when no recorded strategy finds a control. Replay pins the version it loaded, so the same artifact, inputs
and map version give the same steps.
"""
import copy
import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from interface.evidence import structure_only
from interface.pii import Masker

SOURCE_WEIGHT = {"replay": 1.0, "reviewed_discovery": 0.7, "discovery": 0.5, "human": 0.2}
HALF_LIFE_S = 30 * 24 * 3600
# The least weight an edge needs before replay may follow it: one fresh verified discovery (0.5) is enough, a
# person's one-off step (0.2) only once it has been seen working twice
TRUSTED = 0.4
_REF = re.compile(r"\s*\[(?:ref|cursor|active)=?[^\]]*\]")
_FIELD_VALUE = re.compile(r'^(\s*- \'?(?:textbox|combobox|searchbox|spinbutton)[^\n]*?)(?::\s.*)?$')
_NAMED = re.compile(r'^\s*- \'?(\w+) "((?:[^"\\]|\\.)*)"')
_TEXT = re.compile(r"^\s*- text: \"?(.+?)\"?$")
_TEMPLATE = re.compile(r"^\{\{\s*(inputs|secrets)\.\w+\s*\}\}$")


@dataclass
class Screen:
    id: str
    labels: list[str]


def screen_of(snapshot: str) -> Screen:
    """A screen's identity and labels, from an accessibility snapshot, with no data in either."""
    lines = []
    for line in _REF.sub("", snapshot).splitlines():
        lines.append(_FIELD_VALUE.sub(r"\1", line))  # what's typed in a field isn't part of the screen
    shape = structure_only("\n".join(lines))
    masked = Masker().snapshot("\n".join(lines))
    labels = set()
    for line in masked.splitlines():
        m = _NAMED.match(line) or _TEXT.match(line)
        text = (m.group(2) if m and m.re is _NAMED else m.group(1) if m else "").replace('\\"', '"').strip()
        if text and len(text) <= 60 and not re.search(r"\d|⟨|\{\{", text):
            labels.add(text)
    return Screen(id=hashlib.sha256(shape.encode()).hexdigest()[:12], labels=sorted(labels))


def edge_key(frm: str, action: str, target: dict, value: str | None) -> str:
    return hashlib.sha256(json.dumps([frm, action, target, value], sort_keys=True).encode()).hexdigest()[:12]


def target_name(target: dict) -> tuple[str, str]:
    """(role, name) for reading an edge aloud and for the policy classifier: the first named strategy."""
    for s in target.get("strategies", []):
        if s["kind"] == "role":
            return s["role"], s["name"] if isinstance(s["name"], str) else ""
    for s in target.get("strategies", []):
        for key in ("label", "anchorText", "text"):
            if isinstance(s.get(key), str):
                return s.get("role") or "control", s[key]
    return "control", ""


class PageMap:
    def __init__(self, path: Path | None = None, bank: str = ""):
        self.path = path
        self.data = {"bank": bank, "nodes": {}, "edges": {}}
        if path and path.exists():
            self.data = json.loads(path.read_text())

    # ── Reading ────────────────────────────────────────────────────────────────────────────────────────────────

    def version(self) -> str:
        """A hash of the content: what a replay records, so it can be rerun against exactly this map."""
        return hashlib.sha256(json.dumps(self.data, sort_keys=True).encode()).hexdigest()[:12]

    def frozen(self) -> "PageMap":
        pinned = PageMap(bank=self.data["bank"])
        pinned.data = copy.deepcopy(self.data)
        return pinned

    def weight(self, edge: dict, now: float | None = None) -> float:
        now = now or time.time()
        decay = lambda at: 0.5 ** ((now - at) / HALF_LIFE_S)
        good = sum(SOURCE_WEIGHT[o["source"]] * decay(o["at"]) for o in edge["seen"] if o["ok"])
        bad = sum(decay(o["at"]) for o in edge["seen"] if not o["ok"])
        return good / (1 + bad)

    def edges_from(self, screen: str, min_weight: float = 0.0, risks: tuple[str, ...] = ("read", "reversible")) -> list[dict]:
        out = [e for e in self.data["edges"].values() if e["from"] == screen and e["risk"] in risks
               and self.weight(e) >= min_weight]
        return sorted(out, key=lambda e: -self.weight(e))

    def labels(self, screen: str) -> set[str]:
        return set(self.data["nodes"].get(screen, {}).get("labels", []))

    def paths(self, screen: str, max_edges: int = 2, **kw) -> list[list[dict]]:
        """Every route of up to max_edges edges that leaves this screen, best first. Typing edges stay on the screen
        they started on, so a route is a sequence, not just a walk between screens."""
        routes, queue = [], deque([(screen, [])])
        while queue:
            at, route = queue.popleft()
            for e in self.edges_from(at, **kw):
                if e in route:
                    continue
                longer = [*route, e]
                routes.append(longer)
                if len(longer) < max_edges:
                    queue.append((e["to"], longer))
        return routes

    def describe(self, screen: str, depth: int = 6, limit: int = 14) -> tuple[str, dict[str, dict]]:
        """Known actions from this screen and onwards, numbered k1, k2… for the model, in the order they were done."""
        lines, ids = [], {}

        def walk(at: str, indent: int, visited: set[str]):
            # Each screen once: its own actions in the order they were done, then onwards through the ones that leave
            for e in sorted(self.edges_from(at, min_weight=0.01), key=lambda e: e.get("order", 0)):
                if len(ids) >= limit:
                    return
                kid = f"k{len(ids) + 1}"
                ids[kid] = e
                role, name = target_name(e["target"])
                what = f"{e['action']} {e['value']} into {role} '{name}'" if e["value"] else f"{e['action']} {role} '{name}'"
                landed = "" if e["to"] == at else f"  → a screen showing {', '.join(repr(l) for l in self._shown(e['to']))}"
                lines.append(f"{'  ' * indent}{kid}: {what} ({e['intent']}){landed}")
                if e["to"] != at and e["to"] not in visited and indent + 1 < depth:
                    visited.add(e["to"])
                    walk(e["to"], indent + 1, visited)

        walk(screen, 0, {screen})
        return "\n".join(lines), ids

    def _shown(self, screen: str, n: int = 3) -> list[str]:
        labels = self.data["nodes"].get(screen, {}).get("labels", [])
        return sorted(labels, key=len, reverse=True)[:n]

    # ── Writing ────────────────────────────────────────────────────────────────────────────────────────────────

    def learn(self, transitions: list[dict], source: str, verified: bool, bad_screens: set[str] = frozenset()):
        """transitions: dicts with from, from_labels, action, target, value, intent, risk, to, to_labels.
        Verified: each becomes (or reinforces) an edge. Not verified: only edges it used are marked as failing."""
        now = time.time()
        for n, t in enumerate(transitions):
            value = t["value"] if t["value"] and _TEMPLATE.match(t["value"]) else None
            if t["value"] and value is None:
                continue  # a literal typed value could be data: never kept
            if t.get("to") in bad_screens or t["risk"] == "irreversible" or (verified and "to" not in t):
                continue
            key = edge_key(t["from"], t["action"], t["target"], value)
            edge = self.data["edges"].get(key)
            if edge is None:
                if not verified:
                    continue  # an unverified run can't add knowledge
                edge = self.data["edges"][key] = {
                    "key": key, "from": t["from"], "to": t["to"], "action": t["action"], "target": t["target"],
                    "value": value, "intent": re.sub(r"\d", "#", t["intent"])[:80], "risk": t["risk"],
                    "order": n, "seen": []}
            edge["seen"] = [*edge["seen"][-19:], {"source": source, "at": now, "ok": verified}]
            if verified:
                for screen, labels in ((t["from"], t["from_labels"]), (t["to"], t["to_labels"])):
                    node = self.data["nodes"].setdefault(screen, {"labels": [], "seen": 0})
                    node["labels"] = sorted(set(node["labels"]) | set(labels))[:200]
                    node["seen"] += 1

    def save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n")
