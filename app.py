"""Agent Maestro Streamlit interface.

The UI stays intentionally thin: it collects the operator request, calls the
CrewAI orchestration layer in crews/crew.py, renders results, and persists eval
signals for continuous improvement.
"""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st

from crews.crew import DEFAULT_OPENAI_MODEL, run_operations_crew, tokenize


APP_DIR = Path(__file__).parent
OUTPUT_DIR = APP_DIR / "outputs"
EVAL_LOG = OUTPUT_DIR / "eval_log.csv"
BACKLOG = OUTPUT_DIR / "improvement_backlog.csv"
FEEDBACK_LOG = OUTPUT_DIR / "feedback_log.csv"
OVERRIDE_LOG = OUTPUT_DIR / "override_log.csv"
AGENT_PERFORMANCE_LOG = OUTPUT_DIR / "agent_performance_log.csv"

EVAL_COLUMNS = [
    "timestamp",
    "request_type",
    "user_question",
    "agent_used",
    "confidence",
    "missing_context",
    "risk_level",
    "output_quality_placeholder",
    "improvement_needed",
]

BACKLOG_COLUMNS = [
    "timestamp",
    "request_type",
    "trigger",
    "improvement_item",
    "owner",
    "status",
]

FEEDBACK_COLUMNS = [
    "timestamp",
    "request_type",
    "user_question",
    "rating",
    "what_was_wrong",
    "added_context",
]

OVERRIDE_COLUMNS = [
    "timestamp",
    "request_type",
    "user_question",
    "override_occurred",
    "override_reason",
    "approved_by",
    "root_cause",
]

AGENT_PERFORMANCE_COLUMNS = [
    "timestamp",
    "request_type",
    "agent",
    "failed_check",
]


