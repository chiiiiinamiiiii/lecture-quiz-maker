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

## Where the LLM is used

| Step | What the LLM decides | Why it needs an LLM |
|---|---|---|
| Vocabulary | Which concept kinds and relation labels this course needs, and how to ask about each relation | The right labels differ by field (see Evaluation); fixed labels break when the subject changes |
| Extraction | Concepts, typed relations, and their source pages | Semantic structure, not keywords |
| Question writing | Question text and four choices, following each relation's `question_style` | — |
| Distractor self-check | Whether each wrong choice is really wrong according to the slides | Prevents “distractor that is actually correct” |
| Short-answer grading | 1 / 0.5 / 0 with feedback (LLM-as-judge) | Free-text answers |

Only the map drawing and the selection are plain code.

## Design decisions

**Derive the vocabulary instead of fixing it.** A first prototype used fixed relation labels written for an ethics course. Applied to other material it silently forced relations into wrong labels. The app now asks the model to propose the labels for each course and stores them in `vocab.json`. The vocabulary is then frozen and reused on every re-import, so results can be counted and compared. The share of relations that fall into “other” is the signal that the vocabulary does not fit the material.

**Two-stage extraction.** Concepts are extracted first, lecture by lecture, with the list of concepts already found passed in so names stay consistent across lectures. Relations are extracted second, and the model may only use the concept ids from stage one. Extracting both at once produced edges pointing at concepts that did not exist.

**Every concept and relation keeps its page.** Hints, explanations and the slide shown after grading all depend on it.

**Text normalisation before anything else.** PDF text extraction returned CJK radicals instead of ordinary kanji (人 → ⼈, 民 → ⺠). NFKC fixes the Kangxi block but not the CJK Radicals Supplement, so a manual table is applied as well, then line breaks inside words are removed. Without this, search and matching silently fail.

**Questions are not templated.** The code never says how to ask a question. Each relation label carries its own `question_style`, written by the model during vocabulary derivation, and the quiz prompt follows it. A different course gets different question styles automatically.

**Distractors from neighbours, then a self-check.** Neighbouring concepts on the map make plausible wrong answers. A second call asks for a one-line, slide-based reason why each distractor is wrong and replaces the ones that have none.

**Hints do not reveal answers.** The first version showed the concept's definition as a hint, which *is* the answer to a definition question. The hint now reveals the source slide, which keeps the retrieval step with the learner.

**Display density is not extraction depth.** The map can show fewer dots, but it never deletes extracted data. How many concepts are extracted at all is decided at extraction time and depends on the model and the number of calls (see Known limitations).

**Map component.** The first map used Plotly. Streamlit resets a Plotly chart's selection whenever the figure changes, so keeping a lasso selection, lecture-button selection and the current zoom alive required layers of browser-side workarounds that broke repeatedly. The map is now a small canvas component (Streamlit components v2) that keeps its view and selection in the browser and only reports the selection back to Python.

## Models and cost

**Development:** Claude Code (harness) with Claude Fable 5.1 and Claude Opus 5.5 (models). Development ran as a team of Claude Code sessions: one coordinator and five roles (UI reviewer, UI improver, output reviewer, prompt improver, feature developer), each in its own git worktree and terminal, coordinated through GitHub issues and pull requests. Reviewers never fix and fixers never judge their own fixes. See `CLAUDE.md` and `.claude/agents/`.

**Inside the app:** `gemini-3.1-flash-lite` through the Google AI Studio free tier. The work is structured extraction, not hard reasoning, so the cheapest model that does the job was chosen, following the course principle. I listed the available models with the API before choosing: the cheapest free-tier model, `gemini-2.5-flash-lite`, is no longer offered to new users, so the next cheapest was used. The model name is a setting (`QUIZMAP_MODEL`) so other models can be compared. All calls use JSON-schema structured output, wait between calls, and back off on HTTP 429.

| Operation | API calls | Time |
|---|---|---|
| Import 17 lectures / 185 pages (ethics) | 35 | about 4 min |
| Import 4 lectures / 136 pages (NLP, English) | 9 | 88 s |
| Generate 5 questions | 2 | about 11 s |
| Grade (multiple choice only / with short answers) | 0 / 1 | — |

## Evaluation

EVALUATION_PLACEHOLDER

## Known limitations

- **Extraction coverage.** The ethics course yields 74 concepts. An earlier multi-pass prototype with a stronger model found 163. Later lectures tend to repeat concepts already listed instead of adding new ones. Coverage depends on the model and on how many passes are made; I plan to expose this as a setting rather than fix one value.
- **Repeated slides.** Later lectures sometimes repeat earlier slides, which inflates occurrence counts.
- **Course name.** The derived vocabulary sometimes takes the course name from the folder name; the name can be edited in the app.

## Repository layout

| Path | Contents |
|---|---|
| `app.py` | Entry point: course and view switching |
| `ui/` | Screens (`map_view.py`, `quiz_view.py`, `ingest_view.py`), the map component (`static/map.js`, `map.css`), UI strings (`i18n.py`) |
| `quizmap/` | PDF text, LLM calls (`common.py`), vocabulary (`vocab.py`), extraction (`extraction.py`), quiz (`quiz.py`), grading (`grade.py`), courses and background import (`courses.py`) |
| `tests/` | Unit tests and browser tests (Playwright) |
| `quiz-map-spec.md` | Specification (Japanese) |
| `derive_vocab.py`, `extract.py`, `generate_quiz.py` | Command-line versions of the same steps |
