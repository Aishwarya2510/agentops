import pandas as pd

import app
from crews.crew import run_operations_crew


def test_log_eval_writes_eval_and_backlog_rows(tmp_path, monkeypatch):
    eval_log = tmp_path / "eval_log.csv"
    backlog = tmp_path / "improvement_backlog.csv"

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "EVAL_LOG", eval_log)
    monkeypatch.setattr(app, "BACKLOG", backlog)
    monkeypatch.setattr(app, "FEEDBACK_LOG", tmp_path / "feedback_log.csv")
    monkeypatch.setattr(app, "OVERRIDE_LOG", tmp_path / "override_log.csv")
    monkeypatch.setattr(app, "AGENT_PERFORMANCE_LOG", tmp_path / "agent_performance_log.csv")

    app.ensure_output_files()
    result = run_operations_crew("Refund Approval", "Customer asks for a $7,500 refund.")
    app.log_eval("Refund Approval", "Customer asks for a $7,500 refund.", result)

    eval_frame = pd.read_csv(eval_log)
    backlog_frame = pd.read_csv(backlog)

    assert len(eval_frame) == 1
    assert eval_frame.loc[0, "request_type"] == "Refund Approval"
    assert bool(eval_frame.loc[0, "improvement_needed"]) is True
    assert "High risk" in set(backlog_frame["trigger"])


def test_similar_cases_and_feedback_loop_use_csv_memory(tmp_path, monkeypatch):
    eval_log = tmp_path / "eval_log.csv"
    backlog = tmp_path / "improvement_backlog.csv"
    feedback = tmp_path / "feedback_log.csv"

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "EVAL_LOG", eval_log)
    monkeypatch.setattr(app, "BACKLOG", backlog)
    monkeypatch.setattr(app, "FEEDBACK_LOG", feedback)
    monkeypatch.setattr(app, "OVERRIDE_LOG", tmp_path / "override_log.csv")
    monkeypatch.setattr(app, "AGENT_PERFORMANCE_LOG", tmp_path / "agent_performance_log.csv")

    app.ensure_output_files()
    result = run_operations_crew("Refund Approval", "Customer asks for a $7,500 refund.")
    app.log_eval("Refund Approval", "Customer asks for a $7,500 refund.", result)

    similar = app.find_similar_cases("Refund Approval", "Duplicate charge refund for $7,000")
    assert similar["count"] == 1
    assert similar["previous_risk"] == "High"

    app.log_feedback(
        "Refund Approval",
        "Duplicate charge refund for $7,000",
        "Incorrect",
        "Wrong approval owner",
        "Finance Director must approve this segment.",
    )

    feedback_frame = pd.read_csv(feedback)
    backlog_frame = pd.read_csv(backlog)
    assert feedback_frame.loc[0, "rating"] == "Incorrect"
    assert "Human feedback" in set(backlog_frame["trigger"])


def test_override_logging_writes_governance_row(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "EVAL_LOG", tmp_path / "eval_log.csv")
    monkeypatch.setattr(app, "BACKLOG", tmp_path / "improvement_backlog.csv")
    monkeypatch.setattr(app, "FEEDBACK_LOG", tmp_path / "feedback_log.csv")
    monkeypatch.setattr(app, "OVERRIDE_LOG", tmp_path / "override_log.csv")
    monkeypatch.setattr(app, "AGENT_PERFORMANCE_LOG", tmp_path / "agent_performance_log.csv")

    app.ensure_output_files()
    app.log_override(
        "Refund Approval",
        "Customer asks for retention refund.",
        True,
        "Customer retention exception",
        "Director",
        "Policy incomplete",
    )

    override_frame = pd.read_csv(tmp_path / "override_log.csv")
    backlog_frame = pd.read_csv(tmp_path / "improvement_backlog.csv")
    assert bool(override_frame.loc[0, "override_occurred"]) is True
    assert override_frame.loc[0, "approved_by"] == "Director"
    assert "Decision override" in set(backlog_frame["trigger"])
