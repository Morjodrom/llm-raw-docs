from collections import Counter
from dataclasses import asdict
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path

from . import __version__
from .adapters import registry
from .links import LinkRewriter, anchors
from .normalization import with_title
from .publishing import json_bytes, publish
from .reconciliation import reconcile
from .safety import offline, safe_path, validate_roots
from .scanner import scan


def normalize(source, output):
    source, output = Path(source).absolute(), Path(output).absolute()
    validate_roots(source, output)
    source, output = Path(os.path.abspath(source)), Path(os.path.abspath(output))
    validate_roots(source, output)
    with offline():
        return _normalize(source, output)


def _normalize(source, output):
    records = scan(source, output)
    by_path = {r.path: r for r in records}
    converted, files, destinations, asset_records = {}, {}, {}, {}
    versions = {"docnorm": __version__, **{p: version(p) for p in ["markitdown", "beautifulsoup4", "openpyxl", "markdown-it-py", "charset-normalizer"]}}

    def add_asset(data, suffix, source_path, locator=None):
        digest = hashlib.sha256(data).hexdigest()
        target = f"assets/{digest}{suffix.lower()}"
        files[target] = data
        entry = asset_records.setdefault(target, {"path": target, "sha256": digest, "sources": []})
        ref = {"root_id": "source", "path": source_path, "role": "extraction input" if locator else "original asset"}
        record = by_path.get(source_path)
        ref["sha256"] = record.sha256 if record else None
        if locator:
            ref["locator"] = locator
        if ref not in entry["sources"]:
            entry["sources"].append(ref)
        return target

    for record in records:
        if record.status != "pending":
            continue
        try:
            if record.kind == "asset":
                data = safe_path(source, source / record.path).read_bytes()
                if not data:
                    raise ValueError("empty asset file")
                record.output = add_asset(data, Path(record.path).suffix, record.path)
                record.status = "asset"
                destinations[record.path] = record.output
            else:
                result = registry.convert(source, record)
                converted[record.path] = result
                record.warnings.extend(result.warnings)
                record.status = "partial" if record.warnings else "converted"
        except Exception as e:
            record.status = "failed"
            record.error = f"{type(e).__name__}: {e}"
    groups = reconcile(source, records, converted)
    document_anchors = {}
    for ident, canonical, members, winner in groups:
        target = f"documents/{ident}/content.md"
        for member in members:
            destinations[member] = target
        document_anchors[target] = anchors(with_title(converted[winner].text, converted[winner].title))
    rewriter = LinkRewriter(source, destinations, document_anchors,
                            lambda data, suffix, src: add_asset(data, suffix, src, "embedded image"))
    catalog = ["# Normalized documentation", "", "Local document catalog. Source locations are recorded in provenance.json.", ""]
    partial_documents = 0
    for ident, canonical, members, winner in groups:
        result = converted[winner]
        target = destinations[winner]
        warnings = [f"{member}: {warning}" for member in members for warning in by_path[member].warnings]
        for member in members:
            if by_path[member].error:
                warnings.append(f"{member}: representation failed; using {winner}: {by_path[member].error}")
        link_warnings = []
        rewritten = "".join(rewriter.rewrite(f.text, f.base, target, link_warnings, winner) for f in result.fragments)
        warnings.extend(link_warnings)
        warnings = sorted(set(warnings))
        files[target] = with_title(rewritten, result.title).encode("utf-8")
        partial_documents += bool(warnings)
        sources = []
        source_converters = []
        for member in members:
            record = by_path[member]
            outcome = record.status
            if member != winner:
                record.status = "merged"
            elif warnings:
                record.status = "partial"
            record.output, record.document_id = target, ident
            if member == winner:
                record.warnings.extend(link_warnings)
            sources.append({"root_id": "source", "path": member, "sha256": record.sha256,
                            "role": "rendered representation" if record.kind == "html" and len(members) > 1 else "original document",
                            "conversion_status": outcome, "error": record.error,
                            "metadata": converted[member].metadata if member in converted else {},
                            "locators": converted[member].locators if member in converted else {},
                            "warnings": sorted(set(record.warnings))})
            if member in converted:
                source_converters.append({"source": member, "name": converted[member].converter})
        for included in result.extraction_inputs:
            record = by_path.get(included)
            sources.append({"root_id": "source", "path": included, "role": "extraction input", "sha256": record.sha256 if record else None})
        provenance = {"schema_version": 1, "document_id": ident, "title": result.title,
                      "normalized_path": target, "canonical_source": canonical,
                      "selected_source": winner, "sources": sources,
                      "converter": {"name": result.converter, "versions": versions},
                      "source_converters": source_converters, "warnings": warnings,
                      "status": "partial" if warnings else "converted",
                      "locators": {**result.locators, "normalized_anchors": sorted(document_anchors[target])}}
        files[f"documents/{ident}/provenance.json"] = json_bytes(provenance)
        title = result.title.replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
        catalog.append(f"- [{title}]({target})")
    if asset_records:
        catalog.extend(["", "## Assets", ""])
        for target, entry in sorted(asset_records.items()):
            label = entry['sources'][0]['path'].replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
            catalog.append(f"- [{label}]({target})")
    counts = dict(sorted(Counter(r.status for r in records).items()))
    for name in ["converted", "partial", "merged", "asset", "skipped", "failed"]:
        counts.setdefault(name, 0)
    warning_records = [{"source": r.path, "warnings": sorted(set(r.warnings))} for r in records if r.warnings]
    errors = [{"source": r.path, "error": r.error, "status": r.status} for r in records if r.error]
    report = {"schema_version": 1, "source_root_id": "source", "scanned_files": len(records),
              "file_status_counts": counts, "normalized_documents": len(groups),
              "partial_documents": partial_documents, "assets": len(asset_records),
              "warnings": warning_records, "errors": errors, "converter_versions": versions,
              "exclusions": [{"source": r.path, "reason": r.reason} for r in records if r.status == "skipped"]}
    files["assets/provenance.json"] = json_bytes({"schema_version": 1, "assets": [asset_records[p] for p in sorted(asset_records)]})
    files["roots.json"] = json_bytes({"schema_version": 1, "roots": {"source": {"path": str(source), "description": "Change this path if the original input folder moves; source paths remain relative."}}})
    files["manifest.jsonl"] = ("".join(json.dumps(asdict(r), ensure_ascii=False, sort_keys=True) + "\n" for r in records)).encode()
    files["llms.txt"] = ("\n".join(catalog) + "\n").encode()
    files["report.json"] = json_bytes(report)
    publish(output, files)
    return report
