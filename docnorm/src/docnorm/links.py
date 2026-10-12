import base64
import hashlib
import html
import os
from pathlib import Path
import re
from urllib.parse import quote, unquote, urlsplit

from markdown_it import MarkdownIt

from .normalization import map_prose
from .safety import safe_path


def anchors(text):
    ids = set(re.findall(r'<a\s+id=["\']([^"\']+)', text))
    counts = {}
    tokens = MarkdownIt().parse(text)
    for i, token in enumerate(tokens):
        if token.type == "heading_open":
            title = tokens[i + 1].content
            title = re.sub(r"[`*_]", "", title).lower()
            slug = re.sub(r"[^\w\-\s]", "", title).replace(" ", "-")
            n = counts.get(slug, 0)
            counts[slug] = n + 1
            ids.add(slug + (f"-{n}" if n else ""))
    return ids


class LinkRewriter:
    def __init__(self, root, destinations, document_anchors, asset_writer):
        self.root = root
        self.destinations = destinations
        self.document_anchors = document_anchors
        self.asset_writer = asset_writer

    def resolve(self, target, base, output, warnings, source):
        original = target
        target = html.unescape(target).replace("\\(", "(").replace("\\)", ")")
        try:
            parsed = urlsplit(target)
        except ValueError as e:
            warnings.append(f"malformed link {original}: {e}")
            return original
        if parsed.scheme or target.startswith("//"):
            if parsed.scheme == "data" and target.startswith("data:image/"):
                try:
                    header, payload = target.split(",", 1)
                    if ";base64" not in header:
                        raise ValueError("only base64 inline images are supported")
                    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/svg+xml": ".svg", "image/webp": ".webp"}.get(header[5:].split(";")[0])
                    if not ext:
                        raise ValueError("unsupported inline image type")
                    data = base64.b64decode(payload, validate=True)
                    if not data:
                        raise ValueError("empty inline image")
                    asset_path = self.asset_writer(data, ext, source)
                    return quote(os.path.relpath(asset_path, Path(output).parent).replace(os.sep, "/"), safe="/.-_")
                except (ValueError, TypeError) as e:
                    warnings.append(f"unresolved inline asset: {e}")
            return original
        if not parsed.path:
            if parsed.fragment and unquote(parsed.fragment) not in self.document_anchors.get(output, set()):
                warnings.append(f"unresolved anchor: {original}")
            return original
        try:
            local = safe_path(self.root, self.root / unquote(parsed.path).lstrip("/") if parsed.path.startswith("/") else base / unquote(parsed.path))
            relative = local.relative_to(self.root).as_posix()
            candidates = [relative]
            if not local.suffix:
                candidates += [relative + ".md", relative + ".html", relative.rstrip("/") + "/index.md", relative.rstrip("/") + "/index.html"]
            destination = next((self.destinations[p] for p in candidates if p in self.destinations), None)
            if destination is None:
                raise ValueError("no normalized destination")
            if parsed.fragment and destination.startswith("documents/") and unquote(parsed.fragment) not in self.document_anchors.get(destination, set()):
                warnings.append(f"unresolved anchor: {original}")
            rewritten = quote(os.path.relpath(destination, Path(output).parent).replace(os.sep, "/"), safe="/.-_")
            if parsed.query:
                rewritten += "?" + parsed.query
            if parsed.fragment:
                rewritten += "#" + parsed.fragment
            return rewritten
        except (ValueError, OSError) as e:
            warnings.append(f"unresolved local link {original}: {e}")
            return original

    def rewrite(self, text, base, output, warnings, source):
        def resolve(value):
            return self.resolve(value, base, output, warnings, source)

        def prose(part):
            # Reference definitions, retaining optional title verbatim.
            part = re.sub(r"^( {0,3}\[[^\]\n]+\]:\s*)(<[^>\n]+>|\S+)",
                          lambda m: m[1] + ("<" + resolve(m[2][1:-1]) + ">" if m[2].startswith("<") else resolve(m[2])), part, flags=re.M)
            # Inline links/images: balanced parentheses and angle destinations.
            edits = []
            for m in re.finditer(r"(?<!\\)\]\(\s*", part):
                start = m.end()
                if start >= len(part):
                    continue
                if part[start] == "<":
                    end = part.find(">", start + 1)
                    if end >= 0:
                        edits.append((start + 1, end, resolve(part[start + 1:end])))
                    continue
                depth, end = 0, start
                while end < len(part):
                    c = part[end]
                    if c == "\\" and end + 1 < len(part):
                        end += 2
                        continue
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        if depth == 0:
                            break
                        depth -= 1
                    elif c.isspace() and depth == 0:
                        break
                    end += 1
                if end > start and end < len(part):
                    edits.append((start, end, resolve(part[start:end])))
            for start, end, value in reversed(edits):
                part = part[:start] + value + part[end:]
            return re.sub(r"(\b(?:href|src)\s*=\s*)([\"'])(.*?)(\2)",
                          lambda m: m[1] + m[2] + html.escape(resolve(html.unescape(m[3])), quote=True) + m[4], part, flags=re.I)

        return map_prose(text, prose)
