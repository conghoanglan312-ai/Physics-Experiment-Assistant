"""Safe entry point for the manual ingestion (llm/src/ingest.py).

ingest() rebuilds everything every time it runs: it deletes the `physics_manual`
collection, re-reads the PDF and re-embeds every chunk through the Gemini API.
Call this wrapper instead of ingest() from the app or from scripts. It:

  * skips the run when the collection is already populated (force=True rebuilds)
  * checks the PDF exists BEFORE anything is deleted
  * resolves ingest()'s relative paths against the project root, so it works no
    matter which directory the app was started from
  * refuses to start a second run while one is in progress
  * verifies the collection actually holds documents afterwards

Run from the project root:
    python -m llm.src.ingestWrapper            # ingest only if needed
    python -m llm.src.ingestWrapper --force    # delete and rebuild

From code:
    from llm.src.ingestWrapper import runIngestion
    runIngestion()                             # e.g. once at app startup
"""

import argparse
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import chromadb

from llm.app.geminiEmbedding import GeminiEmbeddingFunction
from llm.src.ingest import ingest

log = logging.getLogger(__name__)

# These three must match ingest.py and contextRetrieval.py.
COLLECTION_NAME = "physics_manual"
CHROMA_PATH = "llm/chroma_db"
PDF_PATH = Path("llm") / "pdf" / "instructionBook.pdf"

# llm/src/ingestWrapper.py -> parents: [0] src, [1] llm, [2] project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

_running = threading.Lock()


class IngestionError(RuntimeError):
    """Ingestion could not run, or did not leave a usable collection behind."""


@contextmanager
def _inProjectRoot():
    """Run with the project root as the working directory.

    ingest() and contextRetrieval() use paths relative to the working directory.
    chdir is process-wide, so do not run this alongside code that depends on the
    current directory (calling it once at startup is fine).
    """
    previous = Path.cwd()
    if previous == PROJECT_ROOT:
        yield
        return
    os.chdir(PROJECT_ROOT)
    try:
        yield
    finally:
        os.chdir(previous)


def _documentCount():
    """Documents in the collection; 0 if the collection does not exist yet."""
    try:
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        collection = client.get_collection(
            name=COLLECTION_NAME,
            embedding_function=GeminiEmbeddingFunction()
        )
        return collection.count()
    except Exception:
        # Which exception a missing collection raises differs between chromadb
        # versions, so treat any failure here as "nothing usable yet".
        log.debug("Collection %r not readable", COLLECTION_NAME, exc_info=True)
        return 0


def isIngested():
    """True if the collection exists and holds at least one document."""
    with _inProjectRoot():
        return _documentCount() > 0


def runIngestion(force=False):
    """Build the vector store from the PDF.

    Returns {"status": "skipped" | "ingested", "documents": int, "seconds": float}.
    Raises IngestionError if the PDF is missing, another run is in progress,
    ingest() fails, or the collection is empty afterwards.

    force=True deletes the existing collection first (that is what ingest() does),
    so a failure part-way leaves it empty; the next non-forced call notices and
    rebuilds it.
    """
    if not _running.acquire(blocking=False):
        raise IngestionError("An ingestion is already running")

    started = time.perf_counter()
    try:
        pdf = PROJECT_ROOT / PDF_PATH
        if not pdf.is_file():
            raise IngestionError(f"Manual not found: {pdf}")

        with _inProjectRoot():
            if not force:
                existing = _documentCount()
                if existing > 0:
                    log.info("Already ingested (%d documents); skipping", existing)
                    return {
                        "status": "skipped",
                        "documents": existing,
                        "seconds": time.perf_counter() - started
                    }

            log.info("Ingesting %s ...", pdf.name)
            try:
                ingest()
            except Exception as exc:
                raise IngestionError(f"ingest() failed: {exc}") from exc

            documents = _documentCount()

        if documents == 0:
            raise IngestionError("ingest() finished but the collection is empty")

        return {
            "status": "ingested",
            "documents": documents,
            "seconds": time.perf_counter() - started
        }
    finally:
        _running.release()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ingest the instruction manual into the Chroma vector store."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete the existing collection and rebuild it"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        result = runIngestion(force=args.force)
    except IngestionError as exc:
        log.error("Ingestion failed: %s", exc)
        return 1

    if result["status"] == "skipped":
        log.info("Nothing to do. Use --force to rebuild.")
    else:
        log.info("Ingested %d documents in %.1f s",
                 result["documents"], result["seconds"])
    return 0


if __name__ == "__main__":
    sys.exit(main())