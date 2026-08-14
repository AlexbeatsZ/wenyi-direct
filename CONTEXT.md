# Kamyi domain context

## Core language

- **Formal text**: the last accepted chapter translation exposed to assembly.
- **Shadow text**: resumable candidate translation being reviewed; never assembled.
- **Read scope**: source context visible to a model call.
- **Write scope**: stable segment IDs that call is allowed to change.
- **Factual audit**: source-and-target comparison for meaning, reference, omission,
  addition, terminology, speaker, and temporal consistency.
- **Chinese Reader Audit**: source-free inspection of what a Chinese reader actually
  sees: coherence, naturalness, voice, subject continuity, and dialogue pragmatics.
- **Source validation**: verifies either a Chinese-reader finding or a proposed
  repair against neighboring source text.
- **Repair region**: a contiguous, context-expanded group of segments that may be
  wider than the reported symptom.
- **Terminology group**: a source/target fragment shared by explicitly linked term
  rules. It coordinates rendering and says nothing about entity identity.
- **Term mode**: `hard` blocks promotion when violated; `preferred` is advice only.
- **Term status**: `active` participates; `candidate` and `rejected` stay out of
  prompts. Status is lifecycle, not constraint strength.
- **Past knowledge**: a bounded raw tail from already completed chapters. It is
  evidence, not an authoritative generated synopsis.

## Pipeline

```text
strong direct translation -> factual audit/repair -> Chinese-only audit
-> source validation/repair -> changed-region fidelity validation -> atomic promotion
```

The pipeline uses fresh calls for task and information separation, not as a claim
that multiple calls from one model are independent human reviewers.

Parallel execution is a cross-chapter schedule, not a different quality path: chapter
N downstream Chinese review may overlap chapter N+1 translation and factual audit.
The next chapter may read N's completed factual snapshot as provisional past context;
future terminology remains inactive until N reaches Formal.