def ensure_output_files() -> None:
    """Create output CSV files if a fresh clone does not have them yet."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not EVAL_LOG.exists():
        pd.DataFrame(columns=EVAL_COLUMNS).to_csv(EVAL_LOG, index=False)
    if not BACKLOG.exists():
        pd.DataFrame(columns=BACKLOG_COLUMNS).to_csv(BACKLOG, index=False)
    if not FEEDBACK_LOG.exists():
        pd.DataFrame(columns=FEEDBACK_COLUMNS).to_csv(FEEDBACK_LOG, index=False)
    if not OVERRIDE_LOG.exists():
        pd.DataFrame(columns=OVERRIDE_COLUMNS).to_csv(OVERRIDE_LOG, index=False)
    if not AGENT_PERFORMANCE_LOG.exists():
        pd.DataFrame(columns=AGENT_PERFORMANCE_COLUMNS).to_csv(AGENT_PERFORMANCE_LOG, index=False)


def append_csv_row(path: Path, row: dict, columns: list[str]) -> None:
    frame = pd.DataFrame([row], columns=columns)
    frame.to_csv(path, mode="a", header=not path.exists() or path.stat().st_size == 0, index=False)


def log_eval(request_type: str, user_question: str, result: dict) -> None:
    """Persist one evaluation row and optional improvement backlog items."""
    timestamp = datetime.now().isoformat(timespec="seconds")
    missing_context = bool(result["missing_context"])
    confidence = result["confidence"]
    risk_level = result["risk_level"]
    improvement_needed = missing_context or confidence < 0.72 or risk_level in {"High", "Critical"}

    append_csv_row(
        EVAL_LOG,
        {
            "timestamp": timestamp,
            "request_type": request_type,
            "user_question": user_question,
            "agent_used": "Audit, Knowledge, Workflow, Finance Risk, Executive Summary",
            "confidence": confidence,
            "missing_context": missing_context,
            "risk_level": risk_level,
            "output_quality_placeholder": "Pending human review",
            "improvement_needed": improvement_needed,
        },
        EVAL_COLUMNS,
    )

    if improvement_needed:
        triggers = []
        if missing_context:
            triggers.append(("Missing context", "Add or refresh SOP coverage for this scenario."))
        if confidence < 0.72:
            triggers.append(("Low confidence", "Review agent reasoning and enrich request classification examples."))
        if risk_level in {"High", "Critical"}:
            triggers.append(("High risk", "Confirm governance review path and threshold policy language."))

        for trigger, item in triggers:
            append_csv_row(
                BACKLOG,
                {
                    "timestamp": timestamp,
                    "request_type": request_type,
                    "trigger": trigger,
                    "improvement_item": item,
                    "owner": "Ops Excellence",
                    "status": "Open",
                },
                BACKLOG_COLUMNS,
            )

    for failed in result.get("interaction_report", {}).get("failed_checks", []):
        append_csv_row(
            AGENT_PERFORMANCE_LOG,
            {
                "timestamp": timestamp,
                "request_type": request_type,
                "agent": failed.get("agent", "Unknown"),
                "failed_check": failed.get("check", "Unknown"),
            },
            AGENT_PERFORMANCE_COLUMNS,
        )


def load_table(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path)


def find_similar_cases(request_type: str, user_question: str, limit: int = 3) -> dict:
    eval_frame = load_table(EVAL_LOG, EVAL_COLUMNS)
    if eval_frame.empty:
        return {"count": 0, "typical_resolution": "Not enough history", "previous_risk": "N/A", "cases": []}

    query_tokens = tokenize(f"{request_type} {user_question}")
    scored = []
    for _, row in eval_frame.iterrows():
        case_tokens = tokenize(f"{row.get('request_type', '')} {row.get('user_question', '')}")
        score = len(query_tokens & case_tokens)
        if row.get("request_type") == request_type:
            score += 3
        if score:
            scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    rows = [row for _, row in scored[:limit]]
    if not rows:
        return {"count": 0, "typical_resolution": "Not enough history", "previous_risk": "N/A", "cases": []}

    risk_values = [str(row.get("risk_level", "Unknown")) for row in rows]
    previous_risk = pd.Series(risk_values).mode().iloc[0]
    avg_confidence = pd.Series([float(row.get("confidence", 0)) for row in rows]).mean()
    typical_resolution = "Same business day" if previous_risk in {"High", "Critical"} else "1-2 business days"
    return {
        "count": len(rows),
        "typical_resolution": typical_resolution,
        "previous_risk": previous_risk,
        "average_confidence": avg_confidence,
        "cases": [
            {
                "timestamp": row.get("timestamp", ""),
                "request_type": row.get("request_type", ""),
                "risk_level": row.get("risk_level", ""),
                "confidence": row.get("confidence", ""),
                "user_question": row.get("user_question", ""),
            }
            for row in rows
        ],
    }


def log_feedback(request_type: str, user_question: str, rating: str, wrong: str, added_context: str) -> None:
    append_csv_row(
        FEEDBACK_LOG,
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "request_type": request_type,
            "user_question": user_question,
            "rating": rating,
            "what_was_wrong": wrong,
            "added_context": added_context,
        },
        FEEDBACK_COLUMNS,
    )
    if rating == "Incorrect" or added_context.strip():
        append_csv_row(
            BACKLOG,
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "request_type": request_type,
                "trigger": "Human feedback",
                "improvement_item": added_context.strip() or wrong.strip() or "Review incorrect agent output.",
                "owner": "Ops Excellence",
                "status": "Open",
            },
            BACKLOG_COLUMNS,
        )


def log_override(
    request_type: str,
    user_question: str,
    override_occurred: bool,
    override_reason: str,
    approved_by: str,
    root_cause: str,
) -> None:
    append_csv_row(
        OVERRIDE_LOG,
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "request_type": request_type,
            "user_question": user_question,
            "override_occurred": override_occurred,
            "override_reason": override_reason,
            "approved_by": approved_by,
            "root_cause": root_cause,
        },
        OVERRIDE_COLUMNS,
    )
    if override_occurred:
        append_csv_row(
            BACKLOG,
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "request_type": request_type,
                "trigger": "Decision override",
                "improvement_item": f"Override reason: {override_reason}. Root cause: {root_cause}.",
                "owner": "Ops Excellence",
                "status": "Open",
            },
            BACKLOG_COLUMNS,
        )


def get_secret_api_key() -> str:
    try:
        return st.secrets.get("OPENAI_API_KEY", "")
    except Exception:
        return ""


def render_app_shell() -> tuple[str, str]:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@500;600;700&display=swap');

        :root {
            --ink: #17203c;
            --muted: #667085;
            --purple: #6c3cff;
            --purple-dark: #4c24c9;
            --cyan: #15c7d4;
            --coral: #ff6b6b;
            --lime: #b8e83e;
            --sun: #ffca3a;
            --canvas: #f7f5ff;
            --card: rgba(255, 255, 255, 0.92);
            --line: #e5e0f6;
        }
        html, body, [class*="css"] {
            font-family: 'DM Sans', sans-serif;
        }
        .stApp {
            color: var(--ink);
            background:
                radial-gradient(circle at 8% 3%, rgba(108, 60, 255, 0.13), transparent 26rem),
                radial-gradient(circle at 96% 10%, rgba(21, 199, 212, 0.14), transparent 25rem),
                linear-gradient(180deg, #fbfaff 0%, #f7f5ff 52%, #f0faff 100%);
        }
        .block-container {
            padding-top: 1.35rem;
            padding-bottom: 3rem;
            max-width: 1440px;
        }
        h1, h2, h3, h4 {
            font-family: 'Space Grotesk', sans-serif !important;
            color: var(--ink);
            letter-spacing: -0.025em !important;
        }
        h2, h3 { margin-top: 0.75rem; }
        p, label, [data-testid="stCaptionContainer"] { color: var(--muted); }

        [data-testid="stHeader"] {
            background: rgba(247, 245, 255, 0.72);
            backdrop-filter: blur(14px);
        }
        [data-testid="stSidebar"] {
            background:
                radial-gradient(circle at 10% 10%, rgba(21, 199, 212, 0.16), transparent 16rem),
                linear-gradient(165deg, #25154f 0%, #17112f 58%, #102d3b 100%);
            border-right: 1px solid rgba(255, 255, 255, 0.12);
        }
        [data-testid="stSidebar"] * { color: #f7f4ff; }
        [data-testid="stSidebar"] p { color: #c9c3df; }
        [data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.15); }
        .am-sidebar-brand {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            margin: 0.25rem 0 1.35rem;
        }
        .am-logo {
            display: grid;
            place-items: center;
            width: 42px;
            height: 42px;
            border-radius: 13px;
            color: #24154b;
            font: 700 1.05rem 'Space Grotesk', sans-serif;
            background: linear-gradient(135deg, var(--lime), var(--cyan));
            box-shadow: 0 10px 24px rgba(21, 199, 212, 0.28);
        }
        .am-brand-name { font: 700 1.05rem 'Space Grotesk', sans-serif; color: #fff; }
        .am-brand-kicker { color: #aaa1c7; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.12em; }

        .am-page-hero {
            position: relative;
            overflow: hidden;
            padding: 2.15rem 2.35rem;
            margin: 0.2rem 0 1.35rem;
            border-radius: 26px;
            color: white;
            background: linear-gradient(120deg, #5930df 0%, #6c3cff 42%, #08aeba 100%);
            box-shadow: 0 22px 54px rgba(76, 36, 201, 0.22);
        }
        .am-page-hero::after {
            content: '';
            position: absolute;
            width: 250px;
            height: 250px;
            right: -55px;
            top: -95px;
            border-radius: 50%;
            border: 34px solid rgba(184, 232, 62, 0.24);
        }
        .am-eyebrow {
            color: #dcff8d;
            font-size: 0.76rem;
            font-weight: 700;
            letter-spacing: 0.13em;
            text-transform: uppercase;
        }
        .am-page-hero h1 {
            color: #fff;
            font-size: clamp(2rem, 4vw, 3.3rem);
            margin: 0.2rem 0 0.45rem;
            line-height: 1.04;
        }
        .am-page-hero p { color: #eeeaff; max-width: 720px; margin: 0; font-size: 1.02rem; }
        .am-status-pill {
            display: inline-flex;
            align-items: center;
            gap: 0.45rem;
            margin-top: 1.15rem;
            padding: 0.42rem 0.72rem;
            border: 1px solid rgba(255,255,255,0.28);
            border-radius: 999px;
            color: #fff;
            background: rgba(255,255,255,0.12);
            font-size: 0.78rem;
            font-weight: 600;
        }
        .am-status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--lime); box-shadow: 0 0 0 5px rgba(184,232,62,.13); }

        [data-testid="stMetric"] {
            position: relative;
            overflow: hidden;
            border: 1px solid rgba(108, 60, 255, 0.13);
            border-radius: 18px;
            padding: 1rem 1.05rem;
            background: var(--card);
            min-height: 108px;
            box-shadow: 0 12px 30px rgba(71, 48, 132, 0.08);
        }
        [data-testid="stMetric"]::before {
            content: '';
            position: absolute;
            left: 0;
            top: 0;
            width: 100%;
            height: 4px;
            background: linear-gradient(90deg, var(--purple), var(--cyan), var(--lime));
        }
        [data-testid="stMetric"] label,
        [data-testid="stMetric"] label p {
            color: #7a7094 !important;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            font-size: 0.72rem;
        }
        [data-testid="stMetricValue"] {
            font-family: 'Space Grotesk', sans-serif;
            font-size: 1.55rem;
            line-height: 1.15;
            color: var(--ink) !important;
        }
        [data-testid="stMetricDelta"] { color: #079da8 !important; }
        [data-testid="stSidebar"] .stTextInput input {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }
        div[data-testid="stForm"] {
            border: 1px solid rgba(108, 60, 255, 0.14);
            border-radius: 20px;
            padding: 1.2rem;
            background: var(--card);
            box-shadow: 0 14px 36px rgba(71, 48, 132, 0.08);
        }
        div[data-testid="stForm"] label,
        div[data-testid="stForm"] label p,
        div[data-testid="stForm"] p,
        div[data-testid="stForm"] span {
            color: var(--ink) !important;
        }
        div[data-testid="stTextArea"] textarea,
        div[data-testid="stTextInput"] input,
        div[data-baseweb="select"] > div {
            background: #fff !important;
            color: var(--ink) !important;
            border: 1px solid #ded8f0 !important;
            border-radius: 12px !important;
        }
        div[data-testid="stTextArea"] textarea::placeholder,
        div[data-testid="stTextInput"] input::placeholder {
            color: #9a91b0 !important;
        }
        .am-hero-body {
            color: var(--muted);
            font-size: 0.95rem;
            line-height: 1.55;
        }
        .am-panel {
            position: relative;
            overflow: hidden;
            border: 1px solid rgba(108, 60, 255, 0.13);
            border-radius: 18px;
            padding: 1.05rem 1.15rem 1.05rem 1.35rem;
            margin-bottom: 0.9rem;
            background: var(--card);
            color: var(--ink);
            box-shadow: 0 10px 28px rgba(71, 48, 132, 0.07);
        }
        .am-panel::before {
            content: '';
            position: absolute;
            inset: 0 auto 0 0;
            width: 5px;
            background: linear-gradient(180deg, var(--coral), var(--purple));
        }
        .am-panel h4 {
            margin: 0 0 0.45rem 0;
            color: var(--ink);
            font-size: 1.02rem;
        }
        .am-panel p {
            margin: 0.25rem 0;
            color: var(--muted);
            line-height: 1.45;
        }
        .am-kv {
            display: grid;
            grid-template-columns: minmax(110px, 0.35fr) 1fr;
            gap: 0.45rem 0.8rem;
            font-size: 0.94rem;
        }
        .am-kv b {
            color: var(--purple-dark);
        }
        .am-list {
            margin: 0.25rem 0 0 1rem;
            padding: 0;
        }
        .am-list li {
            margin: 0.2rem 0;
            color: var(--muted);
        }
        .am-badge-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.45rem;
            margin-top: 0.55rem;
        }
        .am-badge {
            border: 1px solid #d8cffd;
            border-radius: 999px;
            padding: 0.26rem 0.62rem;
            background: #f0ecff;
            color: var(--purple-dark);
            font-size: 0.84rem;
            font-weight: 700;
        }
        div[data-testid="stTabs"] [data-baseweb="tab-list"] {
            gap: 0.5rem;
            padding: 0.35rem;
            border-radius: 14px;
            background: rgba(226, 220, 247, 0.58);
        }
        div[data-testid="stTabs"] button {
            height: auto;
            padding: 0.62rem 1rem;
            border-radius: 10px;
            color: #756b8f;
            font-weight: 700;
        }
        div[data-testid="stTabs"] button p { color: inherit !important; }
        div[data-testid="stTabs"] button[aria-selected="true"] {
            color: #fff;
            background: linear-gradient(110deg, var(--purple), #8c4fff);
            box-shadow: 0 6px 16px rgba(108,60,255,.24);
        }
        div[data-testid="stTabs"] [data-baseweb="tab-highlight"] { display: none; }
        .stButton > button, [data-testid="stFormSubmitButton"] > button {
            border: 0;
            border-radius: 12px;
            min-height: 44px;
            color: #fff;
            font-weight: 700;
            background: linear-gradient(110deg, var(--purple), #8b4fff 55%, #0eb8c3);
            box-shadow: 0 10px 22px rgba(108,60,255,.22);
            transition: transform .18s ease, box-shadow .18s ease;
        }
        .stButton > button:hover, [data-testid="stFormSubmitButton"] > button:hover {
            color: #fff;
            transform: translateY(-1px);
            box-shadow: 0 14px 28px rgba(108,60,255,.3);
        }
        [data-testid="stAlert"] {
            border-radius: 14px;
            border-width: 1px;
        }
        [data-testid="stDataFrame"] {
            border: 1px solid var(--line);
            border-radius: 16px;
            overflow: hidden;
            box-shadow: 0 10px 26px rgba(71,48,132,.06);
        }
        [data-testid="stExpander"] {
            border: 1px solid var(--line);
            border-radius: 14px;
            background: rgba(255,255,255,.78);
        }
        [data-testid="stProgress"] > div > div { background: linear-gradient(90deg, var(--purple), var(--cyan), var(--lime)); }
        div[role="radiogroup"] label { padding: .28rem .7rem; border-radius: 999px; }
        div[data-testid="stToggle"] [data-checked="true"] { background: var(--purple) !important; }
        @media (max-width: 780px) {
            .block-container { padding: 1rem 0.8rem 2rem; }
            .am-page-hero { padding: 1.6rem 1.35rem; border-radius: 20px; }
            .am-page-hero h1 { font-size: 2rem; }
        }
        textarea {
            font-size: 0.92rem !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.markdown(
            """
            <div class="am-sidebar-brand">
                <div class="am-logo">AM</div>
                <div><div class="am-brand-name">Agent Maestro</div><div class="am-brand-kicker">Operations OS</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.header("⚙️ Workspace setup")
        default_key = get_secret_api_key()
        api_key = st.text_input(
            "OpenAI API key",
            value=default_key,
            type="password",
            placeholder="sk-...",
            help="Stored only in this Streamlit session unless you provide it through Streamlit secrets.",
        )
        model = st.text_input("Model", value=DEFAULT_OPENAI_MODEL)
        if api_key:
            st.success("OpenAI enhancement is ready.")
        else:
            st.info("Add a key to use the OpenAI API. The app can still run local routing checks.")

        st.divider()
        st.subheader("🛡️ Guardrails")
        st.write("The agent only accepts Command Ops work.")
        st.write("Out-of-scope questions are blocked before retrieval or LLM calls.")
        st.write("Policy, approver, source, and risk facts must stay grounded in the local context.")

    return api_key.strip(), model.strip() or DEFAULT_OPENAI_MODEL


