"""
Assignment 3: build the index (`uv run python main.py index`).

Corpus: Spanish and EU rules on electric-vehicle charging infrastructure.
Pipeline, one function per step so server.py can import them:

    download(doc)              -> corpus/<id>.pdf (skipped if already there)
    extract_pages(path)        -> [(page number, raw text)]
    clean_lines(pages)         -> [(page, line)] without headers, footers,
                                  footnotes and tables of contents
    split_sections(lines)      -> [{"section", "words", "pages"}] by article,
                                  disposición, annex, ITC apartado, considerando
    chunk_file(path, design)   -> chunks with source, section and pages
    index_file(path, design)   -> embeds only chunks with no saved vector and
                                  stores them in ChromaDB (index/)

Two chunking designs are built, one ChromaDB collection each, so evaluate.py
can compare them by hit rate:

    "structure": one chunk per article / apartado; long ones split in
                 overlapping windows of 300 words.
    "fixed":     150-word windows with 30 words of overlap, ignoring structure
                 (the design of the class notebook).

Every vector is saved to disk as soon as its batch comes back, so hitting the
daily embedding limit loses nothing: run the command again the next day and it
continues where it stopped.
"""

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

import chromadb
from chromadb.config import Settings
from google import genai
from google.genai import errors, types
from pypdf import PdfReader

from api_key import load_api_key

ROOT = Path(__file__).resolve().parent
CORPUS_DIR = ROOT / "corpus"  # git-ignored: downloaded again by `main.py index`
INDEX_DIR = ROOT / "index"  # committed, vectors included

EMBEDDING_MODEL = "gemini-embedding-2"
DESIGNS = ["structure", "fixed"]  # embedded in this order (the daily quota may run out)
CHOSEN_DESIGN = "structure"  # the one server.py uses; set it from the hit rates in evaluate.py

DOCUMENTS = [
    {
        "id": "itc_bt_52",
        "title": "Real Decreto 1053/2014, ITC-BT-52: infraestructura para la recarga de vehículos eléctricos (texto consolidado)",
        "url": "https://www.boe.es/buscar/pdf/2014/BOE-A-2014-13681-consolidado.pdf",
    },
    {
        "id": "rd_184_2022",
        "title": "Real Decreto 184/2022: servicios de recarga energética de vehículos eléctricos (texto consolidado)",
        "url": "https://www.boe.es/buscar/pdf/2022/BOE-A-2022-4361-consolidado.pdf",
    },
    {
        "id": "rd_450_2022",
        "title": "Real Decreto 450/2022: modificación del Código Técnico de la Edificación (sección HE 6, recarga) y de la ITC-BT-52",
        "url": "https://www.boe.es/boe/dias/2022/06/15/pdfs/BOE-A-2022-9848.pdf",
    },
    {
        "id": "afir",
        "title": "Reglamento (UE) 2023/1804 (AFIR), infraestructura para los combustibles alternativos",
        # The EUR-Lex website blocks scripts; the EU Publications Office serves the same PDF.
        "url": "https://publications.europa.eu/resource/celex/32023R1804",
    },
]


def log(message):
    """Progress goes to stderr: server.py imports this file, and its stdout carries MCP messages."""
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 1. download


