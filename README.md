# QUIZ MAP — Lecture Quiz Maker

LNG302 Natural Language Processing, term project. Chinami Tani.
[日本語版 README](README.ja.md)

**QUIZ MAP turns a semester of lecture slides into a concept map. Lasso part of the map, and it writes a quiz about how the concepts you circled relate to each other.**

Existing quiz generators ask about one term at a time. What I actually get wrong before an exam is the link between ideas ("how does Kant's *person* relate to *dignity*?"). QUIZ MAP makes those links the unit of study: an LLM extracts typed relations from the slides, the map lets me choose a region of the course, and the quiz asks about the relations inside it.

![map](docs/screenshot-map.png)

## What it does

Everything happens inside the app.

1. **Upload** the lecture PDFs. Lecture numbers are read from file names (`第3回`, `week3`, `Lecture 03` …) and can be corrected in a table.
2. **Vocabulary derivation** (LLM). The model reads the slides and proposes the *kinds of concepts* and the *kinds of relations* that this particular course uses, each relation with a definition and a *question style*.
3. **Extraction** (LLM). Using that fixed vocabulary, the model extracts concepts, then relations between them, each with the lecture and page it came from.
4. **Map.** Concepts are dots, relations are lines. Two layouts: by links (force-directed) and by lecture.
5. **Select a region** with the lasso or marquee (Shift adds, ⌘ subtracts, as in Adobe tools), by clicking dots, or with the lecture buttons.
6. **Quiz** (LLM). Questions about relations, examples and definitions in the selected region. Distractors come from neighbouring concepts on the map, then the model checks each distractor against the slides and replaces any it cannot justify as wrong.
7. **Answer and grade.** Multiple choice is graded automatically; short answers are graded by the LLM (1 / 0.5 / 0) with a one-line note on what is missing. Each question shows its explanation and the source slide.
8. **Asked concepts change colour** on the map, so the next quiz can target what you have not practised.

The interface is available in Japanese and English.

## How to run

Requirements: Python 3.12, poppler (`pdftotext`, `pdftoppm`), a Google AI Studio API key (free tier).

```
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo 'GEMINI_API_KEY=...' > .env
.venv/bin/streamlit run app.py
```

Open http://localhost:8501, choose “+ New course”, upload PDFs, and press “Start import”.

Tests (no API calls): `.venv/bin/pip install -r requirements-dev.txt && .venv/bin/playwright install chromium`, then `.venv/bin/python -m pytest tests -q`.

End-to-end test with the real API: `RUN_API_TESTS=1 .venv/bin/python -m pytest tests/e2e/test_full_flow_api.py -q`. A browser uploads a PDF, imports it with RAG on, selects a lecture range on the map, generates 3 questions, answers, grades, and checks that the explanations, `results.json` and the asked-concept colors appear. On the final code (2026-10-24) it passed in 120 s with 14 API calls. Short-answer grading was not exercised in that run because no short-answer question came up; it was measured separately in the evaluation.

## Where the LLM is used

| Step | What the LLM decides | Why it needs an LLM |
|---|---|---|
| Vocabulary | Which concept kinds and relation labels this course needs, and how to ask about each relation | The right labels differ by field (see Evaluation); fixed labels break when the subject changes |
| Extraction | Concepts, typed relations, and their source pages | Semantic structure, not keywords |
| Question writing | Question text and four choices, following each relation's `question_style` | — |
| Distractor self-check | Whether each wrong choice is really wrong according to the slides | Prevents “distractor that is actually correct” |
| Short-answer grading | 1 / 0.5 / 0 with feedback (LLM-as-judge) | Free-text answers |
| Relation completion (RAG) | Whether two concepts that the slides leave unconnected are related, backed by a slide passage or a Wikipedia passage | Slides often skip steps between ideas |
| Answer verification (RAG) | Whether the answer follows from the slides, and whether another choice is also correct in general knowledge | A question can have one answer within the slides but two in reality |

Only the map drawing and the selection are plain code.

## Design decisions

