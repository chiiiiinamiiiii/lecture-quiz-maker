# RAG の設計（2026-10-24 利用者の決定に基づく）

## 利用者が決めた方針（必ず守る）

1. **地図に出す単語（概念）は、必ずアップロードした資料に載っているもの。** 外部の資料から概念を足してはいけない。
2. **外部の資料（Wikipedia）は、概念どうしの関係を確かめるためだけに使ってよい。**
3. **出題の正しさの確認（ほかの選択肢が正解になっていないか、答えが一意か）には外部の資料を使ってよい。**
4. **ただし、答えは必ずアップロードした資料から導けること。** 資料から導けない内容を出題してはいけない。
5. Wikipedia は使ってよい（日本語版・英語版。コースの言語に合わせる）。

## 検索の元（3 層）

| 層 | 中身 | 何に使うか |
|---|---|---|
| スライド | pages.json の全ページ（全講義） | 関係の根拠、出題の答えの根拠（唯一の根拠になれる） |
| Wikipedia | 概念名で検索した記事の冒頭 | 関係の確認、選択肢の正しさの確認（答えの根拠にはしない） |

アップロードした PDF はすべて「スライド」の層に入る（講義回の割り当ては廃止済み）。

## 部品と担当

### `quizmap/rag.py`（RAG 担当・新規）

```python
EMBED_MODEL = os.environ.get("QUIZMAP_EMBED_MODEL", "gemini-embedding-001")

def embed(client, texts: list[str], task: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]
    # batchEmbedContents を 100 件ずつ。common._post_with_retry を使い、待機と再試行は共通の規則に従う

class SlideIndex:
    @classmethod
    def build(cls, client, pages: list[dict]) -> "SlideIndex"     # pages.json と同じ形
    def save(self, course_dir: Path) -> None                       # course_dir/rag_index.json（ベクトルは float16 相当に丸めて保存）
    @classmethod
    def load(cls, course_dir: Path) -> "SlideIndex | None"
    def search(self, client, query: str, k: int = 5, exclude: set[tuple[int, int]] = frozenset()) -> list[dict]
        # [{"lecture", "page", "text", "score"}]。query は RETRIEVAL_QUERY で埋め込む

class Wiki:
    def __init__(self, course_dir: Path, lang: str)                # lang は "ja" / "en"。キャッシュは course_dir/wiki_cache.json
    def lookup(self, term: str) -> dict | None
        # {"title", "url", "extract"}。MediaWiki API: list=search で 1 件 → prop=extracts&exintro&explaintext
        # User-Agent を付ける。見つからなければ None。結果はキャッシュし、同じ語で二度問い合わせない
```

- 依存を増やさない（requests と標準ライブラリ、numpy は pandas に付いてくるので使ってよい）。
- 単体テストは HTTP を差し替えて書く（API を呼ばない）。

### 関係の補完（プロンプト改善担当、`quizmap/extraction.py`）

```python
def complete_relations(client, model, vocab, graph, pages, index: SlideIndex, wiki: Wiki, log=print) -> dict
```

- 対象: どの関係にもつながらない概念と、同じスライドのページに一緒に出てくるのに関係の無い概念の対。
- 各対について、索引から両方を含む／近いスライドのページを数件、Wikipedia から両方の概念の記事の冒頭を取り、語彙のラベルで関係があるかを判定させる。
- **新しい概念は作らない**（両端は既存の概念 id のみ）。
- 追加する関係には `origin` を付ける。
  - `"slide"`: スライドの記述が根拠。`source` は根拠のページ、`evidence` は引用
  - `"external"`: スライドには明示が無く、Wikipedia で確かめた関係。`source` は両端の概念が出てくるスライドのページ、`evidence` は Wikipedia の引用、`evidence_url` を付ける
- 既存の関係（抽出で取ったもの）には `origin: "slide"` を付ける。
- 照合（端の種類、evidence の本文照合）は既存の規則に通す。external の evidence は Wikipedia の本文と照合する。
- stats に `completion`（候補数、追加した slide / external の数、API 回数）を残す。

### 出題の検証（プロンプト改善担当、`quizmap/quiz.py`）

```python
def verify_uniqueness(client, model, questions, graph, pages, index: SlideIndex, wiki: Wiki) -> tuple[list, list]
    # 戻り値: (残す問題, 検証の記録)
```

- 生成と自己確認の後に 1 回だけ呼ぶ（全問をまとめて 1 回の API 呼び出し）。
- 各問について、正答と各選択肢の語を Wikipedia で引き、索引で関連するスライドのページを取る。
- 判定させること:
  1. 正答が**スライドの記述から導けるか**（根拠のページを 1 つ以上挙げる）。導けなければ**その問題を外す**
  2. 正答以外の選択肢が、スライドまたは一般的な知識（Wikipedia）で**正解と言えてしまわないか**。言えてしまうなら、その選択肢を差し替える（差し替え先も資料に載っている語に限る）か、問題を外す
  3. 記述問題は、模範解答以外に資料から別の正答が成り立たないか
- 関係を問う問題の材料には **`origin: "external"` の関係を使わない**（答えが資料から導けないため）。
- 記録（どの問題を外した・差し替えたか、理由、使った Wikipedia の URL）は quiz の meta に残す。画面には出さない。

### 取り込みへの組み込み（機能開発担当、`quizmap/courses.py`）

- 取り込みの段階に「索引づくり」（抽出の前）と「関係の補完」（抽出の後、スライド画像の前）を足す。
- 設定 `QUIZMAP_RAG`（既定 `on`）で切れるようにする。`off` なら今までと同じ動き（評価で比べるため）。
- 索引が無い既存のコースでも、出題のときに索引が無ければ検証は Wikipedia とスライドの本文だけで行う（落ちない）。

### 画面（UI 改善担当）

- 地図で `origin: "external"` の辺は点線にし、ツールチップに「外部資料（Wikipedia）で補った関係」と出典の記事名を出す。凡例にも足す。
- 出題・解答の画面は変えない（検証の記録は画面に出さない）。

### 評価（出力評価担当）

- 関係の補完: 追加した関係を無作為に 30 本（足りなければ全部）判定。どの関係にもつながらない概念の数の変化。external の関係が本当に Wikipedia で裏付けられているか。
- 出題の検証: 基準値と同じ 6 範囲・30 問を、検証あり／なしで作って比べる。③（誤答が正答）の ×、資料から答えられない問題の数、外された問題の数。
- 「検索の誤り」と「生成の誤り」を分けて数える（検証の記録を使う）。
