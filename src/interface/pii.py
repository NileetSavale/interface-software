"""PII detection and masking (docs/ARCHITECTURE.md §6, "the LLM never sees raw PII").

Two kinds of detector:
- patterns, for values that look like what they are wherever they appear (account numbers, money, SSNs, dates,
  emails, phones);
- labels, for values that only their context gives away: the cell to the right of "Name:", or a column headed
  "Name". A name has no shape a regex could find.

A Masker replaces each value with a placeholder that is stable for its lifetime (the same name is ⟨NAME_1⟩ every time
the model sees it), so the model can still tell records apart without knowing who they belong to. Identifiers long
enough to keep it meaningful also show last-4 (D16, the bank-statement convention); a 5-digit member number doesn't,
since four of its five digits would give it away. The placeholder → value table lives only in memory.

Free text (a note, a sentence in a cell) can mention a name no label points at. There, a third detector hides every
run of two or more capitalised words, or a title and a name ("Mr Lee"), unless the page itself uses that run as a
label: a control, heading or column name (D24). It fails closed: a phrase that isn't a name may be hidden, a two-word
name isn't let through. Its limit, stated in REPORT: a lone first name ("ask Carlos") or a name in lower case.
"""
import re
from dataclasses import dataclass, field

PATTERNS: list[tuple[str, str]] = [
    ("SSN", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("ACCT", r"\b\d{4}-\d{4,6}-\d{2,4}\b|\b\d{9,19}\b"),
    ("MONEY", r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d{1,3}(?:,\d{3})+\.\d{2}\b"),
    ("DATE", r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    ("EMAIL", r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    ("PHONE", r"\(\d{3}\)\s?\d{3}-\d{4}|\b\d{3}[-.]\d{3}[-.]\d{4}\b"),
]
LABELS: list[tuple[str, str]] = [
    ("ACCT", r"(?i)member\s*(#|no\b|number)|account|acct|routing|card"),
    ("SSN", r"(?i)\bssn\b|social security|\btin\b"),
    ("DATE", r"(?i)birth|\bdob\b"),
    ("ADDR", r"(?i)address|street|city|zip"),
    ("NAME", r"(?i)\bname\b|owner|beneficiary|joint"),
    ("MONEY", r"(?i)balance|amount|available|deposit"),
]
_COMPILED = [(kind, re.compile(p)) for kind, p in PATTERNS]
_PLACEHOLDER = re.compile(r"⟨[A-Z]+_\d+(?: \*{4}\d{4})?⟩|\{\{(?:inputs|secrets|outputs)\.\w+\}\}")
# One line of an aria snapshot: indent, role, and a quoted name (YAML-quoted when it holds a #)
_LINE = re.compile(r'^(?P<indent>\s*)- \'?(?P<role>[a-z]+) "(?P<name>(?:[^"\\]|\\.)*)"')
# Text without a name in quotes: `- text: Account Summary`, `- paragraph: Called from…`
_PLAIN = re.compile(r"^(?P<indent>\s*)- (?P<role>[a-z]+)(?: \[[^\]]*\])*: (?P<name>.+)$")
# Where prose lives, as opposed to the names of controls, headings and columns, which are the page's own vocabulary
PROSE_ROLES = {"cell", "gridcell", "text", "paragraph", "generic", "listitem", "note", "blockquote", "caption",
               "definition", "status", "alert", "log", "group"}
PROSE_MIN_WORDS = 5
_UP, _LOW = "A-ZÀ-ÖØ-Þ", "a-zß-öø-ÿ"  # Latin letters, accented included
# A capitalised word, a title with its dot (before plain words, so "Mr." isn't read as "Mr"), or an initial
_WORD = rf"(?:(?:Mr|Mrs|Ms|Mx|Dr)\.|(?:Mc|Mac|O['’])?[{_UP}][{_LOW}]+(?:-[{_UP}][{_LOW}]+)?|[{_UP}]\.)"
_PARTICLE = r"(?:de|da|del|della|di|do|dos|du|van|von|der|den|la|le|bin|ibn|al)"
_NAME_RUN = re.compile(rf"(?<![\w.'’-]){_WORD}(?:\s+(?:{_PARTICLE}\s+)*{_WORD})+(?![\w'’-])")


def _in(run: str, vocabulary: list[str]) -> bool:
    """Whether the page uses this run, as whole words, in one of its labels."""
    pattern = re.compile(rf"(?<!\w){re.escape(run.lower())}(?!\w)")
    return any(pattern.search(words) for words in vocabulary)


def label_kind(label: str) -> str | None:
    return next((kind for kind, pattern in LABELS if re.search(pattern, label)), None)


@dataclass
class Masker:
    values: dict[str, str] = field(default_factory=dict)   # value -> placeholder; memory only
    from_prose: set[str] = field(default_factory=set)        # values the free-text detector guessed at
    counts: dict[str, int] = field(default_factory=dict)

    def placeholder(self, kind: str, value: str) -> str:
        value = value.strip()
        if value not in self.values:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            digits = re.sub(r"\D", "", value)
            tail = f" ****{digits[-4:]}" if kind == "ACCT" and len(digits) >= 8 else ""
            self.values[value] = f"⟨{kind}_{self.counts[kind]}{tail}⟩"
        return self.values[value]

    def text(self, text: str, keep: set[str] = frozenset()) -> str:
        """Pattern detectors only: for free text, logs and the model's own words."""
        for value in sorted(self.values, key=len, reverse=True):  # values already known, wherever they reappear
            if value not in keep:
                text = text.replace(value, self.values[value])
        for kind, pattern in _COMPILED:
            text = pattern.sub(lambda m, k=kind: self.placeholder(k, m.group(0)), text)
        return text

    def snapshot(self, snapshot: str) -> str:
        """Label detectors first (they need the page's structure), then the patterns over everything."""
        lines = snapshot.splitlines()
        self._by_label(lines)
        self._by_column(lines)
        vocabulary = self._in_prose(lines)
        # A guess from an earlier note that this page uses as a label ("Holiday Club" as an option) was not a name
        keep = {v for v in self.from_prose if _in(v, vocabulary)}
        return self.text("\n".join(lines), keep)

    def _swap(self, lines: list[str], i: int, kind: str):
        if not _LINE.match(lines[i]):
            # A cell with no text of its own whose value is a control inside it (ParaBank: the account number is
            # the link in the cell): the first named line under it holds the value
            indent = len(lines[i]) - len(lines[i].lstrip())
            for j in range(i + 1, len(lines)):
                if len(lines[j]) - len(lines[j].lstrip()) <= indent:
                    return
                if _LINE.match(lines[j]):
                    i = j
                    break
            else:
                return
        m = _LINE.match(lines[i])
        name = m.group("name").replace('\\"', '"') if m else ""
        if not m or not name.strip() or _PLACEHOLDER.fullmatch(name.strip()) or name.rstrip().endswith(":"):
            return
        lines[i] = lines[i][:m.start("name")] + self.placeholder(kind, name) + lines[i][m.end("name"):]

    def _by_label(self, lines: list[str]):
        """`cell "Name:"` then a sibling `cell "Jane Doe"`: the value cell is masked by what its label says."""
        for i, line in enumerate(lines[:-1]):
            m, nxt = _LINE.match(line), _LINE.match(lines[i + 1])
            if m and nxt and m.group("name").rstrip().endswith(":") and nxt.group("indent") == m.group("indent"):
                if kind := label_kind(m.group("name")):
                    self._swap(lines, i + 1, kind)

    def _in_prose(self, lines: list[str]) -> list[str]:
        """Names in free text, which no label points at: every capitalised run that isn't the page's own vocabulary.
        Returns that vocabulary."""
        vocabulary, prose = [], []
        for i, line in enumerate(lines):
            m = _LINE.match(line) or _PLAIN.match(line)
            if not m or _PLACEHOLDER.fullmatch(m.group("name").strip()):
                continue
            # A paragraph is prose however short ("Welcome Avery Quill", found on ParaBank); a cell or loose text is
            # prose from five words, below which it's a label or a value the other detectors handle
            role = m.group("role")
            if role in PROSE_ROLES and (role == "paragraph" or len(m.group("name").split()) >= PROSE_MIN_WORDS):
                prose.append((i, m))
            elif m.group("role") not in PROSE_ROLES or m.group("role") == "text":
                vocabulary.append(m.group("name").lower())

        def hide(run: re.Match) -> str:
            rest = run.group(0).split(maxsplit=1)[1]
            if _in(run.group(0), vocabulary) or _in(rest, vocabulary):
                # "(Use) Member Inquiry" in a sentence is the link of that name, not a person; one word left over
                # (a sentence's first) is not a name either
                return run.group(0)
            self.from_prose.add(run.group(0))
            return self.placeholder("NAME", run.group(0))

        for i, m in prose:
            name = _NAME_RUN.sub(hide, m.group("name"))
            lines[i] = lines[i][:m.start("name")] + name + lines[i][m.end("name"):]
        return vocabulary

    def _by_column(self, lines: list[str]):
        """A table whose header row has a "Name" or "Account" column: every cell in that column is masked."""
        header: list[str] | None = None
        for i, line in enumerate(lines):
            if re.match(r"\s*- table\b", line):
                header = None
            if not re.match(r"\s*- row\b", line):
                continue
            indent = len(line) - len(line.lstrip())
            cells = []
            for j in range(i + 1, len(lines)):
                child_indent = len(lines[j]) - len(lines[j].lstrip())
                if child_indent <= indent:
                    break
                if child_indent == indent + 2:
                    cells.append(j)
            roles = [(_LINE.match(lines[j]) or re.match(r"\s*- '?(?P<role>[a-z]+)", lines[j])).group("role") for j in cells]
            if roles and all(r == "columnheader" for r in roles):
                header = [(_LINE.match(lines[j]).group("name") if _LINE.match(lines[j]) else "") for j in cells]
                continue
            for col, j in enumerate(cells):
                if header and col < len(header) and (kind := label_kind(header[col])):
                    self._swap(lines, j, kind)
