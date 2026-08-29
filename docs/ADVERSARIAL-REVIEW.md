# Adversarial review — 2026-08-26, two days before kickoff

This is the record of a deliberate attempt to break this agent before the
contest does. The brief was hostile on purpose: *find what makes it lose money
or embarrass its author in front of judges, during a week of unattended
operation on a live paper account.*

It found **17 real defects**. Four of them would have been visible to judges as
"the agent stopped trading"; three could have doubled a position, given a
spread away at an unchosen price, or built the hedged book the gates exist to
forbid. One is not hypothetical at all — it is **live in the production ledger
as of this morning** (§2.1).

## Method

Every defect below has a test that was written to **fail against the tree at
`edcef10`** and to describe the behaviour the agent must have instead. Measured
directly, on a worktree checked out at `edcef10` with the two absent modules
shimmed so pytest could collect the file:

```
50 failed, 4 passed in 0.56s        # tests/test_adversarial.py @ edcef10
54 passed                            # the same file @ HEAD
```

The 4 that pass on both trees are regression guards by design: they assert
behaviour that was already correct and must survive the fixes (dedup still
separates genuinely different orders, a sane credit spread still prices
correctly, dry-run and shadow closes still contribute no real money).

Nothing here weakened a gate to make a test pass. Where an existing test had to
change, it was because the test set up its precondition the way a *rehearsal*
does rather than the way a *live run* does — see §2.3.

Both live switches stayed off throughout. No order was placed, no scheduled
task was registered.

---

## The shape of everything found

The 08-25 live-fire session found three bugs of one family: *works when run by
hand, fails in the context it will actually run in.* The 08-26 morning sweep
found ten more of the same shape. This review deliberately looked somewhere
else, and the defects have a different common shape:

> **The agent's model of the world was built from its own intentions, and
> nobody ever checked it against the world.**

The ledger recorded what the agent *meant* to do and treated that as what
happened. The dedup key described the quote rather than the trade. The halt
gate summed a record kind that nothing writes. The close rules decided
"market order" and then sent a limit. The scheduler fired on a weekday
calendar that knows nothing about market holidays. In every case the log
looked healthy while the belief was wrong — which is worse than a crash,
because a crash is loud.

---

## 1. Money and math

### 1.1 The dedup gate did not dedup — CRITICAL

`dedup_key()` hashed `run_date + symbol + legs`, and `legs` carry `limit` (a
live mid) and `qty` (sized off live equity). The same trade, re-derived twenty
minutes later, produced a **different key**, so gate 3 — one of the four —
waved it straight through.

This is not theoretical, and it is not rare. It sits directly on the run's own
documented retry path: a symbol's Alpaca 5xx leaves the day open on purpose
(`rc=1`, no `run_complete`), and the next invocation re-runs every symbol,
re-pricing each one on the way. The result is a second live order for a
position that is already open, and a book at twice the intended size.

The evidence was already in the committed ledger before this review started:

```
2026-08-25  live  order_intent  SPY  submitted  long  dedup_key c087d01f25f9ed19
2026-08-25  live  order_intent  SPY  dry_run    long  dedup_key b0406842cc2bba04
```

Same day, same symbol, same direction, two keys, and the dedup gate allowed
both.

**Fix.** The key now identifies the *structure* — sorted `(occ_symbol, side)`
pairs — and deliberately excludes price and size. What makes an order "the
same order today" is which contracts on which side, not what they cost at the
instant we happened to look. `tests/test_adversarial.py` covers the drift
(quote move, resize) and the separations that must survive (different strike,
flipped side, different day, different symbol), plus an end-to-end run of the
partial-failure retry that previously double-sent.

### 1.2 The −2% daily loss halt was inert — CRITICAL

`Ledger.realized_pnl()` summed records of `kind == "fill"`. Nothing in this
project has ever written a `fill` record. The exit rules write
`position_closed`, and those rows carried no P&L at all.

So the headline safety gate advertised in the README returned `0.0` every time
it was asked, on every day, forever. It had only ever been exercised by tests
that fed it a number by hand.

