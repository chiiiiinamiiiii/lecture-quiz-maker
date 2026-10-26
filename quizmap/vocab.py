"""§4 語彙の導出。講義資料から、この授業に合った「概念の種類」と「関係の種類」を LLM に決めさせる。

derive_vocab.py（コマンド）とアプリの取り込み画面の両方から呼ぶ。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from quizmap.common import OTHER_LABEL, call_json, lecture_as_prompt_text

# 1 回の呼び出しに入れる文字数の上限。超えたら代表的な回を等間隔に選ぶ（§4「または代表的な数回分」）
MAX_CORPUS_CHARS = 150_000

# ------------------------------------------------------------------ 資料の言語（#26）
# LLM が作る中身（語彙・概念・関係・定義・問題・採点）は資料の言語で書かせる。画面の言語（ui/i18n.py）とは別。
# プロンプトの指示文は日本語のまま、出力の言語だけを差し込む。

LANGUAGE_NAMES = {"ja": "日本語", "en": "英語（English）"}
_JA_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")      # かな・漢字
_LATIN_RE = re.compile(r"[A-Za-z]")
# かな・漢字 ÷（かな・漢字 ＋ ラテン文字）がこれ以上なら日本語。
# 2026-10 の実データ: 倫理学（日本語）0.88〜0.95、NLP（英語）0.003。英単語は 1 語で数文字になるので低めに置く
JA_THRESHOLD = 0.3


def detect_language(texts) -> str:
    """資料の本文から言語を決める（ja / en）。文字が無ければ ja。"""
    ja = latin = 0
    for t in texts:
        ja += len(_JA_RE.findall(t))
        latin += len(_LATIN_RE.findall(t))
    if ja + latin == 0:
        return "ja"
    return "ja" if ja / (ja + latin) >= JA_THRESHOLD else "en"


def language_of(vocab: dict, texts=()) -> str:
    """そのコースの言語。vocab.json の meta.language を使い、無ければ（#26 より前の vocab）本文から決める。"""
    lang = (vocab.get("meta") or {}).get("language")
    if lang in LANGUAGE_NAMES:
        return lang
    return detect_language(texts)


def language_name(lang: str) -> str:
    return LANGUAGE_NAMES.get(lang, LANGUAGE_NAMES["ja"])


def language_rule(lang: str) -> str:
    """システムプロンプトに差し込む、出力の言語の指示。"""
    return (f"出力（名前・定義・説明・問題文など、あなたが書く中身）はすべて{language_name(lang)}で書きます。"
            "資料の用語は、資料での表記のまま使います（訳さない）。")


# 関係ラベルの型（#143）。ラベルの名前は資料に合わせて LLM が決め、型はラベルの組み合わせの偏りを確かめるためだけに使う
CORE_STRUCTURES = ("一種・下位概念", "部分と全体", "対立・対比", "具体例", "前提・根拠", "用いる・適用")
STRUCTURES = CORE_STRUCTURES + ("定義・言い換え", "人物の主張・批判", "出典", "その他の構造", "例題の形")
MIN_CORE_STRUCTURES = 3      # ラベルの組み合わせに含まれてほしい、構造の型の数の下限

SYSTEM = """あなたは大学の授業資料を読み、その授業に固有の「概念の種類」と「概念どうしの関係ラベル」を設計する専門家です。
{language_rule}資料に実際に現れるものだけを挙げ、一般論で思いつくものを足してはいけません。"""

