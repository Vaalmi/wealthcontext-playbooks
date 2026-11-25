# Rules, threats and limits

What this layer holds to whatever else happens, what it is built to protect against, and
what its checks cannot do. [running.md](running.md) has the commands and the full list of
limits; [evidence.md](evidence.md) has the evidence runs.

## The rules

- **Three jobs, kept apart.** WealthContext decides the facts, who may see them, and the
  math. Playbooks decide the firm's response. Models choose, propose and explain. This
  layer reads WealthContext as its operations persona and writes nothing to it. A triage
  note names a warning's code and subject as WealthContext returned them, and holds no
  figure. A playbook step has no field for a figure, a permission or a status, and the gate
  refuses a step whose one line of wording holds a figure.
- **The curator proposes. The gate and a person decide.** A proposal is never in use. It
  goes into use only when every check of the gate has passed and the playbook owner then
  approves it, by its hash. Each person's operation takes that person's token: the
  analyst's is refused where the owner's is needed (403), and an agent's key is no
  person's token (401). So no agent promotes its own change, and the analyst who triages
  with a playbook does not approve it.
- **Approved actions only.** The triage agent chooses a step from the closed list it is
  offered, and the backend checks the choice against the step's fixed fields. What a
  person is told to do comes from the step, never from the model. A version with no
  recorded approval gives no action: the note names the step and says so. The founding
  versions have the playbook owner's approval, recorded in a ledger line of its own.
- **Which source wins.** A later dated memo from the same desk replaces an earlier one. A
  correction or a gap report never overrides a memo by itself. Where sources disagree
  about an outcome a proposal changes, the proposal must say so, and the playbook owner
  rules on each disagreement for each change it is about.
- **Nothing learned from the test.** The curator never sees the held-out cases or how they
  are graded. Only the backend's image holds them. Before the curator is asked, its bundle
  is compared by hash with every case file and every file that grades them, and a match
  stops the run. The model that wrote the cases' expected notes may be neither the curator
  nor the model the cases are run on.
- **Synthetic data only.** The memos, the correction, the playbooks and the desks were
  written for this repository and describe no real firm. WealthContext's data is synthetic
  too.
- **No claim of compliance.** This is a demonstration, not a production system, and not a
  financial-advice product.

## What it protects against

| Threat | What stands against it |
| --- | --- |
| A poisoned or mistaken source becoming a rule that weakens a WealthContext guarantee | The gate's guardrails refuse a step whose fields or wording would tell someone to show a total on an incomplete household, carry an old price forward, leave out a warning, or give personal data to operations; the held-out cases; the playbook owner. The recorded cycle takes in a poisoned memo and shows that nothing is approved from it |
| A change no source supports reaching a playbook | The gate's support check: each change must rest on items of the curator's own bundle whose scope covers every outcome it changes, worked out for every condition an exception can have, so a change that reaches beyond what it rests on is refused; a notice that changes no rule supports nothing |
| Instructions hidden in a memo, a gap report or a proposal steering the curator, the triage agent or a later triage run | Text addressed to an agent is refused when a memo is taken in, a correction recorded, a gap report filed or a proposal recorded, and at the gate. A step is fixed fields and one line, and the triage agent chooses among steps; it is never given a memo |
| Personal data or client identifiers leaking into playbooks, gap reports or proposals | The same text rules at each of those entries (identifiers by shape, the synthetic names and titles, email and phone shapes), and the gate's personal check over the whole proposal. Neither agent is given a client identifier |
| An agent, or the analyst, approving their own change | Approving, refusing and retiring take the playbook owner's token; the backend does not start when two of its secrets are the same |
| A misled agent filing many gap reports to fake a pattern | One gap report at most for a trace, warning and subject, filed only on a triage run recorded here. The curator sees reports of one content once, with their count. A report supports a change only within its scope, however many there are, and the held-out cases and the playbook owner still stand between it and a playbook |
| An approval landing on something other than what was checked | An approval names the proposal's hash and the gate run that passed it. If the playbooks in use have changed since, the proposal is stale and the approval is refused |
| The curator tuning the playbooks to the test | The curator never sees a held-out case (above), and the gate runs the cases on the triage agent, not on the curator |

**Out of scope:** production sign-in and more than one firm, a dishonest playbook owner, a
compromised WealthContext, and real company sources such as chat or wiki connectors.

## What the checks cannot do

Fixed rules catch identifiers by their shape, the known synthetic names and titles, and a
closed list of forbidden instructions. They cannot catch a name they have never seen in
free text, or a forbidden instruction reworded cleverly enough. That is why a playbook step
is fixed fields with one short line of wording, and why a person approves.

The support check reads scopes, not meanings: a change inside the scope of what it cites
passes, whatever the source says. The held-out cases stand there, and the playbook owner.

A held-out case's expected notes are a model's reading of the firm sources. Gemini 4 Neon
wrote every one, from the firm sources and each case's inputs alone, and they were filed
as it gave them; [cases/case-set.yaml](../cases/case-set.yaml) names it. The case set was
read by a person and signed off as written on 2026-11-23, by its hash. If that model and the
curator misread a memo the same way, and the person who signed the set off did not notice,
the gate passes the mistake.

No model grades anything. The cases are graded by fixed rules, and a triage note by its
step's fields.

The full list of limits, each check's included, is in [running.md](running.md#limits).