**Fix.** `position_manager.structure_pnl()` computes realized dollars on every
closed structure — `(V − E) × 100 × contracts`, using the same signed-value
convention R2/R3 already use — and `position_closed` rows carry it.
`realized_pnl()` counts `fill` *and* `position_closed`, excluding dry runs and
the shadow book, both of which move imaginary money.

### 1.3 The halt trusted only its own bookkeeping — HIGH

Even repaired, the halt reads a number this agent computes about itself. It
cannot see an exit that never got logged, a manual trade, an early assignment,
or a fill it never heard about.

**Fix.** `daily_loss_halt_gate` now also takes `account_day_pnl` — Alpaca's own
`equity − last_equity` — and **the worse of the two wins**. A bug in our own
accounting can now only ever halt trading early, never late.

### 1.4 A negative worst-case loss passed the 5% cap — MEDIUM-HIGH

For a credit spread, `estimate_max_loss` returned `width − credit`. When a
crossed or stale book makes the credit exceed the width, that is a **negative
number**, which sails under any percentage-of-equity ceiling and under the
red-team stub's `frac > 0.05` check as well. The gate designed to fail closed
on anything it cannot price was instead reporting free money.

**Fix.** A negative worst case is unpriceable and returns `None`, which the
position-size gate already fails closed on.

---

## 2. State corruption — the believed book vs. the actual book

### 2.1 Nothing ever reconciled the ledger against the broker — CRITICAL

**This is the most dangerous defect found, and it is live right now.**

The direction-flip guard — and therefore the ban on hedged books — reads
`Ledger.open_direction()`, which is assembled from order *intents*. Intents
drift from reality in both directions and nothing corrected either:

* **Believed open, actually flat.** An order that was accepted and never
  filled, a position that expired worthless, one that was assigned away, or a
  `--dry-run` rehearsal. Nothing writes `position_closed` for a position the
  broker does not have, so `open_direction()` answers `"long"` forever, the
  symbol is frozen in one direction for the rest of the competition, and no
  log line anywhere says why.
* **Believed flat, actually open.** A dry-run close writing `position_closed`
  for an unwind that never happened, or manual intervention. Opening the
  reverse here builds exactly the hedged book gate 4 exists to ban.

Run against the production ledger and the live paper account this morning:

```
ledger believes open : {'IWM': 'long', 'QQQ': 'long', 'SPY': 'long'}
broker actually holds: {'QQQ': 'long', 'SPY': 'long'}
PHANTOM (frozen for the week): ['IWM']
```

The 08-25 IWM order was accepted and expired unfilled. On kickoff morning IWM
would have been silently frozen behind the flip guard for the whole contest,
with a perfectly healthy-looking log.

**Fix.** `position_manager.reconcile()` runs at the start of every round,
before any exit rule and long before any open:

* believed open, broker has nothing → `position_reconciled` (clears it),
* broker holds it, ledger disagrees → `position_adopted` (the account is the
  source of truth).

Naming the direction of a position the agent did not open needs
`structure_direction()`: pure arithmetic over strikes and signs (bull call
debit and bull put credit are `long`; bear call credit and bear put debit are
`short`; a lone long call or short put is `long`). Six parametrised cases.

Verified live: a fresh ledger pointed at the real account emitted
`position_adopted QQQ long` and `position_adopted SPY long` on the first round.

### 2.2 A torn ledger line bricked the entire agent — CRITICAL

Every read went through `json.loads` on every line. A killed task or a power
loss mid-append leaves a partial line — and one truncated byte range made
dedup, the once-per-day guard, the flip guard, the halt gate **and the
judge-facing dashboard** all raise at the same moment. The agent would not have
traded again for the rest of the week, and the demo URL would have shown a
traceback.

**Fix.** `read_jsonl()` distinguishes the two cases, because they mean
different things:

* A torn **tail** is a crash artefact. It is quarantined to a `.torn` sidecar
  (nothing is destroyed), truncated out of the ledger so the next append cannot
  glue itself to the fragment, and recorded as a `ledger_torn_tail` row.
