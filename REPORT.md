# interface — design write-up

Claude discovers a task once against a live legacy UI, and the run becomes a typed, versioned capability. Once a
person approves it, it replays without the model: same inputs, same steps, same typed result, in about 4 s for $0.
Numbers come from [`evals/report.md`](evals/report.md) (nine suites, each stamped with its commit, N stated). The full design is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the 32 decisions, including where I overrode the AI I built this
with, are in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Architecture

**One process, no infrastructure; the seams are interfaces** (Python 3.13, Playwright, Pydantic, FastAPI).

- **Discovery**: Claude Sonnet 5.5 reads an accessibility-tree snapshot with refs and acts by tool call. When stuck,
  Opus 5.5 takes over the same conversation, then a person. The model only asks; code validates and policy-checks
  every call.
- **Recorder**: turns each live action into a surface-neutral target, described from the live page.
- **Artifact**: the contract, with an exported JSON Schema. **Replay** runs an approved one with no model.
- **To a calling agent**, each approved artifact is an MCP tool (`interface mcp`, one server per bank, D30).
- **Shared**: the surface, the policy gate, the PII masker, a control token for handoff, a redacted evidence log,
  and each bank's **page map** of screens and actions learned from verified runs (D3/D4).

**Trade-offs**
- **The accessibility tree, not screenshots and coordinates**, as in Vercel Labs' agent-desktop (v1.0.2, 30 Oct
  2026): exact, cheap, layout-proof. An app with no tree is invisible, so bbox strategies stay in the schema and D2
  (a screenshot on a screen's first visit) is designed.
- **Model choice.** Discovery's cost is spread over every free replay, so first-try success beats token price
  (D12). Over 20 real runs: median $0.017 (lookup) and $0.029 (sub-account), probes included, all recorded and
  replayed on another member, no escalation.
- **History** is append-only with old snapshots cleared server-side: Claude 5.5 binds its thinking to the exact
  conversation (D17).
- **Reused from Loop Runner**, my agent behind *When Tests Lie* (submitted to AGENT 2027 @ ICSE): the tool gate,
  stuck detection, model cascades, redaction and the eval method.

## Artifact schema

`src/interface/artifact/schema.py`; real artifacts in `artifacts/`. A calling agent reads it as a tool signature, a
reviewer as a spec.

- **Contract**: `capability` (id, semver), `app` (vendor product, version range), typed **`inputs`** (pattern, enum,
  `sensitive`, `identifies: member`), typed **`outputs`**, named **`outcomes`** (business, recoverable, escalate,
  fatal, each with a detector), and `status` (draft or approved, with provenance).
- **Steps**: intent, action, target, a value template (`{{inputs.memberId}}`, `{{secrets.APP_USER}}`, never a
  literal), a risk class, condition waits and a postcondition. Sign-on is a **subflow**, reused for recovery.
- **Targets**: a frame path, an optional region, and **ranked strategies**: role + name › label › anchor text,
  table cell or text › `onlyInRegion` ("the only button in the Member Inquiry fieldset") › structural path, bbox.
  Each is checked unique when recorded; duplicates are told apart by region, never index; digits are data, never
  anchors.
- **`success.checkpoint`**: a visible landmark, every output read, and a mandatory **identity check** that this is
  the record asked for (D16).
- **`overrides`** per tenant by step id. **Validation rejects** irreversible steps, unknown ids, undeclared
  templates, ambiguous extractors, and restart recovery on a flow that changes data.

## Determinism & error handling

- Waits are on conditions, never sleeps. Strategies go in rank order; several matches fail as ambiguous, never
  pick one. Inputs are validated before the UI is touched.
- **Measured:** 80/80 stability replays (normal and slow app) read identical values; across 120 shuffled layouts (four flows),
  **no step ever found the wrong control.**

**Five results:** `success` with outputs; `business_outcome` (an answer, not a crash); `invalid_input`;
`needs_human` with a run handle; `failed` with the step, expected vs observed, and masked evidence.

**Runtime conditions are declared outcomes that race every wait**, and are re-checked where a control resolves
(the fault matrix caught a non-modal dialog being clicked past without that).

| Condition | Handling |
|---|---|
| Not found, malformed, restricted, deposit below minimum | `business_outcome` |
| Session expired | Re-sign-on and restart if read-only; escalate on a flow that changes data |
| Slow page | Absorbed by the waits |
| Unknown dialog or notice | A person if attached, else `failed` |
| App error | Fatal |
| A different member shown | `WRONG_RECORD` before anything is read; structure-only evidence; circuit breaker after two in 24 h (D19) |

**Flow-specific outcomes aren't hand-written** (D18, D22). The goal lists probe inputs and their expected codes
(`99999` → `MEMBER_NOT_FOUND`); each is replayed and the model names the screen. An outcome is kept only with fixed
wording, the expected code, the probe now ending in it, and the happy path still succeeding.

**Several matches and pages.** A row no label names is picked by the cell showing an input ("Select where Date of
Birth = `{{inputs.dob}}`"); two matches fail `AMBIGUOUS_TARGET`, never the first. A row pages away is reached by
`pageUntil`, the only loop an artifact can hold: Next until that row is on screen, at most `maxPages` times (D28).

**Fault matrix: 97/97 across three banks**, 0 accounts opened. A fallback hit succeeds and is reported as drift.

**When no strategy survives, the bank's page map** may stand in: at most two trusted edges from this bank's
verified runs, `read` only when unattended, ending in the step's own action with the next control then present,
from a **frozen, recorded version**, reported as `MAP_FALLBACK` and proposed as an override. It may also confirm a
postcondition, but only exactly: the screen this action is known to lead to here (D23).

## Heterogeneity & multi-tenant

- **Surface seam.** Artifacts never contain selectors; a `Surface` (`observe`, `resolve`, `act`, `wait_any`)
  resolves targets. Legacy web is built (framesets, layout tables, unlabelled fields). A desktop app gets a
  `DesktopSurface` over UIA, AX or AT-SPI, as agent-desktop does; artifact, replay and results don't change.
- **A green screen, built** (D31): `TerminalSurface` reads a 24x80 VT100 host through an emulator and builds the same
  snapshot from the characters, so discovery, the masker, the schema and replay are shared. Claude recorded it 5/5
  and each recording replayed on another member; the fault matrix holds 31/31 across two banks, where bank B's
  renamed controls are found by their place on the grid; a commit key on the review screen is never sent (suite 8).
- **Pixels only, built** (D32): `VisionSurface` reads an app that exposes only a picture by OCR, into the same
  snapshot, and acts by mouse and keyboard, on a canvas or a native window. A value is read twice and must agree, or
  the step fails `UNREADABLE`, never a guess: 500/500 values right with 0 silent misreads; 31/31 faults across two
  banks; Claude recorded it 5/5 reading OCR text, never the picture (suite 9). Building it found a masking leak the
  other surfaces couldn't have, now closed and tested.
- **Tenants.** Artifacts are keyed by vendor product and version range, not bank. A bank specialises one with an
  **override** addressed by **step id**, never array index (indexes shift silently), under a semver `appliesTo`;
  anything that doesn't apply is refused at load.
- **Bank B** says "Find Member" and has no `<label>` on Member #. Bank A's artifact **succeeds** there via fallbacks,
  reports drift and **writes a pending override**; once approved, B runs clean and A is untouched.
- **Bank C** also renames "Member Inquiry", which no strategy survives. Without a map both flows stop at sign-on.
  With C's map, learned from **one** verified lookup, the whole fault matrix holds on C, including the sub-account
  flow the map never saw. Maps are per bank, never shared (D5): learned without review, unlike curated artifacts.
- **On an app we didn't write.** Two capabilities discovered on ParaBank, Parasoft's public demo bank, replay with no
  model on other accounts and fail cleanly where the app has no answer
  ([`evidence/parabank/`](evidence/parabank/README.md)). It found what the mock hadn't: captions over fields, four
  identical buttons, an onload focus move that misplaced typed text, results lists (D27), and values filled in after
  load, which raised a false `WRONG_RECORD` in 2 of 20 replays. Each is fixed and tested, and its stability is now
  a suite of its own: **40/40** replays on ParaBank, one distinct output per account.
- **Discovery with a map** cut median turns 4 → 3 (lookup) and 8 → 5 (sub-account), Claude running 6 known actions
  per run, and median cost $0.017 → $0.011 and $0.029 → $0.023. In an earlier run the sub-account flow got slightly
  dearer with the map, as the known-actions list adds tokens to every turn. N=5 per arm: a direction, not a rate.
- **At scale**, `interface drift` reads the run logs and says, per bank and step, approve the override or re-record,
  only once 3 runs in a row agree (evidence 25, D29). Replays share no state:
  72/72 concurrent lookups across two banks (each its own server) returned their own member's answer, and 8 at once
  ran 4.8× the throughput of one on a laptop (suite 7). A deployment puts the same unit behind a per-bank queue.

## Escalation & handoff

- **Stuck in discovery:** the same action on an unchanged screen → one nudge → Opus → a person. **In replay:** an
  unknown screen, unresolvable control, failed postcondition or `escalate` outcome; unattended, it fails cleanly
  (`ESCALATION_UNATTENDED`).
- **Control:** `AUTOMATION → PAUSED_FOR_HUMAN → HUMAN → RESUMING → AUTOMATION` (`ABORTED` from any). A token with a
  holder, epoch and expiry: the epoch rises on every transfer and is checked just before input is dispatched, so a
  click decided before a handoff can't land after it (D9); a silent operator loses it on expiry.
- **Handoff:** the intervention carries capability, step, reason, a redacted snapshot and a screenshot. The operator
  page, usable only with the intervention's own key from the printed link (D25), offers Take control, Hand back and
  Abort; the person works in **the automation's own browser**, recorded by
  injected listeners. On hand-back, automation re-observes and decides: resolved, already done, retry, skip, or
  pause again. In discovery, a person's actions become steps flagged for review.
- **The caller** gets `needs_human` and a run handle at its deadline while the run continues; the late result is
  kept 15 minutes (D11).
- **Mocked:** the console is a minimal local page (production: a CDP screencast through the same token).
  [`evidence/11-handoff-unknown-notice/`](evidence/11-handoff-unknown-notice/) has the log and a masked recording.

## Safety

- **The allowlist is enforced below the page.** Allowed origins and paths, a `maxRisk`, and declared **commit
  paths** gate every browser request: off-origin requests and commit posts are aborted whatever the button says.
  Until this existed, the safety eval got a commit button labelled "OK" and an off-site link through (D20). Only
  `http(s)` may load a page: an outside review of the repo found `file://` slipping past, so a tampered artifact
  could have read the replaying machine's disk. Fixed, tested, and now a safety-eval case.
- **Irreversible actions are never executed by automation**: discovery refuses them, the schema rejects them, replay
  re-checks the live control. Capabilities stop at the commit point; every suite checks the opened-accounts counter
  stays 0. Committing is for the agent product and a person (D1).
- **Data.** The model sees `{{placeholders}}` for inputs and secrets and stable `⟨NAME_1⟩` tokens for names,
  accounts, amounts, SSNs, phones and emails, and can't type a value it read off the page. **Names in free text**
  ("spoke with her son Carlos Mendes") are caught by a deterministic, fail-closed rule: capitalised runs in prose
  that the page doesn't use as labels (D24); a model-based NER can miss, and would tie replay to a model version.
  Credentials are typed by code on the sign-on page only, and each bank's are its own (`APP_USER__B`, no fallback;
  a missing one fails before anything runs, D26), read through a mapping a vault can sit behind. Everything on disk is redacted at
  the write boundary; screenshots are masked in-page and fail closed.
- **Measured:** 18/18 safety cases without the LLM, and 12/12 with Claude on hostile pages (injection, a fake
  password form, an off-site link, an "OK" commit), zero harm.
- **Limits:** the risk classifier is name-based and commit paths are declared per app; a lone first name or a
  lower-case name in free text still reaches the model; masking depends on accessibility-tree coverage.

## Cuts

- **Approval-gated commits** through the handoff channel: designed, deliberately not built.
- **Desktop surface, TN3270, real co-browsing console:** designed at their seams. No attended handoff on a terminal.
- **Screenshots for the model** (D2): the tree sufficed in 20/20 discovery runs.
- **RAG / vector database: deliberately not used.** Every lookup is an exact key or a short list.
- **Next:** lone first names in free text; per-tenant base URLs as config; sharing learned outcomes across
  capabilities; racing the "already signed in?" check (about 2 s of each ~4 s replay).
- **Time:** built in priority order inside a 5-day time-box (D15); commits span 14–16 Nov 2026.
