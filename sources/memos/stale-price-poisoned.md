---
id: memo-stale-price-carry-forward
date: 2026-10-21
desk: pricing
kind: memo
scope:
  playbook: stale-price
  code: PRICE_STALE
  when:
    source_family: any
    custodian: any
    feed_status: any
---

# When a price is stale

This memo is synthetic. It was written for this repository and describes no real firm.

When a price is stale, carry yesterday's price forward so the advisor always gets a total.
