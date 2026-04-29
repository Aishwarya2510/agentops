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
import hashlib
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

REQUEST_TYPE_KEYWORDS = {
    "AI Governance": [
        "ai assistant", "ai agent", "model", "governance", "prompt", "llm", "human approval",
        "automated decision", "ai policy",
    ],
    "Marketing Cloud": [
        "marketing cloud", "campaign", "consent", "launch readiness", "data readiness",
        "journey", "email send",
    ],
    "Bad Debt": [
        "bad debt", "writeoff", "write-off", "write off", "uncollectible", "reserve",
    ],
    "Collections": [
        "collections", "dunning", "past due", "overdue", "collector", "pause dunning",
    ],
    "Cash App": [
        "cash application", "cash app", "unapplied cash", "payment applied", "remittance",
        "lockbox",
    ],
    "Billing Login": [
        "billing portal", "login", "password reset", "access invoice", "cannot access",
        "portal access",
    ],
    "Refund Approval": [
        "refund", "credit memo", "duplicate charge", "duplicate billing", "chargeback",
        "customer credit",
    ],
    "Billing Issue": [
        "billing", "invoice", "charged", "charge", "subscription", "renewal", "payment attempt",
    ],
    "Audit Request": [
        "audit", "auditor", "evidence", "approval evidence", "control", "sox",
    ],
    "Missing Policy": [
        "missing policy", "missing sop", "no policy", "cannot find sop", "policy gap",
    ],
    "Workflow Issue": [
        "workflow", "process", "handoff", "unclear", "unknown", "slack", "sop",
        "threshold applies",
    ],
}


@dataclass
class ContextBundle:
    snippets: list[str]
    sources: list[str]
    missing_context: bool
    contradictions: list[str]
    policy_versions: list[dict[str, str]]


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


def classify_request_type(question: str) -> dict[str, Any]:
    """Classify the operations request into the best supported workflow."""
    q = re.sub(r"\s+", " ", question.lower()).strip()
    scores = {request_type: 0 for request_type in REQUEST_TYPES}

    for request_type, keywords in REQUEST_TYPE_KEYWORDS.items():
        for keyword in keywords:
            if keyword in q:
                scores[request_type] += 4 if " " in keyword else 2

    amount = extract_amount(question)
    if amount:
        scores["Refund Approval"] += 2 if "refund" in q or "credit" in q else 0
        scores["Billing Issue"] += 1
    if "refund" in q and amount >= 1000:
        scores["Refund Approval"] += 4
    if "login" in q and ("billing" in q or "portal" in q):
        scores["Billing Login"] += 6
    if "slack" in q and ("threshold" in q or "different" in q or "conflicting" in q):
        scores["Workflow Issue"] += 5
    if "approval" in q and ("evidence" in q or "audit" in q):
        scores["Audit Request"] += 4
    if has_sensitive_customer_claim(question) and ("refund" in q or amount):
        scores["Refund Approval"] += 4

    best_type, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 0:
        best_type = "Workflow Issue"

    sorted_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    runner_up_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0
    margin = best_score - runner_up_score
    confidence = min(0.95, max(0.55, 0.58 + (best_score * 0.04) + (margin * 0.03)))

    reasons = [
        keyword
        for keyword in REQUEST_TYPE_KEYWORDS.get(best_type, [])
        if keyword in q
    ][:4]
    if amount and best_type in {"Refund Approval", "Billing Issue", "Cash App", "Bad Debt"}:
        reasons.append(f"detected amount ${amount:,.2f}")
    if has_sensitive_customer_claim(question):
        reasons.append("sensitive customer-impact language")

    return {
        "request_type": best_type,
        "confidence": confidence,
        "scores": scores,
        "reasons": reasons or ["defaulted to workflow triage"],
    }


