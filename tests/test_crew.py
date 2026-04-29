from crews.crew import (
    approval_band,
    extract_amount,
    retrieve_context,
    run_operations_crew,
)


def test_extract_amount_uses_largest_detected_amount():
    assert extract_amount("Invoice 123 has duplicate charge of $7,500 and credit 25") == 7500


def test_retrieve_context_finds_refund_and_billing_sources():
    context = retrieve_context(
        "Refund Approval",
        "Customer needs a refund after duplicate billing and login failure.",
    )

    assert any("refund" in source for source in context.sources)
    assert any("billing" in source for source in context.sources)
    assert context.missing_context is False


def test_approval_band_routes_high_value_refund_to_finance_director():
    band = approval_band(7500)

    assert band["approval_band"] == "High"
    assert "Director" in band["required_approver"]


def test_run_operations_crew_agents_interact_and_report_checks():
    result = run_operations_crew(
        "Refund Approval",
        "Customer asks for a $7,500 refund after duplicate billing and login failure.",
    )

    assert result["risk_level"] == "High"
    assert result["confidence"] >= 0.65
    assert "Director" in result["recommended_workflow"]
    assert result["interaction_report"]["checks_total"] > 0
    assert result["interaction_report"]["checks_passed"] <= result["interaction_report"]["checks_total"]

    trace = result["interaction_trace"]
    assert [step["agent"] for step in trace] == [
        "Knowledge Agent",
        "Audit Agent",
        "Workflow Agent",
        "Finance Risk Agent",
        "Executive Summary Agent",
    ]
    assert trace[1]["input_from"] == "Knowledge Agent"
    assert "Workflow Agent" in trace[3]["input_from"]


def test_missing_context_creates_high_risk_signal():
    result = run_operations_crew("Workflow Issue", "Unknown process with missing SOP.")

    assert result["missing_context"] is True
    assert result["risk_level"] == "High"
    assert result["interaction_report"]["failed_checks"]


def test_out_of_scope_question_is_blocked_before_agent_flow():
    result = run_operations_crew("Workflow Issue", "Write a birthday poem about the moon.")

    assert result["risk_level"] == "Out of Scope"
    assert list(result["agent_outputs"]) == ["Scope Gate"]
    assert result["llm_status"] == "not_run"
    assert result["interaction_report"]["failed_checks"]


def test_kb_detects_critical_bankruptcy_refund():
    result = run_operations_crew(
        "Refund Approval",
        "Customer requests a $30,000 refund related to bankruptcy.",
    )

    assert result["risk_level"] == "Critical"
    assert "Legal" in result["risk_note"] or "Legal" in result["recommended_workflow"]
    assert any("agent_maestro_kb" in source for source in result["sources"])


def test_kb_detects_refund_policy_contradiction():
    result = run_operations_crew(
        "Workflow Issue",
        "A regional SME gave a different refund threshold in Slack.",
    )

    assert result["contradictions"]
    assert any("known_issues" in source for source in result["sources"])