**Derive the vocabulary instead of fixing it.** A first prototype used fixed relation labels written for an ethics course. Applied to other material it silently forced relations into wrong labels. The app now asks the model to propose the labels for each course and stores them in `vocab.json`. The vocabulary is then frozen and reused on every re-import, so results can be counted and compared. The share of relations that fall into “other” is the signal that the vocabulary does not fit the material.

**Two-stage extraction.** Concepts are extracted first, lecture by lecture, with the list of concepts already found passed in so names stay consistent across lectures. Relations are extracted second, and the model may only use the concept ids from stage one. Extracting both at once produced edges pointing at concepts that did not exist.

**Every concept and relation keeps its page.** Hints, explanations and the slide shown after grading all depend on it.

**Text normalisation before anything else.** PDF text extraction returned CJK radicals instead of ordinary kanji (人 → ⼈, 民 → ⺠). NFKC fixes the Kangxi block but not the CJK Radicals Supplement, so a manual table is applied as well, then line breaks inside words are removed. Without this, search and matching silently fail.

**Questions are not templated.** The code never says how to ask a question. Each relation label carries its own `question_style`, written by the model during vocabulary derivation, and the quiz prompt follows it. A different course gets different question styles automatically.

**Distractors from neighbours, then a self-check.** Neighbouring concepts on the map make plausible wrong answers. A second call asks for a one-line, slide-based reason why each distractor is wrong and replaces the ones that have none.

**Hints do not reveal answers.** The first version showed the concept's definition as a hint, which *is* the answer to a definition question. The hint now reveals the source slide, which keeps the retrieval step with the learner.

**The vocabulary also says which kinds may sit at each end of a relation.** “Proposes” runs from a person to an idea, for example. After extraction, code checks every relation against this and relabels the ones that do not fit as “Other”. In the evaluation this single check cut wrong relations from 9 to 2 out of 30.

**Cited books are not concepts.** Slides list their sources, and the model kept turning those titles into concepts. Telling it not to did not work. Instead, the vocabulary marks which concept kind holds cited works (`cited_work`), and code removes those concepts before relations are extracted. The removed titles are logged.

**Output follows the language of the material.** The app detects whether the slides are Japanese or English and asks the model to write the vocabulary, concepts, questions and explanations in that language. The interface language is a separate setting.

**Retrieval (RAG) checks relations and answers, but never adds words to the map.** The rules were set by the user: every concept on the map must appear in the uploaded material; outside sources may only be used to check relations between those concepts and to check that a question has one correct answer; and every answer must follow from the uploaded material. During import, all slide pages are embedded (`gemini-embedding-001`) into a per-course index. A completion step then looks at concepts left without relations and at concepts that share a slide but have no relation. For each pair it retrieves related slide pages and the Wikipedia introductions of both concepts, and asks the model whether a relation from the course vocabulary holds. A relation backed by a slide quote is added as a normal relation; one backed only by Wikipedia is added as an *external* relation, drawn dashed on the map with its source. External relations are never used as material for questions. After questions are generated, a verification step retrieves slide pages and Wikipedia introductions for the answer and every choice, removes questions whose answer cannot be traced to a slide, and replaces or removes choices that would also be correct. Every retrieval and decision is logged, so retrieval errors and generation errors can be told apart. The design is in `docs/rag-design.md`.

**Display density is not extraction depth.** The map can show fewer dots, but it never deletes extracted data. How many concepts are extracted at all is decided at extraction time and depends on the model and the number of calls (see Known limitations).

**Map component.** The first map used Plotly. Streamlit resets a Plotly chart's selection whenever the figure changes, so keeping a lasso selection, lecture-button selection and the current zoom alive required layers of browser-side workarounds that broke repeatedly. The map is now a small canvas component (Streamlit components v2) that keeps its view and selection in the browser and only reports the selection back to Python.

## Models and cost

**Development:** Claude Code (harness) with Claude Fable 5.1 and Claude Opus 5.5 (models). Development ran as a team of Claude Code sessions: one coordinator and five roles (UI reviewer, UI improver, output reviewer, prompt improver, feature developer), each in its own git worktree and terminal, coordinated through GitHub issues and pull requests. Reviewers never fix and fixers never judge their own fixes. See `CLAUDE.md` and `.claude/agents/`.

