# book-reading

Use only the tools provided by this skill to inspect the current book. Every tool is scoped to that book.

- `get_book_outline` returns logical reader chapters. Use those chapter indices with paragraph tools.
- `search_book_text` returns excerpts and paragraph references. Read a hit before relying on its surrounding argument.
- `read_book_paragraph` returns the requested paragraph plus neighboring excerpts. For long paragraphs, continue with `offset` and `next_offset`; each returned source segment is marked `original_text`.
- `read_existing_note` returns prior interpretations as `existing_note`, never as source evidence.
- `read_layer_note` returns the book layer or one chapter layer on its own, untruncated. Same rule: it is an
  upper layer's interpretation, not source evidence.
- Before using a quotation, call `verify_source_quote`. A quote is supported only when it is found verbatim in a source passage read during this task.
  If you trim a quote with an ellipsis (“A……B”), it is checked in segments and passes when every segment is found.
- `search_book_text` falls back to sub-terms when the exact term is not found (a compound such as 「自在自为」
  may be written split as 「自在」「自为」). Such hits come back with `expanded: true` — they are not verbatim
  occurrences of the whole term, so do not treat them as a quotation of it.

Do not search the filesystem, another book, or the public internet. Distinguish source text from notes, and say when the book does not establish a claim.