* Corruption **anywhere else** means something rewrote an append-only file.
  Silently skipping it could drop an order record, which is how a duplicate
  gets sent. It raises `LedgerCorruption`.

And the sharp edge: `seen_order()` **fails closed against a torn tail that
mentions the key**. We cannot parse the row that was burning it, so we must
assume the order went out. Because the fragment survives as a record, that
holds across process restarts. Same trade-off the repo already made elsewhere —
a key wrongly burned costs one skipped trade; a key wrongly free costs a
duplicate position.

### 2.3 A `--dry-run` rehearsal opened phantom positions — HIGH

`open_direction()` excluded only `status == "error"`. A dry run — which by
definition never reached the market — wrote an `order_intent` carrying a
direction and engaged the flip guard. Every rehearsal on a box with real keys
froze all five symbols.

**Fix.** `NON_OPENING_STATUSES` is deliberately a **deny-list** (`error`,
`dry_run`, `rejected`, `canceled`, `expired`): an *unrecognised* status is
treated as a real order, because wrongly believing a position is open costs a
skipped trade while wrongly believing it is closed costs a hedged book.

Four pre-existing tests used `StubCLIBroker(dry_run=True)` to stand in for an
open position, so they had to change. They now use an `AcceptedStubBroker`
whose orders come back `submitted` — setting the precondition up the way a live
run does rather than the way a rehearsal does. No assertion was relaxed.

### 2.4 Assigned stock was invisible to every rule — HIGH (detection only)

`fetch_option_positions()` filters to `asset_class == "us_option"`. Early
assignment on the short leg of a credit spread leaves 100 shares per contract
in the account, and that stock is invisible to R1–R4, to the flip guard and to
the position-size gate — permanently — while the orphaned long leg gets
re-evaluated as though it were a fresh structure.

**Partial fix.** `detect_non_option_positions()` writes a loud
`assignment_suspected` record and surfaces it on the dashboard. Automatically
liquidating assigned stock is a bigger decision than a review should take
unilaterally; making it impossible to *miss* is not. See §7.

---

## 3. The close path

### 3.1 The quote-gap force-close was a limit order at a made-up price — HIGH

`evaluate()` decides `order_type: "market"` for the "never hold a position we
cannot see" force-close after three gapped rounds. `check_positions()` then
**discarded that field**, and `submit_legs()` always built `--type limit`,
priced off the very quotes that were missing — with `close_legs()` flooring
each absent mid to `0.0`.

A limit order on a structure priced with a `0.00` leg either never fills (so we
keep the blind position the rule exists to shed) or fills at a price nobody
chose. On a sell-to-close, a `$0.00` net limit is an instruction to accept
anything at all.

**Fix.** `order_type` is plumbed through `check_positions → submit_legs → argv`
(`--type market`, no `--limit-price`). `close_legs()` emits `limit: None` for an
unpriceable leg instead of `0.0`, and `submit_legs()` **refuses** a limit order
containing a leg it cannot price.

### 3.2 One bad structure abandoned the whole round — MEDIUM

`check_positions()` had no per-structure isolation. The first raising submit
aborted the loop for every other underlying *and* skipped the state and
close-log writes at the end — so the quote-gap counters silently never
advanced, and a round that definitely happened left no row on the judges' page.

**Fix.** Per-structure `try/except` writing a `close_check_error` record and an
`exec_ok: False` row; the failed structure keeps its gap counter rather than
having it reset by the failure; state and log are written unconditionally.

### 3.3 The close task could never place a live close — HIGH

`run_daily.cmd` documents the two independent go-live switches. `run_close_
check.cmd` documented **neither**. Following the written procedure would have
produced a close path raising `refusing live order: ALPACA_HACKATHON_LIVE != 1`
on every unwind, all week, while opens kept working normally.

An agent that can open and cannot close is considerably worse than one that
does neither.

**Fix.** The script documents and carries both switches inside its own
`setlocal`, and the entry point now refuses up front with `rc=2` and a clear
message instead of raising once per structure (which §3.2 would otherwise have
logged as a routine per-structure error, all week).

