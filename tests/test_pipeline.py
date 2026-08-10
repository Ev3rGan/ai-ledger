from ai_intel_agent.pipeline import run_daily_report


def test_sample_pipeline_generates_report():
    report = run_daily_report(sample=True)

    assert report.briefs
    assert "AI Intelligence Daily" == report.title
    assert all(brief.evidence for brief in report.briefs)
