// QUIZ MAP のブラウザ側の補助（親ページに一度だけ組み込む）。
// iframe の中で登録したイベント処理は、その iframe が作り直された時点で呼ばれなくなる（ブラウザの仕様）。
// Streamlit の components.html は再実行で作り直されることがあるので、処理の本体はすべて親ページで定義する。
(function () {
  if (window.__qmTeardown) window.__qmTeardown();          // 古い版が入っていれば外して入れ直す
  const doc = document, P = () => window.Plotly, gdOf = () => doc.querySelector(".js-plotly-plot");
  const off = [];
  const on = (target, type, fn, opts) => { target.addEventListener(type, fn, opts); off.push(() => target.removeEventListener(type, fn, opts)); };

  // ---------------------------------------------------------------- 選択の反映
  // 図は選択に依存させない（Streamlit は図が変わると投げ縄の選択を消す）。講義回ボタンを含む選択は
  // ここで selectedpoints として反映し、投げ縄で囲んだのと同じ見た目にする。
  // 再実行で初期値に戻ってしまうツール（dragmode）も、利用者が選んだものに戻す。
  let busy = false;
  function same(a, b) {
    if (a == null && b == null) return true;
    if (a == null || b == null || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }
  function desiredPoints(gd, d) {
    const idx = [], sp = [];
    gd.data.forEach((t, i) => {
      if (!Array.isArray(t.customdata)) return;
      const pts = [];
      t.customdata.forEach((cd, j) => {
        if (cd[1] === "node") { if (d.sel.has(cd[0])) pts.push(j); }
        else if (cd[1] === "edge") { const e = d.edges[cd[0]]; if (e && d.sel.has(e[0]) && d.sel.has(e[1])) pts.push(j); }
      });
      idx.push(i); sp.push(pts);
    });
    return { idx, sp };
  }
  function applySelection(gd) {
    const d = gd.__qmDesired;
    if (!d || busy || !P()) return;
    // 投げ縄の形が残っていると Plotly が描画のたびに選択を再計算して上書きするので、
    // Python 側が結果を受け取った後は形を消す
    if ((gd.layout.selections || []).length) {
      P().relayout(gd, { selections: [] }).then(() => applySelection(gd));
      return;
    }
    if (d.sel.size === 0) {
      if (gd.data.some(t => Array.isArray(t.customdata) && t.selectedpoints != null)) P().restyle(gd, { selectedpoints: null });
      return;
    }
    const { idx, sp } = desiredPoints(gd, d);
    if (idx.some((i, k) => !same(gd.data[i].selectedpoints, sp[k]))) P().restyle(gd, { selectedpoints: sp }, idx);
  }
  function keepDragmode(gd) {
    if (window.__qmDragmode && gd._fullLayout.dragmode !== window.__qmDragmode) P().relayout(gd, { dragmode: window.__qmDragmode });
  }
  function installOn(gd) {
    if (gd.__qmInstalled) return;
    gd.__qmInstalled = true;
    gd.on("plotly_relayout", ev => { if (ev && ev.dragmode && !window.__qmTempPan && !window.__qmHandActive) window.__qmDragmode = ev.dragmode; });
    gd.on("plotly_selecting", () => { gd.__qmDesired = null; });   // 新しく囲み始めたら古い選択を押し付けない
    gd.on("plotly_selected", () => { gd.__qmDesired = null; });
    gd.on("plotly_afterplot", () => applySelection(gd));            // Streamlit が描き直したら戻す
  }
  on(doc, "mousedown", e => { if (e.target.closest && e.target.closest(".js-plotly-plot")) busy = true; }, true);
  on(doc, "mouseup", () => { setTimeout(() => { busy = false; const gd = gdOf(); if (gd) applySelection(gd); }, 300); }, true);

  window.__qmSelect = {
    update(Q) {
      const gd = gdOf();
      if (!gd || !gd.data || !gd._fullLayout || !P()) return false;
      installOn(gd);
      keepDragmode(gd);
      if (gd.__qmLastNonce !== Q.nonce) {             // この再実行ぶんにつき 1 回
        gd.__qmLastNonce = Q.nonce;
        gd.__qmDesired = { sel: new Set(Q.selected), edges: Q.edges };
        applySelection(gd);
      }
      return true;
    },
  };

  // ---------------------------------------------------------------- 手のひら（スペース＋2本指）
  // スペースを押している間は、2 本指の移動とドラッグが平行移動。スペースなしの 2 本指はズームのまま。
  if (!doc.getElementById("qm-pan-style")) {
    const style = doc.createElement("style");
    style.id = "qm-pan-style";
    style.textContent = ".quizmap-pan, .quizmap-pan .nsewdrag, .quizmap-pan .drag { cursor: grab !important; }";
    doc.head.appendChild(style);
  }
  let space = false, savedDragmode = null, acc = { dx: 0, dy: 0 }, raf = null;
  const typing = () => {
    const a = doc.activeElement;
    return a && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
  };
  function setPan(onoff) {
    space = onoff;
    window.__qmHandActive = onoff;
    doc.querySelectorAll(".js-plotly-plot").forEach(g => g.classList.toggle("quizmap-pan", onoff));
    const gd = gdOf();
    if (!gd || !gd._fullLayout || !P()) return;
    window.__qmTempPan = true;          // スペース中の一時的な pan を「利用者のツール選択」と記録しない
    if (onoff) { savedDragmode = gd._fullLayout.dragmode; P().relayout(gd, { dragmode: "pan" }); }
    else if (savedDragmode) { P().relayout(gd, { dragmode: savedDragmode }); savedDragmode = null; }
    window.__qmTempPan = false;
  }
  on(doc, "keydown", e => {
    if (e.code !== "Space" || typing()) return;
    e.preventDefault();                                   // ページのスクロールを止める
    if (space) return;
    // ボタンやピルにフォーカスが残っていると、スペースを離したときにそれが「押され」てしまう
    const a = doc.activeElement;
    if (a && a !== doc.body && a.blur) a.blur();
    setPan(true);
  }, true);
  on(doc, "keyup", e => {
    if (e.code !== "Space") return;
    if (space) { e.preventDefault(); setPan(false); }    // フォーカス中のボタンを押させない
  }, true);
  on(window, "blur", () => { if (space) setPan(false); });
  function flush() {
    raf = null;
    const gd = gdOf();
    if (!gd || !gd._fullLayout || !P()) return;
    const xa = gd._fullLayout.xaxis, ya = gd._fullLayout.yaxis;
    const dx = acc.dx * (xa.range[1] - xa.range[0]) / xa._length;
    const dy = acc.dy * (ya.range[1] - ya.range[0]) / ya._length;
    acc = { dx: 0, dy: 0 };
    P().relayout(gd, { "xaxis.range": [xa.range[0] + dx, xa.range[1] + dx], "yaxis.range": [ya.range[0] - dy, ya.range[1] - dy] });
  }
  on(doc, "wheel", e => {
    if (!space) return;                                   // スペースなし → Plotly のズームに任せる
    if (!(e.target.closest && e.target.closest(".js-plotly-plot"))) return;
    e.preventDefault();
    e.stopPropagation();
    acc.dx += e.deltaX; acc.dy += e.deltaY;               // 1 フレームにまとめてから描き直す
    if (!raf) raf = requestAnimationFrame(flush);
  }, { capture: true, passive: false });

  window.__qmTeardown = () => { off.forEach(f => f()); if (space) setPan(false); };
})();
