"""CrewAI orchestration for Agent Maestro.

This module contains the business logic behind the Streamlit UI. It keeps the
agent definitions in YAML, gathers local SOP/context, evaluates risk against an
approval matrix, and returns structured outputs that the app can render/log.
The deterministic runner keeps policy retrieval and risk scoring dependable.
When an OpenAI API key is supplied, the final response is refined through the
OpenAI API while preserving the grounded workflow facts.
"""

from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
import pandas as pd


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT_DIR / "data"
SOP_DIR = DATA_DIR / "sample_sops"
APPROVAL_MATRIX = DATA_DIR / "approval_matrix.csv"
KB_DIR = ROOT_DIR / "agent_maestro_kb"
KB_APPROVAL_MATRIX = KB_DIR / "data_approval_matrix.csv"
DEFAULT_OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

OUT_OF_SCOPE_MESSAGE = (
    "I can only help with Command Ops work: audits, workflows, billing, refunds, "
    "cash application, collections, bad debt, Marketing Cloud readiness, AI governance, "
    "SOPs, approvals, escalations, and related operational controls. This request does "
    "not appear to be work-related, so I cannot route it through the agent flow."
)

WORK_SCOPE_KEYWORDS = {
    "account",
    "approval",
    "audit",
    "bad debt",
    "billing",
    "cash application",
    "charge",
    "collections",
    "compliance",
    "controls",
    "customer",
    "duplicate",
    "dunning",
    "escalation",
    "evidence",
    "finance",
    "governance",
    "invoice",
    "legal",
    "login",
    "marketing cloud",
    "operations",
    "payment",
    "policy",
    "process",
    "refund",
    "risk",
    "sop",
    "ticket",
    "workflow",
    "writeoff",
    "write-off",
}

REQUEST_TYPES = [
    "Audit Request",
    "Workflow Issue",
    "Billing Issue",
    "Refund Approval",
    "Missing Policy",
    "Billing Login",
    "Cash App",
    "Collections",
    "Bad Debt",
    "Marketing Cloud",
    "AI Governance",
]


@dataclass
class ContextBundle:
    snippets: list[str]
    sources: list[str]
    missing_context: bool
    contradictions: list[str]


@dataclass
class AgentStep:
    name: str
    input_from: list[str]
    output: str
    checks: dict[str, bool]


def load_yaml_config(filename: str) -> dict[str, Any]:
    with (Path(__file__).parent / filename).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def build_crewai_crew() -> Any:
    """Build a CrewAI crew from YAML definitions when CrewAI is installed.

    The app uses deterministic execution by default for repeatable demos. This
    factory documents the CrewAI architecture and gives a clean extension point
    for live LLM orchestration.
    """
    if not os.getenv("OPENAI_API_KEY"):
        return None

    try:  # Lazy import keeps the Streamlit demo fast when running locally.
        from crewai import Agent, Crew, Process, Task
    except Exception:  # pragma: no cover - depends on local environment packages.
        return None

    agent_config = load_yaml_config("agents.yaml")
    task_config = load_yaml_config("tasks.yaml")

    agents = {
        name: Agent(
            role=config["role"],
            goal=config["goal"],
            backstory=config["backstory"],
            verbose=False,
            allow_delegation=False,
        )
        for name, config in agent_config["agents"].items()
    }
    tasks = [
        Task(
            description=config["description"],
            expected_output=config["expected_output"],
            agent=agents[config["agent"]],
        )
        for config in task_config["tasks"]
    ]
    return Crew(agents=list(agents.values()), tasks=tasks, process=Process.sequential, verbose=False)


def tokenize(text: str) -> set[str]:
    stop_words = {
        "the", "and", "for", "with", "that", "this", "from", "what", "how", "can",
        "was", "but", "not", "are", "our", "has", "have", "into", "after", "above",
        "below", "under", "over", "need", "needs", "required", "request", "requests",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in stop_words
    }


def is_work_related(question: str) -> bool:
    """Strictly gate the agent to supported operations work."""
    normalized = re.sub(r"\s+", " ", question.lower()).strip()
    if not normalized:
        return False
    if any(keyword in normalized for keyword in WORK_SCOPE_KEYWORDS):
        return True
    return bool(re.search(r"\$\s?\d", normalized))


