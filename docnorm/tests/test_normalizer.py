import hashlib
import html
import json
from pathlib import Path
import shutil
import socket
import zipfile

import pytest

from docnorm.cli import main
from docnorm.engine import normalize
from docnorm.normalization import fence
from docnorm.publishing import publish
from docnorm.safety import offline


def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def manifest(output):
    return {r["path"]: r for r in map(json.loads, (output / "manifest.jsonl").read_text().splitlines())}


def contents(output):
    return {p.parent.name: p.read_text() for p in (output / "documents").rglob("content.md")}


def all_content(output):
    return "\n".join(contents(output).values())


def snapshot(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def diplodoc(root, name, body=None, source=None, leading=None):
    data = {"title": "Document title", "meta": {"sourcePath": source} if source else {}}
    if body is not None:
        data["html"] = html.escape(body)
    if leading is not None:
        data.update(leading=True, data=leading)
    return write(root, name, '<script id="diplodoc-state" type="application/json">' + json.dumps({"data": data}) + '</script>')


def docx(path):
    # A real minimal OOXML archive with a heading, numbered list and table.
    document = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Office heading</w:t></w:r></w:p>
    <w:p><w:r><w:t>Office paragraph</w:t></w:r></w:p>
    <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Office list item</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell A</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Cell B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:body></w:document>'''
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>''')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", '''<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>''')
        z.writestr("word/numbering.xml", '''<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>''')


def test_supported_formats_and_source_immutability(tmp_path):
    src, out = tmp_path / "source", tmp_path / "out"
    src.mkdir()
    write(src, "русский.md", "# Unicode\n\n## Child\n\n- item\n\n```sh\nrm -rf /\n```\n")
    write(src, "text", "Extensionless words")
    (src / "utf16.txt").write_bytes("UTF16 words\r\nnext".encode("utf-16"))
    write(src, "plain.txt", "plain words")
    write(src, "ordinary.html", '<nav>unwanted navigation</nav><main><h1>HTML heading</h1><p>Visible</p><script>evil()</script><table><tr><th>X</th></tr><tr><td>Y</td></tr></table></main>')
    docx(src / "office.docx")
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "Лист A"
    wb.active.append(["X", "Y"])
    wb.active.append(["a|b", "line\nbreak"])
    wb.create_sheet("Second").append([123, "=1+1"])
    wb.save(src / "sheets.xlsx")
    write(src, "data.csv", 'name,value\n"a|b",42\n')
    write(src, "other.json", '{"key":"value"}')
    write(src, "other.yaml", "hello: world")
    for name, body in [("diagram.mmd", "graph LR; A-->B"), ("diagram.puml", "@startuml\nA -> B\n@enduml"), ("tool.sh", "echo NEVER_EXECUTED"), ("tool.js", "throw Error('NEVER_EXECUTED')")]:
        write(src, name, body)
    write(src, "image.svg", '<svg><script>NEVER_EXECUTED</script></svg>')
    before = snapshot(src)

    report = normalize(src, out)

    text = all_content(out)
    assert before == snapshot(src)
    assert "Office paragraph" in text and "Cell A" in text and "Office list item" in text
    assert "# Office heading" in text
    assert "Лист A" in text and "## Second" in text and "=1+1" in text
    assert "a\\|b" in text and "line<br>break" in text
    assert "unwanted navigation" not in text and "evil()" not in text
    assert "```mermaid" in text and "```plantuml" in text and "```javascript" in text
    assert "UTF16 words\nnext" in text
    assert report["file_status_counts"]["failed"] == 0
    assert len(manifest(out)) == len(before)
    assert (out / "assets/provenance.json").is_file()


