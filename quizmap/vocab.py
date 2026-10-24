"""§4 語彙の導出。講義資料から、この授業に合った「概念の種類」と「関係の種類」を LLM に決めさせる。

derive_vocab.py（コマンド）とアプリの取り込み画面の両方から呼ぶ。
"""
from __future__ import annotations

import datetime as dt
import json

from quizmap.common import OTHER_LABEL, call_json, lecture_as_prompt_text

# 1 回の呼び出しに入れる文字数の上限。超えたら代表的な回を等間隔に選ぶ（§4「または代表的な数回分」）
MAX_CORPUS_CHARS = 150_000

SYSTEM = """あなたは大学の授業資料を読み、その授業に固有の「概念の種類」と「概念どうしの関係ラベル」を設計する専門家です。
出力は日本語で書きます。資料に実際に現れるものだけを挙げ、一般論で思いつくものを足してはいけません。"""

USER_TEMPLATE = """以下は「{course_hint}」という授業の資料です（{n_lectures}回分、ページ番号付き）。

この資料に出てくる概念どうしの関係を分類するラベルを 6〜12 個、提案してください。
各ラベルについて、
 (a) 名前（短い日本語。例: 提唱、対立、批判、前提）
 (b) 定義（どういうときにこのラベルを付けるか）
 (c) その関係をクイズにするときの問い方（例: 「AとBの違いを問う」「誰がAを提唱したかを問う」）
 (d) 資料中の実例（「A ⟷ B」のように概念名で）
 (e) from 側・to 側になる概念の種類（下で提案する種類の名前で。複数可）
を書いてください。

守ること:
- 資料に実際に現れる関係だけを挙げること。一般論で思いつくラベルを足さないこと
- 出現回数の多寡で絞らないこと。1 度しか出てこない関係でも、資料にあるなら挙げてよい
- 「{other}」というラベルは作らないこと（こちらで別途用意します）
- 提案した関係ラベルそれぞれについて、from 側・to 側になる概念の種類を、提案した種類の名前で書くこと。書けない場合は種類を追加すること
- この授業で繰り返し出てくる人名、学説・立場の名前、基本となる概念（価値・心の働きなど）が、どの種類にも入らないままにならないようにすること

同様に、概念の「種類」も 4〜8 個提案してください。各種類について、名前・定義・資料中の実例（3 つ以上）を書いてください。
- 種類は授業全体を通して使える分類の軸に限る。特定の回や特定の人物・話題でしか使わない種類は作らない
- 種類の名前を、その種類に属する概念の名前と同じにしない（例: 「X」という概念を入れるための「X」という種類は作らない）
- 各種類について、異なる単元（異なる人物・話題）から実例を 3 つ以上挙げられること。挙げられないものは種類ではなく概念として扱う

最後に、この授業の名前を資料から読み取って course に入れてください。

=== 資料 ===
{corpus}
"""

SCHEMA = {
    "type": "object",
    "properties": {
        "course": {"type": "string"},
        "kinds": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "definition": {"type": "string"},
                    "examples": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "definition", "examples"],
                "additionalProperties": False,
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "definition": {"type": "string"},
                    "question_style": {"type": "string"},
                    "example": {"type": "string"},
                    "from_kinds": {"type": "array", "items": {"type": "string"}},
                    "to_kinds": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "definition", "question_style", "example", "from_kinds", "to_kinds"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["course", "kinds", "relations"],
    "additionalProperties": False,
}


