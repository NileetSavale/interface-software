# interfaze — design write-up

Claude discovers a task once against a live legacy UI, and the run becomes a typed, versioned capability. Once a
person approves it, it replays without the model: the same inputs give the same steps and the same typed result,
in about 4 s for $0. The numbers here come from [`evals/report.md`](evals/report.md) (five suites, a clean commit, N
stated). The full design is [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). The 22 decisions, including where I
overrode the AI I built this with, are in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Architecture

**One process, no infrastructure; the seams are interfaces.** It's built on Python 3.13, Playwright, Pydantic and
FastAPI.

- **Discovery**: an observe → decide → act loop. Claude Sonnet 5.5 reads an accessibility-tree snapshot with refs
  and acts by tool call. When it's stuck, Opus 5.5 takes over the same conversation, then a person. The model only
  asks; code validates and policy-checks every call, and errors go back to the model.
- **Recorder**: describes each live action as a surface-neutral target, from the live page, never from the
  transcript.
- **Artifact**: the contract, with an exported JSON Schema.
- **Replay**: runs an approved artifact with no model.
- **Shared by both**: the surface, the policy gate, the PII masker, a control token for handoff, and a redacted
  evidence log.

**Decisions and trade-offs**
- **The accessibility tree, not screenshots and coordinates.** This is the perception model of Vercel Labs'
  agent-desktop (v1.0.2, 30 Oct 2026): exact, cheap, and robust to layout. The cost: an app with no accessibility
  tree is invisible. That's why bbox strategies stay in the schema, and why D2 (a screenshot to the model on a
  screen's first visit) is designed, though not yet built.
- **Model choice.** Discovery's cost is spread over every free replay, so first-try success matters more than token
  price (D12). Over 10 real runs: median $0.02 for lookup and $0.05 for the sub-account flow (probes included), with
  no escalation needed.
- **History.** It's append-only, with old snapshots cleared server-side. Claude 5.5 binds its thinking to the exact
  conversation, so editing earlier turns would break it (D17).
- **A real Claude run → artifact → replay first** (D10), then each part deepened.
- **Reused from my agent Loop Runner** (the system behind *When Tests Lie*, submitted to AGENT 2027 @ ICSE): the tool
  gate, stuck detection, model cascades, redaction, and the eval method.

## Artifact schema

`src/interfaze/artifact/schema.py`; the real artifacts are in `artifacts/`.

- **The contract**:
  - `capability`: id, semver, title, description;
  - `app`: vendor product, version range, entry path;
  - typed **`inputs`**: pattern, enum, `sensitive`, and `identifies: member`;
  - typed **`outputs`**: string, money, integer or date;
  - named **`outcomes`**, each classified as business, recoverable, escalate or fatal, with a detector;
  - `status`: draft or approved, with provenance.

  A calling agent can read it as a tool signature; a reviewer can read it as a spec.
- **Steps** carry an intent, an action, a target, and a value template (`{{inputs.memberId}}`,
  `{{secrets.APP_USER}}`, never a literal). They also carry a risk class, condition waits, and a postcondition.
  Sign-on is a **subflow**, run as a precondition and reused to recover from an expired session.
- **Targets** are surface-neutral: a frame path, an optional region scope, and **ranked strategies**.

  | Rank | Strategy |
  |---|---|
  | 1 | role + name |
  | 2 | label |
  | 3 | anchor text ("right of `Name:`"), table cell ("Balance where Type = Savings"), or visible text |
  | 4 | `onlyInRegion` ("the only button in the Member Inquiry fieldset") |
  | 5 | structural path, bbox |

  - Each strategy is checked to be unique on the live page when recorded.
  - Duplicates are told apart by a labelled region, never by index.
  - Anything with a digit is data, and never an anchor.
- **`success.checkpoint`**: a heading or label is visible, every output was read, and an **identity check** confirms
  this is the record asked for. The identity check is mandatory for any input marked `identifies` (D16).
- **`overrides`** per tenant, by step id (next section).
- **Validation** rejects:
  - irreversible steps;
  - unknown step ids;
  - undeclared templates;
  - an output with no extractor, or two;
  - a restart recovery on a capability that changes data.