def test_diplodoc_reconciliation_fallback_and_landing(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "raw_docs/pay/page.md", "# Raw\nRaw alternate")
    diplodoc(src, "build/page.html", '<h2 id="deep">Rendered</h2><div class="yfm-note">Critical warning</div><details><summary>Details</summary><p>Collapsed text</p></details>', "pay/page.md")
    write(src, "raw_docs/pay/empty.md", "# Fallback\nFallback text")
    diplodoc(src, "build/empty.html", "", "pay/empty.md")
    diplodoc(src, "build/index.html", leading={"title": "Landing", "description": ["Intro"], "links": [{"title": "Page", "href": "page.html#deep"}]})

    report = normalize(src, out)

    assert report["normalized_documents"] == 3
    assert report["file_status_counts"]["merged"] == 2
    text = all_content(out)
    assert "Raw alternate" not in text
    assert "Critical warning" in text and "Collapsed text" in text and '<a id="deep">' in text
    assert "Fallback text" in text and "Landing" in text
    assert "page.html#deep" not in text
    prov = [json.loads(p.read_text()) for p in (out / "documents").rglob("provenance.json")]
    matched = next(p for p in prov if p["selected_source"] == "build/page.html")
    assert {s["path"] for s in matched["sources"]} == {"build/page.html", "raw_docs/pay/page.md"}
    assert "source_line" not in str(prov)


def test_exact_duplicates_same_titles_and_relocation(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "a.md", "# Same\nFirst")
    write(src, "copy.md", "# Same\nFirst")
    write(src, "b.md", "# Same\nDifferent")
    first = normalize(src, out)
    before = snapshot(out)

    second = normalize(src, out)

    assert first == second
    assert before == snapshot(out)
    assert first["normalized_documents"] == 2
    moved = tmp_path / "moved"
    shutil.copytree(src, moved)
    normalize(moved, tmp_path / "moved-out")
    assert set(contents(out)) == set(contents(tmp_path / "moved-out"))


def test_links_reference_code_assets_unicode_and_fragments(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "юникод (1).md", "# Destination\n\n## Section\n")
    write(src, "image.svg", "<svg/>")
    write(src, "a.md", '''# Links
[inline](%D1%8E%D0%BD%D0%B8%D0%BA%D0%BE%D0%B4%20%281%29.md#section)
[reference][r]
[r]: <юникод (1).md> "title"
![image](image.svg)
[broken](missing.md)
[external](https://example.test/doc)
[bad anchor](юникод%20%281%29.md#absent)
`[code](missing-code.md)`
```markdown
[code](missing-fenced.md)
```
''')

    normalize(src, out)

    m = manifest(out)["a.md"]
    text = (out / m["output"]).read_text()
    assert "../../assets/" in text and "../" in text
    assert '[r]: <../' in text and '"title"' in text
    assert "https://example.test/doc" in text
    assert "[code](missing-fenced.md)" in text and "[code](missing-code.md)" in text
    assert len(m["warnings"]) == 2
    assert any("unresolved anchor" in w for w in m["warnings"])


