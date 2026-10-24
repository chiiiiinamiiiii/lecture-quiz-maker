"""ブラウザ側で動かす補助スクリプト（Streamlit の外から Plotly を操作する）。"""
import hashlib
import json
from pathlib import Path

# ---- ブラウザ側の補助。本体（ui/static/parent_lib.js）は親ページに一度だけ組み込み、iframe からはそれを呼ぶだけにする。
#
# Streamlit は Plotly 図のスペックを要素 ID に含める（key_as_main_identity=False 固定）ため、
# 選択に応じて Python 側で図を変えると投げ縄の選択が消える。そこで図は選択に依存させず、
#  (1) 講義回ボタンの選択を投げ縄と同じ見た目（selectedpoints）にする
#  (2) 再実行で初期値に戻ってしまうツール（dragmode）の選択を維持する
#  (3) スペース＋2本指で平行移動（手のひら）
# を親ページの中で行う。iframe の中で登録したイベント処理は iframe が作り直されると呼ばれなくなるので、
# 処理を iframe 側に置いてはいけない（実際にそれで「操作中」の印が外れなくなる不具合が出た）。
PARENT_LIB = (Path(__file__).parent / "static" / "parent_lib.js").read_text(encoding="utf-8")
PARENT_LIB_VERSION = hashlib.sha1(PARENT_LIB.encode()).hexdigest()[:10]

_INSTALL = """
<script>
(function () {
  const P = window.parent;
  if (P.__qmLibVersion !== __VER__) {
    const s = P.document.createElement("script");
    s.textContent = __LIB__;
    P.document.head.appendChild(s);
    P.__qmLibVersion = __VER__;
  }
})();
</script>
""".replace("__VER__", json.dumps(PARENT_LIB_VERSION)).replace("__LIB__", json.dumps(PARENT_LIB))

# 再実行ごとに、選択状態（__PAYLOAD__）を親ページの本体に渡す
SELECT_JS = _INSTALL + """
<script>
(function () {
  const Q = __PAYLOAD__;
  const go = () => { const s = window.parent.__qmSelect; if (s) s.update(Q); };
  [0, 250, 700, 1500, 3000].forEach(ms => setTimeout(go, ms));
})();
</script>
"""

# 手のひらは本体に含まれる。地図の描画直後にも組み込みを呼んでおく
PAN_JS = _INSTALL


# 表示を初期位置に戻すボタン（iframe の中から親のグラフを relayout する）
HOME_JS = """
<script>
  // この iframe にフォーカスが残っていても、スペースキーを親ページ（手のひらツール）へ渡す
  ["keydown", "keyup"].forEach(t => document.addEventListener(t, e => {
    if (e.code !== "Space") return;
    e.preventDefault();
    window.parent.document.dispatchEvent(new KeyboardEvent(t, { code: "Space", key: " ", bubbles: true }));
  }));
</script>
<style>
  body { margin: 0; background: transparent; font-family: sans-serif; }
  button { width: 100%; height: 38px; border: 1px solid rgba(214,51,108,0.35); border-radius: 8px;
           background: #FFF5F8; color: #3B1F2B; font-size: 14px; cursor: pointer; }
  button:hover { border-color: #D6336C; color: #D6336C; }
</style>
<button onclick="(function(){ const w=window.parent, gd=w.document.querySelector('.js-plotly-plot');
  if(gd && w.Plotly) w.Plotly.relayout(gd, {'xaxis.range': __X__, 'yaxis.range': __Y__});
  document.activeElement && document.activeElement.blur();
  const a = w.document.activeElement; if (a && a.tagName === 'IFRAME') a.blur();   // フォーカスを親に返す
  w.focus(); })()">表示を元に戻す</button>
"""


