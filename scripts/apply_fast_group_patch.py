from pathlib import Path

path = Path("wenyi_direct/pipeline/tasks.py")
text = path.read_text(encoding="utf-8")

old = """    def run_fast(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        \"\"\"Overlap chapter N Chinese review with chapter N+1 factual work.\"\"\"
        store = self.prepare(source_path)
        selected = self._selected_chapters(store, chapters, pending_only=True)
        if not selected:
            return store
        concurrent_store = _ConcurrentStore(store)
        with store.lock():
            self._allow_provisional_factual_context = True
            try:
                self._run_upstream_full(concurrent_store, selected[0])
                previous = selected[0]
                for current in selected[1:]:
                    concurrent_store.log_event(
                        \"staggered_pair_started\",
                        chinese_chapter=previous,
                        factual_chapter=current,
                    )
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        downstream = executor.submit(
                            self._run_downstream, concurrent_store, previous
                        )
                        upstream = executor.submit(
                            self._run_upstream_audit, concurrent_store, current
                        )
                        downstream.result()
                        upstream.result()
                    self._run_upstream_repair(concurrent_store, current)
                    concurrent_store.log_event(
                        \"staggered_pair_completed\",
                        chinese_chapter=previous,
                        factual_chapter=current,
                    )
                    self._save_usage(concurrent_store)
                    previous = current
                self._run_downstream(concurrent_store, previous)
                self._save_usage(concurrent_store)
            finally:
                self._allow_provisional_factual_context = False
        return store

"""
new = """    def run_fast(
        self,
        source_path: str | Path,
        *,
        chapters: set[int] | None = None,
    ) -> RunStore:
        \"\"\"Overlap only genuinely adjacent pending chapters.\"\"\"
        store = self.prepare(source_path)
        selected = self._selected_chapters(store, chapters, pending_only=True)
        if not selected:
            return store
        concurrent_store = _ConcurrentStore(store)
        with store.lock():
            self._allow_provisional_factual_context = True
            try:
                for run in self._contiguous_runs(selected):
                    self._run_fast_sequence(concurrent_store, run)
                self._save_usage(concurrent_store)
            finally:
                self._allow_provisional_factual_context = False
        return store

    @staticmethod
    def _contiguous_runs(indexes: list[int]) -> list[list[int]]:
        if not indexes:
            return []
        runs = [[indexes[0]]]
        for index in indexes[1:]:
            if index == runs[-1][-1] + 1:
                runs[-1].append(index)
            else:
                runs.append([index])
        return runs

    def _run_fast_sequence(self, store: RunStore, selected: list[int]) -> None:
        self._run_upstream_full(store, selected[0])
        previous = selected[0]
        for current in selected[1:]:
            store.log_event(
                \"staggered_pair_started\",
                chinese_chapter=previous,
                factual_chapter=current,
            )
            with ThreadPoolExecutor(max_workers=2) as executor:
                downstream = executor.submit(self._run_downstream, store, previous)
                upstream = executor.submit(self._run_upstream_audit, store, current)
                downstream.result()
                upstream.result()
            self._run_upstream_repair(store, current)
            store.log_event(
                \"staggered_pair_completed\",
                chinese_chapter=previous,
                factual_chapter=current,
            )
            self._save_usage(store)
            previous = current
        self._run_downstream(store, previous)

"""
if old in text:
    text = text.replace(old, new, 1)

path.write_text(text, encoding="utf-8")
