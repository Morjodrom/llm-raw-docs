"""Standalone CLI; all search behavior belongs to the library."""
import argparse
from dataclasses import asdict
import json
import sqlite3
import sys

from docsearch import DocSearch, SearchConfig, DocSearchError, SectionNotFoundError


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline Markdown search. Agent workflow: search --json, then read --json.")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("index", "search", "read"):
        p = sub.add_parser(command)
        p.add_argument("--db", required=True)
        p.add_argument("--json", action="store_true", help="Emit one JSON object on stdout")
        if command == "index":
            p.add_argument("root")
            p.add_argument("--relocate", action="store_true", help="Rebind a moved collection")
            p.add_argument("--exclude", action="append", default=[], help="Additional relative path/component glob")
        elif command == "search":
            p.add_argument("query")
            p.add_argument("--limit", type=int, default=10)
            p.add_argument("--mode", choices=("plain", "advanced", "literal"), default="plain")
            p.add_argument("--match", choices=("all", "any"), default="all", help="Explicitly broaden plain queries; no automatic fallback")
            p.add_argument("--title-boost", type=float, default=5)
            p.add_argument("--heading-boost", type=float, default=3)
            p.add_argument("--body-boost", type=float, default=1)
        else:
            p.add_argument("section_id")
    args = parser.parse_args(argv)
    try:
        config = SearchConfig()
        if args.command == "index":
            config = SearchConfig(exclude=config.exclude + tuple(args.exclude))
        elif args.command == "search":
            config = SearchConfig(title_boost=args.title_boost, heading_boost=args.heading_boost, body_boost=args.body_boost)
        with DocSearch(args.db, config, read_only=args.command != "index") as engine:
            code = 0
            if args.command == "index":
                report = engine.index(args.root, relocate=args.relocate)
                value = asdict(report)
                code = 1 if report.failures else 0
                for failure in report.failures:
                    print(f"{failure.relative_path}: {failure.error}; retained_stale={failure.retained_stale}", file=sys.stderr)
            elif args.command == "search":
                results = engine.search(args.query, args.limit, mode=args.mode, match=args.match)
                value = {"query": args.query, "mode": args.mode, "match": args.match, "results": [asdict(r) for r in results]}
            else:
                value = asdict(engine.read_section(args.section_id))
            if args.json:
                print(json.dumps(value, ensure_ascii=False))
            elif args.command == "search":
                for r in results:
                    print(f"{r.section_id}  {r.title} / {' / '.join(r.heading_path)}\n{r.relative_path}:{r.start_line}-{r.end_line} [{r.matched_representation}] stale={r.stale}\n{r.excerpt}\n")
                if not results:
                    print("No matches. Try --match any explicitly to broaden a plain query.")
            elif args.command == "read":
                print(f"{value['title']} — {value['relative_path']}:{value['start_line']}-{value['end_line']}\nparent={value['parent_section_id']} stale={value['stale']}\n")
                print(value["original_markdown"], end="")
            else:
                print(json.dumps(value, ensure_ascii=False, indent=2))
            return code
    except SectionNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (DocSearchError, OSError, ValueError, sqlite3.DatabaseError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