def test_includes_context_notes_cycles_and_escape(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(tmp_path, "secret.md", "SECRET")
    write(src, "part/inner.md", "## Included\n[link](target.md)\n{% include [cycle](../a.md) %}")
    write(src, "part/target.md", "# Target\nbody")
    write(src, "a.md", '''# Main
{% note alert "Danger" %}
Operational warning
{% endnote %}
{% cut "Details" %}Hidden content{% endcut %}
{% include [part](part/inner.md) %}
{% include [escape](../secret.md) %}
{% mystery unknown %}
`{% include [inline example](inline-missing.md) %}`
```markdown
{% include [example](no-such-file.md) %}
```
''')

    normalize(src, out)

    m = manifest(out)["a.md"]
    text = (out / m["output"]).read_text()
    assert "Operational warning" in text and "Hidden content" in text
    assert "SECRET" not in text and "../secret.md" in text
    assert "circular include" in str(m["warnings"])
    assert "[link](../" in text
    assert "no-such-file" not in str(m["warnings"])
    assert "inline-missing" not in str(m["warnings"])
    p = json.loads((out / Path(m["output"]).parent / "provenance.json").read_text())
    assert any(s["role"] == "extraction input" for s in p["sources"])


def test_architecture_unknown_fields_and_exclusions(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    obj = {"schemaVersion": 1, "title": "Architecture", "entities": [{"id": "entity", "summary": "Entity text"}],
           "components": [{"name": "Component"}], "relations": [{"from": "A", "to": "B"}], "scenarios": [{"title": "Scenario", "goal": "Goal text"}], "unknown": {"secret": "retained"}}
    write(src, "architecture.json", json.dumps(obj))
    for path in [".DS_Store", "_bundle/app.js", "_bundle/app.css", "llms-full.txt", "export.zip", "unsupported.bin"]:
        write(src, path, "excluded")
    (src / "link.md").symlink_to(src / "architecture.json")
    (src / "linked-dir").symlink_to(src, target_is_directory=True)

    normalize(src, out)

    text = all_content(out)
    assert all(t in text for t in ["Entity text", "Component", "Goal text", "retained"])
    m = manifest(out)
    assert all(m[p]["status"] == "skipped" for p in m if p != "architecture.json")
    assert all(m[p]["reason"] for p in m if p != "architecture.json")


@pytest.mark.parametrize("name,body", [("bad.json", "{"), ("bad.html", '<script id="diplodoc-state">bad</script>'), ("empty.md", ""), ("empty.html", "<script>only script</script>"), ("bad.docx", "not a zip"), ("bad.xlsx", "not a zip")])
def test_failed_files_do_not_stop_conversion(tmp_path, name, body):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, name, body)
    write(src, "good.md", "# Good\nSurvives")

    report = normalize(src, out)

    assert manifest(out)[name]["status"] == "failed"
    assert report["normalized_documents"] == 1
    assert "Survives" in all_content(out)


def test_stale_owned_cleanup_and_unowned_preservation(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "old.md", "# Old\nbody")
    normalize(src, out)
    old = manifest(out)["old.md"]["output"]
    write(out, "user.txt", "KEEP")
    (src / "old.md").unlink()
    write(src, "new.md", "# New\nbody")

    normalize(src, out)

    assert not (out / old).exists()
    assert (out / "user.txt").read_text() == "KEEP"


def test_unsafe_output_and_collision(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    write(src, "a.md", "# A\nbody")
    with pytest.raises(ValueError):
        normalize(src, src)
    with pytest.raises(ValueError):
        normalize(src, tmp_path)
    link = tmp_path / "link"
    link.symlink_to(src, target_is_directory=True)
    with pytest.raises(ValueError):
        normalize(src, link)
    out = tmp_path / "out"
    write(out, "report.json", "UNOWNED")
    with pytest.raises(ValueError, match="unowned"):
        normalize(src, out)
    assert (out / "report.json").read_text() == "UNOWNED"


def test_nested_output_is_excluded(tmp_path):
    write(tmp_path, "a.md", "# A\nbody")
    out = tmp_path / "normalized"
    normalize(tmp_path, out)
    normalize(tmp_path, out)
    assert set(manifest(out)) == {"a.md"}


def test_interrupted_publication_can_be_retried(tmp_path, monkeypatch):
    from docnorm import publishing
    real_replace = publishing.os.replace
    count = 0

    def interrupted(src, dest):
        nonlocal count
        count += 1
        if count == 3:
            raise OSError("interrupted")
        return real_replace(src, dest)
    monkeypatch.setattr(publishing.os, "replace", interrupted)
    with pytest.raises(OSError):
        publish(tmp_path, {"a": b"A", "b": b"B", "report.json": b"{}"})
    monkeypatch.setattr(publishing.os, "replace", real_replace)

    publish(tmp_path, {"a": b"A", "b": b"B", "report.json": b"{}"})

    assert (tmp_path / "b").read_bytes() == b"B"
    assert not (tmp_path / ".docnorm-transaction.json").exists()


def test_network_guard_and_cli_exit_codes(tmp_path):
    with offline(), pytest.raises(RuntimeError, match="network"):
        socket.create_connection(("example.com", 443))
    src = tmp_path / "src"
    src.mkdir()
    write(src, "good.md", "# Good\nbody")
    assert main(["normalize", str(src), "--output", str(tmp_path / "out")]) == 0
    write(src, "bad.json", "{")
    assert main(["normalize", str(src), "--output", str(tmp_path / "out")]) == 1
    assert main(["normalize", str(src), "--output", str(src)]) == 2


def test_fence_escapes_embedded_fences():
    assert fence("```\ncontent\n```", "text").startswith("````text")


def test_inline_image_provenance_and_malformed_url(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "a.md", '# Image\n![tiny](data:image/png;base64,aW1hZ2U=)\n[malformed](http://[)')

    normalize(src, out)

    record = manifest(out)["a.md"]
    assert "../../assets/" in (out / record["output"]).read_text()
    assert "malformed link" in str(record["warnings"])
    assets = json.loads((out / "assets/provenance.json").read_text())["assets"]
    assert assets[0]["sources"][0]["path"] == "a.md"
    assert assets[0]["sources"][0]["locator"] == "embedded image"


def test_ambiguous_metadata_does_not_merge_different_sources(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "one/pay/doc.md", "# One\ncontent one")
    write(src, "two/pay/doc.md", "# Two\ncontent two")
    diplodoc(src, "page.html", "<p>Rendered</p>", "pay/doc.md")

    normalize(src, out)

    assert len(contents(out)) == 3
    assert "ambiguous" in str(manifest(out)["page.html"]["warnings"])


def test_legacy_encoding_empty_binary_and_broken_csv(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "legacy.txt").write_bytes("Привет, это русский текст документации.\n".encode("cp1251"))
    (src / "binary").write_bytes(b"\x00\x01binary")
    write(src, "bad.csv", 'a,b\n"unterminated')

    normalize(src, out)

    records = manifest(out)
    assert records["legacy.txt"]["status"] == "partial"
    assert "encoding inferred" in str(records["legacy.txt"]["warnings"])
    assert records["binary"]["status"] == "skipped"
    assert records["bad.csv"]["status"] == "failed"


def test_output_descendant_symlink_and_malicious_ownership(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "documents").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        publish(out, {"documents/foo/content.md": b"data"})
    (out / "documents").unlink()
    write(out, ".docnorm-owned.json", json.dumps({"tool": "docnorm", "schema_version": 1, "paths": ["../user.txt"]}))
    with pytest.raises(ValueError, match="invalid owned"):
        publish(out, {})


def test_ids_survive_content_edits(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "a.md", "# A\nfirst")
    normalize(src, out)
    before = set(contents(out))
    write(src, "a.md", "# A\nsecond")

    normalize(src, out)

    assert set(contents(out)) == before
    assert "second" in all_content(out)


def test_relative_parent_paths(tmp_path, monkeypatch):
    write(tmp_path / "src", "a.md", "# A\nbody")
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")

    report = normalize("../src", "../out")

    assert report["normalized_documents"] == 1
    roots = json.loads((tmp_path / "out/roots.json").read_text())
    assert roots["roots"]["source"]["path"] == str(tmp_path / "src")


def test_indented_code_is_not_rewritten_or_expanded(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    write(src, "a.md", '# Example\n\n    {% include [sample](missing.md) %}\n    [code](missing-too.md)\n\nText\n')

    normalize(src, out)

    assert manifest(out)["a.md"]["warnings"] == []
    assert "    [code](missing-too.md)" in all_content(out)


def test_html_inline_control_ids_do_not_break_paragraphs_or_tables(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    diplodoc(src, "article.html", '<p>Use <code id="inline-code-id-random">command</code> safely.</p><table><tr><th id="column">Name</th></tr><tr><td id="value">Data</td></tr></table>')

    normalize(src, out)

    text = all_content(out)
    assert "Use `command` safely." in text
    assert '| <a id="value"></a>Data |' in text