def render_agent_outputs(result: dict) -> None:
    st.subheader("Agent Outputs")
    for agent, output in result["agent_outputs"].items():
        with st.expander(agent, expanded=agent in {"Executive Summary Agent", "Workflow Agent"}):
            st.write(output)


def panel(title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="am-panel">
            <h4>{escape(title)}</h4>
            <div class="am-hero-body">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_overview_strip(result: dict) -> None:
    priority = result["priority"]
    impact = result["business_impact"]
    left, second, middle, right, fourth, fifth = st.columns([1.1, 1.05, 1, 1, 1, 0.9])
    left.metric("Detected", result.get("request_type", "Unknown"))
    second.metric("Priority", f"{priority['label']}", priority["handle_by"])
    middle.metric("Risk", result["risk_level"], result.get("approval_owner") or "No approver")
    right.metric("Confidence", f"{result['confidence']:.0%}", "with driver breakdown")
    fourth.metric("Time Saved", f"{impact['time_saved_minutes']} min", f"${impact['cost_impact_usd']} impact")
    fifth.metric("Sources", len(result["sources"]), "local context")


def render_decision_panel(result: dict, selected_role: str, executive_mode: bool) -> None:
    if executive_mode:
        summary = result["executive_summary"]
        rows = "".join(
            f"<div><b>{escape(label)}</b></div><div>{escape(value)}</div>"
            for label, value in summary.items()
        )
        panel("Executive Summary", f'<div class="am-kv">{rows}</div>')
        return

    role_output = escape(result["role_views"][selected_role]).replace("\n", "<br>")
    panel(f"{selected_role} Decision View", role_output)


def render_classification_panel(result: dict) -> None:
    classification = result.get("classification", {})
    detected_type = classification.get("request_type", result.get("request_type", "Unknown"))
    confidence = classification.get("confidence", 0)
    reasons = classification.get("reasons", [])
    reason_badges = "".join(
        f'<span class="am-badge">{escape(str(reason))}</span>'
        for reason in reasons
    )
    panel(
        "Detected Workflow",
        (
            f"<p><b>{escape(detected_type)}</b> "
            f"({confidence:.0%} classification confidence)</p>"
            f'<div class="am-badge-row">{reason_badges}</div>'
        ),
    )


def render_memory_layer(similar_cases: dict) -> None:
    st.markdown("#### Similar Past Cases")
    if similar_cases["count"] == 0:
        st.info("No similar historical cases found yet. This run will become part of future memory.")
        return

    left, middle, right = st.columns(3)
    left.metric("Similar Cases", similar_cases["count"])
    middle.metric("Typical Resolution", similar_cases["typical_resolution"])
    right.metric("Previous Risk", similar_cases["previous_risk"])
    st.dataframe(pd.DataFrame(similar_cases["cases"]), use_container_width=True, hide_index=True)


def render_confidence_breakdown(result: dict) -> None:
    st.markdown("#### Confidence Breakdown")
    left, right = st.columns(2)
    with left:
        items = "".join(f"<li>{escape(item)}</li>" for item in result["confidence_drivers"]["positive"])
        panel("Positive Drivers", f'<ul class="am-list">{items}</ul>')
    with right:
        items = "".join(f"<li>{escape(item)}</li>" for item in result["confidence_drivers"]["negative"])
        panel("Watch Items", f'<ul class="am-list">{items}</ul>')


def render_enterprise_signals(result: dict) -> None:
    st.markdown("#### Business Impact Engine")
    impact = result["business_impact"]
    left, middle, right, fourth = st.columns(4)
    left.metric("Time Saved", f"{impact['time_saved_minutes']} min")
    middle.metric("Cost Impact", f"${impact['cost_impact_usd']}")
    right.metric("SLA Improvement", impact["sla_improvement"])
    fourth.metric("Risk Avoided", impact["risk_avoided"])

    st.markdown("#### Priority Decision")
    priority = result["priority"]
    driver_items = "".join(f"<li>{escape(driver)}</li>" for driver in priority["drivers"])
    panel(
        f"{priority['label']} Priority ({priority['score']}/100)",
        f"<p><b>Target:</b> {escape(priority['handle_by'])}</p><ul class=\"am-list\">{driver_items}</ul>",
    )

    st.markdown("#### Integration Simulation")
    integrations = result["integration_simulation"]
    panel(
        "Enterprise Stack Signals",
        (
            f"<p><b>Source:</b> {escape(integrations['source'])}</p>"
            f"<p>{escape(integrations['crm_sync'])}</p>"
            f"<p>{escape(integrations['slack'])}</p>"
        ),
    )

    exception = result["exception"]
    if exception["detected"]:
        st.warning("Exception Detected")
        for reason in exception["reasons"]:
            st.write(f"- {reason}")
        st.write(exception["required_action"])
    else:
        st.success(exception["required_action"])


def render_governance_signals(result: dict) -> None:
    left, right = st.columns(2)
    with left:
        confidence_action = result["confidence_action"]
        panel(
            "Confidence to Action",
            (
                f"<p><b>{escape(confidence_action['band'])}</b>: "
                f"{escape(confidence_action['action'])}</p>"
                f"<p>{escape(confidence_action['reason'])}</p>"
            ),
        )
        sensitivity = result["data_sensitivity"]
        flags = "".join(f"<li>{escape(flag)}</li>" for flag in sensitivity["flags"])
        panel(
            "Security and Data Sensitivity",
            (
                f"<p><b>Level:</b> {escape(sensitivity['level'])}</p>"
                f"<p><b>Sensitive data detected:</b> {'Yes' if sensitivity['detected'] else 'No'}</p>"
                f"<ul class=\"am-list\">{flags}</ul>"
                f"<p>{escape(sensitivity['handling'])}</p>"
            ),
        )
    with right:
        llm = result.get("llm_usage", {})
        panel(
            "LLM Cost Control",
            (
                f"<p><b>Decision:</b> {escape(str(llm.get('decision', 'Not estimated')))}</p>"
                f"<p><b>Estimated tokens:</b> {llm.get('estimated_input_tokens', 0)} in / "
                f"{llm.get('estimated_output_tokens', 0)} out</p>"
                f"<p><b>Estimated API cost:</b> ${llm.get('estimated_cost_usd', 0):.4f}</p>"
            ),
        )
        fallback = result["fallback"]
        panel(
            "Failure Mode UX",
            f"<p><b>Fallback needed:</b> {'Yes' if fallback['needed'] else 'No'}</p><p>{escape(fallback['message'])}</p>",
        )


def render_policy_traceability(result: dict) -> None:
    st.markdown("#### Policy Version Trace")
    policies = result.get("policy_versions", [])
    if not policies:
        st.info("No policy version metadata available for this request.")
        return
    st.dataframe(pd.DataFrame(policies), use_container_width=True, hide_index=True)


def render_subtasks(result: dict) -> None:
    st.markdown("#### Multi-Request Breakdown")
    st.dataframe(pd.DataFrame(result.get("subtasks", [])), use_container_width=True, hide_index=True)


def render_rule_updates(result: dict) -> None:
    suggestions = result.get("suggested_rule_updates", [])
    if not suggestions:
        st.success("No rule update suggested for this run.")
        return
    st.markdown("#### Suggested Rule Updates")
    for item in suggestions:
        st.write(f"- {item}")


def render_role_view(result: dict, selected_role: str, executive_mode: bool) -> None:
    if executive_mode:
        st.markdown("#### Executive Summary")
        summary = result["executive_summary"]
        for label, value in summary.items():
            st.write(f"**{label}:** {value}")
        return

    st.markdown(f"#### {selected_role} View")
    st.markdown(result["role_views"][selected_role])


def render_feedback_form(request_type: str, user_question: str) -> None:
    st.subheader("Human Feedback Loop")
    with st.form("feedback_form"):
        rating = st.radio("Was this helpful?", ["Correct", "Incorrect"], horizontal=True)
        wrong = st.text_area("What was wrong?", height=90)
        added_context = st.text_area("Add missing context or correction", height=110)
        saved = st.form_submit_button("Save feedback", use_container_width=True)
    if saved:
        log_feedback(request_type, user_question, rating, wrong, added_context)
        st.success("Feedback captured and routed into the improvement loop.")


def render_override_form(request_type: str, user_question: str) -> None:
    st.subheader("Decision Override Tracking")
    with st.form("override_form"):
        override_occurred = st.checkbox("Override occurred")
        override_reason = st.text_input("Why override?", placeholder="Example: Customer retention exception")
        approved_by = st.text_input("Approved by", placeholder="Example: Director")
        root_cause = st.selectbox(
            "Root cause",
            ["System wrong", "Policy incomplete", "Customer exception", "Business judgment", "Other"],
        )
        saved = st.form_submit_button("Save override", use_container_width=True)
    if saved:
        log_override(request_type, user_question, override_occurred, override_reason, approved_by, root_cause)
        st.success("Override decision captured for governance review.")


def render_check_report(result: dict) -> None:
    st.markdown("#### Automatic Agent Check Report")
    report = result["interaction_report"]
    if report["failed_checks"]:
        st.warning(report["report"])
    else:
        st.success(report["report"])
    st.progress(report["checks_passed"] / report["checks_total"])
    st.dataframe(pd.DataFrame(result["interaction_trace"]), use_container_width=True, hide_index=True)


def render_workflow_and_risk(result: dict) -> None:
    left, right = st.columns([1.15, 0.85])
    with left:
        st.markdown("#### Recommended Workflow")
        st.markdown(result["recommended_workflow"])
    with right:
        st.markdown("#### Risk Note")
        st.info(result["risk_note"])

        exception = result["exception"]
        if exception["detected"]:
            st.warning("Exception Detected")
            for reason in exception["reasons"]:
                st.write(f"- {reason}")
            st.write(exception["required_action"])
        else:
            st.success(exception["required_action"])


def render_context_sources(result: dict) -> None:
    st.markdown("#### Source / Context Used")
    if result["sources"]:
        for source, snippet in zip(result["sources"], result.get("context_snippets", [])):
            with st.expander(source):
                st.write(snippet)
    else:
        st.warning("No local source matched this request.")

    if result.get("contradictions"):
        st.warning("Contradictions or outdated context detected:")
        for item in result["contradictions"]:
            st.write(f"- {item}")

    if result["missing_context"]:
        st.warning("Missing context detected. An improvement backlog item will be created.")


def render_results(result: dict, similar_cases: dict | None = None, selected_role: str = "Analyst", executive_mode: bool = False) -> None:
    render_overview_strip(result)

    if result.get("llm_status") == "enhanced":
        st.success(f"OpenAI-generated analysis applied with {result['openai_model']}.")
    elif result.get("llm_status") == "error":
        st.warning(f"OpenAI generation failed, so grounded local output is shown. {result['llm_error']}")

    if result["risk_level"] == "Out of Scope":
        panel("Scope Gate", escape(result["recommended_workflow"]))
        render_confidence_breakdown(result)
        render_check_report(result)
        return

    decision_tab, execute_tab, evidence_tab, system_tab = st.tabs(
        ["Decision", "Execute", "Evidence", "System"]
    )

    with decision_tab:
        render_classification_panel(result)
        render_governance_signals(result)
        top_left, top_right = st.columns([1.15, 0.85])
        with top_left:
            render_decision_panel(result, selected_role, executive_mode)
        with top_right:
            priority = result["priority"]
            panel(
                "Next Best Action",
                (
                    f"<p><b>{escape(priority['label'])}</b> priority: "
                    f"{escape(priority['handle_by'])}</p>"
                    f"<p>Owner: {escape(result.get('approval_owner') or 'Not assigned')}</p>"
                ),
            )
        render_enterprise_signals(result)

    with execute_tab:
        render_subtasks(result)
        render_workflow_and_risk(result)
        if result["email_draft"]:
            st.markdown("#### Email Draft")
            st.text_area("Draft", result["email_draft"], height=210, label_visibility="collapsed")

    with evidence_tab:
        if similar_cases is not None:
            render_memory_layer(similar_cases)
        render_policy_traceability(result)
        render_context_sources(result)

    with system_tab:
        render_confidence_breakdown(result)
        render_rule_updates(result)
        render_check_report(result)
        render_agent_outputs(result)


def main() -> None:
    st.set_page_config(page_title="Agent Maestro", page_icon="⚡", layout="wide")
    ensure_output_files()
    api_key, model = render_app_shell()

    st.markdown(
        """
        <section class="am-page-hero">
            <div class="am-eyebrow">AI-powered command center</div>
            <h1>Turn operations into momentum.</h1>
            <p>Route complex requests, surface risk, and move from decision to action with a coordinated crew of specialist agents.</p>
            <div class="am-status-pill"><span class="am-status-dot"></span> Operations crew online</div>
        </section>
        """,
        unsafe_allow_html=True,
    )

    request_tab, dashboard_tab = st.tabs(["⚡ Operations Console", "◈ Evaluation Dashboard"])

    with request_tab:
        st.subheader("Launch a command flow")
        st.caption("Give the crew a real operations scenario. It will classify, validate, and recommend the next best action.")
        control_left, control_right = st.columns([1, 1])
        with control_left:
            selected_role = st.selectbox("View as role", ["Analyst", "Manager", "Director"])
        with control_right:
            executive_mode = st.toggle("Executive Summary Mode")

        with st.form("operations_request"):
            user_question = st.text_area(
                "Operations request",
                placeholder="Example: A customer requested a $7,500 refund after duplicate billing and failed login attempts.",
                height=150,
            )
            submitted = st.form_submit_button("⚡ Run command flow", type="primary", use_container_width=True)

        if submitted:
            if not user_question.strip():
                st.error("Enter an operations request to route through the agent crew.")
            else:
                with st.spinner("Routing request through the operations crew..."):
                    result = run_operations_crew(
                        user_question.strip(),
                        api_key=api_key,
                        model=model,
                    )
                    request_type = result["request_type"]
                    similar_cases = find_similar_cases(request_type, user_question.strip())
                    log_eval(request_type, user_question.strip(), result)
                st.session_state["last_result"] = result
                st.session_state["last_request_type"] = request_type
                st.session_state["last_user_question"] = user_question.strip()
                st.session_state["last_similar_cases"] = similar_cases

        if "last_result" in st.session_state:
            render_results(
                st.session_state["last_result"],
                similar_cases=st.session_state.get("last_similar_cases"),
                selected_role=selected_role,
                executive_mode=executive_mode,
            )
            render_feedback_form(
                st.session_state["last_request_type"],
                st.session_state["last_user_question"],
            )
            render_override_form(
                st.session_state["last_request_type"],
                st.session_state["last_user_question"],
            )

    with dashboard_tab:
        eval_frame = load_table(EVAL_LOG, EVAL_COLUMNS)
        backlog_frame = load_table(BACKLOG, BACKLOG_COLUMNS)
        feedback_frame = load_table(FEEDBACK_LOG, FEEDBACK_COLUMNS)
        override_frame = load_table(OVERRIDE_LOG, OVERRIDE_COLUMNS)
        agent_frame = load_table(AGENT_PERFORMANCE_LOG, AGENT_PERFORMANCE_COLUMNS)

        st.subheader("System pulse")
        if eval_frame.empty:
            st.info("No evaluation data yet. Run a few Command Ops flows to populate trends.")
        else:
            left, middle, right = st.columns(3)
            left.metric("Total Runs", len(eval_frame))
            middle.metric("Missing Context Rate", f"{eval_frame['missing_context'].mean():.0%}")
            right.metric("Improvement Needed", f"{eval_frame['improvement_needed'].mean():.0%}")

            risk_counts = eval_frame["risk_level"].value_counts().reset_index()
            risk_counts.columns = ["risk_level", "count"]
            st.write("Queue and Throughput View")
            queue_left, queue_mid, queue_right = st.columns(3)
            queue_left.metric("Open Requests", len(eval_frame))
            queue_mid.metric("High-Risk Queue", int(eval_frame["risk_level"].isin(["High", "Critical"]).sum()))
            queue_right.metric("SLA Breach Risk", int(eval_frame["improvement_needed"].sum()))
            st.dataframe(
                eval_frame[["timestamp", "request_type", "risk_level", "confidence", "improvement_needed"]].sort_values(
                    ["improvement_needed", "risk_level"], ascending=[False, True]
                ),
                use_container_width=True,
                hide_index=True,
            )

            st.write("Risk-heavy request types")
            st.dataframe(
                eval_frame.groupby(["request_type", "risk_level"]).size().reset_index(name="count").sort_values("count", ascending=False),
                use_container_width=True,
                hide_index=True,
            )

        st.subheader("Agent Performance Monitoring")
        if agent_frame.empty:
            st.info("No agent-level failed checks logged yet.")
        else:
            st.dataframe(
                agent_frame.groupby(["agent", "failed_check"]).size().reset_index(name="count").sort_values("count", ascending=False),
                use_container_width=True,
                hide_index=True,
            )

        if not backlog_frame.empty:
            st.write("Top failure reasons")
            st.dataframe(
                backlog_frame["trigger"].value_counts().reset_index().rename(columns={"trigger": "failure_reason", "count": "count"}),
                use_container_width=True,
                hide_index=True,
            )
            st.write("Most common missing SOP or improvement items")
            st.dataframe(
                backlog_frame["improvement_item"].value_counts().reset_index().rename(columns={"improvement_item": "item", "count": "count"}),
                use_container_width=True,
                hide_index=True,
            )
            st.write("Suggested rule updates")
            rule_candidates = backlog_frame.groupby(["request_type", "trigger"]).size().reset_index(name="count")
            rule_candidates["suggested_rule_update"] = rule_candidates.apply(
                lambda row: f"If {row['request_type']} repeatedly triggers {row['trigger']}, add or refine routing/SOP rule.",
                axis=1,
            )
            st.dataframe(rule_candidates.sort_values("count", ascending=False), use_container_width=True, hide_index=True)

        st.subheader("Eval Log")
        st.dataframe(eval_frame, use_container_width=True, hide_index=True)

        st.subheader("Improvement Backlog")
        st.dataframe(backlog_frame, use_container_width=True, hide_index=True)

        st.subheader("Human Feedback")
        st.dataframe(feedback_frame, use_container_width=True, hide_index=True)

        st.subheader("Decision Overrides")
        st.dataframe(override_frame, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
