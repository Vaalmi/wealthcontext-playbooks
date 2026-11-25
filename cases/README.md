# Cases

The held-out triage cases: cases used only to test a proposed change to the playbooks. The
curator never sees one. Before a curator is asked, the backend compares its bundle by hash
with every file here and does not start if one matches.

`regression/` and `new-rule/` hold four cases each: `<case>.yaml` is a day's WealthContext
envelopes and the exceptions built from them, and `expected-notes.json` holds the notes every
case of that kind must give, as their writer gave them. `case-set.yaml` names the writer, a
model that may never run the cases it wrote. The gate reads them as a set, records the
set's hash, grades them by fixed rules and runs each three times on the triage agent.

Do not edit an expected note: a change to the case set is a new set, with a new hash, to be
signed off again.

Do not put a firm source here, and do not copy a case into `sources/`.

`hostile/` holds hostile proposals: proposals that the gate's checks must each refuse, by
the check and the rule each one names, and by no check it does not name. They are handed in
with `playbooks check`, never given to the curator, and are compared with each bundle like
every other file here.
