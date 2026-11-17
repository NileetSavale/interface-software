# interface — design write-up

Claude discovers a task once against a live legacy UI, and the run becomes a typed, versioned capability. Once a
person approves it, it replays without the model: same inputs, same steps, same typed result, in about 4 s for $0.
Numbers come from [`evals/report.md`](evals/report.md) (nine suites, each stamped with its commit, N stated). The full
design is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); the 32 decisions, including where I overrode the AI I
built this with, are in [`docs/DECISIONS.md`](docs/DECISIONS.md).

## Architecture

**One process, no infrastructure; the seams are interfaces** (Python 3.13, Playwright, Pydantic, FastAPI).

- **Discovery**: Claude Sonnet 5.5 reads a masked accessibility-tree snapshot and acts by tool call; when stuck,
  Opus 5.5 takes over the same conversation, then a person. The model only asks; code validates and policy-checks
  every call. The **recorder** turns each action that happened into a surface-neutral target, checked unique.
- **Replay** runs an approved artifact with no model. **To a calling agent**, each one is an MCP tool (D30).
- **Shared**: the `Surface` seam, the policy gate, the PII masker, a control token for handoff, a redacted run log
  (`interface log`), and each bank's **page map** of screens and actions learned from verified runs (D3–D7).

**Trade-offs**
- **The accessibility tree, not screenshots**: exact, cheap, layout-proof, and a picture would send the record to
  the model. Apps with no tree get a snapshot built from characters (green screen) or OCR (pixels); the model still
  reads masked text.
- **Model choice** (D12): discovery's cost is spread over every free replay, so first-try success beats token price.
  Over 20 real runs: median $0.017 (lookup) and $0.029 (sub-account), probes included, all replayed on another member.
- **Reused from Loop Runner**, my agent behind *When Tests Lie* (submitted to AGENT 2027 @ ICSE): the tool gate,
  stuck detection, model cascades, redaction and the eval method.

## Artifact schema

`src/interface/artifact/schema.py` (JSON Schema exported); real artifacts in `artifacts/`.

- **Contract**: `capability` (id, semver), `app` (vendor product, version range, surface), typed **`inputs`**
  (pattern, `sensitive`, `identifies: member`), typed **`outputs`**, named **`outcomes`** (business, recoverable,
  escalate, fatal, each with a detector), `status` (draft or approved, with provenance).
- **Steps**: intent, action, target, a value template (`{{inputs.memberId}}`, `{{secrets.APP_USER}}`, never a
  literal), a risk class, condition waits and a postcondition. Sign-on is a **subflow**, reused for recovery.
- **Targets**: a frame (or screen code) and **ranked strategies**: role + name › label › anchor text, table cell,
  region › path, position. Each is checked unique when recorded; duplicates are told apart by region, never index.
- **`success.checkpoint`**: a landmark, every output read, and a mandatory **identity check** (D16).
- **`overrides`** per tenant by step id. **Validation rejects** irreversible steps, undeclared templates and
  ambiguous extractors.

## Determinism & error handling

- Waits are on conditions, never sleeps. Strategies go in rank order; several matches fail as ambiguous, never pick
  one. Inputs are validated before the UI is touched.
- **Five results:** `success`; `business_outcome` (an answer, not a crash); `invalid_input`; `needs_human` with a run
  handle; `failed` with the step, expected vs observed, and masked evidence. Even a crash ends typed, with its
  traceback in the run log.
- **Measured:** 80/80 stability replays identical; across 120 shuffled layouts **no step ever found the wrong
  control**; **fault matrix 97/97 across three banks**, 0 accounts opened.

| Condition | Handling |
|---|---|
| Not found, malformed, restricted, below minimum | `business_outcome` |
| Session expired | Re-sign-on and restart if read-only; escalate on a flow that changes data |
| Slow page, values filled in late | Absorbed by the waits |
| Unknown dialog | A person if attached, else `failed` |
| A different member shown | `WRONG_RECORD` before anything is read; circuit breaker after two in 24 h (D19) |

**Flow-specific outcomes aren't hand-written** (D18): probe inputs (`99999`) are replayed and the model names the
screen; an outcome is kept only with fixed wording, the expected code, and the happy path still succeeding. A row no
label names is picked by the cell showing an input; a row pages away is reached by a bounded `pageUntil` (D27, D28).
When no strategy survives, the bank's page map may stand in for at most two trusted, read-only edges, reported as
`MAP_FALLBACK` (D23).

