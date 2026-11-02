"""Convert narrative HTML to TXT without dropping non-<p> narrative blocks.
Do not strip whitespace inside actual narration except cosmetic boundary spaces.
"""
from __future__ import annotations
import re
from bs4 import BeautifulSoup, NavigableString, Tag

BLOCKS = {"p", "div", "section", "article", "main", "blockquote", "li", "ul", "ol", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
IGNORE = {"script", "style", "noscript", "svg", "canvas", "rt", "rp"}

def narrative_text(block: Tag | None) -> str:
    if block is None:
        return ""
    soup = BeautifulSoup(str(block), "html.parser")
    root = soup.find()
    if root is None:
        return ""
    out: list[str] = []
    def walk(node):
        if isinstance(node, NavigableString):
            value = str(node).replace("\r\n", "\n").replace("\r", "\n")
            if not value.strip() and "\n" in value:
                return
            out.append(value)
            return
        if not isinstance(node, Tag):
            return
        name = (node.name or "").lower()
        if name in IGNORE:
            return
        if name == "br":
            out.append("\n")
            return
        if name in BLOCKS and out and not out[-1].endswith("\n"):
            out.append("\n")
        for child in node.children:
            walk(child)
        if name in BLOCKS:
            out.append("\n")
    walk(root)
    text = "".join(out).replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+\n", "\n", text)
    text = re.sub(r"\n[ \t\u3000]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
