# AI Agent Governance Policy - Draft

Owner: AI Operations / Governance
Last Reviewed: 2026-03-01
Status: Draft

## Purpose
Define guardrails for using AI agents in operations.

## Rules
1. AI may recommend, but humans approve high-risk finance decisions.
2. AI outputs must show source references.
3. AI must flag missing or conflicting context.
4. AI must not fabricate approval authority.
5. High-risk outputs must be logged for eval.
6. Feedback must be reviewed weekly.
7. Any repeated failure pattern must create an improvement backlog item.

## High-Risk Categories
- refunds above approval threshold,
- bankruptcy,
- suspected fraud,
- legal escalation,
- customer-impacting launch communications,
- financial write-offs,
- strategic accounts.

## Eval Metrics
- answer accuracy,
- source match rate,
- confidence calibration,
- missing context rate,
- escalation correctness,
- time-to-resolution estimate,
- user feedback score.