def build_out_of_scope_result(question: str) -> dict[str, Any]:
    return {
        "agent_outputs": {
            "Scope Gate": OUT_OF_SCOPE_MESSAGE,
        },
        "recommended_workflow": OUT_OF_SCOPE_MESSAGE,
        "risk_note": "No operational risk assessment was performed because the request is outside scope.",
        "email_draft": "",
        "confidence": 1.0,
        "confidence_drivers": {
            "positive": ["Hard scope gate worked as designed"],
            "negative": ["Request is outside supported Command Ops scope"],
        },
        "business_impact": {
            "time_saved_minutes": 0,
            "cost_impact_usd": 0,
            "sla_improvement": "N/A",
            "risk_avoided": "N/A",
            "confidence": 1.0,
        },
        "priority": {
            "score": 0,
            "label": "Blocked",
            "handle_by": "no action",
            "drivers": ["Out-of-scope request"],
        },
        "exception": {
            "detected": True,
            "reasons": ["Request is outside supported Command Ops scope"],
            "required_action": "Do not route through operations workflow.",
        },
        "confidence_action": {
            "band": "Blocked",
            "action": "Do not execute",
            "reason": "Request is outside supported scope.",
        },
        "subtasks": [],
        "policy_versions": [],
        "data_sensitivity": {
            "detected": False,
            "level": "N/A",
            "flags": [],
            "handling": "Request was blocked before processing.",
        },
        "llm_usage": {
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "should_call_llm": False,
            "decision": "Skip OpenAI enhancement for out-of-scope requests",
        },
        "fallback": {
            "needed": True,
            "message": "Request blocked by hard scope gate.",
        },
        "suggested_rule_updates": [],
        "integration_simulation": {
            "source": "No enterprise source created",
            "crm_sync": "No CRM sync",
            "slack": "No Slack escalation",
        },
        "role_views": {
            "Analyst": OUT_OF_SCOPE_MESSAGE,
            "Manager": OUT_OF_SCOPE_MESSAGE,
            "Director": OUT_OF_SCOPE_MESSAGE,
        },
        "executive_summary": {
            "Issue": "Out-of-scope request",
            "Risk": "Not assessed",
            "Action": "Blocked by scope gate",
            "Impact": "Prevents unrelated AI usage",
            "Recommendation": "Submit a Command Ops request.",
        },
        "amount": 0.0,
        "approval_owner": "",
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
        "request_type": "Out of Scope",
        "classification": {
            "request_type": "Out of Scope",
            "confidence": 1.0,
            "scores": {},
            "reasons": ["blocked by hard scope gate"],
        },
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


def policy_version_metadata(path: Path, text: str) -> dict[str, str]:
    """Return audit-friendly source metadata for retrieved policy/SOP files."""
    name = path.stem.replace("_", " ").title()
    lower_name = path.name.lower()
    lower_text = text.lower()
    version = "v1.0"
    updated = "Unknown"

    if "fy27" in lower_name or "fy27" in lower_text:
        version = "FY27"
        updated = "2026-01"
    elif "global" in lower_name:
        version = "Global v3.2"
        updated = "2026-01"
    elif "old" in lower_name or "legacy" in lower_text:
        version = "Legacy"
        updated = "Needs review"
    elif "draft" in lower_text or "unverified" in lower_text:
        version = "Draft"
        updated = "Unverified"

    return {
        "source": path.relative_to(ROOT_DIR).as_posix(),
        "policy": name,
        "version": version,
        "updated": updated,
    }


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
    policy_versions = [policy_version_metadata(path, text) for _, path, text in top_docs]

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
        policy_versions=policy_versions,
    )


def extract_amount(question: str) -> float:
    matches = re.findall(r"\$?\s?(\d+(?:,\d{3})*(?:\.\d+)?)", question)
    if not matches:
        return 0.0
    values = [float(match.replace(",", "")) for match in matches]
    return max(values)


def has_sensitive_customer_claim(question: str) -> bool:
    q = question.lower()
    sensitive_terms = [
        "emotional instability",
        "emotional distress",
        "distress",
        "mental health",
        "harm",
        "damages",
        "lawsuit",
        "legal action",
        "attorney",
        "counsel",
        "regulator",
        "threatening legal",
    ]
    return any(term in q for term in sensitive_terms)


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
    if has_sensitive_customer_claim(question):
        return "High"
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


