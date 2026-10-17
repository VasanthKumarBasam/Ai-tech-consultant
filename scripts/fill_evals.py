"""Copy the latest measured eval summaries into EVALS.md (between the RESULTS markers).

Only files produced by the eval runners are used, so every number in EVALS.md
traces back to a CSV in */evaluation/results/. Runs without real models are
labelled as such by the runners and should not be reported as results.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "task1": ROOT / "task1-knowledge-brain/evaluation/results/kb_summary_latest.md",
    "task2": ROOT / "task2-inbox-agent/evaluation/results/inbox_summary_latest.md",
    "task3": ROOT / "task3-meeting-to-scope/evaluation/results/scope_summary_latest.md",
}


def main() -> None:
    evals = ROOT / "EVALS.md"
    text = evals.read_text()
    for key, path in SOURCES.items():
        start, end = f"<!-- RESULTS:{key}:start -->", f"<!-- RESULTS:{key}:end -->"
        if start not in text or end not in text:
            continue
        body = path.read_text().strip() if path.exists() else "_Not run yet: `./start.sh eval`._"
        head, rest = text.split(start, 1)
        _, tail = rest.split(end, 1)
        text = f"{head}{start}\n{body}\n{end}{tail}"
        print(f"{key}: {'updated from ' + str(path.relative_to(ROOT)) if path.exists() else 'no results yet'}")
    evals.write_text(text)


if __name__ == "__main__":
    main()