---

## 4. Time and calendar

### 4.1 Nothing ever asked whether the market was open — HIGH

Both tasks fire `/SC WEEKLY /D MON,TUE,WED,THU,FRI`, and no code path
downstream checked a clock. Three ways that bites:

* **A weekday market holiday.** 2026-09-07 (Labor Day) is a Monday. It falls
  just outside the 08-28 – 09-04 window, but the tasks keep firing long after
  the judges leave, and the box is not going to be re-checked.
* **An early close.** Half-days close 13:00 ET, which puts the 12:15 PT
  (= 15:15 ET) close-check round **two hours after the bell**, pricing unwinds
  off a dead tape.
* **A box that is not in US Pacific**, where the hardcoded `/ST` values mean
  something else entirely.

**Fix.** `market_verdict()` prefers Alpaca's own `/v2/clock` (authoritative,
and the only thing that knows about *unscheduled* closures) and falls back to a
new deterministic `market_calendar` module. The fallback matters as much as the
check: blanket-refusing whenever `/v2/clock` hiccups would let one flaky
endpoint cost the whole contest, while assuming "open" would send day orders
into a shut market.

`market_calendar` computes US Eastern itself — second-Sunday-March to
first-Sunday-November, two lines of arithmetic — because Windows ships no
system tz database and `tzdata` is not a dependency here. It carries NYSE full
closures and 13:00 ET early closes for 2026 and 2027, so the agent does not
silently lose the calendar the moment it outlives the contest.

A closed market records `market_closed` and exits `0` **without** writing
`run_complete`, so the next scheduled round simply retries. `--ignore-clock`
exists for manual rehearsal and records a `clock_override` row.

Verified live at 04:03 ET this morning:

```
gated-agent dry run — 2026-08-26 (equity $99,936, real Alpaca chain, MCP red-team)
Market closed — clock: market closed (broker ts 2026-08-26T04:03:24-04:00,
next open 2026-08-26T09:30:00-04:00). Nothing evaluated, nothing sent.
```

### 4.2 A missed start was a silent no-trade day — MEDIUM

`schtasks` does not set `StartWhenAvailable`. A box asleep, rebooting or off at
07:00 simply skips that run and nothing anywhere says so — in a one-week
contest that is a 20% data loss with no signal.

**Fix.** `register_task.cmd` sets it on both tasks via the Scheduler cmdlets
(`schtasks` has no flag for it), non-fatally. Catching up late is safe *now*:
the day is idempotent via `run_complete`, and the dedup key no longer drifts
with the quote (§1.1). It would not have been safe before.

---

## 5. The red-team loop itself

### 5.1 Fail-closed was correct, and silent — HIGH

The red-teamer fail-closes on any error: missing binary, MCP server down,
timeout, garbage output. That is right for a single order. But an
infrastructure veto and a considered "this spread is illiquid" veto left the
**identical ledger shape**, so a `claude` CLI that stopped launching on day 2
would have produced a week of plausible "RED-TEAM VETO" lines that read exactly
like a cautious agent doing its job — and zero trades in front of the judges.

Zero trades for a week is the worst possible outcome of a safety feature, and
it must be impossible for it to happen quietly.

**Fix.** Infra failures carry `infra_failure: True` in the protocol JSON.
`run.redteam_health()` escalates when *every* red-team pass has failed for
infrastructure reasons across `REDTEAM_ALARM_DAYS` (2) consecutive days with
activity: a `redteam_infra_alarm` ledger record, a banner on stderr naming the
things to check, a non-zero exit code, and a warning on the dashboard. Each
individual order still fails closed — the alarm never approves anything.

A considered veto, however many days it runs, never raises the alarm.

---

## 6. Judge-facing surfaces and the negative control

### 6.1 The negative control was quietly dying — HIGH

The shadow book is the signature feature. Live positions get closed by R1–R4,
which writes `position_closed` and releases the flip guard. Shadow positions
have no broker and no exit rule, and `position_closed` was only ever written
for the live book — so after its first would-trade in a symbol, the coin-flip
twin was vetoed on every reverse draw, roughly two days in three.