def download(doc):
    """Download one document into corpus/ unless it is already there. Returns its path."""
    CORPUS_DIR.mkdir(exist_ok=True)
    path = CORPUS_DIR / f"{doc['id']}.pdf"
    if path.exists() and path.stat().st_size > 0:
        return path
    log(f"Downloading {doc['id']} ...")
    request = urllib.request.Request(
        doc["url"],
        headers={"Accept": "application/pdf", "Accept-Language": "spa", "User-Agent": "daiist-assignment-3"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"{doc['url']} did not return a PDF ({len(data)} bytes).")
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------- 2. extract and clean


def extract_pages(path):
    """Text of each page, numbered from 1 as in the PDF."""
    return [(n, page.extract_text() or "") for n, page in enumerate(PdfReader(path).pages, start=1)]


# Lines that are page furniture, not content: they would be embedded into every chunk.
FURNITURE = [
    r"BOLETÍN OFICIAL DEL ESTADO",  # BOE header
    r"LEGISLACIÓN CONSOLIDADA",  # BOE consolidated-text footer
    r"Página \d+",  # BOE consolidated page number
    r"Núm\. \d+ .* Pág\. \d+",  # BOE daily edition header
    r"cve: BOE-\S+",
    r"Verificable en https://www\.boe\.es",
    r"(L \d+/\d+ )?ES Diario Oficial de la Unión Europea( \d+\.\d+\.\d{4})?( L \d+/\d+)?",  # EU Official Journal
]
FURNITURE_RE = re.compile(r"^(?:" + "|".join(FURNITURE) + r")$")
FOOTNOTE_START = re.compile(r"^\(\d+\) ")
FOOTNOTE_EVIDENCE = re.compile(
    r"\(DO [LC] |DO [LC] \d|ELI: |https?://|pendiente de publicación|Diario Oficial\)|véase la página"
    r"|^\(\d+\) (?:Reglamento|Directiva|Decisión|Posición|Recomendación|Comunicación|Dictamen|DO)\b"
)


def drop_footnotes(lines):
    """Remove the footnotes at the bottom of an EU Official Journal page.

    They start with "(n) " like recitals do, so we walk up from the bottom and
    drop a "(n) ..." block only if it cites the Official Journal ("DO L ...").
    The first block that is not a citation ends the footnote area.
    """
    keep_until = len(lines)
    block_end = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if FOOTNOTE_START.match(lines[i]):
            if FOOTNOTE_EVIDENCE.search(" ".join(lines[i:block_end])):
                keep_until = block_end = i
            else:
                break
        elif block_end - i > 6:  # a footnote is never this long: we are in the body
            break
    return lines[:keep_until]


def clean_lines(pages):
    """Every content line with its page, in reading order.

    Removes page furniture and footnotes, and the parts that would be
    retrieved for any question while answering none: the table of contents of
    BOE consolidated texts (everything before "TEXTO CONSOLIDADO"), the ITC's
    own index, and the AFIR's correspondence table (Anexo IV).
    """
    out = []
    for page, text in pages:
        lines = [" ".join(line.split()) for line in text.splitlines()]  # also turns   into spaces
        lines = [line for line in lines if line and not FURNITURE_RE.match(line)]
        out += [(page, line) for line in drop_footnotes(lines)]

    texts = [line for _, line in out]
    if "TEXTO CONSOLIDADO" in texts:
        out = out[texts.index("TEXTO CONSOLIDADO") + 1 :]

    cleaned, in_itc, skipping = [], False, False
    for n, (page, line) in enumerate(out):
        following = out[n + 1][1] if n + 1 < len(out) else ""
        if line == ITC_START:
            in_itc = True
        elif in_itc and line == "Índice" and not cleaned[-1][1].startswith("1."):
            skipping = True  # the ITC-BT-52's own index, up to its first apartado
        elif skipping and line == "1. Objeto y ámbito de aplicación":
            skipping = False
        elif line.startswith("ANEXO") and following.startswith("Tabla de correspondencias"):
            break  # AFIR correspondence table, the last page: a list of article numbers
        if not skipping:
            cleaned.append((page, line))
    return cleaned


def count_words(path):
    """Words left after cleaning, as reported in the corpus table."""
    return sum(len(line.split()) for _, line in clean_lines(extract_pages(path)))


# ---------------------------------------------------------------- 3. sections

# Spanish laws: "Artículo 4.  Principios...", "Artículo único.", "Disposición final primera."
BOE_HEADING = re.compile(
    r"^(Artículo (?:\d+(?: bis)?|único|[a-záéíóú]+)|Disposición (?:adicional|transitoria|derogatoria|final)(?: [a-záéíóú]+)?)\.(?:\s+(.*))?$"
)
EU_ARTICLE = re.compile(r"^Artículo (\d+)$")  # AFIR: number alone, title on the next line
ANNEX = re.compile(r"^ANEXO(?: ([IVX]+))?$")
RECITAL = re.compile(r"^\((\d+)\) ")
ITC_START = "INSTRUCCIÓN TÉCNICA COMPLEMENTARIA (ITC) BT-52"
ITC_TOP = re.compile(r"^([1-9])\. ([A-ZÁÉÍÓÚ][a-záéíóúñ ,]+)$")  # "2. Términos y definiciones"
ITC_SUB = re.compile(r"^(\d\.\d) ([A-ZÁÉÍÓÚ].*)$")  # "3.2 Instalación en ... . Text"
HE_SECTION = re.compile(r"^«?Sección (HE \d)\.")  # the section itself, not the index entry


def short(title, limit=90):
    """First sentence of a heading, cut to a readable length."""
    title = title.split(". ")[0].rstrip(". ")
    return title if len(title) <= limit else title[:limit].rsplit(" ", 1)[0] + "…"


def split_sections(lines):
    """Group lines into the document's own units, each with the pages it spans."""
    sections = []
    label, in_itc, in_recitals, article = "Preámbulo", False, False, None
    itc_top = 0  # last top-level ITC apartado seen
    pending_title = None  # AFIR articles and annexes have their title on the next line

    def start(new_label):
        sections.append({"section": new_label, "words": [], "pages": []})

    start(label)
    for page, line in lines:
        if pending_title:
            sections[-1]["section"] = f"{pending_title}. {short(line)}"
            pending_title = None
        elif line == ITC_START:
            in_itc = True
            start("ITC-BT-52, introducción")
        elif line == "Considerando lo siguiente:":
            in_recitals = True
        elif line == "HAN ADOPTADO EL PRESENTE REGLAMENTO:":
            in_recitals = False
        elif in_recitals and (m := RECITAL.match(line)):
            start(f"Considerando ({m.group(1)})")
        elif m := EU_ARTICLE.match(line):
            pending_title = f"Artículo {m.group(1)}"
            start(pending_title)
        elif m := ANNEX.match(line):
            pending_title = f"Anexo {m.group(1) or ''}".strip()
            start(pending_title)
        elif in_itc and (m := ITC_TOP.match(line)) and int(m.group(1)) == itc_top + 1:
            itc_top += 1  # apartados come in order: "3. Esta instrucción no..." inside apartado 1 is a list item
            start(f"ITC-BT-52 apartado {m.group(1)}. {short(m.group(2))}")
        elif in_itc and (m := ITC_SUB.match(line)) and m.group(1).startswith(f"{itc_top}."):
            start(f"ITC-BT-52 apartado {m.group(1)}. {short(m.group(2))}")
        elif not in_itc and (m := BOE_HEADING.match(line)):
            article = m.group(1)
            start(f"{article}. {short(m.group(2))}" if m.group(2) else article)
        elif article and (m := HE_SECTION.match(line)):
            start(f"{article}, Sección {m.group(1)} del CTE")
        words = line.split()
        sections[-1]["words"] += words
        sections[-1]["pages"] += [page] * len(words)
    return [s for s in sections if s["words"]]


# ---------------------------------------------------------------- 4. chunks


def windows(words, size, overlap):
    """Start and end of overlapping windows of `size` words."""
    if len(words) <= size:
        return [(0, len(words))]
    step = size - overlap
    return [(start, min(start + size, len(words))) for start in range(0, len(words) - overlap, step)]


def make_chunk(source, title, url, section, words, pages, start, end):
    return {
        "source": source,
        "title": title,
        "url": url,
        "section": section,
        "first_page": pages[start],
        "last_page": pages[end - 1],
        "text": " ".join(words[start:end]),
    }


def merge_recitals(sections, limit=300):
    """Recitals are short and numerous: group consecutive ones up to `limit` words."""
    merged = []
    for s in sections:
        prev = merged[-1] if merged else None
        if (
            prev
            and s["section"].startswith("Considerando")
            and prev["section"].startswith("Considerando")
            and len(prev["words"]) + len(s["words"]) <= limit
        ):
            first = prev["section"].split(" ")[1].split("-")[0].rstrip(")") + ")"
            prev["section"] = f"Considerandos {first}-{s['section'].split(' ')[1]}"
            prev["words"] += s["words"]
            prev["pages"] += s["pages"]
        else:
            merged.append({**s, "words": list(s["words"]), "pages": list(s["pages"])})
    return merged


def chunk_sections(sections, design, source, title, url):
    chunks = []
    if design == "structure":
        for s in merge_recitals(sections):
            size, overlap = (380, 0) if len(s["words"]) <= 380 else (300, 50)
            for start, end in windows(s["words"], size, overlap):
                chunks.append(make_chunk(source, title, url, s["section"], s["words"], s["pages"], start, end))
    elif design == "fixed":
        words = [w for s in sections for w in s["words"]]
        pages = [p for s in sections for p in s["pages"]]
        section_of = [s["section"] for s in sections for _ in s["words"]]
        for start, end in windows(words, 150, 30):
            chunks.append(make_chunk(source, title, url, section_of[start], words, pages, start, end))
    else:
        raise ValueError(f"Unknown design {design!r}; use one of {DESIGNS}.")
    return chunks


def guess_title(pages):
    """For a file added without a title: the first sentence of its first page (BOE and EU texts open with it)."""
    lines = [" ".join(l.split()) for l in pages[0][1].splitlines() if l.strip()][:4]
    text = " ".join(l for l in lines if not FURNITURE_RE.match(l))
    return short(text, 150) or "Documento sin título"


def chunk_file(path, design=CHOSEN_DESIGN, source=None, title=None, url=""):
    """Extract, clean and chunk one PDF or text file. Every chunk carries its source, section and pages."""
    path = Path(path)
    if path.suffix.lower() == ".pdf":
        pages = extract_pages(path)
    elif path.suffix.lower() in {".txt", ".md"}:
        pages = [(1, path.read_text(encoding="utf-8"))]
    else:
        raise ValueError(f"Only .pdf, .txt and .md files can be indexed, not {path.suffix!r}.")
    lines = clean_lines(pages)
    if not lines:
        raise ValueError(f"No text could be extracted from {path.name} (is it a scanned PDF?).")
    source = source or re.sub(r"\W+", "_", path.stem.lower()).strip("_")
    title = title or guess_title(pages)
    return chunk_sections(split_sections(lines), design, source, title, url or path.name)


def location(chunk):
    """Human-readable location, e.g. 'Artículo 6. Derechos..., p. 9' or '..., pp. 9-10'."""
    first, last = chunk["first_page"], chunk["last_page"]
    return f"{chunk['section']}, " + (f"p. {first}" if first == last else f"pp. {first}-{last}")


# ---------------------------------------------------------------- 5. embed and store

_client = None


def client():
    """One Gemini client, created on first use, that waits and retries on rate limits."""
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=load_api_key(),
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(
                    attempts=5, initial_delay=60, max_delay=60, exp_base=1, jitter=0,
                    http_status_codes=[429, 500, 502, 503, 504],
                )
            ),
        )
    return _client