## Determinism & error handling

**Determinism**
- Every wait is on a condition, never a sleep.
- Strategies are tried in rank order. Several matches count as ambiguous and the run fails; it never picks one.
- Inputs are validated before the UI is touched.
- Measured: in 80/80 stability replays (normal and slow app), every run read the same values. Across 60 shuffled
  layouts (frames, fieldsets, rows, columns, nesting), **no step ever found the wrong control.**

**Five result types**
- `success` with outputs.
- `business_outcome`: an answer, not a crash.
- `invalid_input`.
- `needs_human` with a run handle.
- `failed`: the step, what was expected, what was observed, and evidence (a masked screenshot and an accessibility
  snapshot).

**Runtime conditions are declared outcomes that race every wait**, so an error page is named as soon as it shows.
They're also re-checked on the screen where a control resolves, right before acting: the fault matrix caught a
non-modal dialog being clicked past without that check.

| Condition | Handling |
|---|---|
| Not found, malformed, restricted, deposit below minimum | `business_outcome` |
| Session expired | Re-sign-on and restart on read-only capabilities. On ones that change data, escalate: a step that changed state must not run twice |
| Slow page | Absorbed by the waits |
| Unknown dialog or notice | Escalate to a person if one is attached, otherwise `failed` |
| App error | Fatal |
| A different member shown | `WRONG_RECORD`, caught before anything is read; structure-only evidence; a circuit breaker after two in 24 h (D19) |

**Flow-specific outcomes aren't hand-written** (D18, D22). The goal lists probe inputs and the code each should give
(`99999` → `MEMBER_NOT_FOUND`). After recording, each probe is replayed and the model names the screen. The outcome
is kept only if:
- its phrase is fixed wording;
- its code is the expected one;
- the probe now ends in it;
- the happy path still succeeds.

**Fault matrix: 48/48 across two banks**, with 0 accounts opened.

**Drift** is secondary here, because these UIs are stable. A fallback hit still succeeds and is reported as drift.
On a tenant, it also proposes an override.

## Heterogeneity & multi-tenant

- **Surface seam.** Artifacts never contain selectors. A `Surface` (`observe`, `describe`, `resolve`, `act`,
  `wait_any`) resolves targets.
  - **Legacy web is the case built:** framesets, layout tables, unlabelled fields and duplicate names, handled by
    frame paths, anchors, table cells and region scopes.
  - **A desktop app** gets a `DesktopSurface` over UIA (Windows), AX (macOS) or AT-SPI (Linux), as agent-desktop
    does. Role and name work natively, and bbox matters more where trees are thin.
  - The artifact, the replay engine and the result contract don't change.
- **Reuse across tenants.** Artifacts are keyed by vendor product and version range, not by bank.
  - A tenant specialises one with an **override**: `set` and insert/remove ops addressed by **step id**, never by
    array index. Indexes shift silently and edit the wrong control.
  - Each override declares a semver `appliesTo` range.
  - A missing id, an empty path, or a base version outside the range is refused at load.
- **Measured.** Variant B is a second bank on the same product: it says "Find Member" instead of "Search", and has
  no `<label>` on Member #. The artifact recorded on bank A replays on B:
  - it **succeeds**: the anchor and `onlyInRegion` fallbacks find both controls;
  - it reports drift and **writes a pending override for B**, either the new name or the working strategy moved
    first;
  - after approval (`interfaze override --tenant b --approve`), B runs with no drift, the whole contract holds on B,
    and A is untouched.
- **At scale**, drift events per tenant and step are the signal for when to approve an override and when to
  re-record.

## Escalation & handoff

**Detecting "stuck"**
- **Discovery:** the same action on an unchanged screen (by page fingerprint) triggers one nudge, then Opus, then a
  person.
- **Replay:** an unknown screen, an unresolvable control, a failed postcondition, or an `escalate` outcome. With no
  operator attached, the run fails cleanly (`ESCALATION_UNATTENDED`).

