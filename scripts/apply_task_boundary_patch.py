from pathlib import Path

path = Path("wenyi_direct/pipeline/tasks.py")
text = path.read_text(encoding="utf-8")

old = """            selected = self._selected_chapters(store, chapters)
            for chapter_index in selected:
"""
new = """            selected = self._selected_chapters(store, chapters)
            if chapters is None:
                selected = [
                    index
                    for index in selected
                    if self._task_is_ready(store, index, task)
                ]
            for chapter_index in selected:
"""
if old in text:
    text = text.replace(old, new, 1)

old = """                self._run_upstream(concurrent_store, selected[0])
                previous = selected[0]
                for current in selected[1:]:
"""
new = """                self._run_upstream_full(concurrent_store, selected[0])
                previous = selected[0]
                for current in selected[1:]:
"""
if old in text:
    text = text.replace(old, new, 1)

old = """                        upstream = executor.submit(
                            self._run_upstream, concurrent_store, current
                        )
                        downstream.result()
                        upstream.result()
                    concurrent_store.log_event(
"""
new = """                        upstream = executor.submit(
                            self._run_upstream_audit, concurrent_store, current
                        )
                        downstream.result()
                        upstream.result()
                    self._run_upstream_repair(concurrent_store, current)
                    concurrent_store.log_event(
"""
if old in text:
    text = text.replace(old, new, 1)

marker = """    @staticmethod
    def _selected_chapters(
"""
addition = """    def _task_is_ready(self, store: RunStore, chapter_index: int, task: str) -> bool:
        manifest = store.load_manifest()
        status = {
            int(item["index"]): item.get("status") for item in manifest["chapters"]
        }
        if status.get(chapter_index) == STATUS_DONE:
            return False
        shadow = store.load_shadow(chapter_index)
        if not isinstance(shadow, dict):
            return task == "translate"
        return self._next_task(shadow) == task

"""
if addition not in text:
    text = text.replace(marker, addition + marker, 1)

marker = """    def _run_task_locked(
        self, store: RunStore, chapter_index: int, task: str
    ) -> None:
        chapter = store.load_chapter(chapter_index)
        shadow = self._ensure_shadow(store, chapter)
"""
replacement = """    def _run_task_locked(
        self, store: RunStore, chapter_index: int, task: str
    ) -> None:
        chapter = store.load_chapter(chapter_index)
        shadow = self._ensure_shadow(store, chapter)
        self._validate_task(chapter_index, shadow, task)
"""
if marker in text:
    text = text.replace(marker, replacement, 1)

marker = """    def _run_task_locked(
"""
addition = """    def _validate_task(
        self, chapter_index: int, shadow: dict[str, Any], task: str
    ) -> None:
        expected = {
            "translate": "translate",
            "factual-audit": "factual_audit",
            "factual-repair": "factual_audit",
            "chinese-audit": "chinese_audit",
            "chinese-repair": "chinese_audit",
            "promote": "promote",
        }[task]
        self._require_phase(chapter_index, shadow, expected, task)
        if task == "factual-repair":
            state = shadow.get("factual_state", {})
            if not isinstance(state, dict) or state.get("audit_complete") is not True:
                raise StageTaskError(
                    f"chapter {chapter_index} requires factual-audit before factual-repair"
                )
        if task == "chinese-repair":
            state = shadow.get("chinese_state", {})
            if not isinstance(state, dict) or state.get("audit_complete") is not True:
                raise StageTaskError(
                    f"chapter {chapter_index} requires chinese-audit before chinese-repair"
                )

"""
if addition not in text:
    text = text.replace(marker, addition + marker, 1)

if "    def _run_upstream(" in text:
    old_start = text.index("    def _run_upstream(")
    old_end = text.index("    def _run_downstream(", old_start)
    new_methods = """    def _run_upstream_full(self, store: RunStore, chapter_index: int) -> None:
        self._run_upstream_audit(store, chapter_index)
        self._run_upstream_repair(store, chapter_index)

    def _run_upstream_audit(self, store: RunStore, chapter_index: int) -> None:
        while True:
            chapter = store.load_chapter(chapter_index)
            shadow = self._ensure_shadow(store, chapter)
            phase = str(shadow.get("phase"))
            if phase == "translate":
                self._run_task_locked(store, chapter_index, "translate")
                continue
            if phase == "factual_audit":
                state = shadow.get("factual_state", {})
                if not isinstance(state, dict) or state.get("audit_complete") is not True:
                    self._run_task_locked(store, chapter_index, "factual-audit")
                return
            return

    def _run_upstream_repair(self, store: RunStore, chapter_index: int) -> None:
        chapter = store.load_chapter(chapter_index)
        shadow = self._ensure_shadow(store, chapter)
        if str(shadow.get("phase")) != "factual_audit":
            return
        state = shadow.get("factual_state", {})
        if not isinstance(state, dict) or state.get("audit_complete") is not True:
            raise StageTaskError(
                f"chapter {chapter_index} factual audit did not complete before repair"
            )
        self._run_task_locked(store, chapter_index, "factual-repair")

"""
    text = text[:old_start] + new_methods + text[old_end:]

old = """        approved = self._process_term_revisions(
            store, chapter, shadow, state, audit_batches
        )
        targets = self._targets(shadow)
        if approved:
            issues = [
                issue
                for issue in issues
                if not self._issue_covered_by_revision(chapter, issue, approved)
            ]
        issues.extend(
            self._terminology_issues(
                chapter,
                targets,
                tuple(segment.index for segment in chapter.text_segments),
            )
        )
        state = shadow.setdefault("factual_state", state)
        if not isinstance(state.get("repair_regions"), list):
            state["repair_regions"] = [
                {
                    "id": f"factual-r{index}",
                    "start": region.start,
                    "end": region.end,
                    "issues": list(region.issues),
                }
                for index, region in enumerate(
                    self.repair_planner.plan(issues, len(chapter.segments))
                )
            ]
        state["audit_complete"] = True
"""
new = """        state = shadow.setdefault("factual_state", state)
        state["issue_count"] = len(issues)
        state["term_revision_count"] = sum(
            len(batch.get("term_revisions", []))
            for batch in audit_batches.values()
            if isinstance(batch, dict)
        )
        state.pop("repair_regions", None)
        state["audit_complete"] = True
"""
if old in text:
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