def embed_texts(texts):
    """Embed already-prefixed texts in one call. Returns one list of floats per text."""
    result = client().models.embed_content(
        model=EMBEDDING_MODEL,
        contents=[types.Content(parts=[types.Part(text=t)]) for t in texts],  # one Content per text
    )
    return [e.values for e in result.embeddings]


def document_text(chunk):
    """What gets embedded: the document and section go into the title, as in the class notebook."""
    return f"title: {chunk['title']} — {chunk['section']} | text: {chunk['text']}"


def embed_query(question):
    return embed_texts([f"task: search result | query: {question}"])[0]


def get_collection(design=CHOSEN_DESIGN):
    db = chromadb.PersistentClient(path=str(INDEX_DIR), settings=Settings(anonymized_telemetry=False))
    return db.get_or_create_collection(
        name=f"corpus_{design}",
        configuration={"hnsw": {"space": "cosine"}},
        embedding_function=None,  # we always pass our own Gemini vectors
    )


def chunk_id(chunk, n, design):
    digest = hashlib.sha1(chunk["text"].encode("utf-8")).hexdigest()[:10]
    return f"{chunk['source']}:{design}:{n:04d}:{digest}"


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def embed_and_store(chunks, design=CHOSEN_DESIGN, extra_metadata=None, batch_size=100):
    """Embed the chunks that have no saved vector yet and store each batch at once.

    Returns the number of chunks embedded now. Raises SystemExit with a clear
    message if the API refuses (for example, the daily limit is spent): every
    batch stored before that is already on disk.
    """
    collection = get_collection(design)
    ids = [chunk_id(c, n, design) for n, c in enumerate(chunks)]
    saved = set(collection.get(ids=ids, include=[])["ids"]) if ids else set()
    todo = [(i, c) for i, c in zip(ids, chunks) if i not in saved]
    for start in range(0, len(todo), batch_size):
        batch = todo[start : start + batch_size]
        try:
            vectors = embed_texts([document_text(c) for _, c in batch])
        except errors.APIError as error:
            raise SystemExit(
                f"Embedding stopped after {start} of {len(todo)} new chunks: {error}\n"
                "Everything embedded so far is saved in index/. If this is the daily limit,\n"
                "run the same command again after 09:00 (Spain): it continues from here."
            )
        collection.add(
            ids=[i for i, _ in batch],
            embeddings=vectors,
            documents=[c["text"] for _, c in batch],
            metadatas=[
                {k: c[k] for k in ("source", "title", "url", "section", "first_page", "last_page")}
                | {"design": design}
                | (extra_metadata or {})
                for _, c in batch
            ],
        )
        log(f"  {design}: stored {start + len(batch)}/{len(todo)} new chunks")
    return len(todo)


