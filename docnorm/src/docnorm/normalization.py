import re
from pathlib import Path
from urllib.parse import unquote

from markdown_it import MarkdownIt

from .models import Fragment
from .safety import safe_path


def decode(data: bytes):
    warnings = []
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        text = data.decode("utf-32")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            from charset_normalizer import from_bytes
            match = from_bytes(data).best()
            if match is None:
                text = data.decode("utf-8", errors="replace")
                warnings.append("encoding undecodable; replacement characters retained")
            else:
                text = str(match)
                warnings.append(f"encoding inferred as {match.encoding}; verify decoded content")
    if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
        raise ValueError("binary/control-character content is not text")
    return text.replace("\r\n", "\n").replace("\r", "\n"), warnings


def fence(text: str, language: str):
    longest = max((len(m[0]) for m in re.finditer(r"`+", text)), default=0)
    marker = "`" * max(3, longest + 1)
    return f"{marker}{language}\n{text.rstrip()}\n{marker}\n"


def prose_segments(text: str):
    """Yield (protected, text) without interpreting links/directives inside code."""
    lines = text.splitlines(keepends=True)
    ranges = sorted(token.map for token in MarkdownIt().parse(text)
                    if token.type in {"fence", "code_block"} and token.map)
    cursor = 0
    for start, end in ranges:
        if start < cursor:
            continue
        if start > cursor:
            yield False, "".join(lines[cursor:start])
        yield True, "".join(lines[start:end])
        cursor = end
    if cursor < len(lines):
        yield False, "".join(lines[cursor:])


def code_segments(text):
    for protected, part in prose_segments(text):
        if protected:
            yield True, part
            continue
        # CommonMark inline code uses matching runs of backticks.
        start = 0
        cursor = 0
        for m in re.finditer(r"`+", part):
            if m.start() < cursor:
                continue
            closing = re.search(rf"(?<!`){re.escape(m[0])}(?!`)", part[m.end():])
            if closing:
                end = m.end() + closing.end()
                yield False, part[start:m.start()]
                yield True, part[m.start():end]
                start = cursor = end
        yield False, part[start:]


def map_prose(text, callback):
    return "".join(part if protected else callback(part) for protected, part in code_segments(text))


def title_for(text, fallback):
    for protected, part in prose_segments(text):
        if not protected:
            m = re.search(r"^#\s+(.+?)\s*#*\s*$", part, re.M)
            if m:
                return m[1].strip()
    return fallback


def with_title(text, title):
    text = text.strip()
    if not any(re.search(r"^#\s", part, re.M) for protected, part in prose_segments(text) if not protected):
        text = f"# {title.replace(chr(10), ' ')}\n\n{text}"
    return text + "\n"


def expand_markdown(root: Path, path: Path, warnings: list, inputs: list, stack=()):
    path = safe_path(root, path)
    if path in stack:
        raise ValueError("circular include")
    if len(stack) >= 64:
        raise ValueError("include nesting exceeds 64")
    text, decoding = decode(path.read_bytes())
    warnings.extend(decoding)
    fragments = []
    # Notes/cuts are visible annotations; their content is never elided.
    directive_stack = []
    for protected, segment in code_segments(text):
        if protected:
            fragments.append(Fragment(segment, path.parent))
            continue
        cursor = 0
        for m in re.finditer(r"{%\s*(\w+)\s*(.*?)\s*%}", segment, re.S):
            fragments.append(Fragment(segment[cursor:m.start()], path.parent))
            name, args = m[1], m[2]
            replacement = m[0]
            if name in {"note", "cut"}:
                directive_stack.append(name)
                label = "Note" if name == "note" else "Details"
                replacement = f"\n\n> **{label}: {args.strip().strip(chr(34))}**\n\n"
            elif name in {"endnote", "endcut"}:
                expected = name[3:]
                if not directive_stack or directive_stack[-1] != expected:
                    warnings.append(f"unmatched directive: {name} in {path.relative_to(root)}")
                else:
                    directive_stack.pop()
                    replacement = "\n\n"
            elif name in {"include", "code"}:
                target = re.search(r"\[[^\]]*\]\(([^)]+)\)", args)
                quoted = re.search(r"['\"]([^'\"]+)['\"]", args)
                try:
                    link = target[1] if target else quoted[1] if quoted else None
                    if not link:
                        raise ValueError("include target is missing")
                    included = safe_path(root, path.parent / unquote(link))
                    if included in (*stack, path):
                        raise ValueError("circular include")
                    if not included.is_file():
                        raise ValueError("include file does not exist")
                    rel = included.relative_to(root).as_posix()
                    if name == "code" or included.suffix.lower() in {".puml", ".mmd", ".js", ".sh"}:
                        body, ws = decode(included.read_bytes())
                        warnings.extend(ws)
                        lang = re.search(r"lang=['\"]([^'\"]+)['\"]", args)
                        language = lang[1] if lang else {".puml": "plantuml", ".mmd": "mermaid", ".js": "javascript", ".sh": "bash"}.get(included.suffix, "text")
                        expanded = [Fragment(fence(body, language), included.parent)]
                    else:
                        expanded = expand_markdown(root, included, warnings, inputs, (*stack, path))
                    inputs.append(rel)
                    fragments.extend([Fragment("\n\n", path.parent), *expanded, Fragment("\n\n", path.parent)])
                    replacement = ""
                except (ValueError, OSError) as e:
                    warnings.append(f"unresolved {name} in {path.relative_to(root)}: {e}")
            else:
                warnings.append(f"unresolved directive {name} in {path.relative_to(root)}")
            fragments.append(Fragment(replacement, path.parent))
            cursor = m.end()
        fragments.append(Fragment(segment[cursor:], path.parent))
    if directive_stack:
        warnings.append(f"unclosed directives in {path.relative_to(root)}: {directive_stack}")
    return fragments