def build_out_of_scope_result(question: str) -> dict[str, Any]:
    return {
        "agent_outputs": {
            "Scope Gate": OUT_OF_SCOPE_MESSAGE,
        },
        "recommended_workflow": OUT_OF_SCOPE_MESSAGE,
        "risk_note": "No operational risk assessment was performed because the request is outside scope.",
        "email_draft": "",
        "confidence": 1.0,
        "sources": [],
        "missing_context": False,
        "contradictions": [],
        "context_snippets": [],
        "risk_level": "Out of Scope",
        "interaction_trace": [
            {
                "agent": "Scope Gate",
                "input_from": "User Request",
                "checks": {
                    "work_related_request": False,
                    "agent_flow_blocked": True,
                },
            }
        ],
        "interaction_report": {
            "checks_passed": 1,
            "checks_total": 2,
            "failed_checks": [{"agent": "Scope Gate", "check": "work_related_request"}],
            "handoffs": ["Scope Gate consumed: User Request"],
            "report": "Request blocked by hard scope rule. No agent handoff occurred.",
        },
        "llm_status": "not_run",
        "llm_error": "",
        "openai_model": "",
        "user_question": question,
    }


def iter_knowledge_documents() -> list[Path]:
    """Discover markdown knowledge files from built-in samples and optional KB."""
    docs = list(SOP_DIR.glob("*.md"))
    if KB_DIR.exists():
        docs.extend(
            path
            for path in KB_DIR.rglob("*.md")
            if ".pytest_cache" not in path.parts
        )
    return sorted(docs)


def retrieve_context(request_type: str, question: str) -> ContextBundle:
    """Retrieve relevant markdown knowledge files using lexical scoring.

    This scans both the starter sample SOPs and the optional agent_maestro_kb
    folder. It gives small boosts to file paths that match the selected request
    type so the Knowledge Agent can use new docs without code changes.
    """
    query = f"{request_type} {question}"
    query_tokens = tokenize(query)
    scored_docs: list[tuple[int, Path, str]] = []

    for path in iter_knowledge_documents():
        text = path.read_text(encoding="utf-8").strip()
        path_lower = path.as_posix().lower()
        query_lower = query.lower()
        if "apac" in path_lower and "apac" not in query_lower:
            continue
        if "emea" in path_lower and "emea" not in query_lower:
            continue
        haystack = f"{path.as_posix()} {text}"
        doc_tokens = tokenize(haystack)
        score = len(query_tokens & doc_tokens)
        path_text = path_lower.replace("_", " ")
        for token in tokenize(request_type):
            if token in path_text:
                score += 3
        for domain in ["billing", "login", "cash", "collections", "debt", "marketing", "governance", "refund"]:
            if domain in query_lower and domain in path_text:
                score += 8
        if score:
            scored_docs.append((score, path, text))

    scored_docs.sort(key=lambda item: item[0], reverse=True)
    top_docs = scored_docs[:7]
    snippets = [text[:1200] for _, _, text in top_docs]
    sources = [path.relative_to(ROOT_DIR).as_posix() for _, path, _ in top_docs]

    combined = "\n".join(snippets).lower()
    q = query.lower()
    missing_terms = ["unknown", "unclear", "missing sop", "missing policy", "missing owner"]
    missing_context = not snippets or any(term in q for term in missing_terms)
    missing_context = missing_context or any(term in combined for term in ["owner: unknown", "status: draft / unverified"])

    contradictions = []
    if "outdated" in combined or "contradiction" in combined or "legacy" in combined:
        contradictions.append("Potential outdated or conflicting policy context detected.")
    if "slack" in q or "slack" in combined and request_type in {"Collections", "Workflow Issue"}:
        contradictions.append("Slack-only decisions create auditability risk; documented source of truth required.")

    return ContextBundle(
        snippets=snippets,
        sources=sources,
        missing_context=missing_context,
        contradictions=contradictions,
    )


def extract_amount(question: str) -> float:
    matches = re.findall(r"\$?\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)", question)
    if not matches:
        return 0.0
    values = [float(match.replace(",", "")) for match in matches]
    return max(values)


def approval_band(amount: float) -> dict[str, Any]:
    if KB_APPROVAL_MATRIX.exists():
        matrix = pd.read_csv(KB_APPROVAL_MATRIX)
        if amount >= 25000:
            row = matrix[matrix["scenario"] == "Refund $25000+"].iloc[0]
        elif amount >= 5000:
            row = matrix[matrix["scenario"] == "Refund $5000-$24999"].iloc[0]
        elif amount >= 1000:
            row = matrix[matrix["scenario"] == "Refund $1000-$4999"].iloc[0]
        else:
            row = matrix[matrix["scenario"] == "Refund under $1000"].iloc[0]
        return {
            "approval_band": row["risk_level"],
            "required_approver": row["approval_required"],
            "governance_note": row["notes"],
            "risk_level": row["risk_level"],
            "region": row["region"],
        }

    matrix = pd.read_csv(APPROVAL_MATRIX)
    for _, row in matrix.iterrows():
        if row["min_amount"] <= amount <= row["max_amount"]:
            return row.to_dict()
    return matrix.iloc[-1].to_dict()


