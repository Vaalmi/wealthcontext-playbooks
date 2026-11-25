# Running WealthContext Playbooks

The commands, the records, a live run and the limits. The rules and the threats they stand
against are in [rules.md](rules.md); the evidence runs are in [evidence.md](evidence.md).

## The parts

| Part | What it does | What it can reach |
| --- | --- | --- |
| `backend/` | Owns every record and rule, the playbook folder and the triage log, reads WealthContext, serves the page | WealthContext, as operations only |
| `triage_agent/` | Chooses a playbook step for each exception that has one to choose; says whether to file a gap report where none has one, and after a correction | The backend only, plus the model's provider in a live run |
| `curator/` | Reads one bundle and answers with a proposal or no change | The backend only, on a network of its own, plus the model's provider in a live run |
| `modelkit/` | Builds each agent's model from a setting, or replays replies from a file | The agent's way out, if any |
| `cli/` | The `playbooks` command | The backend only |

The triage agent is never given a subject, an account, a result or trace ID, or the time of a
run. It sees a warning code, feed facts, a count and the steps it may choose among. For a
correction's review it also sees the field, what it should be and the reason, and never
the step's action. The backend attaches subjects, traces and receipts afterwards.

The curator is given its bundle and nothing else. It sees memos by their IDs and dates,
and corrections and gap reports by references made from their content, with no trace, no
time and no account.

## Commands

| Command | What it does | Needs |
| --- | --- | --- |
| `make setup` | Installs the pinned dependencies, the browser and the hooks | uv, a network |
| `make check` | Format, lint, types, tests, isolated imports and the browser test | Nothing else: no Docker, key or network |
| `make page` | Starts WealthContext and the layer on fresh volumes for the recorded cycle, on the committed recordings, and leaves the page's button to press. Plays nothing | Docker, uv, git |
| `make cycle` | The same, then presses the button itself, runs the recorded first cycle and checks each of its six steps | Docker, uv, git |
| `make record-cycle` | The same cycle on the models `.env` names, writing each reply to the two recordings, which replace the committed ones once every check holds | Docker and `.env` |
| `make correction` | Starts both on fresh volumes, triages 2026-10-09, records the committed correction and waits for its review, on scripted replies | Docker, uv, git |
| `make live-correction` | The same with a configured model, within two dollars in all | Docker and `.env` |
| `make curation` | Starts a layer of its own on fresh volumes (port 8131), makes the first cycle's bundle and runs the curator on a scripted reply | Docker, uv, git |
| `make live-curation` | The same with a configured model as the curator, within two dollars | Docker and `.env` |
| `make live` | Starts both on fresh volumes and triages 2026-10-15 only, with the choice made by a configured model | Docker and `.env` |
| `make evidence` | The five evidence scenarios, three runs each on the configured curator and trials' model, each run on fresh volumes, published in `guide/` as it ends, within forty dollars in all | Docker and `.env` |
| `make evidence-rehearsal` | The same runs with no key, on scripted replies only, written under `.local/evidence/` | Docker, uv, git |
| `make down` | Stops both, keeping their data | Docker |

## The recorded cycle

The page at http://127.0.0.1:8100/ has one button, offered on a layer started for the
recorded cycle whose records are still empty: `make page` leaves the layer so. Pressing it,
or `uv run playbooks cycle`, runs the first cycle once, through the same operations a person
uses. `make cycle` presses it itself and then checks every step, so its page shows the
cycle done and no button. The cycle's steps:

1. the playbook owner takes in the two founding memos, and the analyst triages 2026-10-15
   and 2026-10-09 under the first playbooks, whose notes name the step and give its action,
   as the founding versions are approved;
2. the playbook owner takes in the memo that changes the rule for custodian B, the analyst
   corrects the cross-check's note, and the triage agent reviews the correction and files a
   gap report. A hostile proposal is handed to the checks here, before the curator runs (the
   page shows it at step 6);
3. the playbook owner runs the curator, which proposes changes to both playbooks;
4. the gate runs every check on the proposal, the held-out cases three times each among
   them, and the playbook owner approves it with a ruling on each disagreement: for the
   correction and for the gap report, each over the founding memo, and where two memos alone
   disagree, for the later. A ruling is made for each change a disagreement is about: the
   recorded run's curator listed the custodian B memo against the founding feed memo too,
   about its two changes to the feed-outage playbook, so its approval has four rulings;
5. the analyst triages both dates again: the receipts name the playbook set the approval
   put in use (the third: the founding, then its approval, then this approval), and the
   notes give the approved version 2 actions;
6. the playbook owner takes in the poisoned memo and runs the curator again. A proposal
   from it goes to the gate, and one the gate passed would be refused. In the recorded run
   the curator proposed putting the hold back on the missing cross-check, resting on the
   founding memo, and the gate refused it at the held-out cases.

The people are personas of the synthetic firm. The page shows each step with its evidence,
from the records alone, and a register: the playbooks and their versions, the gap reports
and their status, the curator runs, and the proposals with their gate results, approvals
and refusals. A cycle that stops says at which step and why. It runs once: to run it again,
start the layer again on fresh volumes, as `make page` and `make cycle` do.

