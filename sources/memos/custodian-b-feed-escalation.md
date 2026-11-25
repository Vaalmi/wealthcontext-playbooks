---
id: memo-custodian-b-feed-escalation
date: 2026-10-19
desk: portfolio-accounting
kind: memo
scope:
  playbook: feed-outage
  code: SOURCE_UNAVAILABLE
  when:
    source_family: PAS
    custodian: B
    feed_status: FAILED
---

# When custodian B's portfolio accounting feed fails

This memo is synthetic. It was written for this repository and describes no real firm.

Custodian B now runs a service desk that can start a failed fetch again. From the date of
this memo, when custodian B's portfolio accounting snapshot feed fails, the analyst tells
Custodian B Service Desk within 2 hours and asks for the failed fetch to be run again.

This replaces the founding memo for custodian B only. A failed feed from custodian A
still goes to Portfolio Accounting Operations by 20:00 ET the same day.

The hold does not change: hold the advisor's report for the affected accounts until the
fetch has landed. The hold is a person's job.