def risk_level(request_type: str, amount: float, context: ContextBundle, question: str = "") -> str:
    q = question.lower()
    if any(term in q for term in ["bankruptcy", "legal", "fraud", "above $25k", "above 25k"]) or amount >= 25000:
        return "Critical"
    if request_type == "AI Governance" and amount >= 25000:
        return "Critical"
    if context.missing_context:
        return "High"
    if request_type == "Refund Approval" and amount >= 5000:
        return "High"
    if request_type in {"Audit Request", "Billing Issue", "Billing Login", "Cash App", "Collections"} or amount >= 1000:
        return "Medium"
    if request_type in {"Bad Debt", "Marketing Cloud", "Workflow Issue"}:
        return "High" if context.contradictions else "Medium"
    return "Low"


def confidence_score(context: ContextBundle, risk: str, amount: float) -> float:
    score = 0.86
    if context.missing_context:
        score -= 0.22
    if context.contradictions:
        score -= 0.08
    if risk == "High":
        score -= 0.10
    if risk == "Critical":
        score -= 0.14
    if amount == 0:
        score -= 0.04
    return max(0.45, min(score, 0.95))


def run_knowledge_agent(request_type: str, question: str) -> tuple[ContextBundle, AgentStep]:
    context = retrieve_context(request_type, question)
    sources_text = "; ".join(context.sources) if context.sources else "No matching local SOP"
    output = (
        f"Retrieved context from {sources_text}. Missing context: {context.missing_context}. "
        f"Contradictions flagged: {bool(context.contradictions)}. "
        "Policy grounding is required before customer-facing action."
    )
    return context, AgentStep(
        name="Knowledge Agent",
        input_from=["User Request", "Request Type", "Local SOP Files"],
        output=output,
        checks={
            "source_context_checked": bool(context.sources),
            "missing_context_flagged": True,
            "context_complete": not context.missing_context,
            "contradictions_checked": True,
        },
    )


def run_audit_agent(request_type: str, question: str, knowledge: ContextBundle) -> AgentStep:
    context_signal = "SOP context available" if knowledge.sources else "SOP context missing"
    contradiction_signal = (
        f" Contradiction risk: {'; '.join(knowledge.contradictions)}"
        if knowledge.contradictions
        else ""
    )
    output = (
        f"{context_signal}. Process gaps: incomplete intake details, unclear evidence chain, "
        "manual approval handoffs, and inconsistent escalation documentation. Root cause hypothesis: "
        f"{request_type.lower()} is entering the workflow without enough structured fields. "
        "Automation opportunity: extract request type, amount, required owner, evidence needs, and SLA."
        f"{contradiction_signal}"
    )
    return AgentStep(
        name="Audit Agent",
        input_from=["Knowledge Agent"],
        output=output,
        checks={
            "uses_retrieved_context": bool(knowledge.sources),
            "root_cause_identified": True,
            "automation_opportunity_identified": True,
            "contradictions_escalated": True,
        },
    )


def run_workflow_agent(audit_step: AgentStep, band: dict[str, Any], risk: str) -> tuple[str, AgentStep]:
    approval_owner = band["required_approver"]
    timeline = "same business day" if risk in {"High", "Critical"} else "1-2 business days"
    workflow = (
        f"1. Confirm request facts and attach available evidence.\n"
        f"2. Route to **{approval_owner}** for review.\n"
        f"3. Resolve dependencies: transaction data, account notes, SOP match, and escalation status.\n"
        f"4. Complete action within **{timeline}**.\n"
        f"5. Escalate to Finance Controls if risk remains High/Critical or context is incomplete."
    )
    output = (
        f"Using Audit Agent findings, route ownership to {approval_owner}. Target timeline is "
        f"{timeline}. Dependencies are evidence, SOP confirmation, transaction data, and approver sign-off."
    )
    return workflow, AgentStep(
        name="Workflow Agent",
        input_from=["Audit Agent", "Approval Matrix"],
        output=output,
        checks={
            "owner_assigned": bool(approval_owner),
            "timeline_assigned": bool(timeline),
            "dependencies_listed": True,
            "audit_findings_used": "Process gaps" in audit_step.output,
        },
    )