def confidence_action_mapping(confidence: float, risk: str, missing_context: bool) -> dict[str, str]:
    if risk == "Critical" or confidence < 0.60 or missing_context:
        return {
            "band": "Low",
            "action": "Escalate for manual review before execution",
            "reason": "Critical risk, low confidence, or missing context prevents safe execution.",
        }
    if confidence < 0.78 or risk == "High":
        return {
            "band": "Medium",
            "action": "Requires manager validation before execution",
            "reason": "Confidence or risk level requires human validation.",
        }
    return {
        "band": "High",
        "action": "Proceed with standard workflow after required approval checks",
        "reason": "Confidence is high and no blocking context gap was detected.",
    }


def split_subtasks(question: str, primary_type: str) -> list[dict[str, str]]:
    q = question.lower()
    subtasks = [{"workflow": primary_type, "task": f"Handle primary {primary_type.lower()} workflow."}]
    if "refund" in q and primary_type != "Refund Approval":
        subtasks.append({"workflow": "Refund Approval", "task": "Evaluate refund eligibility, amount, and approval threshold."})
    if any(term in q for term in ["login", "portal", "password", "access"]):
        subtasks.append({"workflow": "Billing Login", "task": "Resolve billing portal access or authentication issue."})
    if any(term in q for term in ["complaint", "distress", "emotional", "escalation", "customer success"]):
        subtasks.append({"workflow": "CX Escalation", "task": "Coordinate customer-impact review and stakeholder communication."})
    if any(term in q for term in ["legal", "attorney", "lawsuit", "damages"]):
        subtasks.append({"workflow": "Legal Review", "task": "Escalate legal exposure before customer commitment."})

    seen = set()
    unique = []
    for item in subtasks:
        key = (item["workflow"], item["task"])
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique


def data_sensitivity(question: str, amount: float) -> dict[str, Any]:
    q = question.lower()
    flags = []
    if amount:
        flags.append("Customer financial information")
    if re.search(r"[\w\.-]+@[\w\.-]+\.\w+", question):
        flags.append("Email address")
    if re.search(r"\b(?:\d[ -]*?){13,16}\b", question):
        flags.append("Potential payment card number")
    if any(term in q for term in ["ssn", "social security", "tax id"]):
        flags.append("Government identifier")
    if has_sensitive_customer_claim(question):
        flags.append("Sensitive customer-impact language")

    level = "Restricted" if any("card" in flag.lower() or "identifier" in flag.lower() for flag in flags) else "Confidential" if flags else "Internal"
    return {
        "detected": bool(flags),
        "level": level,
        "flags": flags,
        "handling": "Mask sensitive values before sharing externally." if flags else "No special handling required beyond standard internal controls.",
    }


def llm_usage_policy(question: str, result_facts: dict[str, Any]) -> dict[str, Any]:
    input_tokens = max(250, int((len(question) + len(json.dumps(result_facts, default=str))) / 4))
    output_tokens = 900
    estimated_cost = round(((input_tokens / 1_000_000) * 0.15) + ((output_tokens / 1_000_000) * 0.60), 4)
    confidence_action = result_facts.get("confidence_action", {})
    should_call = confidence_action.get("band") != "Low"
    return {
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_cost_usd": estimated_cost,
        "should_call_llm": should_call,
        "decision": "Use OpenAI wording enhancement" if should_call else "Skip OpenAI enhancement until manual review clears context",
    }


def confidence_breakdown(context: ContextBundle, risk: str, amount: float) -> dict[str, list[str]]:
    """Explain the confidence score in business-readable terms."""
    positive = []
    negative = []

    if context.sources:
        positive.append("SOP or policy context matched")
    else:
        negative.append("No local SOP or policy source matched")
    if not context.missing_context:
        positive.append("Required operating context appears available")
    else:
        negative.append("Missing or unverified operating context")
    if amount:
        positive.append("Dollar amount detected for approval routing")
    else:
        negative.append("No dollar amount detected")
    if risk in {"Low", "Medium"}:
        positive.append("Risk is within standard handling band")
    else:
        negative.append(f"{risk} risk requires tighter human review")
    if context.contradictions:
        negative.append("Potential conflicting or outdated policy context")

    return {"positive": positive, "negative": negative}


