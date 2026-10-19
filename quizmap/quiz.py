"""§7 クイズ生成。確定した概念 id と問題数から、その場で問題を作る。

流れ:  (1) 生成 → (2) 誤答の自己確認（理由を述べられない誤答は差し替え）→ (3) 自動検証
問い方はコードに書かない。vocab.json の各関係ラベルが持つ question_style をプロンプトに渡す。
LLM 呼び出しは quizmap.common.call_json（プロバイダ非依存）だけを使う。
"""
from __future__ import annotations

import datetime as dt
import time
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
    "concepts": {"type": "array", "items": {"type": "string"}},
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
                     "explanation", "concepts", "sources"]

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
    parts = ["## 出題範囲の概念（問題の根拠にしてよいのはこれだけ。concepts にはこの id だけを書く）"]
    for c in selected:
        src = "、".join(f"第{s['lecture']}回p.{s['page']}" for s in c["sources"])
        parts.append(f"- {c['id']}: {c['label']}（{c['kind']}）: {c['definition']} [{src}]")
    parts.append("\n## 出題範囲の概念どうしの関係（関係を問う問題はここから作る）")
    if inner:
        for r in inner:
            parts.append(f"- {concepts[r['from']]['label']} —{r['label']}→ {concepts[r['to']]['label']}"
                         f" [第{r['source']['lecture']}回p.{r['source']['page']}]")
    else:
        parts.append("- （なし）")
    parts.append("\n## 関係ラベルごとの問い方（この授業の語彙。関係を問う問題は必ずこれに沿う）")
    for v in styles:
        parts.append(f"- {v['label']}: 定義「{v['definition']}」／問い方「{v['question_style']}」／例: {v['example']}")
    parts.append("\n## 誤答に使ってよい隣接概念（地図上で隣り合う概念。紛らわしい誤答にする。concepts には書かない）")
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

守ること:
- 関係を問う問題は「関係ラベルごとの問い方」に沿って問う。問い方はそこに書かれたものだけを使う
- 具体例を問う問題は、資料に出てくる具体例と概念の対応を問う
- 定義を問う問題は、資料の記述に基づく定義を問う
- 誤答には「誤答に使ってよい隣接概念」や出題範囲内の別の概念を使い、紛らわしくする
- 問題文・選択肢・解説は資料に書かれていることだけを根拠にする
- concepts には根拠となる概念の id（出題範囲の概念の id のみ）を 1 つ以上書く
- sources には根拠となるページ（lecture, page）を 1 つ以上書く。資料本文の見出し [第N回 p.M] から取る
- explanation には正答の理由を 1〜3 文で書く
- id は q_01, q_02, … とする

{materials}
"""


def verify_user(questions: list[dict], materials: str) -> str:
    import json
    return f"""次のクイズの各 4 択問題について、各誤答（正答以外の選択肢）がなぜ誤りかを、資料の記述に基づいて 1 行で述べてください。
資料に基づいて誤りだと述べられない誤答（実は正しい、資料から判断できない）は、別の誤答に差し替えて replaced を true にしてください。
差し替えた場合も、その新しい誤答がなぜ誤りかを述べてください。
正答そのものが資料と食い違っていれば、問題文・選択肢・answer・explanation を資料に合わせて直してください。
記述式の問題は distractor_reasons を空配列にして、そのまま返してください。
問題の数・id・aspect・type は変えないでください。

## クイズ
{json.dumps({"questions": questions}, ensure_ascii=False, indent=1)}

{materials}
"""


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
    n_replaced = sum(1 for q in final for d in q.get("distractor_reasons", []) if d.get("replaced"))

    # (3) 自動検証
    warnings = validate(final, confirmed_ids, pages)
    meta = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "n_requested": n,
        "n_generated": len(final),
        "aspect_plan": counts,
        "aspect_actual": dict(Counter(q["aspect"] for q in final)),
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