USER_TEMPLATE = """以下は「{course_hint}」という授業の資料です（{n_lectures}回分、ページ番号付き）。

この資料に出てくる概念どうしの関係を分類するラベルを 6〜12 個、提案してください。
各ラベルについて、
 (a) 名前（短い{language}。日本語の資料なら例えば 提唱、対立、批判、前提）
 (b) 定義（どういうときにこのラベルを付けるか。from が何で to が何かを、「from」「to」という語を使って必ず書くこと。
     例: from が手段、to が目的。下位の概念のラベルなら from が下位か上位か、部分と全体のラベルなら from が部分か全体か、
     具体例のラベルなら from が具体例か抽象的な概念かを書く）
 (c) その関係をクイズにするときの問い方（例: 「AとBの違いを問う」「誰がAを提唱したかを問う」）
 (d) 資料中の実例（「A → B」のように概念名で。from → to の順。定義に書いた向きと同じ向きで）
 (e) from 側・to 側になる概念の種類（下で提案する種類の名前で。複数可）。
     from 側・to 側の種類は、そのラベルの実例として資料に出てくる関係の両端の種類をすべて含めること。
     人物・思想家を主語とするラベル（例えば 提唱・批判）の相手には、概念・立場・学説の種類をすべて含めること。
     対立・対比のラベルには、資料で対比される両端の種類をすべて含めること（学説どうしだけでなく、概念どうし、制度と意思など）
 (f) そのラベルの関係の型 structure（次から 1 つ）: {structures}
     どれにも当たらず、特定の例題・練習問題の形（関数が現象を記述する、関数を最大化する など）だけを表すなら「例題の形」
を書いてください。

ラベルの組み合わせについて:
- ラベルは、資料のいろいろな回に共通する、概念どうしの構造を表すものを中心にする。名前は資料に合わせる
- 資料に次の型の関係が出てくるなら、その型のラベルを必ず含める: {core_structures}
- 特定の例題の形だけのラベルにしない。ラベルの少なくとも半分は、両端が同じ種類の概念どうしでも使えるもの
  （from_kinds と to_kinds に同じ種類がある）にする

守ること:
- 資料に実際に現れる関係だけを挙げること。一般論で思いつくラベルを足さないこと
- 出現回数の多寡で絞らないこと。1 度しか出てこない関係でも、資料にあるなら挙げてよい
- 「{other}」というラベルは作らないこと（こちらで別途用意します）
- 提案した関係ラベルそれぞれについて、from 側・to 側になる概念の種類を、提案した種類の名前で書くこと。書けない場合は種類を追加すること
- この授業で繰り返し出てくる人名、学説・立場の名前、基本となる概念（価値・心の働きなど）が、どの種類にも入らないままにならないようにすること
- 資料に、ある概念を別の概念の具体例として挙げる記述や、ある概念を別の語で言い換える記述があれば、それを表すラベルも検討すること。ただし数合わせでラベルを足さないこと

同様に、概念の「種類」も 4〜8 個提案してください。各種類について、名前・定義・資料中の実例（3 つ以上）を書いてください。
- 実例は概念の名前で書く。数式・等式、変数や記号、数値の例、ソフトやウェブツールの名前は実例にしない
- 種類は授業全体を通して使える分類の軸に限る。特定の回や特定の人物・話題でしか使わない種類は作らない
- 種類は、資料のすべての回にわたって、概念を重ならずに分けられるものにする。1 つの種類に概念の大半が入らないようにする。
  分野の名前（例えば「統計」）を種類にして、ほかの分野（微積分・線形代数など）の概念まで入れない。
  数学の資料なら、例えば「数学的対象（関数・行列など）」「定理・手法」「量・指標」「応用の題材」のように、概念の役割で分ける
- 種類を減らしてまとめすぎない。人物・出典の文献以外の内容の種類を {min_content} つ以上作り、役割の違うものは同じ種類にしない
  （例えば、学説・主義・立場の名前と、その中で論じられる個々の概念（価値・心の働き・原則など）と、法律・制度・規範は別の種類）
- 人物が唱えた原則・学説・考え方は、法律・制度・社会の規範の種類に入れず、考え方・学説の種類に入れる
- 各種類について、その種類に属する概念が、資料が出典・参考・引用元として挙げた文献・作品（本・論文・報告書・記事・作品など）であれば cited_work を true、そうでなければ false にする。
  文献・作品と、授業の内容の概念・人物を同じ種類に混ぜない
- cited_work を true にした種類の概念（本・文献・作品）は、地図の概念にしない（関係の抽出の前に落とす）。
  そのため、関係ラベルの from_kinds・to_kinds に cited_work の種類を入れない。例を表すラベルは、
  抽象的な概念 → その具体的な事例・制度・現象の関係として、概念の種類どうしで定義する
- cited_work を true にした種類の definition には、「出典・参考として名前が挙がるだけの本。授業で内容が論じられる作品や、本の題名と同じ名前の学説・考え方は含めない」という範囲を書く
- 各種類について、その種類に属する概念が人物（人名）であれば person を true、そうでなければ false にする
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
                    "cited_work": {"type": "boolean"},
                    "person": {"type": "boolean"},
                },
                "required": ["name", "definition", "examples", "cited_work", "person"],
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
                    "structure": {"type": "string", "enum": list(STRUCTURES)},
                },
                "required": ["label", "definition", "question_style", "example", "from_kinds", "to_kinds",
                             "structure"],
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
    warnings += find_narrow_person_targets(vocab)
    return warnings


def find_narrow_person_targets(vocab: dict) -> list[str]:
    """人物の種類が from にあるラベルで、to の人物以外の種類が 1 つ以下のもの（#74）。警告だけ。

    人物を主語とするラベルの相手が 1 つの種類に限られると、意味の上では合う関係まで、
    抽出の端の種類の照合（check_end_kinds）で「その他」になる。人物かどうかは種類の person の旗で見る
    （種類の名前では決めない。person の無い古い vocab.json では何も出さない）。
    """
    persons = {k["name"] for k in vocab["kinds"] if k.get("person") is True}
    if not persons:
        return []
    warnings = []
    for r in vocab["relations"]:
        if r["label"] == OTHER_LABEL or not persons & set(r.get("from_kinds") or []):
            continue
        others = [k for k in (r.get("to_kinds") or []) if k not in persons]
        if len(others) <= 1:
            warnings.append(f"関係「{r['label']}」は人物の種類が from にあるのに、to の人物以外の種類が "
                            f"{len(others)} 個（{'・'.join(others) or 'なし'}）だけ")
    return warnings


def widen_person_targets(vocab: dict) -> list[str]:
    """人物を主語とするラベルの相手の種類を、コードの規則で広げる（#74）。vocab を書き換え、記録の文を返す。LLM は呼ばない。

    プロンプトで指示しても、語彙の to_kinds が狭いままのことがあった（人物 → 概念 の関係が「その他」になる）。
    規則: from_kinds に人物の種類（person が true）があるラベルで、to_kinds の「人物以外かつ cited_work 以外」の種類が
    1 つ以下なら、「人物以外かつ cited_work 以外」の種類をすべて to_kinds に足す。
    cited_work の種類は関係の抽出の前に落とすので足さない。人物の種類は、もともと to_kinds にあれば残す（足しも消しもしない）。
    種類は名前ではなく旗で決めるので、分野に依らない。person の無い古い vocab.json では何もしない。
    """
    persons = {k["name"] for k in vocab["kinds"] if k.get("person") is True}
    if not persons:
        return []
    content = [k["name"] for k in vocab["kinds"] if k.get("person") is not True and k.get("cited_work") is not True]
    notes = []
    for r in vocab["relations"]:
        if r["label"] == OTHER_LABEL or not persons & set(r.get("from_kinds") or []):
            continue
        to_kinds = list(r.get("to_kinds") or [])
        if len([k for k in to_kinds if k in content]) > 1:
            continue
        added = [k for k in content if k not in to_kinds]
        if not added:
            continue
        r["to_kinds"] = to_kinds + added
        notes.append(f"関係「{r['label']}」の to_kinds に {'・'.join(added)} を足した（人物を主語とするラベルの相手が狭かったため）")
    return notes


def concept_label_kinds(vocab: dict) -> dict[str, list[str]]:
    """概念系の種類どうしのラベルと、その端に許す種類（#119）。vocab は書き換えない。LLM は呼ばない。

    語彙の from_kinds・to_kinds が狭く、意味の上では合う関係まで端の種類の照合で「その他」になっていた
    （#98 で人物を主語とするラベルに行ったのと同じ問題）。
    規則: from_kinds・to_kinds がどちらも空でなく、どちらにも人物（person）・出典の文献（cited_work）の種類が無いラベルは、
    両端に「人物以外かつ cited_work 以外」の種類をすべて許す。種類は名前ではなく旗で決めるので分野に依らない。
    旗の無い古い vocab.json では何も返さない。
    """
    if not any("person" in k or "cited_work" in k for k in vocab["kinds"]):
        return {}
    content = [k["name"] for k in vocab["kinds"] if k.get("person") is not True and k.get("cited_work") is not True]
    out = {}
    for r in vocab["relations"]:
        fk, tk = set(r.get("from_kinds") or []), set(r.get("to_kinds") or [])
        if r["label"] == OTHER_LABEL or not fk or not tk or not (fk | tk) <= set(content):
            continue
        out[r["label"]] = content
    return out


def find_structure_gaps(vocab: dict) -> list[str]:
    """ラベルの組み合わせが例題向けに偏っていないか（#143）。作り直しの不足として文で返す。LLM は呼ばない。

    - 構造の型（CORE_STRUCTURES）のラベルが MIN_CORE_STRUCTURES 種類より少ない
    - 「例題の形」のラベルが半分を超える
    - 両端に同じ種類を持てるラベル（from_kinds と to_kinds が重なる）が半分に満たない
    型（structure）の無い古い形の答えでは、どれも確かめない。
    """
    rels = [r for r in vocab["relations"] if r["label"] != OTHER_LABEL]
    if not rels or not all("structure" in r for r in rels):
        return []
    problems = []
    core = {r["structure"] for r in rels if r["structure"] in CORE_STRUCTURES}
    if len(core) < MIN_CORE_STRUCTURES:
        problems.append(f"概念どうしの構造を表す型のラベルが {len(core)} 種類（{'・'.join(sorted(core)) or 'なし'}）だけ。"
                        f"資料に出てくるなら、{'・'.join(c for c in CORE_STRUCTURES if c not in core)} の型のラベルも含める")
    example = [r["label"] for r in rels if r["structure"] == "例題の形"]
    if len(example) * 2 > len(rels):
        problems.append(f"例題の形だけのラベルが {len(example)} / {len(rels)} 個（{'・'.join(example)}）。"
                        "回をまたいで使える、概念どうしの構造のラベルにする")
    same = [r for r in rels if set(r.get("from_kinds") or []) & set(r.get("to_kinds") or [])]
    if len(same) * 2 < len(rels):
        problems.append(f"両端が同じ種類の概念どうしで使えるラベルが {len(same)} / {len(rels)} 個。少なくとも半分にする")
    return problems


_FROM_RE = re.compile(r"(?<![A-Za-z])from(?![A-Za-z])", re.I)
_TO_RE = re.compile(r"(?<![A-Za-z])to(?![A-Za-z])", re.I)


def find_direction_gaps(vocab: dict) -> list[str]:
    """定義に from と to の両方が書かれていないラベル（#148）。作り直しの不足として文で返す。LLM は呼ばない。
    向きの書かれていない定義（「ある上位の概念に含まれる、より具体的な概念」）では、抽出で逆向きに付くことがあった。"""
    return [f"関係「{r['label']}」の定義に、from と to がそれぞれ何かが書かれていない（例: from が下位の概念、to が上位の概念）"
            for r in vocab["relations"]
            if r["label"] != OTHER_LABEL and not (_FROM_RE.search(r.get("definition", ""))
                                                   and _TO_RE.search(r.get("definition", "")))]


MIN_CONTENT_KINDS = int(os.environ.get("QUIZMAP_MIN_CONTENT_KINDS", "3"))   # 人物・出典以外の内容の種類の数の下限（#150）
MIN_KIND_EXAMPLES = 3


def find_content_kind_gaps(vocab: dict) -> list[str]:
    """内容の種類（person・cited_work 以外）が MIN_CONTENT_KINDS より少ない、または実例の足りない内容の種類があるか（#150）。
    作り直しの不足として文で返す。旗の無い古い vocab.json では確かめない。"""
    if not any("person" in k or "cited_work" in k for k in vocab["kinds"]):
        return []
    content = [k for k in vocab["kinds"] if k.get("person") is not True and k.get("cited_work") is not True]
    problems = []
    if len(content) < MIN_CONTENT_KINDS:
        problems.append(f"人物・出典の文献以外の内容の種類が {len(content)} 個（{'・'.join(k['name'] for k in content) or 'なし'}）"
                        f"だけ。役割の違う概念を分けて {MIN_CONTENT_KINDS} 個以上にする")
    for k in content:
        if len(kind_examples(k)) < MIN_KIND_EXAMPLES:
            problems.append(f"内容の種類「{k['name']}」の実例が {len(kind_examples(k))} 個。{MIN_KIND_EXAMPLES} 個以上挙げる")
    return problems


def find_cited_ends(vocab: dict) -> list[str]:
    """from_kinds か to_kinds が cited_work の種類だけのラベル（#141）。作り直しの不足として文で返す。"""
    cited = {k["name"] for k in vocab["kinds"] if k.get("cited_work") is True}
    problems = []
    for r in vocab["relations"]:
        if r["label"] == OTHER_LABEL or not cited:
            continue
        for side in ("from_kinds", "to_kinds"):
            given = r.get(side) or []
            if given and set(given) <= cited:
                problems.append(f"関係「{r['label']}」の {side} が出典の文献の種類（{'・'.join(given)}）だけ。"
                                f"出典の文献は概念にしないので、このラベルは使えない。概念の種類どうしの関係として定義し直す")
    return problems