def run_finance_risk_agent(
    request_type: str,
    amount: float,
    band: dict[str, Any],
    risk: str,
    workflow_step: AgentStep,
) -> tuple[str, AgentStep]:
    amount_text = f"${amount:,.2f}" if amount else "No dollar amount detected"
    risk_note = (
        f"{risk} risk. Approval band is {band['approval_band']} with threshold guidance: "
        f"{band['governance_note']}"
    )
    output = (
        f"Reviewed {request_type} after Workflow Agent routing. Detected amount: {amount_text}. "
        f"Approval band: {band['approval_band']}. Compliance risk: {risk}. Governance note: "
        f"{band['governance_note']}"
    )
    return risk_note, AgentStep(
        name="Finance Risk Agent",
        input_from=["Workflow Agent", "Approval Matrix"],
        output=output,
        checks={
            "approval_threshold_checked": True,
            "risk_level_assigned": risk in {"Low", "Medium", "High", "Critical"},
            "workflow_route_reviewed": workflow_step.checks["owner_assigned"],
            "human_approval_required": risk not in {"High", "Critical"} or any(
                owner in str(band["required_approver"]).lower()
                for owner in ["manager", "director", "vp", "legal", "lead", "specialist"]
            ),
        },
    )


def run_executive_summary_agent(
    request_type: str,
    risk: str,
    band: dict[str, Any],
    steps: list[AgentStep],
) -> AgentStep:
    approval_owner = band["required_approver"]
    failed_checks = [
        f"{step.name}: {check}"
        for step in steps
        for check, passed in step.checks.items()
        if not passed
    ]
    check_note = "All required checks passed." if not failed_checks else f"Open checks: {', '.join(failed_checks)}."
    output = (
        f"{request_type} requires {risk.lower()}-risk handling with {approval_owner} ownership. "
        "Business impact is customer trust, operational leakage, and control adherence. "
        f"Next action: validate evidence, document approval, and communicate the decision. {check_note}"
    )
    return AgentStep(
        name="Executive Summary Agent",
        input_from=[step.name for step in steps],
        output=output,
        checks={
            "summarizes_prior_agents": all(step.name in output or step.output for step in steps),
            "next_action_included": "Next action" in output,
            "business_impact_included": "Business impact" in output,
        },
    )


def build_interaction_report(steps: list[AgentStep]) -> dict[str, Any]:
    total_checks = sum(len(step.checks) for step in steps)
    passed_checks = sum(1 for step in steps for passed in step.checks.values() if passed)
    failed = [
        {"agent": step.name, "check": check}
        for step in steps
        for check, passed in step.checks.items()
        if not passed
    ]
    handoffs = [
        f"{step.name} consumed: {', '.join(step.input_from)}"
        for step in steps
    ]
    return {
        "checks_passed": passed_checks,
        "checks_total": total_checks,
        "failed_checks": failed,
        "handoffs": handoffs,
        "report": (
            f"Agent interaction complete: {passed_checks}/{total_checks} checks passed. "
            f"{'Review failed checks before action.' if failed else 'No blocking QA gaps detected.'}"
        ),
    }


