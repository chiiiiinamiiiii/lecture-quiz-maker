"""§7 クイズ生成。確定した概念 id と問題数から、その場で問題を作る。

流れ:  (1) 生成 → (2) 誤答の自己確認（理由を述べられない誤答は差し替え）→ (3) 自動検証
問い方はコードに書かない。vocab.json の各関係ラベルが持つ question_style をプロンプトに渡す。
LLM 呼び出しは quizmap.common.call_json（プロバイダ非依存）だけを使う。
"""
from __future__ import annotations

import datetime as dt
import time
import unicodedata
from collections import Counter

from quizmap.common import OTHER_LABEL, call_json

ASPECT_RATIO = {"relation": 4, "example": 3, "definition": 3}   # 仕様 §7 の目安

QUESTION_PROPS = {
    "id": {"type": "string"},
    "type": {"type": "string", "enum": ["mcq", "short"]},
    "aspect": {"type": "string", "enum": ["relation", "example", "definition"]},
    "prompt": {"type": "string"},
    "choices": {"type": "array", "items": {"type": "string"}},
    "answer": {"type": "integer"},
    "answer_text": {"type": "string"},
    "explanation": {"type": "string"},
    "concept_labels": {"type": "array", "items": {"type": "string"}},
    "relation_used": {
        "type": "object",
        "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "label": {"type": "string"}},
        "required": ["from", "to", "label"],
        "additionalProperties": False,
    },
    "sources": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"lecture": {"type": "integer"}, "page": {"type": "integer"}},
            "required": ["lecture", "page"],
            "additionalProperties": False,
        },
    },
}
QUESTION_REQUIRED = ["id", "type", "aspect", "prompt", "choices", "answer", "answer_text",
                     "explanation", "concept_labels", "relation_used", "sources"]

GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {"type": "object", "properties": QUESTION_PROPS,
                      "required": QUESTION_REQUIRED, "additionalProperties": False},
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **QUESTION_PROPS,
                    "distractor_reasons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "choice": {"type": "integer"},
                                "reason": {"type": "string"},
                                "replaced": {"type": "boolean"},
                            },
                            "required": ["choice", "reason", "replaced"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": QUESTION_REQUIRED + ["distractor_reasons"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}

GEN_SYSTEM = """あなたは大学の授業資料から学習用のクイズを作る専門家です。
資料に書かれていることだけを根拠にし、一般知識で補わないでください。出力は日本語です。"""

VERIFY_SYSTEM = """あなたは作成済みのクイズを点検する専門家です。資料に書かれていることだけを根拠に判断します。出力は日本語です。"""


# ------------------------------------------------------------------ 材料の組み立て

def split_counts(n: int, has_relations: bool) -> dict[str, int]:
    """目安の比率 関係4 / 具体例3 / 定義3 で n 問を配分する。関係が 1 本もなければ関係の枠を他に回す。"""
    ratio = dict(ASPECT_RATIO)
    if not has_relations:
        ratio["relation"] = 0
    total = sum(ratio.values())
    counts = {k: int(n * v / total) for k, v in ratio.items()}
    order = sorted(ratio, key=lambda k: -(n * ratio[k] / total - counts[k]))  # 端数の大きい順に 1 問ずつ足す
    for k in order:
        if sum(counts.values()) >= n:
            break
        if ratio[k] > 0:
            counts[k] += 1
    return counts


def build_materials(graph: dict, vocab: dict, pages: list[dict], concept_ids: list[str]):
    concepts = {c["id"]: c for c in graph["concepts"]}
    selected = [concepts[i] for i in concept_ids if i in concepts]
    sel = set(c["id"] for c in selected)

    # 囲んだ概念どうしの関係（「その他」は問い方が無いので除く）
    inner = [r for r in graph["relations"]
             if r["from"] in sel and r["to"] in sel and r["label"] != OTHER_LABEL]
    # 誤答候補: 地図上で隣接する概念（選択の外側も含む）
    neighbors: dict[str, set[str]] = {i: set() for i in sel}
    for r in graph["relations"]:
        if r["from"] in sel and r["to"] not in sel:
            neighbors[r["from"]].add(r["to"])
        if r["to"] in sel and r["from"] not in sel:
            neighbors[r["to"]].add(r["from"])
    # 出典ページの本文
    page_text = {(p["lecture"], p["page"]): p["text"] for p in pages}
    needed: set[tuple[int, int]] = set()
    for c in selected:
        needed.update((s["lecture"], s["page"]) for s in c["sources"])
    for r in inner:
        needed.add((r["source"]["lecture"], r["source"]["page"]))
    needed = sorted(k for k in needed if k in page_text)

    labels_used = {r["label"] for r in inner}
    styles = [v for v in vocab["relations"] if v["label"] in labels_used]
    return concepts, selected, inner, neighbors, needed, page_text, styles


def materials_text(concepts, selected, inner, neighbors, needed, page_text, styles) -> str:
    parts = ["## 出題範囲の概念（問題の根拠にしてよいのはこれだけ。concept_labels にはここの「」内の表記をそのまま書く）"]
    for c in selected:
        src = "、".join(f"第{s['lecture']}回p.{s['page']}" for s in c["sources"])
        parts.append(f"- 「{c['label']}」（{c['kind']}）: {c['definition']} [{src}]")
    parts.append("\n## 出題範囲の概念どうしの関係（関係を問う問題はここから作る）")
    if inner:
        definition = {v["label"]: v["definition"] for v in styles}
        for r in inner:
            a, b = concepts[r["from"]]["label"], concepts[r["to"]]["label"]
            # 向きを言葉にする（#32）。ラベルの定義に from / to の説明があればそれで A・B のどちらが何かが決まる
            parts.append(f"- {a} —{r['label']}→ {b}（from は「{a}」、to は「{b}」。{r['label']}の意味: {definition.get(r['label'], '')}）"
                         f" [第{r['source']['lecture']}回p.{r['source']['page']}]")
    else:
        parts.append("- （なし）")
    parts.append("\n## 関係ラベルごとの出題の方針（出題者向け。この文を問題文に書き写さない）")
    for v in styles:
        parts.append(f"- {v['label']}: 定義「{v['definition']}」／出題の方針「{v['question_style']}」／例: {v['example']}")
    parts.append("\n## 誤答に使ってよい隣接概念（地図上で隣り合う概念。紛らわしい誤答にする。concept_labels には書かない）")
    for cid, ns in neighbors.items():
        if ns:
            parts.append(f"- {concepts[cid]['label']} の隣: " + "、".join(concepts[n]["label"] for n in sorted(ns)))
    parts.append("\n## 資料本文（出典ページ）")
    for lec, page in needed:
        parts.append(f"[第{lec}回 p.{page}] {page_text[(lec, page)]}")
    return "\n".join(parts)


def gen_user(materials: str, n: int, counts: dict[str, int], n_short: int) -> str:
    return f"""次の材料から、学習用のクイズを {n} 問作ってください。

内訳: 関係を問う問題 {counts['relation']} 問 / 具体例を問う問題 {counts['example']} 問 / 定義を問う問題 {counts['definition']} 問
形式: 4 択（type=mcq, choices は 4 つ, answer は正答の添字 0〜3, answer_text は正答の文字列）を基本とし、
      記述式（type=short, choices は空配列, answer は 0, answer_text は模範解答）を {n_short} 問まで混ぜてよい
      answer は 0 始まりの添字（0〜3）。answer_text は choices[answer] と同じ文字列にする

守ること:
- 関係を問う問題は、そのラベルの出題の方針が示す内容を問う。ただし問題文は学習者に向けた疑問文で書き、方針の文（「〜を問う」）を書き写さない
- 問題文に、正答に当たる関係ラベルの名前（「関係ラベルごとの出題の方針」にあるラベル名）を書かない
- 1 つの問題で問うラベルは 1 つだけ
- 関係を問う問題では、材料の関係の from と to を入れ替えない。出題の方針の A・B がどちらに当たるかは、関係の行の括弧の説明で決める
- relation_used には、関係を問う問題で根拠にした関係の行の from・to・label を、概念の表記そのままで書く。関係を問う問題でなければ 3 つとも空文字列にする
- 具体例を問う問題は、資料が概念の例として挙げている事例・比喩・身近な例（例: 第◯回で概念 X の例として挙げられている事柄）と概念の対応を問う。概念の定義文を言い換えて語を当てさせる問題は、具体例ではなく定義の問題として数える
- 資料に具体例が見つからない概念で、具体例の問題を作らない。具体例の枠が埋まらなければ、その分を定義の問題にして、aspect を definition にする
- 「出題範囲の概念どうしの関係」に、ある概念を別の概念の具体例として結ぶラベル（ラベルの意味で判断する）の関係があれば、具体例の問題はその対を優先して使う
- 定義を問う問題は、資料の記述に基づく定義を問う
- 誤答には「誤答に使ってよい隣接概念」や出題範囲内の別の概念を使い、紛らわしくする
- 問題文・選択肢・解説は資料に書かれていることだけを根拠にする
- concept_labels には、問題の根拠となる概念を「出題範囲の概念」の表記そのままで 1 つ以上書く。
  問題文・正答に出てくる概念を書き、名前の似た別の概念（例: 「X」と「X性」）と取り違えない。誤答にだけ使った概念は書かない
- sources には根拠となるページ（lecture, page）を 1 つ以上書く。資料本文の見出し [第N回 p.M] から取る
- explanation には正答の理由を 1〜3 文で書く
- id は q_01, q_02, … とする

{materials}
"""


def verify_user(questions: list[dict], materials: str) -> str:
    import json
    return f"""次のクイズの各 4 択問題について、各誤答（正答以外の選択肢）がなぜ誤りかを、資料本文のすべてのページに照らして 1 行で述べてください。
どこか 1 ページでも、その誤答が問題文の条件に当てはまる記述があれば、誤りとは言えないので差し替えてください。
資料に基づいて誤りだと述べられない誤答（実は正しい、資料から判断できない）も、別の誤答に差し替えて replaced を true にしてください。
差し替えるときは、choices のその位置の文字列を実際に別のものに書き換えてください。
問題文が「資料における〇〇の定義」のように誰の説かを限っていないとき、別の人物の説と一致する誤答は差し替えるか、問題文に誰の説かを書き足してください。
差し替えた場合も、その新しい誤答がなぜ誤りかを述べてください。
正答そのものが資料と食い違っていれば、問題文・選択肢・answer・explanation を資料に合わせて直してください。
関係を問う問題（aspect=relation）は、正答が材料の「出題範囲の概念どうしの関係」のどの行に当たるかを確かめ、向きが逆なら問題文か正答を直してください。
どの行にも当たらなければ、問題を資料の記述に合わせて直してください。relation_used はその行の from・to・label にしてください。
記述式の問題は distractor_reasons を空配列にして、そのまま返してください。
問題の数・id・type は変えないでください。aspect は次の場合だけ直してよい。
aspect=example の問題が、資料の具体例ではなく定義の言い換えを問うていれば、aspect を definition に直してください。
問題文が「〜を問う。」で終わっている、または正答に当たる関係ラベルの名前を含んでいれば、問題文を学習者向けの疑問文に直してください。
answer と distractor_reasons の choice は 0 始まりの添字（0〜3）です。choices の順番は変えないでください。
answer_text は choices[answer] と同じ文字列にしてください。

## クイズ
{json.dumps({"questions": questions}, ensure_ascii=False, indent=1)}

{materials}
"""


# ------------------------------------------------------------------ 概念の対応づけ（#20）

def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("\u3000", "")


def mentioned_labels(text: str, labels: list[str]) -> set[str]:
    """text に出てくる label。長い label の中に含まれるだけの短い label は数えない（「尊厳性」の中の「尊厳」）。"""
    t = _norm(text)
    spans = []
    for label in labels:
        key = _norm(label)
        if not key:
            continue
        start = t.find(key)
        while start != -1:
            spans.append((start, start + len(key), label))
            start = t.find(key, start + 1)
    found = set()
    for a, b, label in spans:
        inside_longer = any(a2 <= a and b <= b2 and (b2 - a2) > (b - a) for a2, b2, _ in spans)
        if not inside_longer:
            found.add(label)
    return found


def resolve_concepts(q: dict, selected: list[dict]) -> tuple[list[str], list[str]]:
    """LLM が書いた concept_labels を概念 id にする。戻り値: (id のリスト, 警告)。

    id は label のローマ字化と衝突回避の「_」だけで区別されることがあり、LLM が取り違えやすい（#20）。
    そこで LLM には label を書かせ、id への対応はここで行う。さらに、書かれた label が
    問題文・正答・解説に出てこず、名前の似た別の概念（一方が他方を含む）が出てくるなら、それに差し替える。
    """
    qid = q.get("id", "?")
    by_label = {_norm(c["label"]): c for c in selected}
    labels = [c["label"] for c in selected]
    text = " ".join([q.get("prompt", ""), q.get("answer_text", ""), q.get("explanation", "")])
    mentioned = mentioned_labels(text, labels)
    warnings: list[str] = []
    chosen: list[dict] = []
    for raw in q.get("concept_labels", []):
        c = by_label.get(_norm(raw))
        if c is None:
            warnings.append(f"{qid}: 出題範囲に無い概念「{raw}」を concept_labels に書いた（外した）")
            continue
        if c["label"] not in mentioned:
            similar = [m for m in mentioned
                       if m != c["label"] and (_norm(m) in _norm(c["label"]) or _norm(c["label"]) in _norm(m))
                       and all(x["label"] != m for x in chosen)]
            if len(similar) == 1:
                warnings.append(f"{qid}: 「{c['label']}」は問題に出てこないので「{similar[0]}」に差し替えた")
                c = by_label[_norm(similar[0])]
        if all(x["id"] != c["id"] for x in chosen):
            chosen.append(c)
    if not chosen and mentioned:
        chosen = [by_label[_norm(m)] for m in sorted(mentioned)]
        warnings.append(f"{qid}: concept_labels が使えないので、問題に出てくる概念を記録した")
    return [c["id"] for c in chosen], warnings


# ------------------------------------------------------------------ 正答の添字（#31）

def _answer_matches(q: dict) -> bool:
    ch = q.get("choices", [])
    a = q.get("answer", -1)
    return 0 <= a < len(ch) and ch[a].strip() == q.get("answer_text", "").strip()


def fix_answer_index(q: dict, before: dict | None) -> tuple[dict | None, list[str]]:
    """自己確認の後の 4 択問題で、answer を answer_text の位置に合わせる。LLM は呼ばない。

    自己確認で添字が 1 始まりに変わることがある（#31）。answer は採点に使うので、answer_text を正とする。
    - answer_text が choices のどれかと一致すれば、answer をその位置にする
    - どれとも一致しなければ、自己確認前の版（before）が一致していればそれに戻す。戻せなければ外す（None）
    戻り値: (使う問題 または None, 警告)
    """
    if q.get("type") != "mcq":
        return q, []
    qid = q.get("id", "?")
    warnings: list[str] = []
    ch = [c.strip() for c in q.get("choices", [])]
    text = q.get("answer_text", "").strip()
    if text in ch:
        at = ch.index(text)
        if at != q.get("answer"):
            warnings.append(f"{qid}: answer を answer_text に合わせて直した（{q.get('answer')} → {at}）")
            q = {**q, "answer": at}
    elif before is not None and before.get("type") == "mcq" and _answer_matches(before):
        warnings.append(f"{qid}: 自己確認後の answer_text が選択肢に無いので、自己確認前の問題に戻した")
        return before, warnings
    else:
        warnings.append(f"{qid}: answer_text が選択肢に無く、戻せる版も無いので、この問題を外した")
        return None, warnings
    for d in q.get("distractor_reasons", []):
        c = d.get("choice")
        if not isinstance(c, int) or not 0 <= c < len(ch):
            warnings.append(f"{qid}: distractor_reasons の choice={c} が choices の範囲外")
        elif c == q["answer"]:
            warnings.append(f"{qid}: distractor_reasons の choice={c} が正答と同じ")
    return q, warnings


# ------------------------------------------------------------------ 関係の向き（#32）

def check_relation_direction(q: dict, inner: list[dict], concepts: dict[str, dict]) -> list[str]:
    """関係を問う問題の relation_used が、材料の関係と同じ向きかを確かめる。警告だけ出す（LLM は呼ばない）。"""
    if q.get("aspect") != "relation":
        return []
    qid = q.get("id", "?")
    used = q.get("relation_used") or {}
    f, t, label = (_norm(used.get(k, "")) for k in ("from", "to", "label"))
    if not (f and t and label):
        return [f"{qid}: 関係を問う問題なのに relation_used が空"]
    rows = {(_norm(concepts[r["from"]]["label"]), _norm(concepts[r["to"]]["label"]), _norm(r["label"])) for r in inner}
    if (f, t, label) in rows:
        return []
    if (t, f, label) in rows:
        return [f"{qid}: 関係の向きが材料と逆（{used.get('from')} —{used.get('label')}→ {used.get('to')}）"]
    return [f"{qid}: relation_used が材料の関係のどれにも当たらない（{used.get('from')} —{used.get('label')}→ {used.get('to')}）"]


# ------------------------------------------------------------------ 誤答の差し替え（#34）

def check_replaced(q: dict, before: dict | None) -> list[str]:
    """replaced=true の誤答が、自己確認前と同じ文字列のままなら警告する（差し替えたと記録しただけ）。"""
    if q.get("type") != "mcq" or before is None:
        return []
    qid = q.get("id", "?")
    old, new = before.get("choices", []), q.get("choices", [])
    warnings = []
    for d in q.get("distractor_reasons", []):
        c = d.get("choice")
        if d.get("replaced") and isinstance(c, int) and 0 <= c < len(new) and c < len(old) \
                and _norm(old[c]) == _norm(new[c]):
            warnings.append(f"{qid}: 誤答 {c}「{new[c]}」は差し替えたと記録されたが、文字列が変わっていない")
    return warnings


# ------------------------------------------------------------------ 問題文（#33）

_TRAILING = "。．.?？!！ 　"


def check_prompt_style(q: dict, styles: list[dict]) -> list[str]:
    """問題文が出題者向けの文（question_style）を書き写していないか。警告だけ出す。"""
    qid = q.get("id", "?")
    prompt = q.get("prompt", "")
    warnings = []
    if prompt.rstrip(_TRAILING).endswith("を問う"):
        warnings.append(f"{qid}: 問題文が「を問う」で終わる（出題の方針を書き写している）")
    body = _norm(prompt)
    for v in styles:
        style = _norm(v.get("question_style", "")).rstrip(_TRAILING)
        if style and style in body:
            warnings.append(f"{qid}: 問題文に「{v['label']}」の出題の方針がそのまま入っている")
    used = (q.get("relation_used") or {}).get("label", "")
    if q.get("aspect") == "relation" and used and _norm(used) in body:
        warnings.append(f"{qid}: 問題文に正答のラベル名「{used}」が入っている")
    return warnings


# ------------------------------------------------------------------ 検証

def validate(questions: list[dict], confirmed_ids: list[str], pages: list[dict]) -> list[str]:
    """§7【6】自動検証。違反は警告文のリストで返す（落とさない）。"""
    allowed = set(confirmed_ids)
    page_keys = {(p["lecture"], p["page"]) for p in pages}
    warnings: list[str] = []
    for q in questions:
        qid = q.get("id", "?")
        outside = [c for c in q.get("concepts", []) if c not in allowed]
        if outside:
            warnings.append(f"{qid}: 確定した概念の外の id を使っている: {outside}")
        if not q.get("concepts"):
            warnings.append(f"{qid}: concepts が空")
        if q.get("type") == "mcq":
            ch = q.get("choices", [])
            if len(ch) != 4:
                warnings.append(f"{qid}: 選択肢が {len(ch)} 個（4 個であるべき）")
            if not (0 <= q.get("answer", -1) < len(ch)):
                warnings.append(f"{qid}: answer={q.get('answer')} が choices の範囲外")
            if len(set(ch)) != len(ch):
                warnings.append(f"{qid}: 選択肢に重複がある")
            if 0 <= q.get("answer", -1) < len(ch) and not _answer_matches(q):
                warnings.append(f"{qid}: choices[answer] が answer_text と一致しない（出題しない）")
        if not q.get("sources"):
            warnings.append(f"{qid}: sources が空")
        for s in q.get("sources", []):
            if (s["lecture"], s["page"]) not in page_keys:
                warnings.append(f"{qid}: 出典 第{s['lecture']}回 p.{s['page']} は存在しない")
        if not q.get("explanation"):
            warnings.append(f"{qid}: explanation が空")
    return warnings


# ------------------------------------------------------------------ 本体

def generate_quiz(client, model: str, graph: dict, vocab: dict, pages: list[dict],
                  confirmed_ids: list[str], n: int) -> dict:
    """戻り値: {"questions": [...], "warnings": [...], "meta": {...}}"""
    t0 = time.monotonic()
    calls = 0
    concepts, selected, inner, neighbors, needed, page_text, styles = build_materials(graph, vocab, pages, confirmed_ids)
    if not selected:
        raise ValueError("確定した概念が graph.json に見つからない")
    counts = split_counts(n, bool(inner))
    n_short = n // 5
    materials = materials_text(concepts, selected, inner, neighbors, needed, page_text, styles)

    # (1) 生成
    gen = call_json(client, model=model, system=GEN_SYSTEM, schema=GEN_SCHEMA,
                    user=gen_user(materials, n, counts, n_short))
    calls += 1
    questions = gen["questions"][:n]

    # (2) 誤答の自己確認
    ver = call_json(client, model=model, system=VERIFY_SYSTEM, schema=VERIFY_SCHEMA,
                    user=verify_user(questions, materials))
    calls += 1
    verified = ver["questions"]
    by_id = {q["id"]: q for q in verified}
    final = [by_id.get(q["id"], q) for q in questions]      # 返ってこなかった問題は生成時のまま

    # 自己確認で aspect が変わった問題を数える（#35）。直してよいのは example → definition だけ
    aspect_changes: list[str] = []
    for q, before in zip(final, questions):
        if q.get("aspect") != before.get("aspect"):
            aspect_changes.append(f"{q.get('id', '?')}: {before.get('aspect')} → {q.get('aspect')}")
            if (before.get("aspect"), q.get("aspect")) != ("example", "definition"):
                q["aspect"] = before.get("aspect")
                aspect_changes[-1] += "（認めていない変更なので元に戻した）"

    # 正答の添字を answer_text に合わせる（#31）。合わせられない問題は外す
    answer_warnings: list[str] = []
    fixed = []
    for q, before in zip(final, questions):
        q2, w = fix_answer_index(q, before)
        answer_warnings += w
        if q2 is not None and q2 is not before:
            answer_warnings += check_replaced(q2, before)
        if q2 is not None:
            fixed.append(q2)
    final = fixed

    # concept_labels を概念 id にする。UI・採点・results.json は従来どおり concepts（id）を使う
    resolve_warnings: list[str] = []
    for q in final:
        q["concepts"], w = resolve_concepts(q, selected)
        resolve_warnings += w
    n_replaced = sum(1 for q in final for d in q.get("distractor_reasons", []) if d.get("replaced"))

    # (3) 自動検証
    direction_warnings = [w for q in final for w in check_relation_direction(q, inner, concepts)]
    style_warnings = [w for q in final for w in check_prompt_style(q, styles)]
    warnings = (["aspect を自己確認で直した: " + c for c in aspect_changes] + answer_warnings + resolve_warnings + direction_warnings + style_warnings
                + validate(final, confirmed_ids, pages))
    # 補正の後でも正答の添字が answer_text と合わない 4 択は出さない（正しく答えても不正解になるため）
    final = [q for q in final if q.get("type") != "mcq" or _answer_matches(q)]
    meta = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "n_requested": n,
        "n_generated": len(final),
        "aspect_plan": counts,
        "aspect_actual": dict(Counter(q["aspect"] for q in final)),
        "aspect_changed_in_verify": len(aspect_changes),
        "type_actual": dict(Counter(q["type"] for q in final)),
        "concepts": confirmed_ids,
        "inner_relations": len(inner),
        "source_pages": len(needed),
        "materials_chars": len(materials),
        "distractors_replaced": n_replaced,
        "api_calls": calls,
        "seconds": round(time.monotonic() - t0, 1),
    }
    return {"questions": final, "warnings": warnings, "meta": meta}
