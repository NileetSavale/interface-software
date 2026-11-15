# interface

**Learn a task once with Claude. Replay it without the model.**

An LLM drives a legacy banking UI (framesets, layout tables, no test ids) to reach a goal in plain English. The run
is recorded as a typed, versioned capability. After a person approves it, it replays deterministically: no tokens,
the same steps every time, and a typed result. That result is a success with outputs, a business answer ("no such
member"), bad input, `needs_human`, or a failure that says where and why. A person can take over the live browser
mid-run, and nothing automated can ever press "Confirm".

- **Design write-up:** [`REPORT.md`](REPORT.md). Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). All
  decisions: [`docs/DECISIONS.md`](docs/DECISIONS.md).
- **Evidence of real runs:** [`evidence/`](evidence/README.md): the real Claude discoveries, and 13 replays covering
  success, business outcomes, recovery, hard failures, a handoff with a screen recording, a second bank, and a
  blocked commit.
- **Measured:** [`evals/report.md`](evals/report.md): five suites, N and commit stated.
- **Visual tour:** <https://nileetsavale.github.io/interface-software/>: interactive architecture, decisions, numbers.

## Setup

Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
cp .env.example .env        # then set ANTHROPIC_API_KEY (only discovery needs it)
```

`.env` is gitignored. It holds:
- `ANTHROPIC_API_KEY`, needed only by `discover` and the two eval suites that call Claude;
- the mock app's demo operator, `APP_USER=teller01` and `APP_PASSWORD=demo-only-password`.

The mock app holds only made-up members, so these are not real credentials. Artifacts refer to them as
`{{secrets.APP_USER}}`, and their values never reach the model, artifacts or logs.

### Without live services
Nothing below needs an API key except discovery itself.

```bash
uv run pytest                              # 107 tests; discovery is driven by a scripted planner, not Claude
uv run python -m evals all                 # the four eval suites that need no LLM (~15 min)
uv run python scripts/make_evidence.py     # rebuilds evidence/ (replays run fresh; no model)
```

Replay never calls a model, so every `replay` command below works without a key.

## Demo

Terminal 1, the target app (a mock legacy core-banking system):

```bash
uv run interface mock-app                  # http://127.0.0.1:4100
```

Terminal 2:

**1. Discover with Claude.** About 30–90 s and $0.02–0.07, including probes that teach it the business outcomes.

```bash
uv run interface discover goals/lookup_member_balance.json --input memberId=12345
# → artifacts/core_banking.lookup_member_balance.json (status: draft); the log is under runs/disc_…/
```

**2. Review and approve.** Unattended replay runs approved artifacts only.

```bash
uv run interface approve artifacts/core_banking.lookup_member_balance.json
```

**3. Replay without the model, for another member.**

```bash
uv run interface replay artifacts/core_banking.lookup_member_balance.json --input memberId=34567
# → {"status": "success", "outputs": {"memberName": "Mei Chen", "savingsBalance": "2500.00"}, …}
```

**4. Replays that hit errors and exceptional states.**

```bash
A=artifacts/core_banking.lookup_member_balance.json
uv run interface replay $A --input memberId=99999   # business_outcome MEMBER_NOT_FOUND (an answer, not a crash)
uv run interface replay $A --input memberId=1234    # invalid_input, rejected before the browser does anything
curl -X POST '127.0.0.1:4100/__faults?name=server_error'
uv run interface replay $A --input memberId=34567   # failed APP_ERROR at the step, with a masked screenshot
curl -X POST '127.0.0.1:4100/__faults?name=session_timeout'
uv run interface replay $A --input memberId=34567   # success, with a SESSION_EXPIRED recovery reported
```

The other faults are `slow`, `unknown_dialog`, `interstitial` and `wrong_record`.

**5. Hand the live session to a person.**

```bash
curl -X POST '127.0.0.1:4100/__faults?name=interstitial'
uv run interface replay $A --input memberId=34567 --attended
```

1. A browser window opens.
2. When the run meets the unknown notice, it prints an operator-page URL (`http://127.0.0.1:4200/interventions/…`).
3. On that page, enter your name and press **Take control**.
4. Click **Acknowledge** in the automation's browser window, then press **Hand back**.
5. The run verifies the screen and finishes. Your click is recorded in its log.

The caller got `needs_human` at the 30 s deadline, and the late result is collected afterwards.

**6. A second bank on the same product.** Variant B says "Find Member" and has no `<label>` on Member #.

```bash
# Terminal 1: Ctrl-C, then
uv run interface mock-app --variant b
# Terminal 2:
uv run interface replay $A --input memberId=34567 --tenant b          # success + drift; proposes an override for b
uv run interface override $A --tenant b                                # review the proposal
uv run interface override $A --tenant b --approve                      # approve it into the artifact
uv run interface replay $A --input memberId=34567 --tenant b          # success, no drift
```

**7. A flow that changes data, stopping at the commit point.**

```bash
uv run interface discover goals/open_sub_account.json --input memberId=12345 \
  --input accountType="Holiday Club" --input nickname="Trip fund" --input initialDeposit=300.00
uv run interface approve artifacts/core_banking.open_sub_account.json
uv run interface replay artifacts/core_banking.open_sub_account.json --input memberId=34567 \
  --input accountType=Certificate --input nickname=College --input initialDeposit=1000.00
curl 127.0.0.1:4100/__confirmed            # {"count": 0}: it reached the review screen and stopped
```

**8. A bank's page map.** Variant C is B with the menu's "Member Inquiry" renamed "Member Lookup", which no recorded
strategy survives.

```bash
# Terminal 1: Ctrl-C, then
uv run interface mock-app --variant c
# Terminal 2:
S=artifacts/core_banking.open_sub_account.json; IN="--input memberId=34567 --input accountType=Certificate --input nickname=College --input initialDeposit=1000.00"
uv run interface replay $S $IN --tenant c                    # failed at sign-on: nothing finds the renamed link
uv run interface discover goals/lookup_member_balance.json --input memberId=12345 --map maps/c.json --out /tmp/c
                                                             # Claude learns bank C once; the map records it
uv run interface replay $S $IN --tenant c --map maps/c.json  # success, with MAP_FALLBACK and the map version
```

The second discovery on a bank with a map runs the known actions several per turn (`follow_known`).

Exit codes: 0 for success or a business outcome, 2 for invalid input, 3 for needs human, 1 for a failure.

## Layout

```
src/interface/
  discovery/   loop.py (the agent loop and escalation ladder), planner.py (Claude), recorder.py, probes.py
  artifact/    schema.py (the contract), overrides.py (tenants), library.py (standard outcomes)
  replay/      executor.py (deterministic replay), result.py (the five result types)
  surface/     web.py + locate.js (Playwright over the accessibility tree), human.js (records a person's actions)
  policy.py    allowlist, risk classes, the network gate        pii.py       the masker the model sees through
  pagemap.py   each bank's learned map of screens and actions
  session.py   the control token                                operator.py  the operator page
  evidence.py  run logs, redaction, audit and circuit breaker   runs.py      invoke with a deadline
apps/mock_core_banking/   the target: faults, variant B, layout shuffles, hostile pages
goals/        the two capabilities' goal specs          artifacts/  the discovered artifacts
evals/        the five suites and their results         evidence/   curated runs
tests/        107 tests                                 docs/       design, decisions, the site
```
