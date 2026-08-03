# Input and output formats

## JSON interchange

JSON is useful for visual novels, game scripts, and custom extractors. The smallest
valid file is:

```json
{
  "title": "Book",
  "source_lang": "ja",
  "chapters": [
    {
      "title": "Chapter 0",
      "segments": [
        {"source": "原文一。", "meta": {"speaker": "Noel"}},
        {"source": "原文二。"}
      ]
    }
  ]
}
```

A segment may also be a plain string. Optional structured fields are `kind`,
`anchor`, `resource_href`, `cont`, and `meta`. Array order defines chapter and
segment indexes; Wenyi Direct derives stable IDs from chapter index, segment index,
and a source digest. Do not reorder or edit the source while resuming an existing
state. JSON export preserves `source`, accepted `target`, structural fields, and
metadata.

For GAL scripts, keep one stable script line per segment and put speaker, voice ID,
physical line number, and control information in `meta`. The model may read those
fields during translation but only the returned target string is written.

## Document formats

- EPUB preserves original resources and layout when assembled back to EPUB.
- FB2, TXT, Markdown, HTML, and PDF are parsed into chapters/segments and may be
  assembled as EPUB, TXT, Markdown, or HTML.
- PDF ingestion uses the inherited MinerU conversion path and requires its normal
  service configuration.

EPUB assembly runs CRC, OPF manifest/spine resolution, and `7z t` when `7z` is
installed.