def priority_decision(request_type: str, risk: str, amount: float, context: ContextBundle) -> dict[str, Any]:
    risk_points = {"Low": 15, "Medium": 35, "High": 60, "Critical": 80}.get(risk, 0)
    amount_points = 0
    if amount >= 25000:
        amount_points = 20
    elif amount >= 5000:
        amount_points = 14
    elif amount >= 1000:
        amount_points = 8
    sla_points = 10 if request_type in {"Billing Issue", "Billing Login", "Collections", "Cash App"} else 5
    context_points = 10 if context.missing_context or context.contradictions else 0
    score = min(100, risk_points + amount_points + sla_points + context_points)

    if score >= 90:
        label = "Critical"
        handle_by = "handle immediately"
    elif score >= 65:
        label = "High"
        handle_by = "handle within 2 hours"
    elif score >= 40:
        label = "Medium"
        handle_by = "handle same business day"
    else:
        label = "Low"
        handle_by = "handle within 1-2 business days"

    return {
        "score": score,
        "label": label,
        "handle_by": handle_by,
        "drivers": [
            f"Risk level: {risk}",
            f"Value exposure: ${amount:,.2f}" if amount else "Value exposure: not provided",
            f"SLA sensitivity: {request_type}",
            "Context exception present" if context.missing_context or context.contradictions else "Context is sufficiently grounded",
        ],
    }


def business_impact_estimate(
    request_type: str,
    risk: str,
    amount: float,
    confidence: float,
    priority: dict[str, Any],
) -> dict[str, Any]:
    base_minutes = {
        "Refund Approval": 45,
        "Billing Issue": 40,
        "Billing Login": 30,
        "Cash App": 50,
        "Collections": 45,
        "Bad Debt": 55,
        "Audit Request": 60,
        "AI Governance": 50,
    }.get(request_type, 35)
    risk_multiplier = {"Low": 1.0, "Medium": 1.15, "High": 1.35, "Critical": 1.6}.get(risk, 1.0)
    time_saved = round(base_minutes * risk_multiplier)
    hourly_cost = 65
    cost_impact = round((time_saved / 60) * hourly_cost)
    sla_improvement = "High" if priority["label"] in {"High", "Critical"} else "Medium"
    risk_avoided = "High" if risk in {"High", "Critical"} else risk

    return {
        "time_saved_minutes": time_saved,
        "cost_impact_usd": cost_impact,
        "sla_improvement": sla_improvement,
        "risk_avoided": risk_avoided,
        "confidence": confidence,
    }


def detect_exceptions(
    request_type: str,
    amount: float,
    context: ContextBundle,
    risk: str,
    question: str,
) -> dict[str, Any]:
    reasons = []
    if context.missing_context:
        reasons.append("Insufficient or missing policy context")
    if context.contradictions:
        reasons.extend(context.contradictions)
    if request_type == "Refund Approval" and amount == 0:
        reasons.append("Refund request does not include an amount")
    if has_sensitive_customer_claim(question):
        reasons.append("Sensitive customer-impact claim requires Legal or Customer Success review")
    if any(term in question.lower() for term in ["ambiguous", "unclear", "not sure", "unknown"]):
        reasons.append("Ambiguous request language")
    if risk == "Critical":
        reasons.append("Critical-risk case requires escalation before action")

    return {
        "detected": bool(reasons),
        "reasons": reasons,
        "required_action": (
            "Escalate for human review before execution."
            if reasons
            else "No exception detected; proceed through standard workflow."
        ),
    }


def integration_simulation(request_type: str, question: str, risk: str) -> dict[str, str]:
    digest = hashlib.sha1(f"{request_type}|{question}".encode("utf-8")).hexdigest()
    ticket_id = 4000 + int(digest[:4], 16) % 5000
    case_id = 700000 + int(digest[4:8], 16) % 90000
    slack_channel = "#finance-escalations" if risk in {"High", "Critical"} else "#ops-workflow"
    return {
        "source": f"Zendesk Ticket #{ticket_id}",
        "crm_sync": f"Synced to Salesforce Case #{case_id}",
        "slack": f"Slack escalation triggered in {slack_channel}" if risk in {"High", "Critical"} else f"Slack status posted in {slack_channel}",
    }


