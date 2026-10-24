"""§7 クイズ生成。確定した概念 id と問題数から、その場で問題を作る。

流れ:  (1) 生成 → (2) 誤答の自己確認（理由を述べられない誤答は差し替え）→ (3) 自動検証
問い方はコードに書かない。vocab.json の各関係ラベルが持つ question_style をプロンプトに渡す。
LLM 呼び出しは quizmap.common.call_json（プロバイダ非依存）だけを使う。
"""
from __future__ import annotations

import datetime as dt
import os
import random
import re
import time
import unicodedata
from collections import Counter

from quizmap.common import OTHER_LABEL, call_json
from quizmap.vocab import detect_language, language_of, language_rule

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
                                "is_wrong": {"type": "boolean"},
                            },
                            "required": ["choice", "reason", "replaced", "is_wrong"],
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
資料に書かれていることだけを根拠にし、一般知識で補わないでください。{language_rule}"""

VERIFY_SYSTEM = """あなたは作成済みのクイズを点検する専門家です。資料に書かれていることだけを根拠に判断します。{language_rule}"""


# ------------------------------------------------------------------ 材料の組み立て

def split_counts(n: int, has_relations: bool, rng: random.Random | None = None) -> dict[str, int]:
    """目安の比率 関係4 / 具体例3 / 定義3 で n 問を配分する。関係が 1 本もなければ関係の枠を他に回す。

    端数の大きい枠から 1 問ずつ足す。端数が同じ枠どうしの順は乱数で決める（#24）。
    元の順のままだと、比率が同じ具体例と定義では同点の 1 問がいつも具体例に回る。
    rng はテストで種を固定するために渡せる。渡さなければ新しく作る。
    """
    rng = rng or random.Random()
    ratio = dict(ASPECT_RATIO)
    if not has_relations:
        ratio["relation"] = 0
    total = sum(ratio.values())
    counts = {k: n * v // total for k, v in ratio.items()}
    remainder = {k: n * v % total for k, v in ratio.items()}   # 整数で比べる（浮動小数の誤差で同点が崩れないように）
    keys = [k for k in ratio if ratio[k] > 0]
    rng.shuffle(keys)
    order = sorted(keys, key=lambda k: -remainder[k])          # 安定ソートなので、同点の順は shuffle の順
    for k in order:
        if sum(counts.values()) >= n:
            break
        counts[k] += 1
    return counts


def build_materials(graph: dict, vocab: dict, pages: list[dict], concept_ids: list[str]):
    concepts = {c["id"]: c for c in graph["concepts"]}
    selected = [concepts[i] for i in concept_ids if i in concepts]
    sel = set(c["id"] for c in selected)

    # 囲んだ概念どうしの関係（「その他」は問い方が無いので除く）。
    # 外部資料（Wikipedia）で補った関係（origin: external）は、答えが資料から導けないので使わない（docs/rag-design.md）
    inner = [r for r in graph["relations"]
             if r["from"] in sel and r["to"] in sel and r["label"] != OTHER_LABEL
             and r.get("origin", "slide") != "external"]
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
distractor_reasons の is_wrong は、最終的に choices に残したその誤答が、資料に照らして誤りだと言えるなら true、
言えない（実は正答にもなる、資料から判断できない）なら false にしてください。

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

# 自己確認の reason が「この誤答も当てはまる」と認めている言い方（is_wrong の無い古い出力のための予備）
_ADMITS_CORRECT = ("該当する", "正しい", "差し替える", "also correct", "also applies", "should be replaced")


def _admits_correct(d: dict) -> bool:
    """自己確認がその誤答を「誤りとは言えない」と判断したか。is_wrong があればそれを使い、無ければ reason の言い方で見る。"""
    if "is_wrong" in d:
        return d["is_wrong"] is False
    reason = d.get("reason", "").lower()
    return any(k.lower() in reason for k in _ADMITS_CORRECT)


def settle_replaced(q: dict, before: dict | None) -> tuple[dict | None, list[str]]:
    """replaced=true なのに自己確認前と同じ文字列のままの誤答を片付ける（#34）。LLM は呼ばない。

    - 自己確認がその誤答を誤りとは言えないと認めている → 正答が 2 つある問題なので外す（None）
    - そうでない（reason が誤りの理由になっている）→ replaced を false に直すだけ
    戻り値: (使う問題 または None, 警告)
    """
    if q.get("type") != "mcq" or before is None:
        return q, []
    qid = q.get("id", "?")
    old, new = before.get("choices", []), q.get("choices", [])
    warnings: list[str] = []
    reasons = []
    for d in q.get("distractor_reasons", []):
        c = d.get("choice")
        if d.get("replaced") and isinstance(c, int) and 0 <= c < len(new) and c < len(old) \
                and _norm(old[c]) == _norm(new[c]):
            if _admits_correct(d):
                warnings.append(f"{qid}: 誤答 {c}「{new[c]}」は自己確認で誤りと言えないとされたのに差し替わっていないので、この問題を外した")
                return None, warnings
            warnings.append(f"{qid}: 誤答 {c}「{new[c]}」は差し替えたと記録されたが文字列が同じで、理由は誤りの説明なので replaced を false に直した")
            d = {**d, "replaced": False}
        reasons.append(d)
    if warnings:
        q = {**q, "distractor_reasons": reasons}
    return q, warnings


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


# ------------------------------------------------------------------ 出題の検証（docs/rag-design.md、#78）
#
# 利用者が決めた方針: 答えは必ずアップロードした資料から導けること。外部の資料（Wikipedia）は
# ほかの選択肢が正解になっていないか、答えが一意かを確かめるためだけに使う（答えの根拠にはしない）。

UNIQUENESS_SYSTEM = """あなたは作成済みのクイズの正しさを点検する専門家です。
正答の根拠にしてよいのは授業の資料のページだけです。Wikipedia の記事は、正答以外の選択肢が正解と言えてしまわないか、答えが一つに決まるかを確かめるためだけに使います。{language_rule}"""

UNIQUENESS_USER = """次のクイズの各問題を点検してください。

