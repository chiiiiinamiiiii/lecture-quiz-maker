"""地図部品（ui/static/map.js）を Streamlit から使う。

Streamlit の components v2 は、キーを付けるとデータが変わっても要素を作り直さない。
そのため表示位置・ツール・選択はブラウザ側に残り、Python には選択が変わったときだけ届く。
"""
from pathlib import Path
from urllib.parse import unquote, urlparse

import streamlit as st

from quizmap.common import OTHER_LABEL, RELATED_LABEL
from ui.i18n import map_texts, t
from ui.layout import compute_weights, layout_relation, layout_timeline

_STATIC = Path(__file__).parent / "static"
_component = st.components.v2.component(
    "quizmap_map",
    js=(_STATIC / "map.js").read_text(encoding="utf-8"),
    css=(_STATIC / "map.css").read_text(encoding="utf-8"),
)


def _positions(graph) -> dict:
    """2 つの配置の座標。画面は下向きが y の正なので、上向きの座標は反転して渡す。"""
    force = {k: [x, -y] for k, (x, y) in layout_relation(graph).items()}
    tl = layout_timeline(graph)
    lectures = sorted({n for c in graph["concepts"] for n in c["lectures"]})
    span = max(1, max(lectures) - min(lectures))
    rows = max(1.0, max(abs(y) for _, y in tl.values()) * 2)
    # 横は講義回、縦は同じ回の中での並び。縦横の比が極端にならないよう縦を詰める
    sy = 0.9 * span / max(rows, span * 0.6)
    timeline = {k: [x, -y * sy] for k, (x, y) in tl.items()}
    return {"force": force, "timeline": timeline, "timeline_axis": {str(n): n for n in lectures}}


def rel_label(label: str) -> str:
    """関係ラベルを画面に出す形にする。「その他」「関連」は資料の言語によらず固定の名前なので、画面の言語で出す。"""
    if label == OTHER_LABEL:
        return t("rel_other")
    if label == RELATED_LABEL:
        return t("rel_related")
    return label


def origin_of(rel: dict) -> str:
    """関係の出どころ: "slide"（資料の記述）/ "external"（Wikipedia）/ "cooccurrence"（同じスライドに出るだけ、#127）。
    origin の無い関係（RAG より前の graph）は slide。"""
    return rel.get("origin") or "slide"


def is_external(rel: dict) -> bool:
    """外部資料（Wikipedia）で補った関係か。origin の無い関係（RAG より前の graph）はスライド由来とみなす。"""
    return rel.get("origin", "slide") == "external"


def article_title(rel: dict) -> str:
    """外部資料の出典の記事名。evidence_url（…/wiki/<記事名>）から読む。読めなければ URL をそのまま返す。"""
    url = rel.get("evidence_url") or ""
    path = urlparse(url).path
    if "/wiki/" in path:
        return unquote(path.split("/wiki/", 1)[1]).replace("_", " ")
    return url


def concept_map(graph, asked: set[str], selected: list[str], sel_version: int, course_id: str,
                height: int | str = 760, hide_definitions: bool = False) -> dict | None:
    """地図を描き、ブラウザ側の選択 {"ids", "via_edge", "version", "n"} を返す（まだ無ければ None）。

    hide_definitions: 解答中は True。定義をブラウザに送らず、説明にも出さない（#42。定義問題の答えになる）。
    """
    weights = compute_weights(graph)
    nodes = [{
        "id": c["id"], "label": c["label"], "definition": "" if hide_definitions else c["definition"],
        "lectures": sorted(c["lectures"]), "asked": c["id"] in asked, "weight": round(weights[c["id"]], 4),
    } for c in graph["concepts"]]
    edges = [{
        "id": f"e{i}", "s": r["from"], "t": r["to"], "label": rel_label(r["label"]),
        "lecture": r["source"]["lecture"], "page": r["source"]["page"],
        "external": is_external(r), "article": article_title(r) if is_external(r) else "",
        "cooccur": origin_of(r) == "cooccurrence",
    } for i, r in enumerate(graph["relations"])]
    data = {
        "course_id": course_id,
        "nodes": nodes,
        "edges": edges,
        "lectures": sorted({n for c in graph["concepts"] for n in c["lectures"]}),
        "positions": _positions(graph),
        "selected": selected,
        "sel_version": sel_version,
        "layout": "force",
        "texts": map_texts(),
        "hide_definitions": hide_definitions,
    }
    result = _component(key=f"map-{course_id}", data=data, default={"selection": None},
                        on_selection_change=lambda: None, height=height)
    return result.selection
