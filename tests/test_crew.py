from crews.crew import (
    approval_band,
    classify_request_type,
    extract_amount,
    parse_openai_json,
    retrieve_context,
    run_operations_crew,
)


def test_extract_amount_uses_largest_detected_amount():
    assert extract_amount("Invoice 123 has duplicate charge of $7,500 and credit 25") == 7500


def test_extract_amount_handles_uncommaed_high_value_amount():
    assert extract_amount("Customer asking refund for $75000") == 75000


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


def test_enterprise_decision_layers_are_returned():
    result = run_operations_crew(
        "Refund Approval",
        "Customer asks for a $7,500 refund after duplicate billing and login failure.",
    )

    assert result["priority"]["label"] == "High"
    assert result["business_impact"]["time_saved_minutes"] > 0
    assert "Zendesk Ticket" in result["integration_simulation"]["source"]
    assert set(result["role_views"]) == {"Analyst", "Manager", "Director"}
    assert result["executive_summary"]["Issue"].startswith("Refund Approval")
    assert result["confidence_drivers"]["positive"]


def test_parse_openai_json_handles_markdown_wrapped_json():
    parsed = parse_openai_json(
        """```json
        {"recommended_workflow": "Route to Finance", "risk_note": "High risk"}
        ```"""
    )

    assert parsed["recommended_workflow"] == "Route to Finance"


def test_parse_openai_json_handles_prefixed_json():
    parsed = parse_openai_json(
        'Here is the grounded JSON: {"email_draft": "Hi team", "agent_outputs": {}}'
    )

    assert parsed["email_draft"] == "Hi team"


def test_uncommaed_high_value_sensitive_refund_routes_to_critical_escalation():
    result = run_operations_crew(
        "Refund Approval",
        "customer asking refund for $75000, says we caused him emotional instability",
    )

    assert result["amount"] == 75000
    assert result["risk_level"] == "Critical"
    assert result["priority"]["label"] == "Critical"
    assert "VP Finance" in result["approval_owner"]
    assert "Legal" in result["recommended_workflow"]
    assert any("Sensitive customer-impact claim" in reason for reason in result["exception"]["reasons"])
    assert result["confidence_action"]["action"] == "Escalate for manual review before execution"
    assert result["data_sensitivity"]["detected"] is True
    assert result["policy_versions"]
    assert any(item["workflow"] == "CX Escalation" for item in result["subtasks"])
    assert result["fallback"]["needed"] is True


def test_classify_request_type_detects_refund_without_dropdown():
    classification = classify_request_type(
        "customer asking refund for $75000, says we caused him emotional instability"
    )

    assert classification["request_type"] == "Refund Approval"


def test_run_operations_crew_auto_detects_request_type():
    result = run_operations_crew(
        "Customer cannot access the billing portal after password reset and needs invoice access."
    )

    assert result["request_type"] == "Billing Login"
    assert result["classification"]["request_type"] == "Billing Login"


def test_classify_request_type_detects_governance_and_collections():
    governance = classify_request_type(
        "A team wants to deploy an AI assistant that can recommend refunds without human approval."
    )
    collections = classify_request_type(
        "Customer is in collections and a Slack note says to pause dunning with no approval."
    )

    assert governance["request_type"] == "AI Governance"
    assert collections["request_type"] == "Collections"


def test_low_confidence_context_skips_llm_policy():
    result = run_operations_crew(
        "Unknown missing SOP for refund exception with no owner.",
        api_key="fake-key",
    )

    assert result["llm_status"] == "skipped"
    assert result["llm_usage"]["should_call_llm"] is False
    assert result["suggested_rule_updates"]
