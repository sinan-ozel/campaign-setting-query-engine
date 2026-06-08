"""Semantic Markdown chunker.

Ported from the prior chunking_pipeline.py reference implementation.
Splits a Markdown document into sections based on headings (ATX, bold,
italic), strips the TOC, strips page-number footers, and emits one chunk
per section with rich metadata including page number, hierarchy, and
book information.
"""

import re
from typing import Optional

import tiktoken
import yaml

_tokenizer = tiktoken.get_encoding("cl100k_base")

# Tokens reserved for the extractor system prompt, known-entities hint, and
# output budget.  Derived empirically from the prompt in extractor.py.
_PROMPT_OVERHEAD = 700


def count_tokens(text: str) -> int:
    """Return the cl100k_base token count for text."""
    return len(_tokenizer.encode(text))


class MarkdownChunker:
    """Heading-based semantic chunker for PDF-extracted Markdown."""

    HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$")
    TOC_LINE_RE = re.compile(
        r"^\s*\**(?P<title>.+?)\**(?:[.\-–—·•\s]{2,})?(?P<page>\d{1,4})\s*$"
    )
    BOLD_TITLE_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
    ITALIC_TITLE_RE = re.compile(r"^_(.+?)_\s*$")
    FOOTER_PAGE_RE = re.compile(r"^\*\*(\d{1,4})\*\*\s*$")
    BARE_NUMBER_RE = re.compile(r"^(\d{1,4})\s*$")
    TABLE_RE = re.compile(r"^\|.+\|")
    CHAPTER_FOOTER_RE = re.compile(r"^Chapter\s+\d+\s+\|.+$", re.IGNORECASE)
    CHAPTER_RE = re.compile(
        r"\b(chapter|chap|ch|cap[ií]tulo|capitulo|capitolo|chapitre|"
        r"kapitel|глава|第\s*\d+\s*章)\.?\s*(\d+)",
        re.IGNORECASE,
    )
    DROP_CAP_RE = re.compile(r"^`?.{1,2}`?\s*$")
    SENTENCE_END_RE = re.compile(r'[.!?",\-—]\s*$')
    BOLD_LEADIN_RE = re.compile(r"^\*\*.+?\*\*\s+\S")
    CODE_FENCE_RE = re.compile(r"^```")
    PAGE_MARKER_RE = re.compile(r"<!--\s*page:\s*(\d+)\s*-->")
    MAX_TITLE_LEN = 80

    @staticmethod
    def parse_frontmatter(text: str) -> tuple[dict, str]:
        """Parse YAML front matter; return (metadata, body)."""
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, text
        i = 1
        yaml_lines: list[str] = []
        while i < len(lines) and lines[i].strip() != "---":
            yaml_lines.append(lines[i])
            i += 1
        body = "\n".join(lines[i + 1:])
        meta = yaml.safe_load("\n".join(yaml_lines)) or {}
        return meta, body

    @staticmethod
    def extract_toc_and_body(text: str) -> tuple[list, str]:
        """Detect and strip the TOC; return (toc_entries, body)."""
        lines = text.splitlines()
        toc: list[dict] = []
        toc_end_idx = 0
        consecutive = 0
        toc_started = False
        failures = 0
        max_lines = min(1000, len(lines))

        for i, raw in enumerate(lines[:max_lines]):
            line = raw.strip().strip("*_`")
            if not line:
                continue
            m = MarkdownChunker.TOC_LINE_RE.match(line)
            if m:
                consecutive += 1
                if consecutive >= 3:
                    toc_started = True
                if toc_started:
                    title = m.group("title").strip()
                    page = int(m.group("page"))
                    ch_match = MarkdownChunker.CHAPTER_RE.search(title)
                    chapter = int(ch_match.group(2)) if ch_match else None
                    toc.append({"title": title, "page": page, "chapter": chapter})
                    toc_end_idx = i + 1
                    failures = 0
            else:
                consecutive = 0
                if toc_started:
                    failures += 1
                    if failures > 5:
                        break

        if not toc_started:
            return [], text
        return toc, "\n".join(lines[toc_end_idx:])

    @staticmethod
    def chunk_markdown(
        text: str,
        book_meta: dict,
        toc: list,
        context_window: int = 4096,
    ) -> list[dict]:
        """Split Markdown body into semantic chunks with metadata."""
        max_chunk_tokens = max(256, context_window * 3 // 4 - _PROMPT_OVERHEAD)
        min_chunk_tokens = max(10, context_window // 64)
        lines = text.splitlines()
        max_pages: Optional[int] = book_meta.get("pages")
        tags: list = book_meta.get("tags", [])

        chapter_toc, section_toc = MarkdownChunker._split_toc(toc)
        section_ranges = MarkdownChunker._build_toc_ranges(section_toc)
        chapter_ranges = MarkdownChunker._build_toc_ranges(chapter_toc)

        # Pass 1 — classify every line
        classified: list[dict] = []
        last_valid_page: int = -1
        current_page: Optional[int] = None

        for raw_line in lines:
            # Check for page marker comment first
            pm = MarkdownChunker.PAGE_MARKER_RE.match(raw_line.strip())
            if pm:
                pnum = int(pm.group(1))
                current_page = pnum
                last_valid_page = pnum
                classified.append({"kind": "page_marker", "page": pnum, "raw": raw_line})
                continue

            is_footer, page_num = MarkdownChunker._is_footer_line(raw_line, max_pages)
            if is_footer:
                if page_num is not None and page_num > last_valid_page:
                    last_valid_page = page_num
                    current_page = page_num
                classified.append({"kind": "footer", "page": page_num, "raw": raw_line})
                continue

            heading = MarkdownChunker._classify_line(raw_line)
            if heading:
                kind_h, level, title = heading
                if kind_h == "drop_cap":
                    classified.append({"kind": "body", "raw": raw_line, "page": current_page})
                    continue
                classified.append({
                    "kind": "heading",
                    "heading_kind": kind_h,
                    "level": level,
                    "title": title,
                    "raw": raw_line,
                    "page": current_page,
                })
                continue

            classified.append({"kind": "body", "raw": raw_line, "page": current_page})

        # Pass 2 — walk classified lines and emit chunks
        hierarchy: dict[int, dict] = {}
        chunks: list[dict] = []
        cur_title: Optional[str] = None
        cur_level: Optional[int] = None
        cur_page: Optional[int] = None
        running_page: Optional[int] = None
        cur_body_lines: list[str] = []

        def _parent_index() -> Optional[int]:
            if cur_level is None:
                return None
            for lvl in sorted(hierarchy.keys(), reverse=True):
                if lvl < cur_level:
                    return hierarchy[lvl].get("chunk_index")
            return None

        def _hierarchy_titles() -> list[str]:
            return [
                hierarchy[lvl]["title"]
                for lvl in sorted(hierarchy.keys())
                if cur_level is None or lvl < cur_level
            ]

        def _flush() -> None:
            nonlocal cur_title, cur_level, cur_page, cur_body_lines
            body = "\n".join(cur_body_lines).strip()
            cur_body_lines = []
            if not body and cur_title is None:
                return
            if count_tokens(body) < 10 and body:
                return

            title_match_sec = MarkdownChunker._toc_entry_by_title(section_ranges, cur_title)
            title_match_chap = MarkdownChunker._toc_entry_by_title(chapter_ranges, cur_title)

            if title_match_sec is not None:
                toc_title = title_match_sec.get("title")
                chapter = MarkdownChunker._chapter_for_page(
                    chapter_ranges, title_match_sec.get("page", cur_page)
                )
            elif title_match_chap is not None:
                toc_title = None
                chapter = title_match_chap.get("title")
            else:
                sec_entry = MarkdownChunker._toc_entry_for_page(section_ranges, cur_page)
                toc_title = sec_entry.get("title") if sec_entry else None
                chapter = MarkdownChunker._chapter_for_page(chapter_ranges, cur_page)

            chunk_index = len(chunks)

            book_struct = {
                "title":               book_meta.get("body_title"),
                "title_from_pdf":      book_meta.get("pdf_title"),
                "author_from_pdf":     book_meta.get("pdf_author"),
                "page_count_from_pdf": book_meta.get("pages"),
                "tags":                tags,
            }

            metadata = {
                "section_title":        cur_title,
                "section_title_in_toc": toc_title,
                "chapter_label_in_toc": chapter,
                "page_number":          cur_page,
                "token_count":          count_tokens(body),
                "parent_index":         _parent_index(),
                "section_hierarchy":    _hierarchy_titles(),
                "book":                 book_struct,
                "chunking_started_at":  None,
                "chunking_completed_at": None,
            }

            if cur_level is not None:
                hierarchy[cur_level] = {"title": cur_title, "chunk_index": chunk_index}
                for lvl in list(hierarchy.keys()):
                    if lvl > cur_level:
                        del hierarchy[lvl]

            chunks.append({"text": body, "metadata": metadata})
            cur_title = None
            cur_level = None

        for item in classified:
            if item["kind"] in ("footer", "page_marker"):
                if item.get("page") is not None:
                    running_page = item["page"]
                continue
            if item["kind"] == "heading":
                _flush()
                cur_title = item["title"]
                cur_level = item["level"]
                cur_page = running_page
                continue
            cur_body_lines.append(item["raw"])

        _flush()
        chunks = MarkdownChunker._merge_small_chunks(chunks, min_chunk_tokens, max_chunk_tokens)
        chunks = MarkdownChunker._split_large_chunks(chunks, max_chunk_tokens)
        return chunks

    @staticmethod
    def _merge_small_chunks(
        chunks: list[dict], min_tokens: int, max_tokens: int
    ) -> list[dict]:
        """Merge chunks below min_tokens into the previous chunk when it fits."""
        if not chunks:
            return chunks
        merged = [chunks[0]]
        for chunk in chunks[1:]:
            prev = merged[-1]
            prev_tokens = prev["metadata"]["token_count"]
            cur_tokens = chunk["metadata"]["token_count"]
            if cur_tokens < min_tokens and prev_tokens + cur_tokens <= max_tokens:
                prev["text"] = prev["text"] + "\n\n" + chunk["text"]
                prev["metadata"]["token_count"] = prev_tokens + cur_tokens
            else:
                merged.append(chunk)
        return merged

    @staticmethod
    def _split_large_chunks(chunks: list[dict], max_tokens: int) -> list[dict]:
        """Split chunks exceeding max_tokens at paragraph boundaries."""
        result = []
        for chunk in chunks:
            if chunk["metadata"]["token_count"] <= max_tokens:
                result.append(chunk)
                continue
            paragraphs = [p for p in chunk["text"].split("\n\n") if p.strip()]
            current_parts: list[str] = []
            current_tokens = 0
            sub_index = 0
            for para in paragraphs:
                para_tokens = count_tokens(para)
                if current_parts and current_tokens + para_tokens > max_tokens:
                    sub_meta = dict(chunk["metadata"])
                    sub_meta["token_count"] = current_tokens
                    sub_meta["sub_index"] = sub_index
                    result.append({"text": "\n\n".join(current_parts), "metadata": sub_meta})
                    sub_index += 1
                    current_parts = [para]
                    current_tokens = para_tokens
                else:
                    current_parts.append(para)
                    current_tokens += para_tokens
            if current_parts:
                sub_meta = dict(chunk["metadata"])
                sub_meta["token_count"] = current_tokens
                if sub_index > 0:
                    sub_meta["sub_index"] = sub_index
                result.append({"text": "\n\n".join(current_parts), "metadata": sub_meta})
        return result

    # ── Private helpers ────────────────────────────────────────────────────

    @staticmethod
    def _is_table_line(line: str) -> bool:
        return bool(MarkdownChunker.TABLE_RE.match(line.strip()))

    @staticmethod
    def _is_prose_line(text: str) -> bool:
        t = text.strip()
        if MarkdownChunker.SENTENCE_END_RE.search(t):
            return True
        if '"' in t or "“" in t or "”" in t:
            return True
        if "'" in t or "’" in t:
            return True
        if "," in t or ";" in t:
            return True
        return False

    @staticmethod
    def _is_prose_line_strict(text: str) -> bool:
        t = text.strip()
        if MarkdownChunker.SENTENCE_END_RE.search(t):
            return True
        if '"' in t or "“" in t or "”" in t:
            return True
        if "'" in t or "’" in t:
            return True
        if len(t) > MarkdownChunker.MAX_TITLE_LEN:
            return True
        if len(t.split()) > 6:
            return True
        return False

    @staticmethod
    def _classify_line(line: str):
        stripped = line.strip()
        if not stripped:
            return None
        if MarkdownChunker._is_table_line(stripped):
            return None
        if MarkdownChunker.CODE_FENCE_RE.match(stripped):
            return None

        m = MarkdownChunker.HEADER_RE.match(stripped)
        if m:
            title_raw = m.group(2).strip()
            title_clean = re.sub(r"\*\*(.+?)\*\*", r"\1", title_raw)
            title_clean = re.sub(r"_(.+?)_", r"\1", title_clean).strip()
            if MarkdownChunker.DROP_CAP_RE.match(title_clean):
                return ("drop_cap", 0, title_clean)
            if not MarkdownChunker._is_prose_line(title_clean):
                return ("atx", len(m.group(1)), title_clean)
            return None

        if MarkdownChunker.BOLD_LEADIN_RE.match(stripped):
            return None

        m = MarkdownChunker.BOLD_TITLE_RE.match(stripped)
        if m:
            title = m.group(1).strip()
            if MarkdownChunker.DROP_CAP_RE.match(title):
                return None
            if MarkdownChunker._is_prose_line(title):
                return None
            return ("bold", 7, title)

        m = MarkdownChunker.ITALIC_TITLE_RE.match(stripped)
        if m:
            title = m.group(1).strip()
            if MarkdownChunker.DROP_CAP_RE.match(title):
                return None
            if MarkdownChunker._is_prose_line_strict(title):
                return None
            return ("italic", 8, title)

        return None

    @staticmethod
    def _is_footer_line(
        line: str, max_pages: Optional[int]
    ) -> tuple[bool, Optional[int]]:
        stripped = line.strip()
        m = MarkdownChunker.FOOTER_PAGE_RE.match(stripped)
        if m:
            page = int(m.group(1))
            if max_pages is None or page <= max_pages:
                return True, page

        m = MarkdownChunker.BARE_NUMBER_RE.match(stripped)
        if m:
            page = int(m.group(1))
            if max_pages is None or page <= max_pages:
                return True, page

        if MarkdownChunker.CHAPTER_FOOTER_RE.match(stripped):
            return True, None

        return False, None

    @staticmethod
    def _split_toc(toc: list) -> tuple[list, list]:
        chapters, sections = [], []
        for entry in toc or []:
            if MarkdownChunker.CHAPTER_RE.search(entry.get("title", "")):
                chapters.append(entry)
            else:
                sections.append(entry)
        return chapters, sections

    @staticmethod
    def _build_toc_ranges(entries: list) -> list:
        if not entries:
            return []
        sorted_entries = sorted(entries, key=lambda e: e.get("page", 0))
        ranges = []
        for i, entry in enumerate(sorted_entries):
            start = entry.get("page", 0)
            end = None
            for j in range(i + 1, len(sorted_entries)):
                next_page = sorted_entries[j].get("page", start)
                if next_page > start:
                    end = next_page - 1
                    break
            ranges.append({**entry, "end_page": end})
        return ranges

    @staticmethod
    def _toc_entry_for_page(
        toc_ranges: list, page: Optional[int]
    ) -> Optional[dict]:
        if page is None or not toc_ranges:
            return None
        best = None
        best_idx = None
        for idx, entry in enumerate(toc_ranges):
            start = entry.get("page", 0)
            end = entry.get("end_page")
            if start <= page and (end is None or page <= end):
                if best is None or start > best.get("page", 0):
                    best = entry
                    best_idx = idx
                elif start == best.get("page", 0) and idx < best_idx:
                    best = entry
                    best_idx = idx
        return best

    @staticmethod
    def _chapter_for_page(
        chapter_ranges: list, page: Optional[int]
    ) -> Optional[str]:
        entry = MarkdownChunker._toc_entry_for_page(chapter_ranges, page)
        return entry.get("title") if entry else None

    @staticmethod
    def _normalize(s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^\w\s]", "", s)
        return re.sub(r"\s+", " ", s)

    @staticmethod
    def _toc_entry_by_title(
        toc_ranges: list, title: Optional[str]
    ) -> Optional[dict]:
        if not title or not toc_ranges:
            return None
        needle = MarkdownChunker._normalize(title)
        for entry in toc_ranges:
            if MarkdownChunker._normalize(entry.get("title", "")) == needle:
                return entry
        return None
