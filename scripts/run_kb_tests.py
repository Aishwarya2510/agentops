"""Run Agent Maestro against the synthetic knowledge-base eval cases.

Usage:
    python scripts/run_kb_tests.py

The script writes outputs/kb_test_results.csv so you can inspect how each agent
flow performed against the expected behavior in agent_maestro_kb/test_cases.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from crews.crew import run_operations_crew  # noqa: E402


TEST_CASES = ROOT_DIR / "agent_maestro_kb" / "test_cases" / "agent_maestro_test_cases.csv"
OUTPUT = ROOT_DIR / "outputs" / "kb_test_results.csv"


def normalize_request_type(category: str) -> str:
    mapping = {
        "Billing Login": "Billing Login",
        "Cash App": "Cash App",
        "Collections": "Collections",
        "Bad Debt": "Bad Debt",
        "Marketing Cloud": "Marketing Cloud",
        "AI Governance": "AI Governance",
        "Missing Policy": "Missing Policy",
        "Workflow Issue": "Workflow Issue",
        "Refund Approval": "Refund Approval",
    }
    return mapping.get(category, "Workflow Issue")


def contains_any(text: str, expected: str) -> bool:
    expected_terms = [
        term.strip().lower()
        for chunk in expected.replace(";", ",").split(",")
        for term in chunk.split(" plus ")
        if len(term.strip()) > 3
    ]
    text_lower = text.lower()
    return any(term in text_lower for term in expected_terms)


def main() -> None:
    cases = pd.read_csv(TEST_CASES)
    rows = []

    for _, case in cases.iterrows():
        request_type = normalize_request_type(case["category"])
        result = run_operations_crew(request_type, case["question"])
        combined_output = "\n".join(
            [
                result["risk_note"],
                result["recommended_workflow"],
                result["email_draft"],
                "\n".join(result["agent_outputs"].values()),
                "\n".join(result.get("contradictions", [])),
            ]
        )
        expected_risk = str(case["risk"])
        risk_match = result["risk_level"] == expected_risk
        behavior_match = contains_any(combined_output, str(case["expected_behavior"]))
        source_match = bool(result["sources"])
        checks_clear = not result["interaction_report"]["failed_checks"]

        rows.append(
            {
                "id": case["id"],
                "category": case["category"],
                "request_type": request_type,
                "question": case["question"],
                "expected_risk": expected_risk,
                "actual_risk": result["risk_level"],
                "risk_match": risk_match,
                "behavior_match": behavior_match,
                "source_match": source_match,
                "checks_passed": result["interaction_report"]["checks_passed"],
                "checks_total": result["interaction_report"]["checks_total"],
                "failed_checks": result["interaction_report"]["failed_checks"],
                "confidence": result["confidence"],
                "sources": "; ".join(result["sources"]),
            }
        )

    OUTPUT.parent.mkdir(exist_ok=True)
    frame = pd.DataFrame(rows)
    frame.to_csv(OUTPUT, index=False)

    passed = int((frame["risk_match"] & frame["source_match"]).sum())
    total = len(frame)
    print(f"KB smoke tests complete: {passed}/{total} cases matched risk and found sources.")
    print(f"Detailed results written to {OUTPUT.relative_to(ROOT_DIR)}")
    print(frame[["id", "category", "expected_risk", "actual_risk", "risk_match", "confidence"]].to_string(index=False))


if __name__ == "__main__":
    main()
