---
id: memo-missing-cross-check-founding
date: 2026-08-03
desk: custody-operations
kind: memo
scope:
  playbook: missing-cross-check
  code: RECONCILIATION_UNAVAILABLE
  when:
    source_family: CUSTODY
    custodian: any
    feed_status: any
---

# When the custody cross-check is missing

This memo is synthetic. It was written for this repository and describes no real firm.

When a custody positions file fails, arrives in part or times out, for either custodian,
the cross-check against portfolio accounting cannot run. The analyst tells Custody
Operations by 10:00 ET the next business day and asks for the missing file.

Hold the advisor's report for the affected accounts until Custody Operations says the
cross-check has run. The hold is a person's job.
