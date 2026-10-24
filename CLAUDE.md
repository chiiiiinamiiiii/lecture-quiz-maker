# Lecture Quiz Maker — 開発チームの決まりごと

講義資料（PDF）から概念マップを作り、地図の一部を囲むとその範囲の概念どうしの関係を問うクイズが出る学習ツール。
仕様は `quiz-map-spec.md`。アプリを起動すれば、アップロードから解説までアプリ内で完結することが目標。

## 構成

| 場所 | 中身 |
|---|---|
| `app.py` | 入口。コースの切り替えと画面の切り替え |
| `ui/` | 画面。`map_view.py`（左の地図と右のパネル）、`map_component.py` と `static/map.js`・`static/map.css`（地図の部品。Streamlit components v2 + canvas）、`quiz_view.py`（出題・解答・採点）、`ingest_view.py`（取り込み）、`i18n.py`（画面の文言 日本語/English）、`layout.py`（配置の計算）、`state.py` |
| `quizmap/` | 中身。`common.py`（PDF・LLM 呼び出し）、`vocab.py`（§4 語彙）、`extraction.py`（§5 抽出）、`quiz.py`（§7 出題）、`grade.py`（§8 採点）、`courses.py`（コース管理と取り込みの裏処理） |
| `tests/e2e/` | ブラウザで実際に操作する戻り確認。UI を変えたら必ず通す |
| `data/` | Git 管理外。`data/courses/<id>/` がコースごとのデータ |

## チーム

指揮（メインのセッション）と 5 つの役割で開発する。役割の定義は `.claude/agents/`。

| 役割 | 書いてよいファイル | API |
|---|---|---|
| ui-reviewer（UI 評価） | 書かない。Issue とスクリーンショットだけ | 使わない（取り込み済みのコースで触る） |
| ui-improver（UI 改善） | `ui/`（`static/map.js`・`map.css` を含む）、`app.py`、`.streamlit/`、`tests/e2e/`（`test_full_flow_api.py` を除く） | 使わない |
| output-reviewer（出力評価） | 書かない。Issue と `data/team/output-reviewer/` の評価結果だけ | 指揮の許可を得て使う |
| prompt-improver（プロンプト改善） | `quizmap/vocab.py`、`quizmap/extraction.py`、`quizmap/quiz.py`、`quizmap/grade.py` | 指揮の許可を得て使う |
| feature-dev（機能開発） | `quizmap/courses.py`、`quizmap/common.py`、新しい機能のファイル | 指揮の許可を得て使う |
| rag-engineer（RAG） | `quizmap/rag.py`（索引・埋め込み・Wikipedia）。設計は `docs/rag-design.md` | 指揮の許可を得て使う |

上の表にないファイルを触る必要が出たら、自分で触らず指揮に相談する。

## 作業環境（役割ごとに分ける）

各役割は自分の作業ツリー・ブランチ・ポート・データ置き場で動く。`dev/team.sh` が用意する。

- 作業ツリー: `../quizmap-team/<役割>`、ブランチ `team/<役割>`
- アプリの起動: `dev/run_app.sh`（作業ツリーの中で実行。ポートとデータ置き場は役割ごとに自動で決まる）
- Python: 本体の `.venv` を使う（`dev/run_app.sh` と `dev/py.sh` が面倒を見る）
- API キー: 本体の `.env` を `QUIZMAP_ENV_FILE` で参照する。キーを表示・コピーしない
- ポート: main 8501 / ui-reviewer 8611 / ui-improver 8612 / output-reviewer 8613 / prompt-improver 8614 / feature-dev 8615
- 改善の確認は、改善担当が自分のポートで起動しているアプリを見て行う（例: UI の修正は http://localhost:8612/）

## やりとりは GitHub の Issue で

- 評価役（ui-reviewer, output-reviewer）は、見つけた問題を Issue に書く。1 件 1 Issue
  - 何をしたら何が起きたか / どうなってほしいか / 優先度 / 確認方法
  - ラベル: 種類（`ui` `output` `feature` `bug` `regression`）と優先度（`p1` `p2` `p3`）
  - スクリーンショットは `data/review/<Issue番号>/` に置き、本文にはファイル名を書く。画像を GitHub に上げない
  - **講義資料の本文やスライドの内容を Issue に書き写さない**（他人の著作物。将来公開する可能性がある）
- 指揮が優先順位を決め、改善役に割り振る（`status:ready` → `status:in-progress`）
- 改善役は `team/<役割>` ブランチで直し、Issue 番号を書いたプルリクエストを出す（`Fixes #番号`）。`status:needs-verify` を付ける
- 評価役がもう一度触って確認し、Issue にコメントする。直っていなければ差し戻す
- 指揮が main に取り込み、push する

## 守ること

- **評価する人は直さない。直す人は自分の修正を評価しない。**
- **1 つのファイルを書くのは 1 人だけ**（上の表）。
- **UI を変えたら `dev/py.sh -m pytest tests/e2e -q` を通してからプルリクエストを出す。** 手のひらツールの 2 回目、投げ縄の選択維持、講義回ボタンの選択は過去に何度も壊れた。
- **LLM の API を呼ぶ作業は、指揮の許可を得てから。** Gemini の無料枠はプロジェクト単位の制限で、同時に叩くと互いに 429 で止まる。
- **方向を変える判断は利用者に聞く**（機能を削る、画面の構成を大きく変える、範囲を広げる）。指揮に上げる。
- コミットメッセージ・Issue・プルリクエストは日本語。
- 地図は `ui/static/map.js`（Streamlit components v2）。キー付きで描くので、再実行しても要素は作り直されず、表示位置と選択はブラウザ側に残る。地図の状態は `window.__qmMapApp` から読める（テスト用）。
- 画面の文言は直接書かず `ui/i18n.py` の `t("キー")` を使う（日本語と English）。

## よく使うコマンド

```
dev/run_app.sh                          # 自分のポートでアプリを起動
dev/py.sh -m pytest tests/e2e -q        # 戻り確認（API を呼ばない。約 2 分）
RUN_API_TESTS=1 dev/py.sh -m pytest tests/e2e -q   # API を呼ぶテストも含める（指揮の許可を得て）
gh issue list --label status:ready      # 割り振られた仕事
```
