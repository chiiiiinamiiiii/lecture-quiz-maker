"""解答中は地図の説明に定義を出さない（#42）。説明の文字列は HTML として無害化する。"""
import json

from conftest import app, node_xy


# ------------------------------------------------------------------ Python 側: いつ隠すか、何を送るか

def test_answering_quiz_only_between_generate_and_grade():
    from ui.map_view import answering_quiz
    assert answering_quiz({}) is False                                     # クイズが無い
    assert answering_quiz({"quiz": {"questions": [1]}}) is True            # 解答中
    assert answering_quiz({"quiz": {"questions": [1]}, "graded": []}) is False   # 採点後


def test_hidden_definitions_are_not_sent_to_browser(graph, monkeypatch):
    import ui.map_component as mc
    sent = {}

    def fake_component(**kw):
        sent.update(kw["data"])
        return type("R", (), {"selection": None})()

    monkeypatch.setattr(mc, "_component", fake_component)
    mc.concept_map(graph, set(), [], 0, "c", hide_definitions=True)
    assert sent["hide_definitions"] is True
    assert all(n["definition"] == "" for n in sent["nodes"])
    mc.concept_map(graph, set(), [], 0, "c", hide_definitions=False)
    assert sent["hide_definitions"] is False
    assert any(n["definition"] for n in sent["nodes"])


# ------------------------------------------------------------------ ブラウザ側: 説明の中身

def _hover_tooltip(page, rank: int = 0) -> tuple[str, str, str]:
    """重み順 rank の概念にマウスを乗せ、(説明の文字, 説明の HTML, 概念 id) を返す。"""
    x, y, cid = node_xy(page, rank)
    page.mouse.move(x - 40, y - 40)
    page.mouse.move(x, y, steps=4)
    page.wait_for_timeout(400)
    return app(page, "a.tooltip.textContent"), app(page, "a.tooltip.innerHTML"), cid


def test_tooltip_shows_definition_without_quiz(page, graph):
    text, _, cid = _hover_tooltip(page)
    definition = next(c["definition"] for c in graph["concepts"] if c["id"] == cid)
    assert definition and definition[:20] in text


def test_tooltip_hides_definition_while_answering(page, graph):
    # 解答中の状態を、部品に渡すデータで作る（クイズを作るには API が要るため）
    app(page, "(a.update({...a.data, hide_definitions: true}), true)")
    text, _, cid = _hover_tooltip(page)
    definition = next(c["definition"] for c in graph["concepts"] if c["id"] == cid)
    label = next(c["label"] for c in graph["concepts"] if c["id"] == cid)
    assert label in text
    assert definition[:20] not in text
    assert "解答中は定義を隠しています" in text


def test_tooltip_escapes_html(page):
    cid = app(page, "a.ranked[0].id")
    evil = '<img src=x onerror="window.__qmXss=1">&<b>太字</b>'
    app(page, f"""(a.update({{...a.data, nodes: a.data.nodes.map(n => n.id === {json.dumps(cid)}
        ? {{...n, label: {json.dumps(evil)}, definition: {json.dumps(evil)}}} : n)}}), true)""")
    text, html, _ = _hover_tooltip(page)
    assert evil in text                                 # 文字としてそのまま見える
    assert "<img" not in html and "&lt;img" in html     # 要素としては入らない
    assert app(page, "window.__qmXss === undefined")