A placebo arm that cannot take the trades the live arm takes is not a placebo
arm, and the comparison the whole project rests on would have been quietly
rigged.

**Fix.** `run.shadow_exits()` applies **R1 verbatim** (DTE ≤ `dte_close`) to the
shadow book's open positions, parsing expiries from the OCC symbols already on
the record. R1 is the only pre-registered rule computable without quotes, and
it is applied unchanged so the comparison is not tilted in either direction.
The asymmetry that remains (the shadow twin does not get R2/R3, which need
quotes) is stated here rather than hidden.

### 6.2 The shadow book still cannot reach the order path — verified

The brief asked specifically whether the new exit paths could emit a real order
for a shadow position. They cannot, and there is now a regression test that
seeds a shadow position, runs both `close_checks` and `shadow_exits`, and
asserts the executor was never touched: `check_positions` only ever iterates
what the **broker** holds, and `shadow_exits` writes a ledger record and
nothing else.

### 6.3 The dashboard crashed on ordinary edges — MEDIUM

One `try/except` covered `/v2/account` only. A hiccup on `/v2/positions` or
`/v2/orders` — or a single order row with a null `submitted_at`, or a torn
ledger line (§2.2) — rendered a Python traceback on the page the judges are
looking at.

**Fix.** Every endpoint is fetched under its own guard and degrades to a
warning; numeric fields go through a coercer; the page shares the agent's
tolerant `read_jsonl` so both agree on what a half-written file says; and
`redteam_infra_alarm` / `assignment_suspected` / `ledger_torn_tail` rows are
surfaced as banners instead of being buried in the tail of a table.

No secrets are rendered: the page reads only what the account already shows its
owner, and `ledger/` is gitignored, so the MCP config written at runtime
(`ledger/.redteam_mcp.json`, which does contain keys) has never been in git.
Confirmed with `git ls-files ledger` — empty.

### 6.4 The "offline" test suite was hitting the live account — MEDIUM

`tests/test_adversity.py` built a real `AlpacaCLIBroker` and let it call
`/v2/account` for real. On a box with keys in `.env`, the suite the README
calls "offline unit + pipeline tests" was quietly talking to the live paper
account — which also means it would fail on a plane, and pass for the wrong
reason if the account changed.

**Fix.** That test injects its own equity, account and clock. The whole suite is
now verified network-free:

```
$ python -c "urllib.request.urlopen = deny; pytest.main([...])"
202 passed in 1.89s
```

---

## 7. Not fixed — recorded as residual risk

Honest list. None of these were reproducible into a failing test, or the fix
was out of scope for a two-day-out review.

| # | Risk | Why it was left |
|---|---|---|
| 1 | **Early assignment is detected, not managed.** §2.4 alarms on assigned stock; the agent will not liquidate it, and the orphaned long leg still gets grouped as a fresh structure. | Auto-liquidating equity is a strategy decision, not a bug fix. R1 (DTE ≤ 2) already keeps the book out of the highest-assignment window. |
| 2 | **Tick rounding can push the realised max loss slightly over the mapper's 1% budget.** The mapper sizes on the raw mid; the legs carry `_tick_round`ed limits, and for options ≥ $3.00 the $0.05 tick can add up to ~$5 per contract. | Bounded, small, and the independent 5%-of-equity gate still catches anything gross. Fixing it means the mapper sizing on rounded prices — a behaviour change worth its own testing window. |
| 3 | **`_signed_value` assumes 1:1 leg ratios.** A structure with unequal leg quantities (partial assignment) would be mispriced by R2/R3. | Alpaca fills `mleg` atomically, so this could not be produced without fabricating a position shape the order path cannot create. |
| 4 | **No aggregate portfolio cap.** The 5% ceiling is per order; five symbols could in principle stack. | The mapper sizes each trade to 1% of equity, so practical worst case is ~5% total — but nothing *enforces* that, and it should be an explicit gate. |
| 5 | **`records()` re-reads the file on every call**, so `open_positions()` is O(n²) in ledger rows. | Trivial at a week's scale (hundreds of rows, ~15 reads per run). Would matter over a month. |
| 6 | **`--date` is unvalidated.** A typo produces a different `run_date` and therefore a different dedup namespace. | Manual-invocation-only footgun; the scheduled path never passes it. |

