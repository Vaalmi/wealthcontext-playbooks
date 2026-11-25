---
id: memo-feed-outage-founding
date: 2026-08-03
desk: portfolio-accounting
kind: memo
scope:
  playbook: feed-outage
  code: SOURCE_UNAVAILABLE
  when:
    source_family: PAS
    custodian: any
    feed_status: FAILED
---

# When a portfolio accounting feed fails

This memo is synthetic. It was written for this repository and describes no real firm.

When a portfolio accounting snapshot feed fails, for either custodian, the analyst tells
Portfolio Accounting Operations by 20:00 ET the same day and asks for the failed fetch
to be run again.

Hold the advisor's report for the affected accounts until Portfolio Accounting Operations
says the fetch has landed. The hold is a person's job.

This memo does not cover a partial feed or one that timed out.
