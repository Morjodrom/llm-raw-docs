import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup

from .normalization import decode
from .safety import safe_path


def reconcile(root, records, converted):
    docs = {r.path: r for r in records if r.kind not in {"asset", "excluded", "symlink", "unknown"}}
    parent = {p: p for p in docs}
    preferred = set()

    def leader(p):
        while p != parent[p]:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    def union(a, b):
        a, b = leader(a), leader(b)
        parent[max(a, b)] = min(a, b)

    hashes = {}
    for path, record in docs.items():
        if record.sha256:
            if record.sha256 in hashes:
                union(path, hashes[record.sha256])
            else:
                hashes[record.sha256] = path
    for path, record in docs.items():
        if record.kind != "html":
            continue
        try:
            soup = BeautifulSoup(decode((root / path).read_bytes())[0], "html.parser")
            state = soup.find("script", id="diplodoc-state")
            meta = json.loads(state.string or state.get_text())["data"].get("meta", {}) if state else {}
            targets = []
            for key in ("sourcePath", "vcsPath"):
                source = meta.get(key)
                if source:
                    # Source paths are identities, not permissions to read outside root.
                    targets.extend(p for p in docs if p == source or p == "raw_docs/" + source or p.endswith("/" + source))
            base = (root / path).parent
            base_tag = soup.find("base", href=True)
            if base_tag:
                try:
                    base = safe_path(root, base / base_tag["href"])
                except ValueError:
                    pass
            alternates = meta.get("alternate", []) + [{"href": a.get("href"), "type": a.get("type")} for a in soup.select("link[rel='alternate']")]
            for alt in alternates:
                href = alt.get("href")
                if href and (alt.get("type") == "text/markdown" or href.endswith(".md")):
                    try:
                        target = safe_path(root, base / unquote(href)).relative_to(root).as_posix()
                        if target in docs:
                            targets.append(target)
                    except ValueError:
                        pass
            targets = sorted(set(p for p in targets if docs[p].kind == "markdown"))
            # Several targets with identical bytes are still confident matches.
            if targets and len({docs[p].sha256 for p in targets}) == 1:
                for target in targets:
                    union(path, target)
                    preferred.add(target)
            elif targets:
                record.warnings.append("ambiguous source representations; not merged")
        except (ValueError, OSError, KeyError, TypeError):
            pass  # Conversion already records malformed input; no speculative merge.
    groups = {}
    for path in docs:
        groups.setdefault(leader(path), []).append(path)
    result = []
    for members in groups.values():
        members.sort()
        canonical = min((p for p in members if p in preferred), default=members[0])
        document_id = hashlib.sha256(("docnorm:v1:" + canonical).encode()).hexdigest()[:24]
        usable = [p for p in members if p in converted]
        # Prefer rendered article, then Markdown, then any other usable source.
        usable.sort(key=lambda p: (0 if docs[p].kind == "html" else 1 if docs[p].kind == "markdown" else 2, p))
        if usable:
            result.append((document_id, canonical, members, usable[0]))
    return sorted(result)