def index_file(path, design=CHOSEN_DESIGN, source=None, title=None, url=""):
    """Run one file through the whole pipeline. Used by `main.py index` and by server.py.

    If the same file (same SHA-256) is already indexed under this source, only
    missing vectors are embedded, so nothing is spent twice. If the source is
    indexed from a different file, it is a new version: the old chunks are
    deleted and the new ones stored, so the index never holds two versions of
    one rule. Returns a summary dict.
    """
    path = Path(path)
    digest = file_hash(path)
    chunks = chunk_file(path, design, source, title, url)
    source = chunks[0]["source"]
    collection = get_collection(design)
    existing = collection.get(where={"source": source}, include=["metadatas"])
    old_hashes = {m.get("file_hash") for m in existing["metadatas"]}
    new_ids = {chunk_id(c, n, design) for n, c in enumerate(chunks)}

    if existing["ids"] and old_hashes != {digest}:
        status = "replaced"  # a new version of a document we already had
        collection.delete(ids=existing["ids"])
    else:
        status = "already indexed" if existing["ids"] else "added"
        stale = [i for i in existing["ids"] if i not in new_ids]  # chunking changed since last run
        if stale:
            collection.delete(ids=stale)
    embedded = embed_and_store(chunks, design, {"file_hash": digest})
    if status == "already indexed" and embedded:
        status = "completed"  # an earlier run had stopped half-way
    return {
        "source": source,
        "title": chunks[0]["title"],
        "status": status,
        "chunks": len(chunks),
        "embedded_now": embedded,
    }