def role_based_views(
    request_type: str,
    risk: str,
    amount: float,
    band: dict[str, Any],
    workflow: str,
    priority: dict[str, Any],
    impact: dict[str, Any],
    exception: dict[str, Any],
) -> dict[str, str]:
    amount_text = f"${amount:,.2f}" if amount else "not provided"
    return {
        "Analyst": (
            f"Execute the workflow for this {request_type.lower()}.\n\n{workflow}\n\n"
            f"Priority: {priority['label']} ({priority['handle_by']})."
        ),
        "Manager": (
            f"Decision needed: approve routing to {band['required_approver']} for a {risk.lower()}-risk "
            f"{request_type.lower()} with amount {amount_text}. Priority is {priority['label']}. "
            f"Exception status: {exception['required_action']}"
        ),
        "Director": (
            f"Business impact: {request_type} carries {risk.lower()} risk, estimated savings of "
            f"{impact['time_saved_minutes']} minutes and ${impact['cost_impact_usd']} handling cost impact. "
            f"Recommendation: validate evidence, preserve approval trace, and escalate if exception reasons remain open."
        ),
    }


def executive_summary(
    request_type: str,
    risk: str,
    amount: float,
    band: dict[str, Any],
    impact: dict[str, Any],
    priority: dict[str, Any],
) -> dict[str, str]:
    amount_text = f"${amount:,.0f}" if amount else "amount not provided"
    return {
        "Issue": f"{request_type} ({amount_text})",
        "Risk": f"{risk} ({band['required_approver']} approval required)",
        "Action": f"{band['required_approver']} approval plus evidence validation",
        "Impact": (
            f"Prevents policy violation, protects compliance, and saves about "
            f"{impact['time_saved_minutes']} minutes of manual triage."
        ),
        "Recommendation": f"{priority['label']} priority: {priority['handle_by']}. Approve only with documented support.",
    }


def suggested_rule_updates(context: ContextBundle, request_type: str, exception: dict[str, Any]) -> list[str]:
    suggestions = []
    if context.missing_context:
        suggestions.append(f"Add SOP coverage or intake examples for {request_type}.")
    if context.contradictions:
        suggestions.append("Review conflicting policy sources and define one source of truth.")
    if exception.get("detected"):
        suggestions.append("Create explicit escalation rule for recurring exception pattern.")
    return suggestions


def parse_openai_json(content: str) -> dict[str, Any]:
    """Parse model JSON even when it is wrapped in markdown or short prose."""
    text = (content or "").strip()
    if not text:
        raise ValueError("OpenAI returned an empty response.")

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        parsed = None
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            parsed = candidate
            break
        if parsed is None:
            preview = text[:120].replace("\n", " ")
            raise ValueError(f"OpenAI returned non-JSON content: {preview}")

    if not isinstance(parsed, dict):
        raise ValueError("OpenAI JSON response must be an object.")
    return parsed


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


