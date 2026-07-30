# Known Contradictions and Errors for Testing

Use this file to test whether Agent Maestro detects conflicts.

## Contradiction 1: Refund Threshold
- Global Refund Policy says refunds $5,000 - $24,999 need Director RevOps approval.
- EMEA Legacy Refund Process says refunds $10,000+ need Director approval.
- FY27 Approval Matrix says Global policy is source of truth.

Expected system behavior:
Flag EMEA document as outdated and use FY27 Approval Matrix.

## Contradiction 2: APAC Refund Exception
- APAC exception notes mention extra tax validation but no owner.
- No approval threshold is defined.

Expected behavior:
Answer with low confidence and create improvement backlog item.

## Contradiction 3: Slack Decisions
- Collections SOP says collectors DM SMEs directly in Slack.
- Governance expectation requires decisions to be documented.

Expected behavior:
Flag auditability risk.

## Contradiction 4: Marketing Cloud Launch
- Launch readiness runbook requires rollback plan.
- Known gap says rollback thresholds are missing.

Expected behavior:
Flag launch readiness risk.

## Contradiction 5: Bad Debt Evidence
- Bad Debt SOP requires final outreach evidence.
- Known issue says collectors sometimes skip documentation.

Expected behavior:
Flag control gap and recommend remediation.