## Heterogeneity & multi-tenant

- **One artifact format, three surfaces.** Legacy web (framesets, layout tables, unlabelled fields); a **green
  screen** read through a VT100 emulator (D31: 31/31 faults across two banks, Claude 5/5); and **pixels only**, read
  by OCR and driven by mouse and keyboard on a canvas or a native window (D32). Pixel reads are double-checked and
  fail `UNREADABLE` rather than guess: 500/500 values right, 0 silent misreads, 31/31 faults, Claude 5/5 from OCR
  text. An app with a desktop tree would get a UIA/AX surface at the same seam.
- **Tenants.** Artifacts are keyed by vendor product and version range, not bank. A bank specialises one with an
  **override** by **step id** (indexes shift silently) under a semver range; anything that doesn't apply is refused.
  Bank B's relabels succeed via fallbacks, report drift and **write a pending override**; once approved, B runs
  clean. Bank C defeats every strategy; with C's page map, learned from **one** verified lookup, the whole fault
  matrix holds. Maps are never shared across banks (D5).
- **Apps we didn't write.** On ParaBank, two capabilities replay with no model (**40/40** stable); it found captions
  over fields, identical buttons, an onload focus move and late values, each fixed. Through pixels only, three
  attempts found seven gaps the mock had hidden, one of which signed into another customer's account on the public
  demo (Claude refused to accept it); fixed, Claude recorded it for $0.035 and it replayed on the other account.
- **At scale**, `interface drift` says per bank and step whether to approve an override or re-record, once three runs
  agree (D29). Replays share no state: 72/72 concurrent lookups returned their own answer; 8 at once ran 4.8× the
  throughput of one. A deployment puts the same unit behind a per-bank queue.

## Escalation & handoff

- **Stuck in discovery:** the same action on an unchanged screen → a nudge → Opus → a person. **In replay:** an
  unknown screen, unresolvable control or `escalate` outcome; unattended, it fails cleanly.
- **Control:** a token with a holder, epoch and expiry. The epoch rises on every transfer and is checked just before
  input is dispatched, so a click decided before a handoff can't land after it (D9).
- **Handoff:** the intervention carries capability, step, reason and a redacted snapshot. The operator page (its own
  key, D25) offers Take control, Hand back, Abort; the person works in **the automation's own browser**, recorded.
  On hand-back, automation re-observes and decides: resolved, done, retry or pause again.
- **The caller** gets `needs_human` and a run handle at its deadline; the late result is kept 15 minutes (D11).
  [`evidence/11-handoff-unknown-notice/`](evidence/11-handoff-unknown-notice/) has the log and a masked recording.

## Safety

- **The allowlist is enforced below the page**: origins, paths and declared **commit paths** gate every browser
  request, whatever the button says (D20). Only `http(s)` loads a page (an outside review found `file://` slipping
  past; fixed and tested).
- **Irreversible actions are never executed by automation** (D1): discovery refuses them, the schema rejects them,
  replay re-checks the live control. Capabilities stop at the commit point; every suite checks 0 accounts opened.
- **Data.** The model sees `{{placeholders}}` for inputs and secrets and stable `⟨NAME_1⟩` tokens for PII, and
  can't type a value it read. Names in free text are hidden by a deterministic, fail-closed rule (D24). Credentials
  are typed by code on the sign-on page only, per bank (D26). Everything on disk is redacted at the write boundary.
- **Measured:** 18/18 safety cases without the model, 12/12 with Claude on hostile pages (injection, a fake password
  form, an off-site link, an "OK" commit), zero harm.
- **Limits:** risk classes and commit paths are declared per app; a lone first name in free text reaches the model;
  masking depends on what the surface can read (the tree, or OCR).

## Cuts

- **Approval-gated commits** through the handoff channel: designed, deliberately not built.
- **A UIA/AX desktop surface, TN3270, a real co-browsing console:** designed at their seams. No attended handoff on
  the terminal or pixel surfaces.
- **RAG / vector database: deliberately not used.** Every lookup is an exact key or a short list.
- **Next:** per-app calibration for the pixel surface; lone first names; racing the "already signed in?" check
  (about 2 s of each ~4 s replay).
- **Time:** built in priority order inside a time-box (D15); commits span 14–16 Nov 2026.
