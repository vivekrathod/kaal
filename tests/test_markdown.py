"""The web app's Markdown renderer, run under Node. It returns a tree, never HTML text."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "kaal" / "web" / "markdown.js"
FILE = "a" * 32
MISSING = "b" * 32

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="needs node")

HARNESS = """
import { renderMarkdown } from "./markdown.mjs";
import { readFileSync } from "node:fs";
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const ser = (n) => typeof n === "string" ? esc(n)
  : `<${n.tag}${Object.entries(n.attrs).filter(([, v]) => v !== false).map(([k, v]) => v === true ? ` ${k}` : ` ${k}="${esc(String(v))}"`).join("")}>${n.children.map(ser).join("")}</${n.tag}>`;
const resource = (id, label, isImage) => id.startsWith("a") ? { tag: "file", attrs: { label, image: isImage }, children: [] } : null;
const cases = JSON.parse(readFileSync(process.argv[2], "utf8"));
const started = Date.now();
const out = cases.map((source) => renderMarkdown(source, { resource }).map(ser).join(""));
console.log(JSON.stringify({ out, ms: Date.now() - started }));
"""


def render(tmp_path, *sources):
    (tmp_path / "markdown.mjs").write_text(MODULE.read_text())
    (tmp_path / "harness.mjs").write_text(HARNESS)
    (tmp_path / "cases.json").write_text(json.dumps(list(sources)))
    done = subprocess.run(["node", str(tmp_path / "harness.mjs"), str(tmp_path / "cases.json")], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


LINK = 'target="_blank" rel="noopener noreferrer"'
CASES = [
    ("# Title\n\nSome *soft* and **strong** and ~~gone~~ text.", "<h1>Title</h1><p>Some <em>soft</em> and <strong>strong</strong> and <del>gone</del> text.</p>"),
    ("### Third ###", "<h3>Third</h3>"),
    ("line one\nline two\n\nnext", "<p>line one<br></br>line two</p><p>next</p>"),
    ("Use `code` and ``a ` b``.", "<p>Use <code>code</code> and <code>a ` b</code>.</p>"),
    ("```python\nx = 1\n<b>not html</b>\n```\nafter", "<pre><code>x = 1\n&lt;b&gt;not html&lt;/b&gt;</code></pre><p>after</p>"),
    ("- one\n- two\n  - inner\n- three", "<ul><li>one</li><li>two<ul><li>inner</li></ul></li><li>three</li></ul>"),
    ("3. three\n4. four", '<ol start="3"><li>three</li><li>four</li></ol>'),
    ("- [ ] todo\n- [x] done", '<ul><li><input type="checkbox" disabled></input> todo</li><li><input type="checkbox" disabled checked></input> done</li></ul>'),
    ("> quoted\n> more\n\nplain", "<blockquote><p>quoted<br></br>more</p></blockquote><p>plain</p>"),
    ("| A | B |\n|---|:-:|\n| 1 | 2 |\n| x \\| y |", "<table><thead><tr><th>A</th><th>B</th></tr></thead><tbody><tr><td>1</td><td>2</td></tr><tr><td>x | y</td><td></td></tr></tbody></table>"),
    ("a\n\n---\n\nb", "<p>a</p><hr></hr><p>b</p>"),
    ("[site](https://example.com/a?b=1) and <https://example.org>.", f'<p><a href="https://example.com/a?b=1" {LINK}>site</a> and <a href="https://example.org" {LINK}>https://example.org</a>.</p>'),
    ("See https://example.com/path, then stop.", f'<p>See <a href="https://example.com/path" {LINK}>https://example.com/path</a>, then stop.</p>'),
    ("snake_case_name and file_name.txt stay; _this_ is soft.", "<p>snake_case_name and file_name.txt stay; <em>this</em> is soft.</p>"),
    ("2 * 3 * 4 and \\*not soft\\*", "<p>2 * 3 * 4 and *not soft*</p>"),
    ("first<br>second &amp; third&nbsp;end", "<p>first<br></br>second &amp; third end</p>"),
    (f"![photo](:/{FILE}) and [statement.pdf](:/{FILE.upper()})", '<p><file label="photo" image></file> and <file label="statement.pdf"></file></p>'),
    (f"![gone](:/{MISSING}) [lost](:/{MISSING})", "<p>gone lost</p>"),
    ("![far](https://example.com/x.png)", f'<p><a href="https://example.com/x.png" {LINK}>far</a></p>'),
]


@pytest.mark.parametrize("source, expected", CASES, ids=[str(number) for number in range(len(CASES))])
def test_markdown_becomes_the_expected_tree(tmp_path, source, expected):
    assert render(tmp_path, source)["out"] == [expected]


HOSTILE = [
    "<script>alert(1)</script>",
    '<img src=x onerror="alert(1)">',
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    "[click](data:text/html,<script>alert(1)</script>)",
    "![x](javascript:alert(1))",
    '[x](https://example.com/" onmouseover="alert(1))',
    "<a href=\"javascript:alert(1)\">x</a>",
    "[x](vbscript:msgbox(1)) <iframe src=//example.com></iframe>",
]


def test_nothing_in_a_note_becomes_an_element_or_a_scripted_link(tmp_path):
    # The harness writes text with < and > escaped, so any tag found here is a real element.
    for source, rendered in zip(HOSTILE, render(tmp_path, *HOSTILE)["out"]):
        tags = set(__import__("re").findall(r"<([a-z0-9]+)[ >]", rendered))
        assert tags <= {"p", "a", "br"}, (source, rendered)
        for href in __import__("re").findall(r'<a href="([^"]*)"', rendered):
            assert href.startswith(("https://", "http://", "mailto:")), (source, href)


def test_odd_and_enormous_notes_are_still_rendered_quickly(tmp_path):
    sources = [
        "*" * 5000, "_" * 5000, "[" * 3000, "`" * 3001, "**a" * 3000, "> " * 2000 + "deep", "- " * 2000 + "deep",
        "| a |\n|---|\n" + "| b |\n" * 5000, "\n".join("  " * depth + "- item" for depth in range(60)), "x" * 500000, "", "\n\n\n", "#", "```\nnever closed",
    ]
    result = render(tmp_path, *sources)
    assert len(result["out"]) == len(sources)
    assert result["ms"] < 5000
    assert result["out"][-1] == "<pre><code>never closed</code></pre>"
    assert result["out"][9].startswith("<pre>")
