# Satisfaction chapter 0 translation experiments

These experiments grew from one concrete failure:

```text
「光った」 -> 「闪光了」
```

The goal was not to insert `亮了` as a fixed answer. It was to determine why a
grammatically interpretable but unnatural Chinese utterance survived translation and
review, then test a simpler chapter-first architecture.

## Common setup

- Source language: Japanese; target: Simplified Chinese.
- Chapter size: 89 stable segments.
- Full source chapter and complete generated translations are not redistributed here.
- Migrated terminology did not contain a rule for `光った`.
- Direct-translation runs disabled factual review, Chinese review, repair, and
  validation unless an experiment explicitly says otherwise.
- Chinese Reader calls received Chinese target text and stable IDs only.

## Experiment timeline

| ID | Experiment | Main result |
| --- | --- | --- |
| E01 | Existing translation diagnosis | `闪光了` is not an established Chinese utterance in this scene; `亮了` is the natural short observation. |
| E02 | Baseline source-free Gemini 3.1 Pro audit, two samples | Found 2 and 5 issues; both missed s6. |
| E03 | Translation-aware source-free audit | Gemini 3.1 Pro found 8 issues including s6; DeepSeek V4 Pro found 2 and missed s6. |
| E04 | Sol source-aware validation of s6 | Approved a one-segment repair, produced `亮了`, and passed fidelity validation. |
| E05 | Fixed repair expansion versus precise write scope | A two-segment automatic write halo changed seven segments; zero expansion kept model-approved write ranges precise. |
| E06 | Baseline three-model Chinese audit | Gemini 3.6 Flash found 5, DeepSeek V4 Flash 16, and Codex 5.6 Sol 19; all missed s6. |
| E07 | Stronger release-acceptance audit motive | Gemini 3.1 Pro found 4 issues but still missed s6, confirming that prompt framing is not a recall guarantee. |
| E08 | Stable ordinary-noun terminology discovery | The factual reviewer proposed `焼き鳥屋 -> 烤鸡串店` with no dedicated terminology call. |
| E09 | Gemini 3.1 Pro direct translation before general guardrails | One full-chapter call still produced `闪光了`; many other old structural errors improved. |
| E10 | General Japanese-to-Chinese guardrails: Gemini 3.1 Pro versus DeepSeek V4 Pro | Gemini produced `亮了` and handled several related structural cases better; DeepSeek retained `闪光了` and failed one hard-term promotion check. |

## Findings

1. A strong whole-chapter first translation is a much better baseline, but it does
   not eliminate literal collocations by itself.
2. Prompt framing changes recall probability, not certainty. The same model can miss
   a problem in one sample and find it in another.
3. General Japanese-to-Chinese guidance was more useful than a case-specific answer:
   recover omitted roles and discourse function, restructure nominal/postposed
   syntax, avoid dictionary-sense assembly, retain deliberate rhythm without invalid
   Chinese, and avoid semantic amplification.
4. A source-free Chinese Reader is useful for real reading problems, but every finding
   still needs source-aware validation. It can reject false positives such as treating
   an intentional black-bird/wing metaphor as helicopter ignorance.
5. Read context and write scope must remain separate. Neighboring source can be broad;
   writable segment IDs should come from the validated causal range rather than a
   fixed halo.
6. Terminology discovery should include ordinary noun phrases that repeatedly carry a
   stable translation function, not just proper nouns. Frequency alone remains
   insufficient.
7. Pronoun guidance stays term-local. These experiments did not justify adding active
   character or cross-window coreference tracking.

## Data files

- [Reader audit samples](results/reader-audits.json)
- [Direct translation comparison](results/direct-translations.json)
- [Pipeline, repair, and terminology results](results/pipeline-and-terminology.json)
- [Reviewed terminology snapshot](terminology.yaml)

The general Japanese guardrails entered the project in commit `b3f6fa5`. The release-
acceptance reader motive and broader terminology discovery entered in `218bae3`.
