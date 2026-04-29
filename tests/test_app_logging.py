import pandas as pd

import app
from crews.crew import run_operations_crew


def test_log_eval_writes_eval_and_backlog_rows(tmp_path, monkeypatch):
    eval_log = tmp_path / "eval_log.csv"
    backlog = tmp_path / "improvement_backlog.csv"

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "EVAL_LOG", eval_log)
    monkeypatch.setattr(app, "BACKLOG", backlog)

    app.ensure_output_files()
    result = run_operations_crew("Refund Approval", "Customer asks for a $7,500 refund.")
    app.log_eval("Refund Approval", "Customer asks for a $7,500 refund.", result)

    eval_frame = pd.read_csv(eval_log)
    backlog_frame = pd.read_csv(backlog)

    assert len(eval_frame) == 1
    assert eval_frame.loc[0, "request_type"] == "Refund Approval"
    assert bool(eval_frame.loc[0, "improvement_needed"]) is True
    assert "High risk" in set(backlog_frame["trigger"])
