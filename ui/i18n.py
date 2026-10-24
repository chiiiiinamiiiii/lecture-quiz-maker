"""画面の文言（日本語 / English）。t("key") で現在の言語の文言を返す。

言語はサイドバーで切り替え、st.session_state["lang"] に入る。LLM が作る中身の言語は別に扱う
（quizmap 側の設定。ここは画面の文言だけ）。
"""
import streamlit as st

LANGS = {"ja": "日本語", "en": "English"}

TEXTS = {
    # 全体・サイドバー
    "app_title": {"ja": "QUIZ MAP", "en": "QUIZ MAP"},
    "language": {"ja": "言語", "en": "Language"},
    "course": {"ja": "コース", "en": "Course"},
    "new_course": {"ja": "＋ 新しいコース", "en": "+ New course"},
    "not_ingested": {"ja": "（未取り込み）", "en": " (not imported)"},
    "page": {"ja": "画面", "en": "View"},
    "page_map": {"ja": "地図とクイズ", "en": "Map & quiz"},
    "page_ingest": {"ja": "資料の取り込み", "en": "Import materials"},
    "rename_course": {"ja": "コース名", "en": "Course name"},
    "rename_help": {"ja": "ここで直した名前は、このコースのデータに保存されます", "en": "Saved to this course's data"},
    "ingesting": {"ja": "取り込み中", "en": "Importing"},
    "stats": {"ja": "概念 {c} / 関係 {r} / 出題済み {a}", "en": "{c} concepts / {r} relations / {a} asked"},

    # 地図（JavaScript 側にも渡す）
    "tool_hand": {"ja": "手のひら（H）: ドラッグで移動", "en": "Hand (H): drag to pan"},
    "tool_lasso": {"ja": "投げ縄（L）: Shift で追加、⌘ で除外", "en": "Lasso (L): Shift to add, ⌘ to subtract"},
    "tool_rect": {"ja": "矩形（M）: Shift で追加、⌘ で除外", "en": "Marquee (M): Shift to add, ⌘ to subtract"},
    "zoom_in": {"ja": "拡大", "en": "Zoom in"},
    "zoom_out": {"ja": "縮小", "en": "Zoom out"},
    "fit": {"ja": "全体を表示（0）", "en": "Fit to view (0)"},
    "clear": {"ja": "選択を解除（Esc）", "en": "Clear selection (Esc)"},
    "layout_force": {"ja": "つながり", "en": "Links"},
    "layout_timeline": {"ja": "講義回", "en": "Lectures"},
    "density": {"ja": "表示する概念の数", "en": "Concepts shown"},
    "unasked": {"ja": "未出題", "en": "Not asked"},
    "asked": {"ja": "出題済み", "en": "Asked"},
    "selected": {"ja": "選択中", "en": "Selected"},
    "hint": {"ja": "2本指でズーム ・ スペース＋ドラッグで移動 ・ Shift 追加 ・ ⌘ 除外 ・ Esc で選択を解除",
             "en": "Two fingers to zoom · Space + drag to pan · Shift add · ⌘ subtract · Esc to clear"},
    "by_lecture": {"ja": "講義回で選ぶ", "en": "By lecture"},
    "all": {"ja": "全部", "en": "All"},
    "lecture_fmt": {"ja": "第{n}回", "en": "L{n}"},
    "count_fmt": {"ja": "{n} 個を選択中", "en": "{n} selected"},

    "rel_other": {"ja": "その他", "en": "Other"},   # 関係ラベル「その他」の表示

    # 右側: 選択
    "sel_title": {"ja": "選択した範囲", "en": "Selection"},
    "sel_empty": {"ja": "地図の上で概念を囲むか、下の講義回ボタンを押すと、ここに並びます。",
                  "en": "Lasso concepts on the map or press a lecture button below to list them here."},
    "sel_caption": {"ja": "{n} 個（うち辺の中点から {m} 個）。出題に使わないものはチェックを外してください。",
                    "en": "{n} concepts ({m} via relation midpoints). Uncheck any you don't want."},
    "via_edge": {"ja": "辺の中点から", "en": "via relation"},
    "asked_mark": {"ja": "出題済み", "en": "asked"},
    "confirm": {"ja": "この範囲で確定", "en": "Use this selection"},
    "confirmed": {"ja": "確定した {n} 概念", "en": "{n} concepts confirmed"},

    # 右側: クイズ
    "quiz_title": {"ja": "クイズ", "en": "Quiz"},
    "n_questions": {"ja": "問題数", "en": "Questions"},
    "generate": {"ja": "クイズを作る", "en": "Generate quiz"},
    "generating": {"ja": "問題を作っています（作成 → 誤答の自己確認）…", "en": "Generating (draft → self-check of distractors)…"},
    "gen_failed": {"ja": "問題を作れませんでした: {e}", "en": "Could not generate questions: {e}"},
    "quiz_meta": {"ja": "{n} 問 ・ 誤答の差し替え {r} 件 ・ {s} 秒", "en": "{n} questions · {r} distractors replaced · {s}s"},
    "q_label": {"ja": "問 {i}", "en": "Q{i}"},
    "answer_short": {"ja": "解答（記述）", "en": "Your answer"},
    "answer_placeholder": {"ja": "ここに答えを書く", "en": "Type your answer"},
    "hint_btn": {"ja": "ヒント", "en": "Hint"},
    "hint_source": {"ja": "出典", "en": "Source"},
    "grade": {"ja": "採点する", "en": "Grade"},
    "grading": {"ja": "採点しています…", "en": "Grading…"},
    "grade_failed": {"ja": "採点できませんでした: {e}", "en": "Could not grade: {e}"},
    "result_title": {"ja": "結果と解説", "en": "Results & explanations"},
    "score": {"ja": "{t} / {n} 点", "en": "{t} / {n}"},
    "saved_note": {"ja": "結果を保存し、出題した概念を地図に反映しました。", "en": "Saved. Asked concepts are now marked on the map."},
    "your_answer": {"ja": "あなたの答え", "en": "Your answer"},
    "correct_answer": {"ja": "正答", "en": "Correct"},
    "model_answer": {"ja": "模範解答", "en": "Model answer"},
    "no_answer": {"ja": "（無回答）", "en": "(no answer)"},
    "feedback": {"ja": "採点コメント", "en": "Feedback"},
    "explanation": {"ja": "解説", "en": "Explanation"},
    "hint_used": {"ja": "ヒント使用", "en": "hint used"},
    "new_quiz": {"ja": "もう一度作る", "en": "New quiz"},
    "json_expander": {"ja": "生成結果の JSON（答えを含む）", "en": "Raw JSON (includes answers)"},

    # 取り込み
    "new_course_title": {"ja": "新しいコースを作る", "en": "Create a course"},
    "new_course_help": {"ja": "講義資料の PDF をまとめてアップロードしてください。ファイル名の「第3回」「week3」「Lecture 03」などから講義回を読み取ります。読み取れないものは次の画面で指定できます。",
                        "en": "Upload your lecture PDFs. Lecture numbers are read from file names such as “week3” or “Lecture 03”; you can set the rest on the next screen."},
    "course_name_ph": {"ja": "例: 倫理学の世界", "en": "e.g. Introduction to Ethics"},
    "pdf_files": {"ja": "講義資料（PDF、複数可）", "en": "Lecture slides (PDF, multiple)"},
    "create_course": {"ja": "コースを作る", "en": "Create course"},
    "ingest_title": {"ja": "資料の取り込み：{name}", "en": "Import materials: {name}"},
    "add_files": {"ja": "資料を追加する（PDF、複数可）", "en": "Add PDFs"},
    "add_btn": {"ja": "追加する", "en": "Add"},
    "added_toast": {"ja": "{n} 件を追加しました", "en": "Added {n} file(s)"},
    "no_files": {"ja": "まだ資料がありません。上からアップロードしてください。", "en": "No files yet. Upload PDFs above."},
    "assign_title": {"ja": "**講義回の割り当て**　講義回が空欄のものや、講義資料ではないもの（読書課題など）は「使う」を外してください。",
                     "en": "**Lecture numbers**  Uncheck “Use” for files without a number or that are not lecture slides."},
    "col_use": {"ja": "使う", "en": "Use"},
    "col_lecture": {"ja": "講義回", "en": "Lecture"},
    "col_file": {"ja": "ファイル", "en": "File"},
    "dup_lectures": {"ja": "同じ講義回が重なっています: {l}", "en": "Duplicate lecture numbers: {l}"},
    "rebuild_vocab": {"ja": "語彙（概念の種類と関係の種類）も作り直す", "en": "Also rebuild the vocabulary (concept and relation types)"},
    "rebuild_help": {"ja": "既定では前回の語彙を使い回します。使い回すと、取り込み直しても結果を比べられます。",
                     "en": "By default the previous vocabulary is reused so results stay comparable."},
    "ingest_estimate": {"ja": "{n} 回分を取り込みます。目安 {m} 分。処理中も画面は操作できます。",
                        "en": "Importing {n} lecture(s). About {m} min. You can keep using the app meanwhile."},
    "other_running": {"ja": "別のコースの取り込みが実行中です。終わってから始めてください。", "en": "Another import is running. Please wait for it to finish."},
    "start_ingest": {"ja": "取り込みを始める", "en": "Start import"},
    "reingest": {"ja": "取り込み直す", "en": "Re-import"},
    "last_failed": {"ja": "前回の取り込みは失敗しました: {e}", "en": "The last import failed: {e}"},
    "log": {"ja": "ログ", "en": "Log"},
    "last_done": {"ja": "前回の取り込みは完了しています（{t}）。", "en": "The last import finished ({t})."},
    "progress_note": {"ja": "開始 {t}。この画面を離れても処理は続きます。", "en": "Started {t}. The import keeps running if you leave this view."},
    "starting": {"ja": "開始しています…", "en": "Starting…"},
    "course_name": {"ja": "コース名", "en": "Course name"},
    "kept_toast": {"ja": "{n} 件は一覧にある同じファイルのため、講義回と「使う」はそのまま残しました",
                   "en": "{n} file(s) already in the list; their lecture number and “Use” were kept"},
    "sentence_sep": {"ja": "。", "en": ". "},
    "list_sep": {"ja": "、", "en": ", "},
    "phase_read": {"ja": "PDF の読み込み", "en": "Reading PDFs"},
    "phase_vocab": {"ja": "語彙の導出", "en": "Deriving the vocabulary"},
    "phase_concepts": {"ja": "概念の抽出", "en": "Extracting concepts"},
    "phase_relations": {"ja": "関係の抽出", "en": "Extracting relations"},
    "phase_slides": {"ja": "スライド画像の作成", "en": "Rendering slide images"},
}


def lang() -> str:
    return st.session_state.get("lang", "ja")


def t(key: str, **kw) -> str:
    s = TEXTS[key].get(lang()) or TEXTS[key]["ja"]
    return s.format(**kw) if kw else s


def map_texts() -> dict:
    """地図部品（JavaScript）に渡す文言。"""
    keys = ["tool_hand", "tool_lasso", "tool_rect", "zoom_in", "zoom_out", "fit", "clear", "layout_force",
            "layout_timeline", "density", "unasked", "asked", "selected", "hint", "by_lecture", "all",
            "lecture_fmt", "count_fmt"]
    return {k: TEXTS[k].get(lang()) or TEXTS[k]["ja"] for k in keys}
