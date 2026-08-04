from pathlib import Path

path = Path("wenyi_direct/pipeline/tasks.py")
text = path.read_text(encoding="utf-8")

lock_line = "        self._terminology_lock = threading.RLock()\n"
while lock_line + lock_line in text:
    text = text.replace(lock_line + lock_line, lock_line)
if lock_line not in text:
    old = """        super().__init__(*args, **kwargs)
        self._allow_provisional_factual_context = False
"""
    text = text.replace(old, old + lock_line, 1)

old = """            added_terms = self.terminology.add_discoveries(
                chapter.index,
                discoveries,
                source,
                "\\n".join(targets[index] for index in window.read_indexes),
            )
"""
new = """            with self._terminology_lock:
                added_terms = self.terminology.add_discoveries(
                    chapter.index,
                    discoveries,
                    source,
                    "\\n".join(targets[index] for index in window.read_indexes),
                )
"""
if old in text:
    text = text.replace(old, new, 1)

old = """    def _knowledge_for(
        self, store: RunStore, chapter: Chapter, read_source: str
    ) -> dict[str, Any]:
        knowledge = super()._knowledge_for(store, chapter, read_source)
"""
new = """    def _promote(
        self, store: RunStore, chapter: Chapter, shadow: dict[str, Any]
    ) -> None:
        with self._terminology_lock:
            super()._promote(store, chapter, shadow)

    def _knowledge_for(
        self, store: RunStore, chapter: Chapter, read_source: str
    ) -> dict[str, Any]:
        with self._terminology_lock:
            knowledge = super()._knowledge_for(store, chapter, read_source)
"""
if old in text:
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