=== 資料のページ（[第N回 pM] の形で示す。正答の根拠にしてよいのはこれだけ） ===
{pages}

=== Wikipedia の記事の冒頭（[W番号] の形で示す。正答の根拠にはしない） ===
{wiki}

=== クイズ ===
{questions}

各問題について 1 件ずつ答える。守ること:
- answer_supported: 正答（4 択は answer の選択肢、記述式は模範解答）が、その問題に付けた資料のページの記述から導けるなら true。
  導けるときは support に根拠のページを 1 つ以上挙げ、quote には、根拠のページの本文から 1 続きの文を、そのまま 80 字以内で写す。
  省略記号で継ぎ合わせない。support にはその文があるページを書く。
  資料に書かれておらず、一般知識や Wikipedia でしか確かめられないなら false にする
- distractors: 4 択の問題では、正答以外の選択肢ごとに 1 件。その選択肢が、資料のページまたは Wikipedia の記事に照らして、
  問題文の条件で正解と言えてしまうなら could_be_correct を true にし、reason に理由を、wiki に根拠にした記事の番号（無ければ 0）を書く。
  true のときは replacement に、資料のページに載っている語で、問題文の条件に当てはまらない別の選択肢を書く（見つからなければ空）。
  正解と言えないなら could_be_correct を false にし、replacement を空にする。記述式の問題は distractors を空配列にする
