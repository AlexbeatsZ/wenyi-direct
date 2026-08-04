from pathlib import Path

readme = Path("README.md")
text = readme.read_text(encoding="utf-8")
old = """This mode uses two model lanes with a one-chapter offset:

```text
chapter N:   Chinese audit -> Chinese repair -> promote
chapter N+1: translate -> factual audit -> factual repair
```

The next chapter may use the previous chapter's completed **factual snapshot** as
provisional past context while the previous chapter is undergoing Chinese-only review.
"""
new = """This mode uses two model lanes with a one-chapter offset:

```text
concurrently:
  chapter N:   Chinese audit -> Chinese repair -> promote
  chapter N+1: translate -> factual audit
then:
  chapter N+1: factual repair
```

Factual repair is deliberately outside the overlap window because terminology
migration may rewrite earlier translated chapters. The next chapter may use the
previous chapter's completed **factual snapshot** as provisional past context while
the previous chapter is undergoing Chinese-only review.
"""
if old in text:
    text = text.replace(old, new, 1)
readme.write_text(text, encoding="utf-8")

architecture = Path("docs/task-execution.md")
text = architecture.read_text(encoding="utf-8")
old = """```text
upstream lane:   translate -> factual-audit -> factual-repair
 downstream lane:             chinese-audit -> chinese-repair -> promote
```

The scheduler first completes upstream work for the first selected chapter. It then
runs downstream work for chapter N concurrently with upstream work for chapter N+1.
After the last upstream chapter completes, the scheduler drains its downstream work.
"""
new = """```text
warmup:           chapter 0 translate -> factual-audit -> factual-repair
overlap:          chapter N chinese-audit -> chinese-repair -> promote
                  chapter N+1 translate -> factual-audit
after each pair:  chapter N+1 factual-repair
```

The scheduler first completes all upstream work for the first selected chapter. It
then runs downstream work for chapter N concurrently with translation and factual
audit for chapter N+1. Only after both lanes complete does it execute chapter N+1
factual repair. This prevents a terminology migration from rewriting chapter N while
that chapter is still being Chinese-reviewed. After the final factual repair, the
scheduler drains the last chapter's downstream work.
"""
if old in text:
    text = text.replace(old, new, 1)
architecture.write_text(text, encoding="utf-8")
