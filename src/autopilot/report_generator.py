"""HTML report generator for autopilot pipeline features."""

import json
from pathlib import Path


def generate_feature_report(docs_path: str, metrics: dict = None) -> str:
    """Generate an HTML report for a completed feature.

    Args:
        docs_path: Path to the docs directory containing generated documents
        metrics: Optional pipeline metrics dictionary

    Returns:
        Path to the generated HTML report
    """
    docs_dir = Path(docs_path)

    # Gather documents
    docs_created = sorted(
        [f.name for f in docs_dir.glob("*.md") if f.name != "pipeline_metrics.json"]
    )

    # Load metrics if not provided
    if metrics is None:
        metrics_path = docs_dir / "pipeline_metrics.json"
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text())
        else:
            metrics = {}

    # Get commit info
    import subprocess

    try:
        commit_hash = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(docs_dir.parent),
        ).stdout.strip()
    except Exception:
        commit_hash = "N/A"

    design_name = metrics.get("design_name", "Feature")
    started_at = (
        metrics.get("started_at", "N/A")[:10] if metrics.get("started_at") else "N/A"
    )

    # Build phase rows
    phase_rows = ""
    for p in metrics.get("phases", []):
        phase_rows += f"""
                <tr style="border-bottom: 1px solid #f0f0f0;">
                    <td style="padding: 0.5rem;">{p.get("name", "Unknown")}</td>
                    <td style="padding: 0.5rem;"><span class="status-badge status-success">Completed</span></td>
                </tr>"""

    # Build docs list
    docs_list = "".join(
        f'<li>📄 <a href="{doc}">{doc}</a></li>' for doc in docs_created
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Feature Report: {design_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f5f5f5; color: #333; line-height: 1.6; }}
        .container {{ max-width: 900px; margin: 0 auto; padding: 2rem; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 2rem; border-radius: 12px; margin-bottom: 2rem; }}
        .header h1 {{ font-size: 1.8rem; margin-bottom: 0.5rem; }}
        .header p {{ opacity: 0.9; font-size: 0.95rem; }}
        .section {{ background: white; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
        .section h2 {{ font-size: 1.2rem; color: #667eea; margin-bottom: 1rem; padding-bottom: 0.5rem; border-bottom: 2px solid #f0f0f0; }}
        .metric {{ display: inline-block; text-align: center; padding: 1rem; min-width: 120px; }}
        .metric .value {{ font-size: 1.8rem; font-weight: bold; color: #667eea; }}
        .metric .label {{ font-size: 0.8rem; color: #666; margin-top: 0.25rem; }}
        .metrics-grid {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 1rem; }}
        .docs-list {{ list-style: none; }}
        .docs-list li {{ padding: 0.5rem 0; border-bottom: 1px solid #f0f0f0; }}
        .docs-list li:last-child {{ border-bottom: none; }}
        .docs-list a {{ color: #667eea; text-decoration: none; }}
        .docs-list a:hover {{ text-decoration: underline; }}
        .status-badge {{ display: inline-block; padding: 0.25rem 0.75rem; border-radius: 20px; font-size: 0.8rem; font-weight: 500; }}
        .status-success {{ background: #d4edda; color: #155724; }}
        .footer {{ text-align: center; color: #999; font-size: 0.85rem; margin-top: 2rem; }}
        .forensics-note {{ background: #f8f9fa; border-left: 4px solid #6c757d; padding: 1rem; margin-top: 1rem; font-size: 0.9rem; color: #555; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>✅ {design_name}</h1>
            <p>Completed on {started_at}</p>
        </div>

        <div class="section">
            <h2>📊 Pipeline Metrics</h2>
            <div class="metrics-grid">
                <div class="metric">
                    <div class="value">{metrics.get("max_iterations", "N/A")}</div>
                    <div class="label">Max Iterations</div>
                </div>
                <div class="metric">
                    <div class="value">{len(docs_created)}</div>
                    <div class="label">Documents Created</div>
                </div>
                <div class="metric">
                    <div class="value">{len(metrics.get("phases", []))}</div>
                    <div class="label">Phases Completed</div>
                </div>
                <div class="metric">
                    <div class="value">{commit_hash}</div>
                    <div class="label">Commit</div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2>📄 Documents Generated</h2>
            <ul class="docs-list">
                {docs_list}
            </ul>
        </div>

        <div class="section">
            <h2>🔧 Phase Summary</h2>
            <table style="width: 100%; border-collapse: collapse;">
                <tr style="border-bottom: 2px solid #f0f0f0;">
                    <th style="text-align: left; padding: 0.5rem;">Phase</th>
                    <th style="text-align: left; padding: 0.5rem;">Status</th>
                </tr>
                {phase_rows}
            </table>
        </div>

        <div class="section">
            <h2>📝 Forensics Note</h2>
            <div class="forensics-note">
                <p>🔍 <strong>Pipeline Self-Improvement:</strong> The forensics phase analyzed this pipeline run and identified opportunities for prompt refinement and methodology improvements. See <code>forensics_report.md</code> for detailed findings.</p>
            </div>
        </div>

        <div class="footer">
            <p>Generated by Hephaestus Autopilot Pipeline</p>
        </div>
    </div>
</body>
</html>"""

    # Write the report
    report_path = docs_dir / "feature_report.html"
    report_path.write_text(html)
    return str(report_path)
