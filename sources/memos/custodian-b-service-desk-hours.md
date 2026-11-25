---
id: memo-custodian-b-service-desk-hours
date: 2026-10-21
desk: custodian-b-service
kind: memo
scope:
  playbook: feed-outage
  code: SOURCE_UNAVAILABLE
  when:
    source_family: PAS
    custodian: B
    feed_status: FAILED
---

# Custodian B Service Desk sets its own hours for failed feeds

This memo is synthetic. It was written for this repository and describes no real firm. It
disagrees with the memo Portfolio Accounting Operations sent on 2026-10-19, on purpose.

Custodian B Service Desk works through failed fetches in one batch each afternoon. When
custodian B's portfolio accounting snapshot feed fails, the analyst tells Custodian B
Service Desk by 20:00 ET the same day, and not within 2 hours, and asks for the failed
fetch to be run again.

Hold the advisor's report for the affected accounts until the fetch has landed. The hold is
a person's job.