**Control model**
- **States:** `AUTOMATION → PAUSED_FOR_HUMAN → HUMAN → RESUMING → AUTOMATION`, and `ABORTED` from any state.
- **The token:** a holder, an epoch and an expiry.
  - The epoch rises on every transfer and is checked just before input is dispatched, so a click decided before a
    handoff can't land after it (D9).
  - An operator who goes quiet loses the token when it expires.

**The handoff**
1. The intervention carries the capability, step, reason, a redacted snapshot and a screenshot.
2. The operator page shows it with a live view and Take control, Hand back and Abort.
3. The person works in **the automation's own headed browser**: the same session.
4. Injected listeners record their actions.
5. On hand-back, automation re-observes and decides: the problem is gone; the step already happened; retry the
   step; skip to the next one; or pause again.

In discovery, a person's actions become steps flagged `needsReview`, and approval waits for a reviewer.

**The caller** gets `needs_human` and a run handle by its deadline, while the run continues; the late result is kept
for 15 minutes (D11).

**Mocked.** The console is a minimal local page; the production design is a CDP screencast with input relay through
the same token. [`evidence/11-handoff-unknown-notice/`](evidence/11-handoff-unknown-notice/) has the log and a
screen recording, with PII masked throughout.

## Safety

- **The allowlist is enforced below the page.** Each artifact's policy lists allowed origins and paths, a `maxRisk`
  (`read` or `reversible`), and declared **commit paths**. Every browser request goes through it: off-origin
  requests and posts to a commit path are aborted before they leave the browser, whatever the button says. The
  safety eval found that a tampered artifact pressing a commit button labelled "OK", and a link leaving the site,
  got through until this gate existed (D20).
- **Risk classes.** Each click is classed `read`, `reversible` or `irreversible` from the live control, and unclear
  cases are classed up.
  - Irreversible actions are **never executed by automation**: discovery is refused, the schema rejects them, and
    replay re-checks the live control.
  - Capabilities reach the commit point and stop. Every eval suite checks the mock's opened-accounts counter, and it
    stayed at 0.
  - Committing money-moving changes is a decision for the agent product and a person (D1).
- **Data handling.**
  - **The model sees** `{{placeholders}}` for inputs and secrets, and stable `⟨NAME_1⟩` tokens for names, account
    numbers, amounts, SSNs, phones and emails. It can't type a value it read off the page.
  - **Credentials** come from the environment, are typed by code, and only on the sign-on page. A form that asks
    later is blocked, and later steps need review.
  - **On disk,** everything is redacted at the write boundary. Screenshots are masked in the page and fail closed.
  - **Wrong-record evidence** keeps structure only.
  - **Measured:** 15/16 safety cases without the LLM, and 12/12 with Claude on hostile pages (injection, a fake
    password form, an off-site link, an "OK" commit), with zero harm.
- **Limits.**
  - The risk classifier is name-based, and commit paths must be declared per app.
  - A name that appears **only in free text** reaches the model. This is the one open case; it needs an NER pass
    that fails closed.
  - Masking depends on accessibility-tree coverage.

## Cuts

- **The page map** (D3/D4): a per-bank graph of screens and actions, for cheaper re-discovery and a bounded,
  map-assisted replay fallback. It's designed (ARCHITECTURE §7a) but not built, and it's next, with its map on/off
  discovery eval.
- **Approval-gated commits** through the handoff channel, logging the approver and the token epoch. Designed, and
  deliberately not built.
- **Capability catalog / tool interface** (D15). The contract is already a tool signature.
- **Desktop surface and a real co-browsing console.** Designed at their seams.
- **Screenshots for the model** (D2). The tree was enough for every flow here: 10/10 discovery runs.
- **RAG or a vector database: deliberately not used.** Every lookup is an exact key or a short list; similarity search
  would only add ways to be wrong.
- **Next:**
  - an NER PII pass;
  - per-tenant base URLs and allowlists as configuration;
  - sharing learned outcomes across an app's capabilities;
  - racing the "already signed in?" check, which costs about 2 s of each ~4 s replay.
- **Time:** built in priority order inside a 5-day time-box (D15). The commits span 14–15 Nov 2026.