# ---------------------------------------------------------------- 6. search (used by server.py and evaluate.py)


def retrieve(question, k=5, design=CHOSEN_DESIGN, query_vector=None, where=None):
    """The k chunks closest to the question, best first, each with a score and its location."""
    collection = get_collection(design)
    result = collection.query(
        query_embeddings=[query_vector if query_vector is not None else embed_query(question)],
        n_results=k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    hits = []
    for text, meta, distance in zip(result["documents"][0], result["metadatas"][0], result["distances"][0]):
        hit = {**meta, "text": text, "score": round(1 - distance, 4)}
        hit["location"] = location(hit)
        hits.append(hit)
    return hits


# ---------------------------------------------------------------- main


def main():
    paths = {doc["id"]: download(doc) for doc in DOCUMENTS}
    print("Corpus after cleaning:")
    for doc in DOCUMENTS:
        print(f"  {doc['id']:<12} {count_words(paths[doc['id']]):>7,} words")
    print(f"  {'total':<12} {sum(count_words(p) for p in paths.values()):>7,} words")

    for design in DESIGNS:
        print(f"\n=== design: {design}")
        for doc in DOCUMENTS:
            summary = index_file(paths[doc["id"]], design, doc["id"], doc["title"], doc["url"])
            print(
                f"  {doc['id']:<12} {summary['status']:<16} {summary['chunks']:>4} chunks, "
                f"{summary['embedded_now']:>4} embedded now"
            )
        print(f"  {get_collection(design).count()} chunks in collection corpus_{design}")


if __name__ == "__main__":
    main()