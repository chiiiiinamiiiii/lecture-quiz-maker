"""いま選んでいるコースと、そのデータの読み込み。"""
import json
from pathlib import Path

import streamlit as st

from quizmap.courses import rename_course


def course_dir() -> Path:
    return Path(st.session_state["course_dir"])


def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


# 注意: st.cache_data は「_」で始まる引数をキャッシュのキーから外す。更新時刻は必ず普通の名前で渡す（#14）
@st.cache_data
def _load_graph(cdir: str, graph_mtime: float, results_mtime: float):
    d = Path(cdir)
    graph = json.loads((d / "graph.json").read_text(encoding="utf-8"))
    vocab = json.loads((d / "vocab.json").read_text(encoding="utf-8"))
    asked = set()
    if (d / "results.json").exists():
        asked = set(json.loads((d / "results.json").read_text(encoding="utf-8")).get("asked_concepts", []))
    return graph, vocab, asked


def load_graph():
    """(graph, vocab, 出題済みの概念 id) を返す。ファイルが更新されれば読み直す。"""
    d = course_dir()
    return _load_graph(str(d), _mtime(d / "graph.json"), _mtime(d / "results.json"))


@st.cache_data
def _load_pages(cdir: str, pages_mtime: float):
    return json.loads((Path(cdir) / "pages.json").read_text(encoding="utf-8"))


def load_pages():
    d = course_dir()
    return _load_pages(str(d), _mtime(d / "pages.json"))


def save_course_name(name: str) -> None:
    """コース名を course.json と graph.json に書き戻す（§16.3 の暫定対応）。"""
    rename_course(course_dir(), name)


SESSION_KEYS = ("confirmed_ids", "selection", "quiz", "graded", "hints", "results_saved", "grade_calls")


def clear_course_state() -> None:
    """コースを切り替えたときに、前のコースの選択・クイズ・解答の状態を捨てる。"""
    for k in SESSION_KEYS:
        st.session_state.pop(k, None)
    st.session_state["sel_version"] = st.session_state.get("sel_version", 0) + 1
    for k in [k for k in st.session_state if k.startswith(("ans-", "chk-", "hint-"))]:
        del st.session_state[k]