def drop_cited_ends(vocab: dict) -> list[str]:
    """関係ラベルの from_kinds・to_kinds から cited_work の種類を除く（#141）。vocab を書き換え、記録の文を返す。LLM は呼ばない。

    cited_work の種類の概念は関係の抽出の前に落とすので、その種類を端にするラベルは使われない
    （端が本の種類だけのラベルは 1 本も付かなかった）。除いて端が空になった側は、人物以外かつ cited_work 以外の
    種類をすべて入れる（例を表すラベルの to が本の種類だけだったものを、概念の事例にも使えるようにする）。
    cited_work の旗の無い古い vocab.json では何もしない。
    """
    cited = {k["name"] for k in vocab["kinds"] if k.get("cited_work") is True}
    if not cited:
        return []
    content = [k["name"] for k in vocab["kinds"] if k.get("person") is not True and k.get("cited_work") is not True]
    notes, kept = [], []
    for r in vocab["relations"]:
        if r["label"] == OTHER_LABEL:
            kept.append(r)
            continue
        removed = {side: [k for k in (r.get(side) or []) if k in cited] for side in ("from_kinds", "to_kinds")}
        if not any(removed.values()):
            kept.append(r)
            continue
        rest = {side: [k for k in (r.get(side) or []) if k not in cited] for side in ("from_kinds", "to_kinds")}
        if not content and (not rest["from_kinds"] or not rest["to_kinds"]):
            notes.append(f"関係「{r['label']}」を除いた（端が出典の文献の種類だけで、ほかに広げる種類も無いため）")
            continue
        names = '・'.join(sorted(set(removed['from_kinds'] + removed['to_kinds'])))
        for side in ("from_kinds", "to_kinds"):
            if not rest[side]:
                rest[side] = list(content)
                notes.append(f"関係「{r['label']}」の {side} が出典の文献の種類 {names} だけだったので、"
                             f"{'・'.join(content)} にした")
        r.update(rest)
        notes.append(f"関係「{r['label']}」の端から出典の文献の種類 {names} を除いた")
        kept.append(r)
    vocab["relations"] = kept
    return notes