The models' replies come from `recordings/first-cycle-triage.json` (the triage agent's,
the held-out trials' among them) and `recordings/first-cycle-curation.json` (the
curator's, the only recording its image holds). **Both were captured from one live cycle,
on 2026-11-24**: GPT-6 Luna (`openai:gpt-6-luna`, with no effort named, so OpenAI's
default) as the triage agent and in the held-out trials, GPT-6.1 Terra at high effort
(`openai:gpt-6.1-terra:high`) as the curator. In that run the gate passed the curator's
proposal on every check: the four regression cases gave their expected notes with the
playbooks in use and once the proposal was made, and the four new-rule cases failed with
the playbooks in use and passed once it was made, three trials each on Luna. It cost
$0.0376 in all by the price table in `modelkit/prices.py` (the curator's two proposals
$0.032327, the 24 trials $0.002324, the rest the four triage runs and the correction's review). The page names each recording's
model and day. Replaying it needs no key and calls no model: each reply is
given once, in the order recorded, for as long as its agent runs, so three trials of one
case get three replies. A changed instruction or tool changes a request, and the replay
then refuses and the cycle stops, saying the recording no longer matches.

With `TRIAGE_MODEL`, `CURATOR_MODEL` and each one's provider's key in `.env`, `make record-cycle` runs the
same cycle on those models, within two dollars for each triage run and each curator run
and ten for the gate's trials, prints what it cost, and once every check holds writes the
two recordings again from what each agent's model answered, but only if nothing any model
wrote in either (its text, its calls' arguments, a call the provider could not read) breaks a
rule on personal data or client identifiers, or is text that is not plain: then neither file
is written, and the command says which and which rule. Neither model may be the one that
wrote the held-out cases' expected notes. The committed recordings were written so, once.
The hostile proposal checked at step 2 cites the gap report of the scripted cycle, by the
reference its words give it. A live recording holds the gap report its model wrote, in other
words and so under another reference, so in the recorded run that proposal fails the support
check as well as the guardrails it is written to fail. The page shows every check's result. Two live runs of the cycle differ in what their models wrote (a disagreement's
words, the order of what a change rests on, a gap report's summary and so its reference),
so each ends in an end state of its own; a replay of one recording always ends in that
run's.

A date is triaged by hand with the command below, from the repository's folder. After
`make cycle` it fails, as the replay has given the cycle's replies already; after
`make live` it asks the configured model.

```bash
uv run playbooks triage --date 2026-10-15 --cutoff 2026-10-31T16:00:00Z
```

```bash
uv run playbooks triage --date 2026-10-09 --cutoff 2026-10-31T16:00:00Z
```

Add `--json` for the whole run. The command exits with 1 unless the run completes.

| Command | What it does | Whose token |
| --- | --- | --- |
| `playbooks triage` | Triages a date and cutoff, or starts its stalled run again | The analyst's |
| `playbooks record` | Lists every proposal with each run of the checks on it and its approval or refusal, then every ledger line: what, when, who and why | The analyst's, or the playbook owner's |
| `playbooks retire` | Retires the version in use. The one before it is in use again | The playbook owner's |
| `playbooks intake <file>` | Takes a memo in as a firm source, with its scope | The playbook owner's |
| `playbooks correct` | Records a correction against a note, then waits for the agent's review | The analyst's |
| `playbooks request-review` | Asks again for a correction's review | The analyst's |
| `playbooks gap-reports` | Lists the gap reports and their states | The analyst's, or the playbook owner's |
| `playbooks dismiss` | Dismisses an open gap report | The playbook owner's |
| `playbooks curate` | Assembles and hashes a bundle and runs the curator on it, or starts its stalled run again | The playbook owner's |
| `playbooks gate --proposal <id>` | Runs every check on a drafted proposal and records each result | The analyst's, or the playbook owner's |
| `playbooks approve-founding` | Shows each founding version file and the memo it rests on whole, then asks for the founding line's hash to approve them | The playbook owner's |
| `playbooks approve --proposal <id>` | Shows each changed step next to the exact words of what it rests on, and the model the held-out trials ran on (saying so when it is a replay, which is still allowed), then asks for the proposal's hash to confirm | The playbook owner's |
| `playbooks refuse --proposal <id> --reason <code>` | Refuses a proposal that is not yet decided | The playbook owner's |
| `playbooks check <file>` | Runs the fixed-rule checks on a proposal file and records the result. The proposal can never be approved | The analyst's, or the playbook owner's |
| `playbooks rebuild` | Makes the whole database again from the two logs | The playbook owner's |
| `playbooks cycle` | Runs the recorded cycle once, as the page's button does, and follows it to its end | None: the button needs no sign-in |

`--as analyst` or `--as owner` sends that person's token in place of the command's own.

```bash
uv run playbooks record
```

```bash
uv run playbooks retire --playbook feed-outage --version 1 --reason source_withdrawn
```

A retirement cannot be taken back. It stays through `make down`, until a make target starts
the layer again on fresh volumes. With no earlier version, triage of that warning says
that no approved step applies.

The reason is one of four, so no free text, and with it no personal data, can enter a
record that is only ever added to:

| Reason | When |
| --- | --- |
| `source_withdrawn` | The firm source it rests on was withdrawn |
| `wrong_action` | Who is told, by when, or the hold instruction is wrong |
| `no_longer_applies` | The rule is no longer the firm's |
| `put_in_use_in_error` | It should not have been put in use |

## Firm sources, corrections and gap reports

```bash
uv run playbooks intake sources/memos/custodian-b-feed-escalation.md
```

A memo starts with its front matter: `id` (beginning `memo-`), `date`, `desk` (one of
`playbooks/desks.yaml`), `kind: memo`, and `scope`. The scope is `none`, for a notice that
changes no rule, or a playbook, a warning code and a `when` in the values a step uses.
Whoever takes the memo in answers for that scope: read it first. The ledger keeps the
fields, the text and the hash of the file's bytes. A changed memo needs an ID of its own.

`playbooks triage` prints the run's ID, and the note's under each note with a step. A
correction names both. Put the run's and the note's in place of `RUN` and `NOTE`:

```bash
uv run playbooks correct --run RUN --note NOTE --field hold_report --should-be no --reason "A missing cross-check alone is no reason to hold an advisor's report."
```

`--field` is `tell` (a desk's ID), `by` (one of the three forms of a deadline) or
`hold_report` (`yes` or `no`). Only a note with a step chosen, in a completed run, can be
corrected. The correction stands whether or not the agent reviews it. If the agent could
not be started, or its review stopped, ask again at once. If it was started and nothing is
heard, ask again after five minutes:

```bash
uv run playbooks request-review --correction CORRECTION
```

```bash
uv run playbooks gap-reports
```

```bash
uv run playbooks dismiss --gap-report REPORT --reason duplicate
```

A dismissal's reason is one of `not_a_gap`, `duplicate`, `already_covered` and
`out_of_scope`.

Four inputs are text. A proposal is described under "The curator". The other three are a
memo (at most 4000 characters), a correction's reason and a gap report's summary (one
line, at most 300 each). Each is refused, with a ledger line that
keeps the rule and none of the text, when it breaks one of these:

| Rule | What it refuses |
| --- | --- |
| `too_long` | A text over its bound |
| `not_plain_text` | Hidden characters, look-alike letters and any other character that is not plain text; a line break where one line is asked for |
| `client_identifier` | WealthContext's identifiers by shape, run and trace IDs, and six digits or more that are not a date |
| `personal_data` | Email and phone shapes, and the invented names and titles on this repository's list |
| `addressed_to_an_agent` | The listed words addressed to an agent, such as an instruction to ignore instructions |

The refusal says the rule and repeats none of the text.

## The curator

```bash
uv run playbooks curate
```

The backend assembles the bundle and records its manifest: each item's kind, reference
and hash, and the hash of the whole.

| Kind | What it holds | Its hash is of |
| --- | --- | --- |
| `firm_source` | A memo's date, desk, scope and text, or a correction's fixed fields and reason | What intake recorded: a memo's file, a correction's content |
| `gap_report` | The open reports with one content, once, with a count: playbook, version, code, feed facts and summary | That content |
| `playbooks_in_use` | The desks' IDs, and each playbook in use with its version and steps. A version with no recorded approval shows no action | That content |
| `triage_outcome_summary` | From completed runs: counts by warning code and step chosen, and counts by code of exceptions no step covered | That content |

The ledger keeps the bundle whole, so a run started again is given the same one. A source
or report that was refused is in no bundle, and neither is a report that is cited or
dismissed.

Before the curator is asked, each item's hash, and the hash of the words of each text in
it, is compared with every file under `cases/` and every file that grades cases. On a
match the command answers 500, the ledger says which item, and no run starts. A stalled
run whose bundle matches when it would be started again ends as failed, so that the next
command assembles a new bundle. It answers 500 with no ledger line, and changes nothing,
when `cases/` cannot be read. The setting is `PLAYBOOKS_CASES`.

The curator answers with one of two tool calls: a proposal, or no change. A proposal is:

- `changes`: each is `add`, `change` or `remove`, with the playbook, the step, the version
  it applies to (null only for a new playbook), the step's fields unless it is removed, and
  `rests_on`, the references of the firm sources and gap reports it rests on;
- `disagreements`: the references of the items that disagree, and one line on what about.

It is refused with one line when it is out of that form, names a desk that is not on the
list, names an item or a step that is not in the bundle, names a gap report that is no
longer open, or when any of its text breaks a rule of the table above (300 characters is
the bound, and 160 for a step's line). The model reads the refusal and answers once more.
After a second refusal the run fails and says why. A recorded proposal is `drafted`: it is
not in use until the gate passes it and the playbook owner approves it.

One curator run works at a time. If it was started and nothing is heard for five minutes,
`playbooks curate` starts it again: the same run and bundle, under a new attempt.

The page is at http://127.0.0.1:8100/. Set `PLAYBOOKS_PORT` before `make page` or `make cycle` to move it.

A replay gives each recorded reply once, for as long as its agent runs. After `make cycle`,
a triage asks again what the cycle has already been given, and fails saying the recording
no longer matches. `recordings/scripted-triage.json`, which `make correction` and
`make curation` replay, holds replies for one triage of each of the two dates and the
review of the committed correction, and for a few triages of the other dates from
2026-10-01 to 2026-10-18, where the warnings that no playbook covers get a scripted
"no gap report". A request holds no date, so 2026-10-10 and 2026-10-11, where the same
cross-check is still missing, replay too. A run that asks anything else fails and says the
recording no longer matches.

## The gate

```bash
uv run playbooks gate --proposal <the id that curate printed>
```

The gate runs every check on a drafted proposal, against the bundle its curator was given
and the playbooks in use now. Each check runs whatever another found, and each failure
says which change, which step and which rule, and repeats none of the proposal's words.
The ledger gets one line with every result, the playbook set's ID, version and hash, the
bundle's hash, and what a ruling will be judged by: each changed outcome that sources
dispute, by the rules or as the proposal lists it, with the change, the sources that speak
there and what supports the change there. The trials are given the steps in the order
triage will be given them once the proposal is approved. The gate writes no playbook file, so a failed gate leaves the approved
playbooks as they were.

| Check | What it refuses |
| --- | --- |
| `support` | A change that rests on nothing, on an item that is not in the bundle, or on notices alone; a step that fits no exception its warning code can have; a change that rests on nothing whose scope covers an exception its step fits. Then, by its effect: the backend works out which step applies, with what action, for every warning code and every condition an exception can have, before the proposal and after it. Each outcome that differs must lie in the scope of an item named by the change that made the difference: the same playbook, warning code and conditions (`outside_scope`, or `unlisted_outcome` where the change's own step no longer covers it). A gap report filed where no playbook covered its warning covers that warning in any playbook. A notice whose scope is `none` supports nothing. A memo that a later memo from the same desk replaces there supports nothing. Two items that both cover a changed outcome and disagree, a memo and a memo from another desk, or a memo and a correction or gap report, must be listed as a disagreement (`hidden_disagreement`), so that the playbook owner rules on it. They disagree whichever playbooks their scopes name, since one step at most fits an exception: naming another playbook hides no disagreement |
| `personal` | Personal data, a client identifier, text that is not plain or is too long, anywhere in the proposal, a step's `playbook/step` included; and a playbook's or step's name that is not lower-case words joined by hyphens |
| `guardrails` | A field out of its closed form (a desk not on the list, a deadline in none of the three forms, a line that is not one line of at most 160 characters); a figure in a line, in digits or in words; words in a line or a name that would tell someone to show a total on an incomplete household, carry an old price forward, leave out a warning, or give personal data to operations; and words addressed to an agent anywhere. A playbook's and a step's names are also read together, as triage shows them: `playbook/step` |
| `base` | A change to a version that is not the one in use, to a step that version does not have, or that adds a step it has already |
| `overlap` | A set of steps in use, after the proposal, in which two could fit one exception, across playbooks and through wildcards |
| `held_out` | A held-out case that does not give its expected notes: a regression case with the playbooks in use or with the proposal made, a new-rule case with the proposal made, or any trial of a case on the triage agent (below). Each failure names the case, the exception and the fields that differ, never the expected note |

A gate run that fails a check makes the proposal `gate_failed`, and the gap reports it
cited are open again. One that fails none makes it `gate_passed`. A proposal that failed
another check is not tried on the triage agent: its `held_out` result is then `not_run`,
unless the cases already fail by fixed rules.

### The held-out cases

[cases/regression/](../cases/regression) and [cases/new-rule/](../cases/new-rule) hold four
cases each. A case is one day's WealthContext envelopes (a quality report, its feed runs and
each failed run's impact, valid against WealthContext's published schemas), with the
exceptions the layer builds from them, in `<case>.yaml`; the expected notes of every case
of a kind are in `expected-notes.json`. Regression cases cover rules that are not changing:
they must give their notes with the playbooks in use and with any proposal. New-rule cases
state what the firm's later sources make a rule produce: they fail with the founding
versions, and must give their notes once a proposal is made.

The expected notes were written by Gemini 4 Neon, from the playbooks, the desks, the firm
sources and each case's inputs alone, and filed as it gave them, after a check of their
shape only. [cases/case-set.yaml](../cases/case-set.yaml) names that writer. A note says,
for each exception, whether a step applies, and if one does its playbook, who is told, by
when and whether the report is held. A step's ID is not graded: the writer cannot know what
a proposal will call a step it adds.

On each gate run, after the fixed-rule checks:

1. The case set is read and checked: four cases of each kind, each built from its envelopes
   as it lists, and an expected note for every exception. Its hash is of every file read.
2. Each case's notes are worked out by fixed rules, from the steps alone, with the playbooks
   in use and with the proposal made, and compared with the expected notes.
3. If every check has passed so far, the triage agent triages each case three times, in
   order, as if the proposal were in use. It is asked as for a triage run, its choices are
   checked and refused in the same way, and a note it cannot settle after two refusals
   fails the trial. A trial is never in the triage log, files no gap report (409, and
   nothing is stored) and is asked no question about one. A trial nothing is heard from
   for five minutes fails.
4. Each trial is a ledger line of its own as it ends, written before the agent is told it
   ended: what each note settled on, what it missed and every call, refused ones too. A
   miss stands: if the backend stops before the gate records its result, the next
   gate on that proposal records that gate run as failed, with its trials, and runs none.
   A gate whose agent stops starting trials after one missed, for any reason, records itself
   failed too.
5. The gate run's ledger line keeps the case set's hash, its writer, the model configured
   for the trials and each model their calls named, each case's grade both ways, and every
   trial.

The trials run on the model the backend's `TRIAGE_MODEL` names. The gate asks the triage
agent for each trial under that name, and the agent refuses before any model call when it
is configured with another. Each trial may spend what is left of `GATE_MAX_DOLLARS` (ten
dollars unless set), and the agent's own cap if that is less; the trials stop, and the check
fails, when the agent stops a trial for its cap or nothing is left. A cap that is not an
amount above zero stops the backend at start.

The gate does not run, records one ledger line saying why, and leaves the proposal
`drafted`, when:

| What | Answer |
| --- | --- |
| No model is configured for the trials (`TRIAGE_MODEL`), or it names no model this layer can record | 503 |
| The case set cannot be read, or does not hold together (four cases of each kind, each built as it lists, a note for every exception, no other file) | 503 |
| The triage agent cannot be reached, or refuses to start, before any trial missed | 503 |
| The model for the trials is the cases' writer, whatever its provider or effort | 409 |
| The proposal's curator run was configured with the writer at any attempt, or a call of it named the writer | 409 |
| A call of the curator run's last attempt named another model than the run recorded | 409 |
| The triage agent is configured with another model than `TRIAGE_MODEL` | 409 |
| A trial's call named another model than the one configured, or the writer | 409 |
| The playbooks in use changed while the gate ran | 409 |
| The gate is already running on that proposal | 409, with no line |

Where trials ran before the refusal, they are their own ledger lines, and the refusal names
the gate run they belong to.

`playbooks record` lists each refusal under its proposal, with its reason.

The record also lists the pairs of items that disagree about an outcome the proposal
changes: the rulings the playbook owner is asked for.

### A proposal handed in

```bash
uv run playbooks check cases/hostile/guardrails-warning-left-out.yaml
```

`playbooks check` hands a proposal file, YAML or JSON with `changes` and
`disagreements` as above, straight to the fixed-rule checks: all of the table's but the
held-out cases. Its support check runs against the bundle as it would be built at that
moment. The proposal is then `checked`: it is in the record with its result, it can never
be approved, and the gate does not run on it. Where any of its text breaks a rule on text,
the ledger keeps its hash and not its words. A file that changes nothing, names one step
twice, or lists a disagreement of fewer than two items is refused, and nothing is recorded.
The command exits with 1 unless every check passes.

[cases/hostile/](../cases/hostile) holds nineteen hostile proposals. Each is the honest
first-cycle proposal with one thing changed, and names the check and the rule that must
refuse it, and any other check that refuses it too. They are written for the first cycle's bundle before curation: the two founding
memos, the custodian B memo and the notice taken in, both dates triaged, and the correction
recorded with its gap report still open. The base case needs version 1 of the missing
cross-check playbook retired first, which it names.

## Approving the founding versions

The first ledger line founded the playbook set with two version 1 playbooks and no approval,
and it is never written again. The playbook owner approved them afterwards:

```bash
uv run playbooks approve-founding
```

It prints each version file whole, the founding memo it rests on in its own words, and the
desks, each with its hash, then asks for the founding line's hash. Anything else typed sends
nothing. The approval is one ledger line, `founding_approved`, naming what the first line
names: each version's ID, number and hash, the memo it rests on by hash, and the desks' hash.
It puts a new playbook set in use, so later receipts name it, and from then on a version 1
note gives its action. It is refused, changing nothing (409), when the hash is not the
founding line's, a founding version is no longer in use, a version file, a founding memo
or the desks are not as the first line names them, or the first line itself records an
approval. Sent again once recorded, it answers what it did the first time. It is the
playbook owner's alone: the analyst is refused with a 403, and an agent's key with a 401.
The committed ledger holds it, as its second line.

## Approving and refusing

Both are the playbook owner's alone. The analyst's token is refused with a 403. An agent's
key is no person's token, so it is refused with a 401, as on every operation of a person.
The backend does not start where two of the four secrets (the two tokens and the two keys)
are the same.

```bash
uv run playbooks approve --proposal PROPOSAL --rule-for 1=SOURCE
```

The command prints the proposal's hash, the gate run with each check's result, and then
each change: the step as the version it applies to has it, the step as proposed, and under
it the exact words of each source it cites, as the curator was given them (a memo's text,
a correction's fields and reason, a gap report's summary). A step of a version with no
recorded approval shows no action. Last come the disagreements, again in the sources' own
words. Then it names the model the gate run's held-out trials ran on, as the gate run
records it, and asks for the hash. Anything else typed approves nothing and sends nothing.

Where that model is a replay (`replay:<file>`), the command says so plainly before it asks:
the trials' replies were read from a file, and no live model ran these trials (a recording may
hold what a live model answered once, as the recorded cycle's does). A real promotion
should rest on held-out trials run on a live model. Approving on a replay is still allowed:
nothing is refused, and no flag is needed to go on. The recorded cycle approves on replayed
trials by design, and its page names the trials' model at the gate's step.

The approval names the proposal's hash and the gate run. The answer is one of these:

| What | Answer |
| --- | --- |
| The hash or the gate run is not this proposal's | 409, and nothing changes: it is still `gate_passed` |
| The playbooks in use are not the ones the gate ran against: any approval or retirement since | 409. The proposal is `stale`, with one ledger line, and its gap reports are open again. The gate must run again, on a new proposal |
| A disagreement has no ruling, or the ruling cannot stand (below) | 422, and nothing changes |
| Its gate run's record holds nothing to judge a ruling by | 409, and nothing changes. Refuse it and curate again, so that the gate runs on a new proposal |
| The proposal is not `gate_passed` | 409, naming its state. A checked proposal can never be approved |
| It is approved already, and what is sent is the same | The first approval's answer, and no second line |
| Otherwise | Each changed playbook's next version is written, then one ledger line puts them in use, keeps the ruling and resolves the gap reports the proposal cited |

Two proposals that passed the gate against one playbook set cannot both be approved: the
first changes the set, and the second goes stale. So the set that goes into use is always
one the gate tested whole. The same approval sent again gets the first one's answer.

**The ruling.** Where the proposal lists a disagreement, or the gate found two sources that
disagree about an outcome it changes, you rule on it. Run the command once to read the
list: it numbers each disagreement, with the change it is about. Press Enter in place of
the hash, and nothing is sent. Then run it again with `--rule-for <number>=<source>` once
for each number. The numbers are the same each time for one proposal.

- Each pair of sources the gate found to disagree is ruled on for each change whose
  outcome they disagree about. So one change can follow one desk's memo and another change
  another's. A wider disagreement that the proposal lists does not settle a pair inside
  it. One the proposal lists that is no more than the pairs the gate found is ruled on as
  those pairs; any other, for each change that rests on some of it.
- A correction or a gap report never overrides a memo by itself. So if you rule for the
  memo where a change rests on that correction or gap report, the proposal is not
  approved: refuse it.
- At each outcome, a change must rest on some source there that you have not ruled
  against. One change's winner, or another disagreement's, does not stand in for it. If
  none is left, refuse the proposal.
- A disagreement that only the proposal lists is judged by scope in the same way: the
  source you rule for beats another at an outcome where it speaks. And a change that rests
  on some of such a disagreement must rest on the source you rule for.

**A version's file** is the playbook's steps after the change, in the order the trials
were given them, under a number one above every one the playbook has had. The ledger line
names its hash and what it rests on. A version with no step is what is left when an approved change removed the last: then no
approved step applies to that warning, and retiring it brings the version before back.

```bash
uv run playbooks refuse --proposal PROPOSAL --reason not_needed
```

A drafted proposal, one that failed the gate and one that passed it can be refused, though
not while the gate is running on it. One ledger line keeps the reason. The gap reports it
still had cited are open again: one that failed the gate opened them then, and refusing it
leaves a report that another proposal has cited since as it is. The reason is one of four:

| Reason | When |
| --- | --- |
| `wrong_change` | What it would have the playbooks say is wrong |
| `against_the_sources` | It does not follow the firm sources, or your ruling on them |
| `source_not_trusted` | A source it rests on is not to be trusted |
| `not_needed` | The change is not needed |

`playbooks record` shows under each approved, refused or stale proposal what changed (or
would have), why (the sources and gap reports it rested on, by hash, with the ruling or
the reason), on what evidence (each check's result) and who decided.

## The records, and getting them back

| What | Where in the containers | Setting |
| --- | --- | --- |
| The playbook folder: desks, version files and the ledger | the `store` volume, at `/app/playbooks` | `PLAYBOOKS_FOLDER` |
| The triage log | the `records` volume, at `/data/triage-log.jsonl` | `PLAYBOOKS_TRIAGE_LOG` |
| The database | the `records` volume, at `/data/playbooks.sqlite` | `PLAYBOOKS_DATABASE` |

The triage log also holds the secret that keys a gap report's subject digest. The digest
only tells two reports apart, is never shown, and cannot be turned into a subject without
that secret.

The tables are no record of their own: a backend that finds tables of an earlier schema
makes them again from the two logs at its start.

The `store` volume is filled from the committed `playbooks/` the first time only. After
that the volume is the record: a later image does not change it. Outside containers the
folder can be the repository's own `playbooks/`, and each change then leaves files to
commit. The triage log and the database may not be in that folder, because they name
accounts and households.

Each move is one line in its log, written and flushed before the database commits. The
line is the commit point: a move interrupted before it left no trace, and one interrupted
after it is applied at the next start.

**Back up the playbook folder and the triage log together.** Everything in the database
comes from them. If the database is lost, start the backend and it makes the database
again. If it is lost while the backend runs, every move is refused with a 500 and writes
nothing (SQLite will not write to a database file that was deleted), while reads still answer
from what it held, until you run:

```bash
uv run playbooks rebuild
```

At start, and before a rebuild, the backend checks every line of both logs and every file
the ledger names. It refuses to go on, and says which file or line, when:

- a version file or the desks file has changed or is gone;
- a line has changed, is out of order, or does not follow the line before it;
- a log is shorter than the database has applied, or is not the log the database applied;
- two steps in use would overlap;
- the database holds records that no log holds;
- another backend holds the same ledger or triage log.

A line a crash cut short is removed, and so is a version file that no ledger line names,
with its folder when no line names that playbook. An approval cut short before its line
therefore leaves the old versions in use and no stray file.
Keep your own drafts out of `playbooks/<playbook>/<number>.yaml`.

## When something goes wrong

| What happens | What the layer does |
| --- | --- |
| WealthContext is down or refuses a read | Answers 502 and writes nothing |
| The triage agent is down or refuses to start | Answers 503 and undoes the run |
| The curator is down, has no model, or has another model than the backend would record | Answers 503 and undoes the run |
| A bundle's item is a held-out case, or a file that grades them | Answers 500, records which item, and does not ask the curator |
| The curator's second answer does not hold | Fails the run, with the last refusal as its reason |
| The agent does not answer in time | Keeps the run `working`, and says so |
| The step that fits is from a version with no recorded approval | Names the step, and gives no action |
| The agent cannot be asked to review a correction | Keeps the correction, says so, and lets the analyst ask again |
| A review stops on an error or its cost cap | Records why, and lets the analyst ask again |
| A gap report's second answer does not hold | Files none: the run still completes, and a review ends saying so |
| A choice does not hold | Refuses it with one line, which the model reads, and records the call |
| A second choice does not hold | Ends the note "No verified choice; needs a person.", with no action |
| Nothing is heard from a run for five minutes | Shows it as stalled |
| The playbooks in use change between a gate run and its approval | Refuses the approval, and the proposal is stale |
| The playbooks in use change while a run works | Refuses to settle its pending notes, so the run fails |
| A file or line of the records is changed | Refuses to start, and says which |

To start a stalled run again, the analyst runs the same `playbooks triage` command. It is
the same run under a new attempt:

- WealthContext is not read again, and the receipts and playbook versions stay the run's
  own;
- if the playbooks in use have changed since it started, none of its pending notes can be
  settled, so no action of a retired version is given out. The run ends as failed, and
  running the command once more triages under the playbooks now in use;
- a note already settled stays settled, and a refusal already made still counts;
- a move from the attempt before is refused, and a tool call among them is recorded as a
  refused step. The model that made it reads the refusal once, and nothing it answers
  after that can settle the note;
- if the agent cannot be reached, or is still working the run, starting again is undone:
  the run stays stalled and can be started again. The attempt's number is spent all the
  same, so a late answer from the attempt before stays refused.

While a run of that date and cutoff is working and has not stalled, the command is refused
with a 409. `--json` shows every recorded step, refused ones included.

## WealthContext is used unmodified

`make cycle` fetches the public repository at commit `a181b3359d12` into `.wealthcontext/`
and refuses to run if that checkout has any change. An override file outside the checkout
gives it a Compose project, image tag and volume of its own, and no host port, so it does
not touch another WealthContext on the same machine.

The operations token comes from WealthContext's own `wealthcontext token --persona OPS-01`
command and is written to `.local/backend.env`, which only the backend is given. The
client refuses to start on a token for anyone else.

## A live run

Copy `.env.example` to `.env` and set `TRIAGE_MODEL` and the key of its provider
(`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`), then:

```bash
make live
```

The key is read from that file only, never from your shell. A run is capped at two
dollars: before each call the agent works out the most that call could cost, retries
included, and stops if that could pass the cap. A call whose provider reports no usage is
recorded at the most it could have cost, and so is a try that brought no answer back (a
lost connection, a timeout), on a refused step of its own, before the run ends failed: a
lost answer may still have been billed. An error the provider answered with is charged
nothing. A run started again
counts what its earlier attempts recorded as spent. It cannot count a call whose cost was
never recorded, as when the agent died during one, or one an earlier attempt records after
the new one began. A provider's error is logged by its kind alone, never its words. Only
models priced in `modelkit/prices.py` can run. Live runs have been made;
besides the recorded cycle (above) and the evidence runs (below), none is published.

A model is set as `provider:model`, for example `anthropic:claude-haiku-5-5` or
`openai:gpt-6-luna`. An OpenAI model takes a reasoning effort as the setting's last part,
`openai:gpt-6.1-terra:high`: the efforts each takes are listed beside its price. With none
named, none is sent and the model's own default applies (GPT-6 Luna's is medium, by OpenAI's
reasoning guide). The setting is recorded whole wherever a run names its model, so the
effort is named with it. A setting that names an effort its model does not take, or any
effort for an Anthropic model, is refused before anything starts.

An OpenAI model is asked on OpenAI's Responses API, with nothing stored at OpenAI, at
Standard processing (the tier its prices are for), for exactly one call on a strict schema.
It is sent to OpenAI's address with the key its setting names, and no proxy or certificate
setting of the machine's is used; it is not built at all while its agent's environment holds
an `OPENAI_` variable other than its key, which OpenAI's client would read in place of ours. Its thinking counts against its output limit and is
billed as output, so its limit is the agent's own (4,096 tokens for triage, 8,192 for the
curator) and the 25,000 OpenAI recommends reserving for reasoning, and a call may take two
minutes. Each call's bound, and so each cap, counts that whole limit.

A call is tried again only when the provider answered that it was busy or failed on its side
(408, 409, 429 or a 5xx), which it bills nothing for: three tries at most, two for a reasoning
model. A try that brought no answer back may still have been billed, so it is not tried again:
its bound is charged and the work fails, for a person to start again. A reply cut off at the
limit has no call the agent can use, even one whose arguments happen to read, and is charged
what the provider reported; a recording keeps it so too.

For a live curator run, set `CURATOR_MODEL` and its provider's key in the same file, then:

```bash
make live-curation
```

Triage stays on scripted replies there, so that only the curator's call costs anything,
and the bundle is the first cycle's. The run prints the proposal, tokens, time and cost,
under the same cap of two dollars. Each curator run records the model it was configured
with. Never configure the model that wrote the held-out cases' expected notes: a curator
is not tested by its own answers. Every make target that runs a model refuses it before
anything starts, and the backend refuses (409) to start a curator run on it.

## The evidence runs

The recorded cycle shows that the parts connect, not how well a model curates. The
evidence runs are for that: five scenarios, each run three times on a live curator, and
every run published in [evidence.md](evidence.md) with its models, settings, proposal,
each check's result and the outcome, misses included. The scenarios are in
[evidence/scenarios.yaml](../evidence/scenarios.yaml), and what the runs rest on is frozen by
hash in [evidence/freeze.json](../evidence/freeze.json): no run starts if any of it has
changed. With `TRIAGE_MODEL`, `CURATOR_MODEL` and each one's provider's key in `.env`:

```bash
make evidence
```

The runs were made once, on 2026-11-24: all fifteen, with `openai:gpt-6.1-terra:high` as
the curator and `openai:gpt-6-luna` in the held-out trials (each scenario's own triage is
scripted), for $0.147193 in all by the price table. Each is published in [evidence.md](evidence.md), its
record in [evidence/runs/](evidence/runs), misses included. `make evidence-rehearsal` makes
the same runs with no key, on scripted replies only, under `.local/evidence/`, to show
that the command runs.

## Limits

- **An agent's way out to its provider is a network with egress, not one host.** Only the
  agent whose model is live is put on it, but from there it could reach any host: nothing
  limits it to `api.openai.com` or Anthropic's API.

- **The playbooks, memos and desks are synthetic** and describe no real firm.
- **The recorded cycle is one live run,** captured once on 2026-11-24 from GPT-6 Luna and
  GPT-6.1 Terra at high. It shows the cycle running end to end on those models, not how
  often a model triages, files or curates well.
- **The evidence runs measure the curator on fixed inputs.** Each scenario's triage replies
  are scripted, so that its bundle is the same each run: a run says nothing of how a model
  files gap reports. Five synthetic scenarios, three runs each, are a small sample.
- **Every scenario is graded on the first cycle's held-out cases.** A scenario whose bundle
  holds neither the correction nor its gap report cannot pass the held-out check with a
  proposal that changes only what its sources say;
  [evidence.md](evidence.md#how-a-run-is-made) says which cases and why.
- **A step has fixed fields, so the backend can check a choice completely.** A model's
  part in triage is choosing right first time. A second refused choice gives a note that
  says "No verified choice; needs a person." and shows no action.
- **A hold instruction is words for a person.** Nothing here blocks anything in
  WealthContext, or writes to it.
- **WealthContext caps each list at 20 entries.** If its feed runs or a failed run's
  affected accounts are cut short, triage refuses and writes nothing. It does not guess
  at the rest.
- **The checks on text catch shapes and listed words, and no more.** They cannot catch a
  name they have never seen, an account number of fewer than six digits, an amount of
  money in a reason or summary, or an instruction in words that are on no list. They also
  refuse some honest text, such as "the agent should wait" or a name in another script:
  say it another way.
- **A poisoned memo can be taken in.** Intake does not judge what a rule says. Only the
  gate and the playbook owner stand between it and a playbook.
- **An approval is as good as the owner's reading.** The backend holds it to the exact
  proposal, gate run and playbook set, and a ruling to what supports each change at each
  outcome it changes. It does not read what a memo means: where a change rests on both
  sides of two memos, it cannot tell which side the change follows. A ruling is for a
  change whole, so one change cannot follow one source at one outcome and the other
  source at another. A dishonest playbook owner is outside what this protects against.
- **An approval can rest on replayed trials.** A layer whose triage model is a replay passes
  the gate on recorded replies, and the playbook owner can approve on it. `playbooks approve`
  says so before it asks for the hash, and the record names the trials' model, but nothing
  refuses it.
- **An approval does not read what arrived after the gate.** A memo taken in later changes
  no playbook set, so it makes no proposal stale. The owner can refuse and curate again.
- **An approval resolves the gap reports the proposal cited, and no other.** One of the
  same content filed since stays open.
- **The held-out cases' expected notes are a model's reading of the firm sources.** Gemini 4
  Neon wrote them. If it and the curator misread a memo the same way, and the person who
  signs the case set off does not notice, the gate passes the mistake.
- **A step's fields decide a triage note.** The backend checks a trial's choice both ways,
  so a trial can only miss by a note it could not settle or a run that stopped. What the
  trials measure is whether the model gets there.
- **The writer is matched by its model's name.** A setting is the writer's when its letters
  and digits hold `gemini4neon`. A replay is known only by its file's name.
- **The comparison with the held-out cases is by hash.** It sees a whole file, also with
  its spaces and line breaks changed. It does not see part of a case, or one reworded.
- **The support check reads scopes, not meanings.** A change inside the scope of what it
  cites passes, whatever the source says: a memo about custodian B's failed feed supports
  any action for custodian B's failed feed. The held-out cases and the playbook owner stand
  there. A hostile file is written for one bundle, and against another it can fail more
  checks than the one it names.
- **The forbidden instructions are closed lists of words.** A line that speaks of a total,
  an old price, leaving out a warning, or a person's name or address is refused, even with
  "do not" before it. A rewording on no list gets by.
- **The two agents' keys are trusted to name their models.** The name is one `provider:name`
  with no spaces, and passes the same checks as any text, so a model or a recording whose
  name holds six digits or more, a dated one for instance, is refused.
- **The checks on a playbook line are closed lists.** They refuse digits and the listed
  number, currency and percentage words. They cannot catch every way to word a figure.
- **The hashes show a change. They do not prevent one, and they are not signatures.**
  Someone who can write the files could write a whole chain again, or cut lines off its
  end. The backend sees that only while it still has the database that applied the old
  lines. With the database gone too, it cannot.
- **There is no backup.** The records are in two Docker volumes on one machine.
- **One backend at a time, and one transaction at a time.** The backend takes the logs'
  locks when it starts.
- **The founding versions' approval is the playbook owner's word, kept as a ledger line.**
  It names what the founding line names by hash. Like every hash here, it shows a change and
  is no signature: it holds who approved as the layer's playbook owner role, not as a person.
- **A run whose agent stops mid-way waits for a person.** Nothing starts it again by
  itself, and it cannot be started again for five minutes.
- **The servers keep no access log.** One would keep each path and query as it was sent,
  with whatever a caller put there. What happened is in the ledger and the triage log.
- **Sign-in is development tokens on loopback.** The page needs no sign-in and shows what
  operations may see.