---

## Scoreboard

| | before | after |
|---|---|---|
| Tests | 148 | **202** |
| New adversarial tests | — | 54 |
| ...failing against `edcef10` | — | **50** |
| ...passing on both (regression guards) | — | 4 |
| Network calls made by the suite | ≥1 | **0** |

Real defects found: **17**. Live switches touched: **none**. Orders placed:
**none**. Scheduled tasks registered: **none**.

The single most dangerous one, restated: **the agent's ledger was its only
model of what it owned, and nothing ever compared that model to the account.**
A believed position the broker does not have freezes a symbol for the entire
contest behind the flip guard; a real position the ledger has forgotten lets
the agent open against itself. Both were reachable from ordinary operation —
an unfilled order, an expiry, a rehearsal — and both leave a log that looks
completely healthy. The first of the two was already true of IWM this morning.

---

# Endgame review — 2026-08-29, five positions live, six trading days to expiry

Day-1 is done: the competition account holds five call debit spreads (SPY /
QQQ / IWM / NVDA expiring 9/11, AAPL expiring 9/4 — the ledger and the broker
agree), both tasks fire weekdays with `StartWhenAvailable` confirmed set, and
the submission deadline is 2026-09-04 15:00 UTC. This review walked every
calendar day from here to expiry and attacked what actually happens on each
one — with particular hostility toward the surface the 08-26 review and the
08-28 live fixes created.

## The calendar, day by day

| Day | What fires | What happens |
|---|---|---|
| Sat 8/29 – Sun 8/30 | nothing | Positions held. Early assignment on a short call needs it ITM with no extrinsic; all five shorts are OTM with a week-plus of time value — and if it happens anyway, Monday's first round runs `reconcile()` + `detect_non_option_positions()` before anything else and shouts `assignment_suspected` on the dashboard. Nothing needs to run before Monday, and nothing does. |
| Mon 8/31 | both tasks | Normal trading day. Verified in the Task Scheduler: next run 8/31 07:00, `StartWhenAvailable=True`, `MultipleInstances=IgnoreNew` on both. |
| Tue 9/1 | both tasks | **Not a holiday — not even a Monday.** The hostile brief planted "Mon 9/1: Labor Day?" on purpose: 2026-09-01 is a **Tuesday**, and Labor Day 2026 is **Monday 9/7**. Normal trading day. The unrelated `TaxablePaperMonthly` task first-fires 9/1 at 18:30 PT — after the bell, different repo, and a boolean compare of the two `.env` files' `ALPACA_API_KEY` values (both set, **SAME=False**; no value printed) proves it cannot touch this account. |
| Wed 9/2 | both tasks | **AAPL hits DTE = 2 → R1 at the 07:00 PT (10:00 ET) round**: market order out, `position_closed` with realized P&L, flip guard released. The same morning may legally re-open AAPL in the same direction at the next eligible expiry (mapper `min_dte=5`); that is a new structure with a new dedup key, by design. Traced end to end by a new test. |
| Thu 9/3 | both tasks | Last full day. **Recommended stage-1 stand-down in the evening**, after the 12:15 round (plan below). Record the demo video today at the latest. |
| Fri 9/4 | 07:00 task (if armed) · **submission 08:00 PT** | The 07:00 run fires **one hour before the judging snapshot** and, if still armed, can open fresh spreads whose entry cost immediately marks against the judged P&L. Not a bug — an operator decision; see the plan. |
| Mon 9/7 | both tasks | **Labor Day.** Both entries ask `/v2/clock` first and fall back to the built-in calendar, which carries 9/7 (pre-existing test). Both exit 0 with a `market_closed` / "nothing checked" record. No order path is reachable. |
| Tue 9/8 | both tasks | Normal day for whatever is still open. |
| Wed 9/9 | both tasks | The four 9/11 spreads hit DTE = 2 → R1 market exits. **This is why stage 1 of the stand-down leaves the close task armed.** The shadow twin's 9/11 entries exit the same day by the same R1 (`shadow_exits`). |
| Fri 9/11 | both tasks | Should already be flat. If not: residual risk #2. |

