import argparse
import json
import sys

def main(argv=None):
    parser = argparse.ArgumentParser(prog="docnorm", description="Normalize local documents without fetching or executing content.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    command = subparsers.add_parser("normalize")
    command.add_argument("source")
    command.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        from .engine import normalize
        report = normalize(args.source, args.output)
    except Exception as e:
        print(f"docnorm: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    print(json.dumps({"documents": report["normalized_documents"], "partial_documents": report["partial_documents"],
                      "files": report["file_status_counts"], "report": str(__import__('pathlib').Path(args.output).absolute() / 'report.json')}, ensure_ascii=False))
    return 1 if report["partial_documents"] or report["file_status_counts"]["failed"] else 0