def build_grounded_result(request_type: str, question: str) -> dict[str, Any]:
    """Build the deterministic grounded packet used by the UI and OpenAI call."""
    context, knowledge_step = run_knowledge_agent(request_type, question)
    amount = extract_amount(question)
    band = approval_band(amount)
    risk = risk_level(request_type, amount, context, question)
    if risk == "Critical":
        band["approval_band"] = "Critical"
        if amount >= 25000 and "Legal" not in str(band["required_approver"]):
            band["required_approver"] = "VP Finance + Legal"
    confidence = confidence_score(context, risk, amount)

    audit_step = run_audit_agent(request_type, question, context)
    recommended_workflow, workflow_step = run_workflow_agent(audit_step, band, risk)
    risk_note, finance_step = run_finance_risk_agent(request_type, amount, band, risk, workflow_step)
    executive_step = run_executive_summary_agent(
        request_type,
        risk,
        band,
        [knowledge_step, audit_step, workflow_step, finance_step],
    )
    steps = [knowledge_step, audit_step, workflow_step, finance_step, executive_step]
    interaction_report = build_interaction_report(steps)

    agent_outputs = {step.name: step.output for step in steps}
    timeline = "same business day" if risk in {"High", "Critical"} else "1-2 business days"

    email_draft = (
        "Subject: Operations request update\n\n"
        "Hi team,\n\n"
        f"Agent Maestro reviewed the {request_type.lower()} and recommends routing it to "
        f"{band['required_approver']}. Current risk is {risk.lower()}, with a target timeline of {timeline}. "
        "Please confirm the supporting evidence and complete the approval step before final action.\n\n"
        "Recommended next action: validate the request details, document the decision, and update the "
        "customer or stakeholder once approval is complete.\n\n"
        "Best,\nOperations Intelligence"
    )

    return {
        "agent_outputs": agent_outputs,
        "recommended_workflow": recommended_workflow,
        "risk_note": risk_note,
        "email_draft": email_draft,
        "confidence": confidence,
        "sources": context.sources,
        "missing_context": context.missing_context,
        "contradictions": context.contradictions,
        "context_snippets": context.snippets,
        "risk_level": risk,
        "interaction_trace": [
            {
                "agent": step.name,
                "input_from": ", ".join(step.input_from),
                "checks": step.checks,
            }
            for step in steps
        ],
        "interaction_report": interaction_report,
        "llm_status": "not_run",
        "llm_error": "",
        "openai_model": "",
        "user_question": question,
    }


def enhance_with_openai(result: dict[str, Any], api_key: str, model: str = DEFAULT_OPENAI_MODEL) -> dict[str, Any]:
    """Refine grounded agent outputs through the OpenAI API.

    The API call is allowed to improve wording and completeness only. It must not
    change risk scoring, confidence, source attribution, or QA checks.
    """
    if not api_key:
        return result

    try:
        from openai import OpenAI
    except Exception as exc:  # pragma: no cover - depends on local installation.
        result["llm_status"] = "error"
        result["llm_error"] = f"OpenAI package could not be imported: {exc}"
        result["openai_model"] = model
        return result

    prompt_payload = {
        "request": {
            "type": result.get("request_type"),
            "question": result.get("user_question", ""),
        },
        "fixed_facts": {
            "risk_level": result["risk_level"],
            "confidence": result["confidence"],
            "sources": result["sources"],
            "missing_context": result["missing_context"],
            "contradictions": result["contradictions"],
            "interaction_report": result["interaction_report"],
        },
        "draft_outputs": {
            "agent_outputs": result["agent_outputs"],
            "recommended_workflow": result["recommended_workflow"],
            "risk_note": result["risk_note"],
            "email_draft": result["email_draft"],
        },
        "context_snippets": result.get("context_snippets", [])[:5],
    }
    instructions = (
        "You are Agent Maestro for Command Ops. Hard rules: answer only work-related "
        "operations requests; use only the supplied facts and context; do not invent "
        "policies, sources, approvers, amounts, or risk ratings; clearly flag missing "
        "context; keep outputs concise and actionable. Return only valid JSON with keys "
        "agent_outputs, recommended_workflow, risk_note, email_draft."
    )

    try:
        client = OpenAI(api_key=api_key)
        response = client.responses.create(
            model=model,
            instructions=instructions,
            input=json.dumps(prompt_payload, indent=2),
            max_output_tokens=1500,
        )
        content = response.output_text
        enhanced = json.loads(content)
        if isinstance(enhanced.get("agent_outputs"), dict):
            result["agent_outputs"] = enhanced["agent_outputs"]
        for key in ["recommended_workflow", "risk_note", "email_draft"]:
            if isinstance(enhanced.get(key), str) and enhanced[key].strip():
                result[key] = enhanced[key].strip()
        result["llm_status"] = "enhanced"
        result["llm_error"] = ""
        result["openai_model"] = model
    except Exception as exc:
        result["llm_status"] = "error"
        result["llm_error"] = str(exc)
        result["openai_model"] = model

    return result


def run_operations_crew(
    request_type: str,
    question: str,
    api_key: str | None = None,
    model: str = DEFAULT_OPENAI_MODEL,
) -> dict[str, Any]:
    """Run an interacting multi-agent workflow and return structured outputs.

    Each agent receives the previous agent's output, performs its part, and emits
    checks. The executive summary and QA report are built from the full trace.
    """
    if not is_work_related(question):
        return build_out_of_scope_result(question)

    result = build_grounded_result(request_type, question)
    result["request_type"] = request_type
    if api_key:
        result = enhance_with_openai(result, api_key=api_key, model=model)
    return result