## New defects

### E1. R1 was not unconditional — a quote gap deferred it (HIGH, fixed)

The frozen config, the README and the WRITEUP all say R1 closes
*unconditionally* at DTE ≤ 2. The implementation checked the quote-gap branch
first: a leg with no bid made the structure's value `None`, which meant
"skip this round" for up to `MAX_QUOTE_GAPS = 3` rounds. A far-OTM short leg
with no bid is not an outage — it is the **normal end-of-life state of exactly
the spreads this account holds**. So the one rule whose entire purpose is
"be out before pin/assignment week" was quietly subordinate to a counter meant
for data outages.

Walking the arithmetic: gaps starting at the 9/3 12:15 round would have put
the force-close at 9/4 15:15 ET — 45 minutes before AAPL stops trading. The
agent stayed out of disaster only because 3 gaps at 2 rounds/day happens to
fit inside the R1 window. That property was accidental, and one skipped round
(a locked ledger, a 5xx) would have broken it.

**Fix.** In `evaluate()`: unpriceable **and** DTE ≤ `dte_close` → close at
market *now*, rule `R1_time` — the same escalation R1 already uses when it can
price the book. Strengthens the exit; weakens nothing. Two tests: the
evaluate-level verdict, and an end-to-end assert that the executor is reached
with a market order on the **first** gapped round inside the window.

### E2. A rehearsed close released the flip guard (MEDIUM, fixed)

`open_direction()` treated every `position_closed` as a release, including
`dry_run: true` rows — written whenever close checks run without `--live`.
The broker still holds the position a rehearsal "closed", so the belief was
simply wrong, and reconcile re-adopting it one round later left a round-long
window with the guard open against a live position. The configuration that
produces this daily is exactly the one the stand-down plan creates (morning
run dry-run, afternoon close task live).

**Fix.** Only a real (`dry_run: false`) close releases the guard; a rehearsal
changes nothing, because rehearsals change nothing at the broker either. Two
pre-existing tests set up "believed flat" the rehearsal way and were updated
to the live shape (a submitted close whose day order expired unfilled) — the
same test-precondition class as §2.3; no assertion was weakened, and a new
test pins the rehearsal case directly.

## Attacks that failed (verified, not assumed)

* **A −3% gap day through the actual gate arithmetic.** Equity ~$100k → halt
  floor −$2,000. Marked-to-market damage shows up in the account's own
  `equity − last_equity` long before our realized ledger sees anything, and
  the worse-of rule (§1.3) halts on it. And the halt blocks **opens only**:
  the same day's R3 stop-loss unwind still reaches the executor, because
  `check_positions` never consults the gates — a halt that blocked exits would
  lock in the very damage it exists to stop. New test pins both halves.
* **The AAPL 9/2 → 9/4 sequence**, end to end in one test: R1 market order,
  `position_closed` carrying `pnl`, flip guard released, and the money visible
  to `realized_pnl` — all two days before the 9/4 08:00 PT snapshot.
* **Labor Day** (both entries), **9/1-is-a-Tuesday**, **TaxablePaperMonthly
  isolation** — table above.
* **Catch-up double-fire**: `MultipleInstances=IgnoreNew` plus per-day
  idempotency plus structure-keyed dedup. A late catch-up into a shut market
  records `market_closed` and retries next round.

## Residual risks — labeled, not fixed