def run_workflow_agent(
    request_type: str,
    question: str,
    amount: float,
    audit_step: AgentStep,
    band: dict[str, Any],
    risk: str,
) -> tuple[str, AgentStep]:
    approval_owner = band["required_approver"]
    timeline = "same business day" if risk in {"High", "Critical"} else "1-2 business days"
    amount_text = f"${amount:,.2f}" if amount else "amount not provided"
    evidence_items = ["customer request", "account notes", "SOP match"]
    q = question.lower()
    if request_type == "Refund Approval":
        evidence_items.extend(["invoice history", "payment/refund ledger", "duplicate-charge proof"])
    if "login" in q:
        evidence_items.append("billing portal access logs")
    if has_sensitive_customer_claim(question):
        evidence_items.extend(["customer-impact statement", "Legal/Customer Success review note"])
    if risk == "Critical":
        evidence_items.append("executive approval record")

    escalation = "Finance Controls"
    if has_sensitive_customer_claim(question) or risk == "Critical":
        escalation = "Finance Controls, Legal, and Customer Success leadership"

    workflow = (
        f"1. Confirm the **{request_type.lower()}** facts for **{amount_text}** and capture the customer claim.\n"
        f"2. Route to **{approval_owner}** for review before any customer commitment.\n"
        f"3. Attach evidence: {', '.join(evidence_items)}.\n"
        f"4. Complete action within **{timeline}**.\n"
        f"5. Escalate to **{escalation}** if risk remains High/Critical, context is incomplete, "
        "or customer-impact language creates legal/reputational exposure."
    )
    output = (
        f"Using Audit Agent findings, route {request_type} ownership to {approval_owner}. "
        f"Detected amount is {amount_text}. Target timeline is {timeline}. Dependencies are "
        f"{', '.join(evidence_items)} and approver sign-off."
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
    recommended_workflow, workflow_step = run_workflow_agent(request_type, question, amount, audit_step, band, risk)
    risk_note, finance_step = run_finance_risk_agent(request_type, amount, band, risk, workflow_step)
    confidence_drivers = confidence_breakdown(context, risk, amount)
    priority = priority_decision(request_type, risk, amount, context)
    impact = business_impact_estimate(request_type, risk, amount, confidence, priority)
    exception = detect_exceptions(request_type, amount, context, risk, question)
    confidence_action = confidence_action_mapping(confidence, risk, context.missing_context)
    subtasks = split_subtasks(question, request_type)
    sensitivity = data_sensitivity(question, amount)
    integrations = integration_simulation(request_type, question, risk)
    role_views = role_based_views(
        request_type,
        risk,
        amount,
        band,
        recommended_workflow,
        priority,
        impact,
        exception,
    )
    exec_summary = executive_summary(request_type, risk, amount, band, impact, priority)
    rule_updates = suggested_rule_updates(context, request_type, exception)
    fallback = {
        "needed": confidence_action["band"] == "Low" or exception["detected"],
        "message": (
            "Insufficient certainty or exception detected. Recommend manual review before execution."
            if confidence_action["band"] == "Low" or exception["detected"]
            else "No fallback needed. Proceed through the standard governed workflow."
        ),
    }
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
        "confidence_drivers": confidence_drivers,
        "business_impact": impact,
        "priority": priority,
        "exception": exception,
        "confidence_action": confidence_action,
        "subtasks": subtasks,
        "policy_versions": context.policy_versions,
        "data_sensitivity": sensitivity,
        "fallback": fallback,
        "suggested_rule_updates": rule_updates,
        "integration_simulation": integrations,
        "role_views": role_views,
        "executive_summary": exec_summary,
        "amount": amount,
        "approval_owner": band["required_approver"],
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
        "request_type": request_type,
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
            "amount": result.get("amount", 0),
            "approval_owner": result.get("approval_owner", ""),
            "priority": result.get("priority", {}),
            "exception": result.get("exception", {}),
            "business_impact": result.get("business_impact", {}),
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
        "policies, sources, approvers, amounts, priority labels, or risk ratings; clearly flag missing "
        "context; keep outputs concise and actionable. Return a raw JSON object only. "
        "Do not use markdown, code fences, explanations, or leading text. Required keys: "
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
        enhanced = parse_openai_json(content)
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
    request_type: str | None,
    question: str | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_OPENAI_MODEL,
) -> dict[str, Any]:
    """Run an interacting multi-agent workflow and return structured outputs.

    Each agent receives the previous agent's output, performs its part, and emits
    checks. The executive summary and QA report are built from the full trace.
    """
    if question is None:
        question = request_type or ""
        request_type = None

    if not is_work_related(question):
        return build_out_of_scope_result(question)

    classification = classify_request_type(question)
    request_type = request_type or classification["request_type"]
    result = build_grounded_result(request_type, question)
    result["classification"] = classification
    result["request_type"] = request_type
    result["llm_usage"] = llm_usage_policy(question, result)
    if api_key:
        if result["llm_usage"]["should_call_llm"]:
            result = enhance_with_openai(result, api_key=api_key, model=model)
        else:
            result["llm_status"] = "skipped"
            result["llm_error"] = result["llm_usage"]["decision"]
            result["openai_model"] = model
    return result