def choose_lectures(lectures, max_chars: int):
    """全体が上限に収まればそのまま。収まらなければ等間隔に代表的な回を選ぶ。"""
    sizes = [sum(len(p) for p in lec.pages) for lec in lectures]
    if sum(sizes) <= max_chars:
        return lectures
    avg = sum(sizes) / len(sizes)
    k = max(2, int(max_chars // avg))
    step = len(lectures) / k
    idx = sorted({int(i * step) for i in range(k)})
    return [lectures[i] for i in idx]


RETRY_TEMPLATE = """{base}

=== 前回のあなたの提案 ===
{previous}

=== 前回の提案の不足 ===
{problems}

上の不足を直した提案を、全体を省略せずに出し直してください。
from_kinds・to_kinds に書く種類が kinds に無ければ、kinds にその種類を追加してください。
"""


def kind_examples(kind: dict) -> list[str]:
    """kinds の実例。旧形式（example が文字列 1 つ）の vocab.json も読めるようにする。"""
    ex = kind.get("examples")
    if ex is None:
        ex = kind.get("example", "")
    if isinstance(ex, str):
        return [ex] if ex.strip() else []
    return [str(e) for e in ex if str(e).strip()]


def find_kind_gaps(vocab: dict) -> list[str]:
    """各関係の from_kinds・to_kinds が kinds にあるか（§16.1）。足りないものを文で返す。"""
    names = {k["name"] for k in vocab["kinds"]}
    problems = []
    for r in vocab["relations"]:
        if r["label"] == OTHER_LABEL:
            continue
        for side in ("from_kinds", "to_kinds"):
            given = r.get(side) or []
            if not given:
                problems.append(f"関係「{r['label']}」の {side} が空")
            missing = [k for k in given if k not in names]
            if missing:
                problems.append(f"関係「{r['label']}」の {side} にある {'・'.join(missing)} が kinds に無い")
    return problems


def find_kind_warnings(vocab: dict) -> list[str]:
    """種類が局所的・概念そのものになっていないかの目安（§16.2）。作り直しはせず警告だけ。"""
    warnings = []
    n = len(vocab["kinds"])
    if not 4 <= n <= 8:
        warnings.append(f"種類が {n} 個（指示は 4〜8 個）")
    for k in vocab["kinds"]:
        ex = kind_examples(k)
        if k["name"] in ex:
            warnings.append(f"種類「{k['name']}」の実例に、同じ名前の概念がある")
        if len(ex) < 3:
            warnings.append(f"種類「{k['name']}」の実例が {len(ex)} 個（指示は 3 個以上）")
    return warnings


def derive_vocab(client, model: str, lectures, course_hint: str, log=print) -> dict:
    """語彙を導出して dict で返す（保存は呼び出し側）。「その他」は必ず 1 つ付ける。"""
    chosen = choose_lectures(lectures, MAX_CORPUS_CHARS)
    if len(chosen) < len(lectures):
        log(f"資料が大きいので代表 {len(chosen)} 回を使う: {[l.number for l in chosen]}")
    corpus = "\n\n".join(lecture_as_prompt_text(lec) for lec in chosen)
    log(f"{len(chosen)} 回分 {len(corpus):,} 文字を {model} に渡して語彙を導出中…")
    user = USER_TEMPLATE.format(
        course_hint=course_hint, n_lectures=len(chosen), other=OTHER_LABEL, corpus=corpus,
    )
    vocab = call_json(client, model=model, system=SYSTEM, schema=SCHEMA, user=user, max_tokens=16000)

    # 関係の端になる種類が kinds に無ければ、不足を伝えて 1 回だけ作り直させる（人間の確認は挟まない）
    gaps = find_kind_gaps(vocab)
    retried = False
    if gaps:
        log("関係の端になる種類が足りない。1 回だけ作り直す: " + " / ".join(gaps))
        retried = True
        vocab = call_json(
            client, model=model, system=SYSTEM, schema=SCHEMA, max_tokens=16000,
            user=RETRY_TEMPLATE.format(
                base=user, previous=json.dumps(vocab, ensure_ascii=False, indent=1),
                problems="\n".join(f"- {g}" for g in gaps),
            ),
        )
        gaps = find_kind_gaps(vocab)
        if gaps:
            log("警告: 作り直しても関係の端になる種類が足りない: " + " / ".join(gaps))
    warnings = find_kind_warnings(vocab)
    for w in warnings:
        log("警告: " + w)

    # 「その他」を常に 1 つ用意する（§3）。LLM が作っていたら除いて付け直す
    vocab["relations"] = [r for r in vocab["relations"] if r["label"] != OTHER_LABEL]
    vocab["relations"].append({
        "label": OTHER_LABEL,
        "definition": "上のどのラベルにも当てはまらない関係",
        "question_style": "出題しない",
        "example": "",
        "from_kinds": [],
        "to_kinds": [],
    })
    vocab["meta"] = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "model": model,
        "lectures_used": [lec.number for lec in chosen],
        "retried_for_kind_gaps": retried,
        "kind_gaps": gaps,
        "kind_warnings": warnings,
    }
    log(f"種類 {len(vocab['kinds'])} 個: " + " / ".join(k["name"] for k in vocab["kinds"]))
    log(f"関係 {len(vocab['relations'])} 個: " + " / ".join(r["label"] for r in vocab["relations"]))
    return vocab