- other_answer: 記述式の問題で、模範解答とは別の答えが資料から正答として成り立つなら true、reason_other に理由を書く。4 択の問題は false
- same_as: このクイズの別の問題と、問うている内容と正答が同じ（重なっている）なら、その問題の id を書く。無ければ空
- conflicts_with: このクイズの別の問題と、同じことについて正答が食い違う（両方は正しくありえない）なら、その問題の id を書く。無ければ空
- 問題文・選択肢・正答は書き換えない。判定だけを返す
- 使わない欄は、文字列なら空、数なら 0 にする
"""

UNIQUENESS_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "answer_supported": {"type": "boolean"},
                    "support": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"lecture": {"type": "integer"}, "page": {"type": "integer"}},
                            "required": ["lecture", "page"],
                            "additionalProperties": False,
                        },
                    },
                    "quote": {"type": "string"},
                    "distractors": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "choice": {"type": "integer"},
                                "could_be_correct": {"type": "boolean"},
                                "reason": {"type": "string"},
                                "wiki": {"type": "integer"},
                                "replacement": {"type": "string"},
                            },
                            "required": ["choice", "could_be_correct", "reason", "wiki", "replacement"],
                            "additionalProperties": False,
                        },
                    },
                    "other_answer": {"type": "boolean"},
                    "reason_other": {"type": "string"},
                    "same_as": {"type": "string"},
                    "conflicts_with": {"type": "string"},
                },
                "required": ["id", "answer_supported", "support", "quote", "distractors", "other_answer", "reason_other",
                             "same_as", "conflicts_with"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["checks"],
    "additionalProperties": False,
}

UNIQUENESS_PAGES_PER_QUESTION = 5   # 1 問に付ける資料のページの上限（出典 + 索引の検索）
UNIQUENESS_SEARCH_K = 3
UNIQUENESS_PAGE_CHARS = 1500
UNIQUENESS_WIKI_CHARS = 600
_WS_RE = re.compile(r"\s+")
EXTERNAL_RELATION_REASON = "関係を問う問題に外部資料で補った関係を使っている"


def _flat(text: str) -> str:
    """引用の照合用。NFKC にして空白・改行をすべて除く。"""
    return _WS_RE.sub("", unicodedata.normalize("NFKC", text or ""))


def uses_external_relation(q: dict, graph: dict) -> bool:
    """関係を問う問題の relation_used が origin: external の関係か（答えが資料から導けないので出さない）。"""
    if q.get("aspect") != "relation":
        return False
    used = q.get("relation_used") or {}
    key = tuple(_norm(used.get(k, "")) for k in ("from", "to", "label"))
    labels = {c["id"]: c["label"] for c in graph["concepts"]}
    for r in graph["relations"]:
        if r.get("origin") != "external":
            continue
        a, b = _norm(labels.get(r["from"], "")), _norm(labels.get(r["to"], ""))
        if key in ((a, b, _norm(r["label"])), (b, a, _norm(r["label"]))):
            return True
    return False


def _on_slides(term: str, graph: dict, page_text: dict) -> bool:
    """差し替えの選択肢が資料に載っている語か（概念名、またはどこかのページ本文にそのまま出てくる）。"""
    t = _flat(term)
    if not t:
        return False
    if any(_flat(c["label"]) == t for c in graph["concepts"]):
        return True
    return any(t in _flat(text) for text in page_text.values())


_ELLIPSIS_RE = re.compile(r"(?:\.{2,}|…+|‥+)")
QUOTE_MIN_FRAGMENT = 6     # これより短い断片は照合に使わない（どこにでも出てくるため）


def match_quote(quote: str, keys: list[tuple[int, int]], page_text: dict) -> list[tuple[int, int]] | None:
    """正答の根拠の引用を、見せたページすべてと照合する（#91）。

    引用を「...」「…」「……」で断片に分け、6 字以上の断片がすべて、見せたページのどれかの本文に
    そのまま（_flat で空白・改行を除いて）あれば通す。断片はページをまたいでもよい。
    戻り値: 断片が見つかったページ（keys の順）、通らなければ None
    """
    frags = [f for f in (_flat(x) for x in _ELLIPSIS_RE.split(quote or "")) if len(f) >= QUOTE_MIN_FRAGMENT]
    if not frags:
        return None
    texts = {k: _flat(page_text[k]) for k in keys if k in page_text}
    hit: list[tuple[int, int]] = []
    for f in frags:
        where = [k for k, t in texts.items() if f in t]
        if not where:
            return None
        hit += [k for k in where if k not in hit]
    return [k for k in keys if k in hit]


def concept_terms(texts: list[str], graph: dict) -> list[str]:
    """Wikipedia で引く語（#93）。選択肢・正答の文に出てくる概念の label（graph の概念）だけ。

    文そのものは引かない。概念に当たらない選択肢からは何も引かない。長い label の中の短い label は数えない
    （mentioned_labels）。読みの括弧などの正規化は rag.Wiki.lookup 側で行う。出てきた順に並べる。
    """
    labels = [c["label"] for c in graph["concepts"]]
    out: list[str] = []
    for text in texts:
        found = mentioned_labels(text, labels)
        pos = {l: _norm(text).find(_norm(l)) for l in found}
        out += [l for l in sorted(found, key=lambda l: (pos[l], l)) if l not in out]
    return out


def verify_uniqueness(client, model, questions, graph, pages, index, wiki) -> tuple[list, list]:
    """出題の検証。全問をまとめて 1 回の LLM 呼び出しで判定する。

    1. 正答がスライドの記述から導けるか。LLM が導けないと判定したら外す。
       根拠の引用は見せたページすべてと断片ごとに照合し（match_quote）、照合に落ちただけでは外さない（記録に残す）
    2. 正答以外の選択肢が、スライドか Wikipedia で正解と言えてしまうなら、資料に載っている語に差し替える。
       差し替えられなければ外す
    3. 記述式で、模範解答以外に資料から別の正答が成り立つなら外す
    4. 同じ範囲の別の問題と重なる問題は、後のほうを外す。正答が食い違う問題は記録だけ残す（どちらが誤りかは決めない）
    Wikipedia で引くのは、選択肢・正答に出てくる概念の label だけ（concept_terms）。
    関係を問う問題で origin: external の関係を使ったものは、LLM に見せる前に外す。
    index（SlideIndex）と wiki（Wiki）は None でもよい（使える層だけで判定する）。
    戻り値: (残す問題, 検証の記録)。記録は問題ごとに 1 件:
      {"id", "action": "kept" / "replaced" / "dropped", "reasons": [...],
       "pages": [{"lecture", "page", "score", "via": "source" / "search"}],   # LLM に見せたスライドのページ
       "wiki": [{"term", "title", "url"}],                                   # 引いた概念名（見つからなければ title・url は空）
       "support": [{"lecture", "page"}], "quote",                            # 正答の根拠（照合で見つかったページ）
       "support_claimed": [{"lecture", "page"}], "quote_check": "ok" / "failed" / "",
       "same_as", "conflicts_with",
       "distractors": [{"choice", "text", "could_be_correct", "reason", "wiki_url", "replacement", "replaced"}],
       "other_answer", "reason_other"}
    """
    page_text = {(p["lecture"], p["page"]): p["text"] for p in pages}
    records: list[dict] = []
    targets: list[dict] = []

    def new_record(q: dict) -> dict:
        return {"id": q.get("id", "?"), "action": "kept", "reasons": [], "pages": [], "wiki": [],
                "support": [], "support_claimed": [], "quote": "", "quote_check": "", "distractors": [],
                "other_answer": False, "reason_other": "", "same_as": "", "conflicts_with": ""}

    for q in questions:
        if uses_external_relation(q, graph):
            rec = new_record(q)
            rec.update(action="dropped", reasons=[EXTERNAL_RELATION_REASON])
            records.append(rec)
        else:
            targets.append(q)
    if not targets:
        return [], records
    by_id = {q["id"]: new_record(q) for q in targets}

    # 問題ごとの資料のページ: 出典のページ + 索引で問題文と正答から探したページ
    for q in targets:
        rec = by_id[q["id"]]
        seen: set[tuple[int, int]] = set()
        for s in q.get("sources", []):
            k = (s["lecture"], s["page"])
            if k in page_text and k not in seen:
                seen.add(k)
                rec["pages"].append({"lecture": k[0], "page": k[1], "score": None, "via": "source"})
        if index is not None:
            for h in index.search(client, f"{q.get('prompt', '')} {q.get('answer_text', '')}", k=UNIQUENESS_SEARCH_K):
                k = (h["lecture"], h["page"])
                if k in page_text and k not in seen:
                    seen.add(k)
                    rec["pages"].append({"lecture": k[0], "page": k[1], "score": h.get("score"), "via": "search"})
        rec["pages"] = rec["pages"][:UNIQUENESS_PAGES_PER_QUESTION]

    # 正答と各選択肢に出てくる概念の名前を Wikipedia で引く（#93。Wiki がキャッシュするので、同じ語は二度問い合わせない）
    articles: list[dict] = []                         # [W番号 - 1] → 記事
    found: dict[str, dict | None] = {}
    q_wiki: dict[str, list[int]] = {}
    for q in targets:
        terms = concept_terms(list(q.get("choices") or []) or [q.get("answer_text", "")], graph)
        nums: list[int] = []
        for term in (t.strip() for t in terms):
            if not term or wiki is None:
                continue
            if term not in found:
                found[term] = wiki.lookup(term)
            art = found[term]
            by_id[q["id"]]["wiki"].append({"term": term, "title": (art or {}).get("title", ""),
                                           "url": (art or {}).get("url", "")})
            if not art:
                continue
            urls = [a.get("url") for a in articles]
            if art.get("url") not in urls:
                articles.append(art)
                urls.append(art.get("url"))
            n = urls.index(art.get("url")) + 1
            if n not in nums:
                nums.append(n)
        q_wiki[q["id"]] = nums

    def keys_of(qid: str) -> list[tuple[int, int]]:
        return [(p["lecture"], p["page"]) for p in by_id[qid]["pages"]]

    all_pages = sorted({k for q in targets for k in keys_of(q["id"])})
    pages_block = "\n\n".join(f"[第{l}回 p{n}]\n{page_text[(l, n)][:UNIQUENESS_PAGE_CHARS]}" for l, n in all_pages) or "（なし）"
    wiki_block = "\n\n".join(
        f"[W{i + 1}] {a.get('title', '')}\n{unicodedata.normalize('NFKC', a.get('extract', ''))[:UNIQUENESS_WIKI_CHARS]}"
        for i, a in enumerate(articles)) or "（なし）"

    def describe(q: dict) -> str:
        lines = [f"- id: {q['id']}（{'4 択' if q.get('type') == 'mcq' else '記述式'}）", f"  問題文: {q.get('prompt', '')}"]
        if q.get("type") == "mcq":
            for i, c in enumerate(q.get("choices", [])):
                lines.append(f"  選択肢 {i}: {c}{'（正答）' if i == q.get('answer') else ''}")
        else:
            lines.append(f"  模範解答: {q.get('answer_text', '')}")
        lines.append("  資料のページ: " + ("、".join(f"第{l}回 p{n}" for l, n in keys_of(q["id"])) or "（なし）"))
        lines.append("  記事: " + ("、".join(f"W{n}" for n in q_wiki[q["id"]]) or "（なし）"))
        return "\n".join(lines)

    lang = detect_language([q.get("prompt", "") + q.get("answer_text", "") for q in targets])
    data = call_json(
        client, model=model, system=UNIQUENESS_SYSTEM.replace("{language_rule}", language_rule(lang)),
        schema=UNIQUENESS_SCHEMA,
        user=UNIQUENESS_USER.format(pages=pages_block, wiki=wiki_block,
                                    questions="\n".join(describe(q) for q in targets)),
    )
    checks: dict[str, dict] = {}
    for c in data["checks"]:
        checks.setdefault(c["id"], c)

    kept: list[dict] = []
    for q in targets:
        qid = q["id"]
        rec = by_id[qid]
        records.append(rec)
        c = checks.get(qid)
        if c is None:
            rec["reasons"].append("検証の答えが返らなかった（そのまま残した）")
            kept.append(q)
            continue
        allowed = set(keys_of(qid))
        claimed = [(s["lecture"], s["page"]) for s in c["support"] if (s["lecture"], s["page"]) in allowed]
        rec.update(support_claimed=[{"lecture": s["lecture"], "page": s["page"]} for s in c["support"]],
                   quote=c["quote"], other_answer=c["other_answer"], reason_other=c["reason_other"],
                   same_as=c["same_as"].strip(), conflicts_with=c["conflicts_with"].strip())
        if not c["answer_supported"]:
            rec["action"] = "dropped"
            rec["reasons"].append("正答が資料の記述から導けない")
            continue
        # 引用の照合（#91）。見せたページすべてと断片ごとに照合し、見つかったページを support にする。
        # 照合に落ちても、LLM が導けると判定した問題は外さない（引用の書き方の違いで外しすぎたため）
        found_at = match_quote(c["quote"], keys_of(qid), page_text)
        if found_at:
            rec.update(support=[{"lecture": l, "page": n} for l, n in found_at], quote_check="ok")
        else:
            rec.update(support=[{"lecture": l, "page": n} for l, n in claimed], quote_check="failed")
            rec["reasons"].append("引用の照合に失敗（残した）")
        # 同じ範囲の別の問題と重なるなら、後のほうを外す。食い違いは記録だけ（どちらが誤りかは決めない）
        earlier = [x["id"] for x in kept]
        if rec["same_as"] and rec["same_as"] != qid and rec["same_as"] in earlier:
            rec["action"] = "dropped"
            rec["reasons"].append(f"{rec['same_as']} と重なる問題")
            continue
        if rec["conflicts_with"] and rec["conflicts_with"] != qid and \
                rec["conflicts_with"] in {x.get("id") for x in targets}:
            rec["reasons"].append(f"{rec['conflicts_with']} と正答が食い違う（残した）")
        if q.get("type") != "mcq":
            if c["other_answer"]:
                rec["action"] = "dropped"
                rec["reasons"].append(f"模範解答のほかに資料から別の正答が成り立つ: {c['reason_other']}")
                continue
            kept.append(q)
            continue

        choices = list(q.get("choices", []))
        ok = True
        for d in c["distractors"]:
            i = d["choice"]
            if not 0 <= i < len(choices) or i == q.get("answer"):
                continue
            url = articles[d["wiki"] - 1].get("url", "") if 1 <= d["wiki"] <= len(articles) else ""
            entry = {"choice": i, "text": choices[i], "could_be_correct": d["could_be_correct"], "reason": d["reason"],
                     "wiki_url": url, "replacement": d["replacement"], "replaced": False}
            rec["distractors"].append(entry)
            if not d["could_be_correct"]:
                continue
            new = d["replacement"].strip()
            if new and _on_slides(new, graph, page_text) and all(_flat(new) != _flat(x) for x in choices):
                rec["reasons"].append(f"選択肢 {i}「{choices[i]}」も正解と言えるので「{new}」に差し替えた: {d['reason']}")
                choices[i] = new
                entry["replaced"] = True
                rec["action"] = "replaced"
            else:
                rec["reasons"].append(f"選択肢 {i}「{choices[i]}」も正解と言え、資料に載っている差し替え先が無い: {d['reason']}")
                ok = False
        if not ok:
            rec["action"] = "dropped"
            continue
        kept.append({**q, "choices": choices} if rec["action"] == "replaced" else q)
    order = {q.get("id"): i for i, q in enumerate(questions)}
    records.sort(key=lambda r: order.get(r["id"], len(order)))
    return kept, records


# ------------------------------------------------------------------ 本体

# 出題の検証（verify_uniqueness）を通すか。generate_quiz の verify を省いたときの既定。QUIZMAP_RAG=off で通さない
UNIQUENESS_DEFAULT = os.environ.get("QUIZMAP_RAG", "on").strip().lower() != "off"

def generate_quiz(client, model: str, graph: dict, vocab: dict, pages: list[dict],
                  confirmed_ids: list[str], n: int, index=None, wiki=None, verify: bool | None = None) -> dict:
    """戻り値: {"questions": [...], "warnings": [...], "meta": {...}}

    index（rag.SlideIndex）と wiki（rag.Wiki）は省略できる。verify が真なら、生成と自己確認の後に
    verify_uniqueness を 1 回通す（index・wiki が None でも、スライドの本文だけで検証する）。
    verify を省くと UNIQUENESS_DEFAULT（QUIZMAP_RAG が off でなければ真）。
    """
    if verify is None:
        verify = UNIQUENESS_DEFAULT
    t0 = time.monotonic()
    calls = 0
    concepts, selected, inner, neighbors, needed, page_text, styles = build_materials(graph, vocab, pages, confirmed_ids)
    if not selected:
        raise ValueError("確定した概念が graph.json に見つからない")
    counts = split_counts(n, bool(inner))
    n_short = n // 5
    materials = materials_text(concepts, selected, inner, neighbors, needed, page_text, styles)

    # (1) 生成
    lang = language_of(vocab, (p["text"] for p in pages))
    gen = call_json(client, model=model, system=GEN_SYSTEM.replace("{language_rule}", language_rule(lang)), schema=GEN_SCHEMA,
                    user=gen_user(materials, n, counts, n_short))
    calls += 1
    questions = gen["questions"][:n]

    # (2) 誤答の自己確認
    ver = call_json(client, model=model, system=VERIFY_SYSTEM.replace("{language_rule}", language_rule(lang)), schema=VERIFY_SCHEMA,
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
            q2, w = settle_replaced(q2, before)
            answer_warnings += w
        if q2 is not None:
            fixed.append(q2)
    final = fixed

    # concept_labels を概念 id にする。UI・採点・results.json は従来どおり concepts（id）を使う
    resolve_warnings: list[str] = []
    for q in final:
        q["concepts"], w = resolve_concepts(q, selected)
        resolve_warnings += w
    n_replaced = sum(1 for q in final for d in q.get("distractor_reasons", []) if d.get("replaced"))

    # 出題の検証（docs/rag-design.md）。通す前の問題も評価のために meta に残す
    before_verify = [dict(q) for q in final] if verify else None
    uniqueness = None
    if verify and final:
        final, records = verify_uniqueness(client, model, final, graph, pages, index, wiki)
        calls += 1
        uniqueness = {
            "records": records,
            "dropped": sum(1 for r in records if r["action"] == "dropped"),
            "replaced": sum(1 for r in records if r["action"] == "replaced"),
            "quote_check_failed": sum(1 for r in records if r.get("quote_check") == "failed"),
            "conflicts": sum(1 for r in records if r.get("conflicts_with")),
            "index": index is not None,
            "wiki": wiki is not None,
            # 索引の検索（埋め込み）は、LLM に見せた問題ごとに 1 回
            "searches": sum(1 for r in records if r["reasons"] != [EXTERNAL_RELATION_REASON]) if index is not None else 0,
        }

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
        "language": lang,
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
    if verify:
        meta["questions_before_verify"] = before_verify
        meta["uniqueness"] = uniqueness
    return {"questions": final, "warnings": warnings, "meta": meta}
