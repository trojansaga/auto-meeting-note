import json
import sys
from pathlib import Path

from sync_diagnostics import analyze_session


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python sync_diagnostics_report.py <session_dir>", file=sys.stderr)
        return 2

    report = analyze_session(Path(argv[1]))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