**Inside the app:** `gemini-3.1-flash-lite` for generation and `gemini-embedding-001` for the retrieval index, through the Google AI Studio free tier. The work is structured extraction, not hard reasoning, so the cheapest model that does the job was chosen, following the course principle. I listed the available models with the API before choosing: the cheapest free-tier model, `gemini-2.5-flash-lite`, is no longer offered to new users, so the next cheapest was used. The model name is a setting (`QUIZMAP_MODEL`) so other models can be compared. All calls use JSON-schema structured output, wait between calls, and back off on HTTP 429.

| Operation | API calls | Time |
|---|---|---|
| Import 17 lectures / 185 pages (ethics) | 35 | about 4 min |
| Import 4 lectures / 136 pages (NLP, English) | 9 | 88 s |
| Generate 5 questions | 2 | about 11 s |
| Grade (multiple choice only / with short answers) | 0 / 1 | — |

## Evaluation

<!-- EVAL:BEGIN（data/team/output-reviewer/summary-for-readme.md から差し込み） -->
All numbers are counts with denominators. "Before" = prompts at commit `fb02246` (vocab/graph generated 2026-10-19, `gemini-3.1-flash-lite`). The evaluation protocol is in `eval-protocol.md` (fixed random seed 20261024, so the samples are reproducible). Vocabulary and extraction "after" = the final code, run three times on the Ethics course (§1). Question quality, grading and map value were measured on the Before graph with the fixed question and grading code, so those changes come from the question and grading fixes only.

#### 1. Vocabulary quality (§12-1, §12-2)

The final code (main at the end of the review) was run three times on all 17 Ethics lectures: twice with `gemini-3.1-flash-lite` and once with `gemini-3.8-flash`. Nothing else changed between the runs. Each run derives its own vocabulary, so the labels and kinds differ between runs.

|  | Before (fb02246) | flash-lite run 1 | flash-lite run 2 | gemini-3.8-flash (stopped) |
|---|---|---|---|---|
| Concept kinds (excluding the dropped book kind) | 6 | 3 | 3 | 4 |
| Relation labels (+ Other) | 6 | 6 | 6 | 8 |
| Concepts / relations | 74 / 104 | 82 / 54 | 77 / 48 | 176 through lecture 13 (concepts only, before dropping books; no relations) |
| Relations labelled "Other" | 32 / 104 (30.8%) | 20 / 54 (37.0%) | 15 / 48 (31.2%) | – |
| Concepts with kind "Other" | 28 / 74 | 1 / 82 | 0 / 77 | – |
| Person concepts | 0 | 23 | 24 | – |
| Book concepts (dropped before relation extraction) | 0 | 0 (39 dropped) | 0 (21 dropped) | – |
| Relations usable for questions (not "Other") | 72 | 34 | 33 | – |
| Relations without a person | 104 | 18 | 8 | – |
| Relations whose end kinds break the vocabulary | – | 0 / 34 | 0 / 33 | – |
| Concepts with no relation | 3 / 74 | 24 / 82 | 19 / 77 | – |
| Concepts of the Before graph that are missing | – | 34 / 74 | 37 / 74 | – |

**Model choice.** The `gemini-3.8-flash` run stopped at lecture 13: the free tier allows 20 requests per day for this model, and one course needs about 35. Up to lecture 13 it had found 176 concepts; both flash-lite runs had about 112 at the same point (before dropping books). Its vocabulary had 5 kinds (4 without the book kind) and 8 labels plus Other, and it was the only run with both *subdivision* (part–whole) and *contrast* (opposition), and with a kind for schools of thought separate from persons. Per call it used about 16,000 tokens (thinking tokens about 1.8 times the output) against about 7,100 for flash-lite (no thinking). 14 successful calls took 3,000 seconds including waits for 429/503; flash-lite needs 230–290 seconds for all 35 calls. Coverage depends strongly on the model, but on the free tier only flash-lite can import a whole course in one day.

| | Ethics before | NLP before (4 lectures, English slides) |
|---|---|---|
| Relation labels | proposes, opposes, means–end, grounds, transformation, inclusion | depends on, composed of, purpose, inclusion, conversion, metric, opposes |
| Relations labelled "Other" | 32 / 104 (30.8%) | 3 / 26 (11.5%) |