def derive_vocab(client, model: str, lectures, course_hint: str, log=print) -> dict:
    """語彙を導出して dict で返す（保存は呼び出し側）。「その他」は必ず 1 つ付ける。"""
    chosen = choose_lectures(lectures, MAX_CORPUS_CHARS)
    if len(chosen) < len(lectures):
        log(f"資料が大きいので代表 {len(chosen)} 回を使う: {[l.number for l in chosen]}")
    corpus = "\n\n".join(lecture_as_prompt_text(lec) for lec in chosen)
    lang = detect_language(page for lec in lectures for page in lec.pages)
    log(f"資料の言語: {lang}（語彙・概念・問題はこの言語で書かせる）")
    system = SYSTEM.replace("{language_rule}", language_rule(lang))
    log(f"{len(chosen)} 回分 {len(corpus):,} 文字を {model} に渡して語彙を導出中…")
    user = USER_TEMPLATE.format(
        course_hint=course_hint, n_lectures=len(chosen), other=OTHER_LABEL, corpus=corpus,
        language=language_name(lang), min_content=MIN_CONTENT_KINDS, structures="・".join(STRUCTURES), core_structures="・".join(CORE_STRUCTURES),
    )
    vocab = call_json(client, model=model, system=system, schema=SCHEMA, user=user, max_tokens=16000)

    # 関係の端になる種類が kinds に無い、端が出典の文献の種類だけ（#141）、ラベルの組み合わせが例題向けに偏る（#143）、
    # 定義に from・to の向きが無い（#148）なら、
    # 不足を伝えて 1 回だけ作り直させる
    # （人間の確認は挟まない）
    gaps = find_kind_gaps(vocab) + find_cited_ends(vocab) + find_structure_gaps(vocab) + find_direction_gaps(vocab) \
        + find_content_kind_gaps(vocab)
    retried = False
    if gaps:
        log("関係の端になる種類が足りない。1 回だけ作り直す: " + " / ".join(gaps))
        retried = True
        vocab = call_json(
            client, model=model, system=system, schema=SCHEMA, max_tokens=16000,
            user=RETRY_TEMPLATE.format(
                base=user, previous=json.dumps(vocab, ensure_ascii=False, indent=1),
                problems="\n".join(f"- {g}" for g in gaps),
            ),
        )
        gaps = find_kind_gaps(vocab) + find_cited_ends(vocab) + find_structure_gaps(vocab) + find_direction_gaps(vocab) \
        + find_content_kind_gaps(vocab)
        if gaps:
            log("警告: 作り直しても関係の端になる種類が足りない: " + " / ".join(gaps))
    cited_ends = drop_cited_ends(vocab)
    widened = widen_person_targets(vocab)
    for w in cited_ends + widened:
        log("語彙の後処理: " + w)
    warnings = widened + find_kind_warnings(vocab)
    for w in warnings[len(widened):]:
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
        "language": lang,
        "retried_for_kind_gaps": retried,
        "kind_gaps": gaps,
        "kind_warnings": warnings,
        "widened_to_kinds": widened,
        "dropped_cited_ends": cited_ends,
    }
    log(f"種類 {len(vocab['kinds'])} 個: " + " / ".join(k["name"] for k in vocab["kinds"]))
    log(f"関係 {len(vocab['relations'])} 個: " + " / ".join(r["label"] for r in vocab["relations"]))
    return vocab
