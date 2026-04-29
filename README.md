# Agent Maestro

Agent Maestro is a Streamlit portfolio app for business operations intelligence. It routes requests through a CrewAI-style multi-agent workflow, grounds recommendations in local SOP/sample data, optionally refines the final output with the OpenAI API, and logs evaluation signals for continuous improvement.

## What It Does

- Accepts an operations request and request type: Audit Request, Workflow Issue, Billing Issue, or Refund Approval.
- Routes the request through five agents: Audit, Knowledge, Workflow, Finance Risk, and Executive Summary.
- Displays agent outputs, recommended workflow, risk note, email draft, confidence level, and sources used.
- Lets users enter an OpenAI API key in the Streamlit sidebar for live response enhancement.
- Blocks questions that are not related to Command Ops work before retrieval or LLM calls.
- Writes evaluation rows to `outputs/eval_log.csv`.
- Writes improvement items to `outputs/improvement_backlog.csv` when context is missing, confidence is low, or risk is high.
- Includes a dashboard tab for reviewing eval logs and the improvement backlog.

## Project Structure

```text
.
|-- app.py
|-- crews/
|   |-- crew.py
|   |-- agents.yaml
|   `-- tasks.yaml
|-- data/
|   |-- approval_matrix.csv
|   `-- sample_sops/
|       |-- billing_login.md
|       |-- escalation_matrix.md
|       `-- refund_policy.md
|-- outputs/
|   |-- eval_log.csv
|   `-- improvement_backlog.csv
|-- README.md
`-- requirements.txt
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Run the app:

```bash
streamlit run app.py
```

Open the Streamlit sidebar and enter an OpenAI API key. You can also set `OPENAI_API_KEY` in `.streamlit/secrets.toml` if you want Streamlit to prefill the key:

```toml
OPENAI_API_KEY = "your_key_here"
```

## Architecture Notes

`app.py` owns the Streamlit interface, API key entry, CSV logging, and dashboard views. `crews/crew.py` owns the operations intelligence workflow: hard scope gating, context retrieval, approval threshold lookup, risk scoring, confidence scoring, OpenAI response enhancement, and structured agent outputs.

The app keeps retrieval, thresholds, QA checks, and risk scoring deterministic. When a key is provided in Streamlit, the OpenAI API improves the wording and completeness of the grounded outputs without changing risk, confidence, sources, approvers, or checks.

## Hard Scope Rule

The agent only accepts Command Ops work: audits, workflows, billing, refunds, cash application, collections, bad debt, Marketing Cloud readiness, AI governance, SOPs, approvals, escalations, and related operational controls. Non-work questions are blocked with a clear message and are not sent to the OpenAI API.

## Agent Handoff Flow

The agents now run as an interacting sequence:

1. Knowledge Agent retrieves SOP/context and flags missing context.
2. Audit Agent consumes Knowledge output and identifies gaps, root cause, and automation opportunities.
3. Workflow Agent consumes Audit output and approval matrix data to assign owner, timeline, dependencies, and escalation path.
4. Finance Risk Agent consumes Workflow output and approval matrix data to check threshold, risk, and governance notes.
5. Executive Summary Agent consumes all prior agent outputs and creates the leadership report.

The UI displays an Automatic Agent Check Report showing handoffs, passed checks, failed checks, and overall report status.

## Testing

Install dependencies:

```bash
pip install -r requirements.txt
```

Run all tests:

```bash
python -m pytest -q
```

Run only crew/orchestration tests:

```bash
python -m pytest tests/test_crew.py -q
```

Run only CSV logging tests:

```bash
python -m pytest tests/test_app_logging.py -q
```

Run the full synthetic knowledge-base smoke test:

```bash
python scripts/run_kb_tests.py
```

This reads `agent_maestro_kb/test_cases/agent_maestro_test_cases.csv`, routes each case through the full agent flow, and writes detailed results to `outputs/kb_test_results.csv`.

Quick manual test cases:

- Refund Approval: `Customer asks for a $7,500 refund after duplicate billing and login failure.`
  - Expected: High risk, Finance Director owner, refund and billing SOP sources, improvement backlog item.
- Billing Issue: `Customer was charged twice and cannot log in.`
  - Expected: Medium risk, billing/login SOP source, workflow recommendation.
- Workflow Issue: `Unknown process with missing SOP.`
  - Expected: High risk, missing context warning, failed context completeness check.
- Out of scope: `Write a birthday poem about the moon.`
  - Expected: blocked by the Scope Gate, no LLM call.
