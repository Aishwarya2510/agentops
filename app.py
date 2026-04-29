"""Agent Maestro Streamlit interface.

The UI stays intentionally thin: it collects the operator request, calls the
CrewAI orchestration layer in crews/crew.py, renders results, and persists eval
signals for continuous improvement.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from crews.crew import DEFAULT_OPENAI_MODEL, REQUEST_TYPES, run_operations_crew


APP_DIR = Path(__file__).parent
OUTPUT_DIR = APP_DIR / "outputs"
EVAL_LOG = OUTPUT_DIR / "eval_log.csv"
BACKLOG = OUTPUT_DIR / "improvement_backlog.csv"

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


def ensure_output_files() -> None:
    """Create output CSV files if a fresh clone does not have them yet."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    if not EVAL_LOG.exists():
        pd.DataFrame(columns=EVAL_COLUMNS).to_csv(EVAL_LOG, index=False)
    if not BACKLOG.exists():
        pd.DataFrame(columns=BACKLOG_COLUMNS).to_csv(BACKLOG, index=False)


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


def load_table(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path)


def get_secret_api_key() -> str:
    try:
        return st.secrets.get("OPENAI_API_KEY", "")
    except Exception:
        return ""


def render_app_shell() -> tuple[str, str]:
    st.markdown(
        """
        <style>
        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 2rem;
        }
        [data-testid="stMetric"] {
            border: 1px solid #e6e8ec;
            border-radius: 8px;
            padding: 0.8rem 0.9rem;
            background: #ffffff;
        }
        [data-testid="stSidebar"] .stTextInput input {
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        }
        div[data-testid="stForm"] {
            border: 1px solid #e6e8ec;
            border-radius: 8px;
            padding: 1rem;
            background: #fbfcfe;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    with st.sidebar:
        st.header("Setup")
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
        st.subheader("Hard Rules")
        st.write("The agent only accepts Command Ops work.")
        st.write("Out-of-scope questions are blocked before retrieval or LLM calls.")
        st.write("Policy, approver, source, and risk facts must stay grounded in the local context.")

    return api_key.strip(), model.strip() or DEFAULT_OPENAI_MODEL


def render_agent_outputs(result: dict) -> None:
    st.subheader("Agent Outputs")
    for agent, output in result["agent_outputs"].items():
        with st.expander(agent, expanded=agent in {"Executive Summary Agent", "Workflow Agent"}):
            st.write(output)


def render_results(result: dict) -> None:
    left, right, third = st.columns(3)
    left.metric("Confidence", f"{result['confidence']:.0%}")
    right.metric("Risk Level", result["risk_level"])
    third.metric("Context Sources", len(result["sources"]))

    if result.get("llm_status") == "enhanced":
        st.success(f"OpenAI enhancement applied with {result['openai_model']}.")
    elif result.get("llm_status") == "error":
        st.warning(f"OpenAI enhancement failed, so grounded local output is shown. {result['llm_error']}")

    if result["risk_level"] == "Out of Scope":
        st.warning(result["recommended_workflow"])
        st.subheader("Automatic Agent Check Report")
        st.warning(result["interaction_report"]["report"])
        st.dataframe(pd.DataFrame(result["interaction_trace"]), use_container_width=True, hide_index=True)
        return

    render_agent_outputs(result)

    st.subheader("Automatic Agent Check Report")
    report = result["interaction_report"]
    if report["failed_checks"]:
        st.warning(report["report"])
    else:
        st.success(report["report"])
    st.progress(report["checks_passed"] / report["checks_total"])
    st.dataframe(pd.DataFrame(result["interaction_trace"]), use_container_width=True, hide_index=True)

    st.subheader("Recommended Workflow")
    st.markdown(result["recommended_workflow"])

    st.subheader("Risk Note")
    st.info(result["risk_note"])

    if result["email_draft"]:
        st.subheader("Email Draft")
        st.text_area("Draft", result["email_draft"], height=210, label_visibility="collapsed")

    st.subheader("Source / Context Used")
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


def main() -> None:
    st.set_page_config(page_title="Agent Maestro", page_icon="AM", layout="wide")
    ensure_output_files()
    api_key, model = render_app_shell()

    st.title("Agent Maestro")
    st.caption("Command Ops intelligence for workflow, billing, refund, control, and governance work.")

    request_tab, dashboard_tab = st.tabs(["Operations Console", "Evaluation Dashboard"])

    with request_tab:
        st.subheader("Run Command Ops Flow")
        with st.form("operations_request"):
            left, right = st.columns([1, 2])
            with left:
                request_type = st.selectbox("Request type", REQUEST_TYPES)
            with right:
                user_question = st.text_area(
                    "Operations request",
                    placeholder="Example: A customer requested a $7,500 refund after duplicate billing and failed login attempts.",
                    height=150,
                )
            submitted = st.form_submit_button("Run flow", type="primary", use_container_width=True)

        if submitted:
            if not user_question.strip():
                st.error("Enter an operations request to route through the agent crew.")
            else:
                with st.spinner("Routing request through the operations crew..."):
                    result = run_operations_crew(
                        request_type,
                        user_question.strip(),
                        api_key=api_key,
                        model=model,
                    )
                    log_eval(request_type, user_question.strip(), result)
                render_results(result)

    with dashboard_tab:
        st.subheader("Eval Log")
        st.dataframe(load_table(EVAL_LOG, EVAL_COLUMNS), use_container_width=True, hide_index=True)

        st.subheader("Improvement Backlog")
        st.dataframe(load_table(BACKLOG, BACKLOG_COLUMNS), use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
