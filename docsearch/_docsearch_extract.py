"""Markdown section slicing; source text is never rendered or rewritten."""
import hashlib
import json
import re


def stable_id(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def source_lines(text):
    """Split only CommonMark line endings, not Unicode NEL/paragraph separators."""
    return [m.group() for m in re.finditer(r"[^\r\n]*(?:\r\n|\r|\n|$)", text) if m.group()]


class Extractor:
    def __init__(self):
        from markdown_it import MarkdownIt
        self.parser = MarkdownIt("commonmark").enable("table")

    def extract(self, text, document_id):
        lines = source_lines(text)
        tokens = self.parser.parse(text.removeprefix("\ufeff"))
        headings = []
        for i, token in enumerate(tokens):
            if token.type == "heading_open" and token.level == 0 and token.map:
                headings.append((token.map[0], int(token.tag[1:]), tokens[i + 1].content))
        boundaries = []
        if not headings or headings[0][0] > 0:
            if lines:
                boundaries.append((0, 0, ""))
        boundaries.extend(headings)
        stack = []
        counts = {}
        sections = []
        for i, (start, level, heading) in enumerate(boundaries):
            end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(lines)
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack and level else None
            path = [x[2] for x in stack] + ([heading] if level else [])
            identity = (parent, heading, level)
            counts[identity] = counts.get(identity, 0) + 1
            sid = stable_id(json.dumps([document_id, parent, heading, level, counts[identity]], ensure_ascii=False))
            section = {
                "section_id": sid, "document_id": document_id, "heading_path": path,
                "heading": " / ".join(path), "parent_section_id": parent,
                "start_line": start + 1, "end_line": end,
                "original_markdown": "".join(lines[start:end]),
            }
            sections.append(section)
            if level:
                stack.append((level, sid, heading))
        return sections, headings[0][2] if headings else None