| # | Risk | Why it was left |
|---|---|---|
| 1 | **`position_closed` is written on submission, not on fill.** A crossed-price R2/R3/R4 day limit that still fails to fill leaves believed-flat/actually-open until the next round's reconcile re-adopts it; an unfilled R4 close followed by the same-run reverse open is the one remaining theoretical hedged-book window. | Crossing the spread (08-28 fix) makes a non-fill rare; the day order expires at the bell; reconcile self-heals next round. Polling fills to confirm closes is a real improvement but a bigger change than a contest-week review should install. |
| 2 | **Riding to 9/11 expiry.** Requires three independent failures (priced R1 from 9/9 at 2 rounds/day, the new R1-with-gap market close, and the gap escalation). If it happens anyway: OCC auto-exercises ITM ≥ $0.01; a long-only-ITM leg exercises into 100 shares/contract (about $32k at AAPL 320, about $77k at SPY 771 — inside this account's cash for qty 1–2), held over a weekend, invisible to R1–R4, caught Monday by `detect_non_option_positions`. | Detection-not-management is unchanged from §2.4 and stated there. The defense is depth on the exit path, which E1 just deepened. |
| 3 | **A late-waking box** can catch up the 07:00 task minutes after the 09:30 ET open, where an R1 market order pays the open's widest spread. | Bounded cost on a defined-risk vertical, and accepted: R1 must complete, and `StartWhenAvailable` is what turned a silent lost trading day into this smaller risk. |
| 4 | **The 9/4 07:00 run trades one hour before judging** unless stage 1 has run. | Deliberately not coded. The agent must not decide by itself to stop trading inside the contest window; standing down is the operator's call, so it lives in a script an operator runs. |

## Stand-down plan — prepared, not executed

`scripts/stand_down.cmd` (thin ASCII wrapper over `scripts/stand_down.py`)
exists as of this review. It is **not scheduled and has not been run**; both
payloads remain armed and `ALPACA_HACKATHON_LIVE` was not touched by this
review. Rehearsed against copies of both payloads: byte-exact minimal diffs,
CRLF preserved, idempotent on re-run, and it refuses to write a half-disarmed
payload.

| Stage | When | Who | What |
|---|---|---|---|
| 1 — opens stand down | Evening 9/3 after the 12:15 round (or immediately after submitting on 9/4) | the user, or MBP over SSH | `scripts\stand_down.cmd` → `run_daily.cmd` back to `--dry-run`, live switch re-commented. **The close task stays armed on purpose**: the 9/11 spreads still need R1 to fire for real around 9/9. An agent that cannot open but can close is the safe shape; the reverse is not. |
| 2 — closes stand down | After the account shows zero open positions (about 9/9) | same | `scripts\stand_down.cmd all`, then optionally `schtasks /Change /TN ... /DISABLE` (disable, not delete — the registration and its settings survive). |
| — | each stage | same | Commit the changed payloads, so git history records when the agent stood down. |

E2 is what makes stage 1 safe: before this review, the mixed configuration it
creates would have had every rehearsed morning close releasing the flip guard
on a still-open position.

## Submission dry-run

The five items, assembled against the form: repo URL (live), account
`PA32VHBO5AOB` (verified by `verify_account_swap.py`, which prints facts and
never key material), demo URL (**needs the Streamlit Community Cloud deploy —
the one item that does not exist yet**), video and slides (placeholders; the
only two requiring human production time, deadline 9/3 evening). Paste-ready
form text and a morning-of checklist are in
[`docs/SUBMISSION.md`](SUBMISSION.md). Nothing in this repo posts anywhere;
the human submits.

README and WRITEUP were refreshed to the shipped architecture — executable-
side close pricing, R1 market escalation and its new gap non-deferral,
per-expiry direction reading, real-close-only guard release, book-scoped
red-team context — so a judge reading the docs reads the code that is running.

## Scoreboard

| | before | after |
|---|---|---|
| Tests | 214 | **219** |
| Network calls made by the suite | 0 | **0** |

Real defects found: **2 fixed** (E1 HIGH, E2 MEDIUM), **4 labeled** residual.
Gates weakened: none — both fixes make an exit fire sooner or a guard hold
longer. Orders placed: none. Live switches touched: none. The armed day-1
payload state is recorded in git deliberately: an uncommitted armed script is
one stray `git checkout` away from silently reverting a live agent to
dry-run mid-contest, which is the "stopped trading and nothing said so"
failure this project keeps refusing to allow.