The derived vocabulary differs by field; only *opposes* and *inclusion* appear in both. With the final code on English slides (NLP lecture 1), the vocabulary, concepts and questions came out in English (0 of 27 concepts and 0 of 5 questions contain Japanese).

#### 2. Extraction accuracy (§12-6, 30 random relations)

Each sample is 30 relations drawn from that run's graph with seed 20261024. The 3.8-flash run has no relations.

|  | Before (fb02246) | flash-lite run 1 | flash-lite run 2 | gemini-3.8-flash (stopped) |
|---|---|---|---|---|
| Correct | 6 / 30 | 17 / 30 | 25 / 30 | – |
| Ambiguous | 16 / 30 | 9 / 30 | 3 / 30 | – |
| Wrong | 8 / 30 | 4 / 30 | 2 / 30 | – |
| Concept pairs with conflicting labels | 10 | 0 | 0 | – |
| Relations whose cited page contains both concept names (person names matched by surname) | 67 / 104 | 39 / 54 | 39 / 48 | – |

**Run-to-run variation.** The two flash-lite runs used the same code and the same model, yet they differ a lot: "Other" 20 / 54 vs 15 / 48, relations without a person 18 vs 8, and correct / ambiguous / wrong 17 / 9 / 4 vs 25 / 3 / 2. In run 2, 30 of the 33 usable relations are *proposes* (person → concept), so relation questions are mostly "who proposed X". Differences of this size between versions should not be read as the effect of a fix. Each run derives the vocabulary again, so the labels, the kinds, and the kinds allowed at each end of a label all change. When the allowed kinds come out narrow, relations that fit a label semantically are relabelled "Other" (issue #74); this is one main source of the variation in the "Other" rate.

#### 3. Question quality (§12-3, 30 questions over 6 lecture ranges)

"After" uses the fixed question-generation code (`97635f3`) on the *same old* graph as "Before", so the change comes from the question-generation fixes only.

| | Before | After |
|---|---|---|
| Usable | 15 / 30 | 20 / 30 |
| Usable after a fix | 5 / 30 | 8 / 30 |
| Not usable | 10 / 30 | 2 / 30 |
| Correct answer wrong | 9 / 30 | 1 / 30 |
| A distractor is actually correct (MCQ) | 6 / 24 | 1 / 24 |
| Answer index points to the wrong choice | 4 / 24 | 0 / 24 |
| Relation asked in the reverse direction | 4 / 30 | 1 / 30 |
| Not usable, by aspect: relation / example / definition | 6/12 · 4/12 · 0/6 | 1/12 · 1/7 · 0/11 |
| Short-answer grading matches the expected score (6 questions × 5 answers, graded twice) | 25 / 30 (too lenient 0, too strict 3, unstable 2) | 29 / 30 (too lenient 0, too strict 1, unstable 0) |

#### 4. Selection and map value (§12-4, §12-5)

| | Before | After |
|---|---|---|
| Questions using a concept outside the selection | 0 / 30 | 0 / 30 |
| Selected concepts covered by at least one question | 35 / 80 | 38 / 80 |
| Near-duplicate question pairs across ranges | 5 | 2 |

| Same size (8 concepts), 5 ranges each | Lasso on the map | Random |
|---|---|---|
| Relations inside the range | 29 | 3 |
| Isolated concepts | 0 / 40 | 30 / 40 |
| Largest connected group (mean) | 8.0 | 1.8 |

Selecting by lasso gives about 10 times more relations inside the range, and those relations are the material for relation questions. We have not yet compared questions generated from the two kinds of range. That comparison needs API calls.

#### 5. RAG (slide index + Wikipedia)

Measured twice on Ethics imports with RAG on (flash-lite): round 1 with main `13b85b7`, round 2 after the fixes for #91–#93 (main `b89a958`). Each round is a new import, so the graphs differ. To keep run-to-run variation out, each step is compared before and after on the same data: the graph before and after relation completion, and the same 30 questions before and after the uniqueness check.

| Relation completion | Round 1 | Round 2 |
|---|---|---|
| New concepts | 0 | 0 |
| Relations added (from slides / from Wikipedia) | 7 (7 / 0) | 2 (1 / 1) |
| Added relations: correct / ambiguous / wrong | 0 / 3 / 4 | 1 / 1 / 0 |
| Concepts with no relation (before → after) | 22 → 17 | 13 → 11 |

| Uniqueness check (6 ranges × 5 questions, without → with) | Round 1 | Round 2 |
|---|---|---|
| Questions | 30 → 25 | 30 → 29 |
| Removed questions that were actually bad | 0 / 5 | 1 / 1 (borderline) |
| Bad questions kept | 2 | 1 |
| A distractor is actually correct (MCQ) | 1 / 24 → 1 / 20 | 1 / 24 → 1 / 24 |
| Not answerable from the slides | 0 → 0 | 0 → 0 |
| Usable / usable after a fix / not usable | 23 / 5 / 2 → 19 / 4 / 2 | 22 / 7 / 1 → 22 / 6 / 1 |
| Wikipedia lookups that found an unrelated article | 34 / 105 | 0 / 54 |

- Every wrong judgement in both rounds came from generation, not retrieval. The pages needed were always in the material.
- Round 1's check removed only good questions, because the model's quotes did not match the page text verbatim. After the fix (#91) a failed quote match is recorded instead of removing the question, and round 2 removed no good question. The check still rarely catches a bad question: the one bad question in round 2 was kept even though the model's own reasons named the other options as examples from the slides.
- The only Wikipedia-backed relation (Kant → maxim) is correct, and the slides support it too; it was marked as external only because the model quoted a page without Kant's name.
- Of 15 sampled "Other" relations in round 2, 6 could have taken a vocabulary label. Half of these were blocked because a concept had kind "Other": five book titles escaped the book filter that way.
<!-- EVAL:END -->

## Known limitations

- **Run-to-run variation.** Extraction runs once per import, and the derived vocabulary changes between runs (one ethics run had *opposes*, the next had *paraphrase* instead). The numbers in the evaluation are single runs; see the evaluation for the size of the variation.
- **Few concept-to-concept relations.** With the cheapest model, most relations connect a person to an idea. Relations between ideas, which are the best material for relation questions, stay at about 20–30 for the ethics course. Prompting for them did not help (issue #51). A stronger model is compared in the evaluation.
- **Removing cited books can remove ideas with the same name.** *Social Contract* or *Escape from Freedom* were extracted only as book titles and were removed with them (issue #69). The removed titles are logged so this can be checked.
- **Many relations are labelled "Other".** In the two full imports with RAG, 49% and 69% of relations were "Other" before completion. The share depends on the vocabulary each run derives. A code rule now widens the kinds a person-subject label may point to (issue #74), but in the second run the vocabulary did not need it, so its effect is not yet measured.
- **Some books escape the book filter.** A book whose kind is "Other" is not removed before relation extraction (issue #99).
- **The answer check rarely catches a bad question.** After the fixes it no longer removes good questions, but it kept one question whose wrong options were also correct, and it judged the same question differently in two ranges.
- **English material gives few concepts.** Four NLP lectures gave only 13 concepts, and later lectures added almost none (issue #25).
- **Repeated slides.** Later lectures sometimes repeat earlier slides, which inflates occurrence counts.

## Repository layout

| Path | Contents |
|---|---|
| `app.py` | Entry point: course and view switching |
| `ui/` | Screens (`map_view.py`, `quiz_view.py`, `ingest_view.py`), the map component (`static/map.js`, `map.css`), UI strings (`i18n.py`) |
| `quizmap/` | PDF text, LLM calls (`common.py`), vocabulary (`vocab.py`), extraction (`extraction.py`), quiz (`quiz.py`), grading (`grade.py`), courses and background import (`courses.py`), retrieval index and Wikipedia (`rag.py`) |
| `tests/` | Unit tests and browser tests (Playwright) |
| `quiz-map-spec.md` | Specification (Japanese) |
| `derive_vocab.py`, `extract.py`, `generate_quiz.py` | Command-line versions of the same steps |
