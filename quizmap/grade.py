"""§8 採点と結果の保存。

- 4 択: 添字の一致で自動採点（1.0 / 0.0）
- 記述: LLM に採点させる（LLM-as-judge）。採点基準を明示して渡し、1.0 / 0.5 / 0.0 の 3 値と
  「どこが足りないか」の短いフィードバックを返させる
- 結果は data/results.json に追記する（§3 result: attempts と asked_concepts）
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from quizmap.common import call_json

JUDGE_SYSTEM = """あなたは大学の授業の記述問題を採点する採点者です。資料の記述と模範解答だけを基準にし、一般知識で甘くしたり厳しくしたりしないでください。出力は日本語です。"""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "grades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "number", "enum": [0, 0.5, 1]},
                    "feedback": {"type": "string"},
                },
                "required": ["id", "score", "feedback"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["grades"],
    "additionalProperties": False,
}


def judge_user(items: list[dict]) -> str:
    blocks = []
    for it in items:
        blocks.append(
            f"### 問題 {it['id']}\n"
            f"問題文: {it['prompt']}\n"
            f"模範解答: {it['answer_text']}\n"
            f"解説（正答の根拠）: {it['explanation']}\n"
            f"出典の本文: {it['source_text']}\n"
            f"学習者の解答: {it['user_answer'] or '（無回答）'}"
        )
    return """次の記述問題を採点してください。

採点基準（この 3 値のみ）:
- 1   : 模範解答と同じ内容を述べている。言い回しや語順の違い、表記ゆれは減点しない
- 0.5 : 核心には触れているが、模範解答の要素の一部が欠けている、または不正確な部分がある
        模範解答の上位概念・より一般的な語で答えた場合（例: 模範解答が個別の用語なのに、それを含むより広い語で答えた）も、核心の方向が合っていれば 0.5
- 0   : 核心を外している、無回答、または資料の記述と矛盾する

feedback には、正誤だけでなく「どこが足りないか／どこが違うか」を 1〜2 文で書いてください。
満点のときは、何が合っていたかを 1 文で書いてください。

""" + "\n\n".join(blocks)


def grade(client, model: str, questions: list[dict], answers: dict[str, object],
          page_text: dict[tuple[int, int], str]) -> tuple[dict[str, dict], int]:
    """answers: {question_id: 選んだ添字(int) または 記述(str)}
    戻り値: ({question_id: {"score", "correct", "feedback", "user_answer"}}, API 呼び出し回数)"""
    results: dict[str, dict] = {}
    short_items = []
    for q in questions:
        ua = answers.get(q["id"])
        if q["type"] == "mcq":
            correct = ua is not None and int(ua) == q["answer"]
            results[q["id"]] = {"score": 1.0 if correct else 0.0, "correct": correct,
                                "feedback": "", "user_answer": None if ua is None else int(ua)}
        else:
            text = (ua or "").strip()
            src = " / ".join(page_text.get((s["lecture"], s["page"]), "")[:1500] for s in q["sources"])
            short_items.append({"id": q["id"], "prompt": q["prompt"], "answer_text": q["answer_text"],
                                "explanation": q["explanation"], "source_text": src, "user_answer": text})
            results[q["id"]] = {"score": 0.0, "correct": False, "feedback": "", "user_answer": text}
    calls = 0
    if short_items:
        out = call_json(client, model=model, system=JUDGE_SYSTEM, schema=JUDGE_SCHEMA,
                        user=judge_user(short_items), max_tokens=4000)
        calls = 1
        for g in out["grades"]:
            if g["id"] in results:
                score = float(g["score"])
                results[g["id"]].update({"score": score, "correct": score >= 1.0, "feedback": g["feedback"]})
    return results, calls


# ------------------------------------------------------------------ 保存

def save_results(path: Path, questions: list[dict], graded: dict[str, dict], hints: dict[str, bool]) -> dict:
    """results.json に追記する。無ければ作る。既存の attempts は保持し、asked_concepts は和集合にする。"""
    data = {"attempts": [], "asked_concepts": []}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            data["attempts"] = list(loaded.get("attempts", []))
            data["asked_concepts"] = list(loaded.get("asked_concepts", []))
        except (ValueError, OSError):
            backup = path.with_suffix(".broken.json")
            path.rename(backup)      # 壊れていたら退避して作り直す（既存を上書きしない）
    now = dt.datetime.now().isoformat(timespec="seconds")
    asked = set(data["asked_concepts"])
    for q in questions:
        g = graded[q["id"]]
        data["attempts"].append({
            "question_id": q["id"],
            "concepts": q["concepts"],
            "score": g["score"],
            "correct": g["correct"],
            "answer": g["user_answer"],
            "hint_used": bool(hints.get(q["id"], False)),
            "at": now,
        })
        asked.update(q["concepts"])
    data["asked_concepts"] = sorted(asked)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return data
