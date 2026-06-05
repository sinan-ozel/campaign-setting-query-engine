"""Campaign Setting Query Engine — Streamlit dashboard.

Communicates exclusively with the mcp-server HTTP API.
Never accesses Redis, MinIO, or Fuseki directly.

Environment variables:
  MCP_SERVER_URL         http://mcp-server:8000
  POLL_INTERVAL_SECONDS  10
"""

import json
import os
import time
from datetime import datetime, timezone

import httpx
import streamlit as st
import yaml

MCP_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000")
POLL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "10"))
PAGE_SIZE = 10  # must match server/status.py PAGE_SIZE
STALE_THRESHOLD_SECONDS = 600

st.set_page_config(
    page_title="Campaign Query Engine",
    page_icon="📚",
    layout="wide",
)

# ── Helpers ────────────────────────────────────────────────────────────────


def _get(path: str, **params) -> dict | None:
    try:
        r = httpx.get(f"{MCP_URL}{path}", params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Cannot reach mcp-server: {exc}")
        return None


def _post(path: str, **kwargs) -> httpx.Response | None:
    try:
        return httpx.post(f"{MCP_URL}{path}", timeout=30, **kwargs)
    except Exception as exc:
        st.error(f"Request failed: {exc}")
        return None


def _progress_str(doc: dict) -> str:
    status = doc.get("status", "")
    if status == "CONVERTING_PDF":
        curr = doc.get("current_page", "?")
        total = doc.get("total_pages", "?")
        return f"Page {curr} / {total}"
    if status in (
        "CLASSIFYING_SECTIONS", "EXTRACTING_ENTITIES",
        "MAPPING_TO_ONTOLOGY", "LOADING_GRAPH",
    ):
        curr = doc.get("current_chunk", "?")
        total = doc.get("total_chunks", "?")
        return f"Chunk {curr} / {total}"
    if status == "COMPLETED":
        n = doc.get("entity_count", "?")
        return f"{n} entities"
    return "—"


def _is_stale(doc: dict) -> bool:
    if doc.get("status") not in (
        "CONVERTING_PDF", "CLASSIFYING_SECTIONS", "EXTRACTING_ENTITIES",
        "MAPPING_TO_ONTOLOGY", "LOADING_GRAPH",
    ):
        return False
    try:
        updated = datetime.fromisoformat(doc["updated_at"])
        return (
            datetime.now(timezone.utc) - updated
        ).total_seconds() > STALE_THRESHOLD_SECONDS
    except (KeyError, ValueError):
        return False


def _status_badge(status: str) -> str:
    colours = {
        "PENDING": "🟡",
        "CONVERTING_PDF": "🔵",
        "MARKDOWN_READY": "🔵",
        "CLASSIFYING_SECTIONS": "🔵",
        "EXTRACTING_ENTITIES": "🔵",
        "MAPPING_TO_ONTOLOGY": "🔵",
        "LOADING_GRAPH": "🔵",
        "COMPLETED": "🟢",
        "FAILED": "🔴",
    }
    return f"{colours.get(status, '⚪')} {status}"


def _slugify(title: str) -> str:
    import re
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    return re.sub(r"[\s-]+", "_", slug.strip())


# ── Views ──────────────────────────────────────────────────────────────────


def status_view() -> None:
    """Paginated status table with retry buttons and progress display."""
    st.subheader("Ingestion Status")

    # Refresh / connection check
    col_refresh, col_health = st.columns([1, 5])
    if col_refresh.button("↻ Refresh"):
        st.rerun()
    health = _get("/health")
    if health:
        fuseki = "✅" if health.get("fuseki") else "❌"
        redis = "✅" if health.get("redis") else "❌"
        col_health.caption(f"Fuseki {fuseki}  Redis {redis}")

    # Pagination controls
    page = st.session_state.get("status_page", 1)
    data = _get("/status", page=page)
    if data is None:
        return

    docs = data.get("documents", [])
    total = data.get("total", 0)
    pages = max(1, -(-total // PAGE_SIZE))

    col_prev, col_info, col_next = st.columns([1, 4, 1])
    if col_prev.button("← Prev", disabled=page <= 1):
        st.session_state["status_page"] = page - 1
        st.rerun()
    col_info.caption(f"Page **{page}** of **{pages}** — {total} documents total")
    if col_next.button("Next →", disabled=page >= pages):
        st.session_state["status_page"] = page + 1
        st.rerun()

    if not docs:
        st.info("No documents ingested yet. Use the **Ingest** tab to submit a PDF.")
        return

    # Table header
    h1, h2, h3, h4, h5 = st.columns([4, 3, 2, 3, 1])
    h1.markdown("**Title**")
    h2.markdown("**Status**")
    h3.markdown("**Progress**")
    h4.markdown("**Updated**")
    h5.markdown("**Action**")
    st.divider()

    for doc in docs:
        c1, c2, c3, c4, c5 = st.columns([4, 3, 2, 3, 1])
        c1.write(doc.get("title", doc.get("document_id", "—")))
        c2.write(_status_badge(doc.get("status", "")))
        c3.write(_progress_str(doc))
        c4.write(doc.get("updated_at", "—")[:19].replace("T", " ") if doc.get("updated_at") else "—")

        doc_id = doc.get("document_id", "")
        is_failed = doc.get("status") == "FAILED"
        stale = _is_stale(doc)
        if is_failed or stale:
            label = "Retry" if is_failed else "Re-queue"
            if c5.button(label, key=f"retry_{doc_id}"):
                resp = _post(f"/admin/requeue/{doc_id}")
                if resp and resp.status_code == 200:
                    st.success(f"{doc_id} re-queued.")
                    st.rerun()
                elif resp:
                    st.error(f"Requeue failed: {resp.text}")

        if is_failed and doc.get("error"):
            with st.expander(f"Error — {doc_id}"):
                st.code(doc["error"])


def ingest_view() -> None:
    """PDF submission form."""
    st.subheader("Ingest a New Book")

    with st.form("ingest_form"):
        title = st.text_input("Title *")
        col_ed, col_can = st.columns(2)
        edition = col_ed.selectbox("Edition *", ["3e", "4e", "5e", "any"])
        canon = col_can.selectbox("Canon type *", ["canon", "kanon", "community"])
        publisher = st.text_input("Publisher")
        year = st.text_input("Publication year")
        authors_raw = st.text_input("Authors (comma-separated)")
        tags_raw = st.text_input(
            "Tags (comma-separated, free-form)",
            help="e.g. core-rulebook, setting-lore, adventure-module",
        )
        pdf_file = st.file_uploader("PDF file *", type=["pdf"])
        submitted = st.form_submit_button("Submit")

    if submitted:
        if not title or not pdf_file:
            st.error("Title and PDF file are required.")
            return

        document_id = _slugify(title)
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        authors = [a.strip() for a in authors_raw.split(",") if a.strip()]

        metadata: dict = {
            "document_id": document_id,
            "title": title,
            "edition": edition,
            "canon_type": canon,
        }
        if publisher:
            metadata["publisher"] = publisher
        if year:
            metadata["publication_year"] = year
        if authors:
            metadata["authors"] = authors
        if tags:
            metadata["tags"] = tags

        resp = _post(
            "/ingest",
            files={"pdf": (pdf_file.name, pdf_file.getvalue(), "application/pdf")},
            data={"metadata": yaml.dump(metadata, allow_unicode=True)},
        )

        if resp is None:
            return
        if resp.status_code == 202:
            st.success(f"Submitted: **{document_id}** — status is now PENDING.")
            st.session_state["active_tab"] = "Status"
            st.rerun()
        elif resp.status_code == 409:
            st.error(f"document_id **{document_id}** already exists.")
        else:
            st.error(f"Submission failed ({resp.status_code}): {resp.text}")


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    st.title("📚 Campaign Setting Query Engine")

    tab_labels = ["Status", "Ingest"]
    active = st.session_state.get("active_tab", "Status")
    tabs = st.tabs(tab_labels)

    with tabs[0]:
        status_view()
        time.sleep(POLL_SECONDS)
        st.rerun()

    with tabs[1]:
        ingest_view()


if __name__ == "__main__":
    main()
