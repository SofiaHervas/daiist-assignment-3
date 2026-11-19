"""
Assignment 3: MCP server. Your editor and agent.py start it; by hand: `uv run server.py`.

Two tools over the EV-charging regulations indexed by index.py:

- search(query): the passages closest to the question, each with the citation
  the answer must use, e.g. [itc_bt_52, p. 31].
- add_document(path): runs one file through index.py's pipeline (imported,
  not rewritten), embeds its chunks and stores them in index/.

It loads the committed index at startup and never re-embeds the corpus: the
only embedding calls are one per search query and those of a new document.
Nothing is printed to stdout, which carries the MCP protocol; logs go to stderr.
"""

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

import index

ROOT = Path(__file__).resolve().parent
MAX_K = 10

mcp = MCPServer("corpus", log_level="WARNING")

_collection = index.get_collection(index.CHOSEN_DESIGN)  # opened once, from the committed files
if _collection.count() == 0:
    print("WARNING: the index is empty. Run `uv run python main.py index` first.", file=sys.stderr)


def citation(hit):
    """The tag the answer must use for this passage: source id and page(s)."""
    first, last = hit["first_page"], hit["last_page"]
    pages = f"p. {first}" if first == last else f"pp. {first}-{last}"
    return f"[{hit['source']}, {pages}]"


@mcp.tool()
def search(query: str, k: int = 5) -> str:
    """Search Spanish and EU regulations on electric-vehicle charging infrastructure.

    The corpus holds: the ITC-BT-52 low-voltage rules for charging installations
    (circuits, protections, connectors, heights, metering, minimum provisions in car
    parks), Real Decreto 184/2022 (charging service: operators, rights and duties,
    prices, information), Real Decreto 450/2022 (building code section HE 6: minimum
    charging infrastructure in buildings) and the EU AFIR Regulation 2023/1804
    (public charging targets, payment, pricing, data). Documents added later with
    add_document are searched too.

    Use it for any question about these rules, before answering, and again with
    other words if the passages do not answer the question. Write the query in
    Spanish or English; the texts are in Spanish.

    Args:
        query: the question or the key terms to look for.
        k: how many passages to return, 1 to 10 (default 5).

    Returns the k closest passages, best first. Each starts with its citation, for
    example [itc_bt_52, p. 31], followed by the document, article or section,
    similarity score and the text. Cite facts with exactly these tags. Passages are
    always returned, even when none answers the question: check that they do.
    """
    query = query.strip()
    if not query:
        raise ValueError("The query is empty.")
    k = max(1, min(int(k), MAX_K))
    hits = index.retrieve(query, k=k, design=index.CHOSEN_DESIGN)
    if not hits:
        return "The index is empty: no passages to search."
    blocks = []
    for n, hit in enumerate(hits, start=1):
        blocks.append(
            f"{n}. {citation(hit)} {hit['title']}\n"
            f"   Section: {hit['section']} | score {hit['score']:.3f}\n"
            f"   {hit['text']}"
        )
    return "\n\n".join(blocks)


@mcp.tool()
def add_document(path: str) -> str:
    """Add one document (PDF, .txt or .md) to the searchable index.

    The file is extracted, cleaned, split by article or section and embedded with
    the same pipeline as the rest of the corpus, then stored in index/ so search
    finds it. The file must be inside this project folder (for example in corpus/).

    If exactly the same file is already indexed, nothing is embedded again. If a
    different file with the same name was indexed before, it is treated as a new
    version: the old passages are removed and replaced, so the index never holds two
    versions of one document.

    Args:
        path: path of the file, absolute or relative to the project folder.

    Returns what happened (added, already indexed, replaced), the source id to cite
    it by and how many passages were stored.
    """
    file = Path(path).expanduser()
    file = (file if file.is_absolute() else ROOT / file).resolve()
    if not file.is_relative_to(ROOT):
        raise ValueError(f"{path} is outside the project folder; only files inside {ROOT.name}/ can be added.")
    if not file.is_file():
        raise ValueError(f"{path} does not exist or is not a file.")
    summary = index.index_file(file, design=index.CHOSEN_DESIGN)
    return (
        f"{summary['status']}: {file.name} as source '{summary['source']}' "
        f"({summary['title']}). {summary['chunks']} passages in the index, "
        f"{summary['embedded_now']} embedded now. Cite it as [{summary['source']}, p. N]."
    )


if __name__ == "__main__":
    mcp.run()  # stdio
