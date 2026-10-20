"""Print sample defects and a recall placeholder. Does not call a model."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def expected_defect(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# defect:"):
            return line.split(":", 1)[1].strip()
    return "unspecified"


def main() -> None:
    samples = sorted((ROOT / "samples").glob("*.py"))
    print(f"samples: {len(samples)}")
    for path in samples:
        print(f"- {path.name}: expected={expected_defect(path)}")
    results_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "results.json"
    if results_path.exists():
        data = json.loads(results_path.read_text(encoding="utf-8"))
        count = len(data) if isinstance(data, list) else 1
        print(f"results: {count}")
    else:
        print("results: none")
    print("recall: placeholder")


if __name__ == "__main__":
    main()
