// QUIZ MAP の地図部品（Streamlit components v2）。
//
// Streamlit はデータが変わるたびにこの関数を呼ぶが、要素（parentElement）は作り直さない。
// そこで最初の呼び出しで MapApp を作って parentElement に保持し、以後は update(data) だけ呼ぶ。
// 表示位置・ツール・選択はブラウザ側で持ち続け、選択が変わったときだけ Python に送る。
//
// 操作:
//   ドラッグ（手のひらツール）/ スペース＋ドラッグ / スペース＋2本指 … 移動
//   2本指の上下 / ピンチ / ＋−ボタン … ズーム
//   投げ縄・矩形 … 範囲選択。Shift で追加、⌘（または Option）で除外、修飾なしで置き換え
//   点のクリック … その点を選択（同じ修飾キーの規則）。何もない所のクリックでは選択は変わらない
//   ダブルクリック … 拡大だけ（選択は変えない）
//   講義回ボタン … その回に出てくる概念を選択（同じ修飾キーの規則）
//   H / L / M … 手のひら（最初のツール）/ 投げ縄 / 矩形、0 … 全体表示、Esc … 選択を解除

const CLICK_DELAY = 250;
const FIT_NODE_R = 10;     // 全体表示の余白に足す点の半径の目安（px）
const FIT_LABEL_H = 18;    // 点の下に描く名前 1 行の高さ（px）
const AXIS_ROW = 25;       // 「講義回」の配置の列の見出しの行の高さ（札 17px＋間 8px）   // 点のクリックを反映するまで待つ時間（ms）。この間のダブルクリックは拡大だけにする

const ICONS = {
  hand: '<svg viewBox="0 0 24 24"><path d="M8 11V5.5a1.5 1.5 0 0 1 3 0V10m0-5.5V4a1.5 1.5 0 0 1 3 0v6m0-4.5a1.5 1.5 0 0 1 3 0V12m0-3.5a1.5 1.5 0 0 1 3 0V15a6 6 0 0 1-6 6h-1.5a6 6 0 0 1-4.9-2.6L4.3 14a1.5 1.5 0 0 1 2.4-1.8L8 14"/></svg>',
  lasso: '<svg viewBox="0 0 24 24"><path d="M7 15.5C4.6 14.3 3 12.4 3 10.2 3 6.2 7.5 3 13 3s10 3.2 10 7.2-4.5 7.2-10 7.2c-1.3 0-2.6-.2-3.7-.5"/><circle cx="7.5" cy="17.5" r="2"/><path d="M7.5 19.5c0 1.2-.8 2-2 2.5"/></svg>',
  rect: '<svg viewBox="0 0 24 24"><rect x="4" y="5" width="16" height="14" rx="1" stroke-dasharray="3 2.5"/></svg>',
  fit: '<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
  plus: '<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  minus: '<svg viewBox="0 0 24 24"><path d="M5 12h14"/></svg>',
  clear: '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>',
};

const COLORS = {
  unasked: [196, 192, 222],   // 未出題: 明るく彩度の低い藤色
  asked: [16, 170, 156],      // 出題済み: 明度を落とした彩度の高いティール
  accent: [255, 122, 172],    // 選択・強調: ピンク
  edge: [170, 170, 196],
  text: [226, 224, 236],
};
// LLM が資料から作った文字列（概念名・定義・関係の名前）を HTML に差し込む前に無害化する（#42）
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const EXTERNAL_DASH = [4, 4];   // 外部資料で補った関係の辺の点線（画面の px）
const rgba = (c, a) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

function pointInPolygon(x, y, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

class MapApp {
  constructor(root, setStateValue) {
    this.setStateValue = setStateValue;
    this.root = root;
    root.innerHTML = `
      <div class="qm-map" tabindex="0">
        <canvas></canvas>
        <div class="qm-toolbar qm-glass">
          <button data-tool="hand"></button><button data-tool="lasso"></button><button data-tool="rect"></button>
          <span class="qm-sep"></span>
          <button data-act="zoomin"></button><button data-act="zoomout"></button><button data-act="fit"></button>
          <span class="qm-sep qm-sep-break"></span><span class="qm-break"></span>
          <div class="qm-seg"><button data-layout="force"></button><button data-layout="timeline"></button></div>
          <span class="qm-sep"></span>
          <label class="qm-density"><span></span><input type="range" min="1" step="1"></label>
          <span class="qm-sep"></span>
          <span class="qm-count"></span>
          <button data-act="clear" class="qm-clear"></button>
        </div>
        <div class="qm-band">
          <div class="qm-hint qm-glass"></div>
          <div class="qm-legend qm-glass"></div>
        </div>
        <div class="qm-lectures qm-glass"></div>
        <div class="qm-tooltip qm-glass"></div>
      </div>`;
    this.el = root.querySelector(".qm-map");
    this.canvas = root.querySelector("canvas");
    this.ctx = this.canvas.getContext("2d");
    this.tooltip = root.querySelector(".qm-tooltip");
    this.tool = "hand";          // 開いた直後はドラッグで移動（Google Map と同じ。#47）
    this.layout = "force";
    this.view = null;               // {scale, tx, ty}
    this.selected = new Set();
    this.viaEdge = new Set();
    this.selVersion = -1;
    this.sendCounter = 0;
    this.activeLectures = new Set();
    this.hover = null;              // {type: "node"|"edge", id}
    this.drag = null;
    this.space = false;
    this.anim = null;
    this.raf = null;
    this.data = null;
    this._bind();
    this._resizeObserver = new ResizeObserver(() => this._resize());
    this._resizeObserver.observe(this.el);
  }

  // ---------------------------------------------------------------- データ
  update(data) {
    const first = !this.data;
    const courseChanged = this.data && this.data.course_id !== data.course_id;
    this.data = data;
    this.T = data.texts;
    this.nodes = data.nodes;
    this.byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.edges = data.edges;
    this.neighbors = new Map(this.nodes.map((n) => [n.id, new Set()]));
    for (const e of this.edges) {
      this.neighbors.get(e.s)?.add(e.t);
      this.neighbors.get(e.t)?.add(e.s);
    }
    this.ranked = [...this.nodes].sort((a, b) => b.weight - a.weight);
    if (first || courseChanged) {
      this.density = this.nodes.length;
      this.layout = data.layout || "force";
      this.pos = this._positions(this.layout);
    }
    if (data.sel_version !== this.selVersion) {        // Python 側から選択を指示された（コース切替・解除）
      this.selVersion = data.sel_version;
      this.selected = new Set(data.selected || []);
      this.viaEdge = new Set();
      this.activeLectures = new Set();
    }
    this._renderChrome();
    this._resize();
    if (first || courseChanged || !this.view) this.fit(false);
    this.draw();
  }

  _positions(layout) {
    const p = new Map();
    const src = this.data.positions[layout];
    for (const n of this.nodes) p.set(n.id, src[n.id] ? [...src[n.id]] : [0, 0]);
    return p;
  }

  visibleIds() {
    return new Set(this.ranked.slice(0, this.density).map((n) => n.id));
  }

  // ---------------------------------------------------------------- 表示の枠
  _renderChrome() {
    const T = this.T;
    const tb = this.root.querySelector(".qm-toolbar");
    const set = (sel, icon, title) => { const b = tb.querySelector(sel); b.innerHTML = ICONS[icon] || icon; b.title = title; };
    set('[data-tool="hand"]', "hand", T.tool_hand);
    set('[data-tool="lasso"]', "lasso", T.tool_lasso);
    set('[data-tool="rect"]', "rect", T.tool_rect);
    set('[data-act="zoomin"]', "plus", T.zoom_in);
    set('[data-act="zoomout"]', "minus", T.zoom_out);
    set('[data-act="fit"]', "fit", T.fit);
    set('[data-act="clear"]', "clear", T.clear);
    tb.querySelector('[data-layout="force"]').textContent = T.layout_force;
    tb.querySelector('[data-layout="timeline"]').textContent = T.layout_timeline;
    const dens = tb.querySelector(".qm-density");
    dens.title = T.density;
    const range = dens.querySelector("input");
    range.max = String(this.nodes.length);
    range.value = String(this.density);
    dens.querySelector("span").textContent = `${this.density}/${this.nodes.length}`;
    tb.querySelectorAll("[data-tool]").forEach((b) => b.classList.toggle("on", b.dataset.tool === this.tool));
    tb.querySelectorAll("[data-layout]").forEach((b) => b.classList.toggle("on", b.dataset.layout === this.layout));

    this.root.querySelector(".qm-legend").innerHTML =
      `<span><i style="background:${rgba(COLORS.unasked, 1)}"></i>${T.unasked}</span>` +
      `<span><i style="background:${rgba(COLORS.asked, 1)}"></i>${T.asked}</span>` +
      `<span><i class="ring"></i>${T.selected}</span>` +
      // 外部資料で補った関係（点線の辺）は、その地図にあるときだけ凡例に出す
      (this.edges.some((e) => e.external) ? `<span class="ext"><i class="dash"></i>${esc(T.external_legend)}</span>` : "");
    const hint = this.root.querySelector(".qm-hint");
    const hintText = this.tool === "hand" ? T.hint_hand : T.hint_select;   // ツールごとに説明を替える（#67）
    hint.textContent = hintText;
    hint.title = hintText;              // 幅が足りず省略されたときも、マウスを乗せれば全文が読める

    const lec = this.root.querySelector(".qm-lectures");
    if (!lec.dataset.built || lec.dataset.course !== this.data.course_id || lec.dataset.lang !== T.lecture_fmt) {
      lec.innerHTML = `<span class="qm-lec-title">${T.by_lecture}</span>` +
        `<button data-lec="all">${T.all}</button>` +
        this.data.lectures.map((n) => `<button data-lec="${n}">${T.lecture_fmt.replace("{n}", n)}</button>`).join("");
      lec.dataset.built = "1";
      lec.dataset.course = this.data.course_id;
      lec.dataset.lang = T.lecture_fmt;
    }
    lec.querySelectorAll("button").forEach((b) => b.classList.toggle("on", this.activeLectures.has(b.dataset.lec)));
    this._lecEdges();
    const c = this.root.querySelector(".qm-count");
    c.textContent = this.selected.size ? T.count_fmt.replace("{n}", this.selected.size) : "";
    c.style.display = this.selected.size ? "" : "none";
    this._fitToolbar();
  }

  // ツールバーが 1 段で地図の幅に収まらなければ 2 段にする（#43）。選択数の文字で幅が変わるので描くたびに見る
  _fitToolbar() {
    if (!this.w) return;
    const tb = this.root.querySelector(".qm-toolbar");
    this.el.classList.remove("qm-narrow");
    this.el.classList.toggle("qm-narrow", tb.scrollWidth > this.w - 24);
  }

  // 講義回ボタンの列: 左右に続きがあるかを data 属性で示す（CSS が端をぼかす。#43）
  _lecEdges() {
    const lec = this.root.querySelector(".qm-lectures");
    const max = lec.scrollWidth - lec.clientWidth;
    lec.dataset.moreLeft = lec.scrollLeft > 1 ? "1" : "";
    lec.dataset.moreRight = lec.scrollLeft < max - 1 ? "1" : "";
  }

  // ---------------------------------------------------------------- 座標
  _resize() {
    const r = this.el.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (!r.width || !r.height) return;
    this.w = r.width; this.h = r.height;
    this.canvas.width = Math.round(r.width * dpr);
    this.canvas.height = Math.round(r.height * dpr);
    this.canvas.style.width = r.width + "px";
    this.canvas.style.height = r.height + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._fitToolbar();
    this._lecEdges();
    this.draw();
  }

  toScreen([x, y]) { return [x * this.view.scale + this.view.tx, y * this.view.scale + this.view.ty]; }
  toWorld(sx, sy) { return [(sx - this.view.tx) / this.view.scale, (sy - this.view.ty) / this.view.scale]; }

  fit(animate = true) {
    if (!this.w) return;
    const vis = this.visibleIds();
    const pts = [...vis].map((id) => this.pos.get(id));
    if (!pts.length) return;
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
    const pad = 70;
    const { top, bottom } = this._fitMargins();
    const scale = Math.min((this.w - pad * 2) / Math.max(x1 - x0, 1e-6), (this.h - top - bottom) / Math.max(y1 - y0, 1e-6));
    const target = { scale, tx: pad + (this.w - pad * 2 - (x1 - x0) * scale) / 2 - x0 * scale,
                     ty: top + (this.h - top - bottom - (y1 - y0) * scale) / 2 - y0 * scale };
    this._setView(target, animate);
  }

  // 全体表示の上下の余白（#75）。上に重ねたツールバーと、下に重ねた帯（説明・凡例・講義回ボタン）を
  // 毎回測り、その外に点と名前が収まるようにする。ツールバーは窓の幅で 1〜3 段になる
  _fitMargins() {
    this._fitToolbar();
    const m = this.el.getBoundingClientRect();
    const tb = this.root.querySelector(".qm-toolbar").getBoundingClientRect();
    const bandTop = Math.min(...[".qm-hint", ".qm-legend", ".qm-lectures"]
      .map((s) => this.root.querySelector(s).getBoundingClientRect().top));
    let top = tb.bottom - m.top + 16 + FIT_NODE_R;
    if (this.layout === "timeline") top += AXIS_ROW;                       // 列の見出しの行
    let bottom = m.bottom - bandTop + FIT_NODE_R + FIT_LABEL_H + 8;        // 点の下に描く名前 1 行分
    const room = this.h - top - bottom;
    if (room < 80) { const k = (this.h - 80) / (top + bottom); top *= k; bottom *= k; }   // とても低い窓では余白を縮める
    return { top, bottom };
  }

  zoomAt(sx, sy, factor) {
    const s = Math.max(5, Math.min(this.view.scale * factor, 50000));
    const f = s / this.view.scale;
    this.view = { scale: s, tx: sx - (sx - this.view.tx) * f, ty: sy - (sy - this.view.ty) * f };
    this.draw();
  }

  _setView(target, animate) {
    if (!animate || !this.view) { this.view = target; this.draw(); return; }
    const from = { ...this.view }, t0 = performance.now(), dur = 280;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      this.view = { scale: from.scale + (target.scale - from.scale) * e, tx: from.tx + (target.tx - from.tx) * e, ty: from.ty + (target.ty - from.ty) * e };
      this.draw();
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  setLayout(layout) {
    if (layout === this.layout) return;
    this.layout = layout;
    const from = new Map([...this.pos].map(([k, v]) => [k, [...v]]));
    const to = this._positions(layout);
    const t0 = performance.now(), dur = 420;
    const step = (t) => {
      const k = Math.min(1, (t - t0) / dur), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      for (const [id, a] of from) { const b = to.get(id); this.pos.set(id, [a[0] + (b[0] - a[0]) * e, a[1] + (b[1] - a[1]) * e]); }
      this.draw();
      if (k < 1) requestAnimationFrame(step); else this.fit(true);
    };
    requestAnimationFrame(step);
    this._renderChrome();
  }

  // ---------------------------------------------------------------- 選択
  _mode(ev) { return ev.shiftKey ? "add" : (ev.metaKey || ev.altKey || ev.ctrlKey) ? "sub" : "replace"; }

  applySelection(ids, viaEdge, mode, lectureKey = null) {
    const ids_ = new Set(ids);
    if (mode === "replace") {
      this.selected = ids_;
      this.viaEdge = new Set([...viaEdge].filter((i) => ids_.has(i)));
      this.activeLectures = new Set(lectureKey ? [lectureKey] : []);
    } else if (mode === "add") {
      for (const i of ids_) { if (!this.selected.has(i) && viaEdge.has(i)) this.viaEdge.add(i); this.selected.add(i); }
      if (lectureKey) this.activeLectures.add(lectureKey);
    } else {
      for (const i of ids_) { this.selected.delete(i); this.viaEdge.delete(i); }
      if (lectureKey) this.activeLectures.delete(lectureKey);
    }
    for (const i of [...this.viaEdge]) if (!this.selected.has(i)) this.viaEdge.delete(i);
    this._send();
    this._renderChrome();
    this.draw();
  }

  _send() {
    this.sendCounter += 1;
    this.setStateValue("selection", {
      ids: [...this.selected], via_edge: [...this.viaEdge],
      version: this.selVersion, n: this.sendCounter,
    });
  }

  selectByShape(poly, mode) {
    const vis = this.visibleIds();
    const ids = new Set(), via = new Set();
    for (const id of vis) { const [x, y] = this.toScreen(this.pos.get(id)); if (pointInPolygon(x, y, poly)) ids.add(id); }
    // 辺の中点を囲んだら、その辺の両端を選択に入れる（§6）。
    // 講義回の配置では離れた回どうしの辺の中点が中ほどの列に集まり、遠い回の概念を大量に拾うので使わない（#17）
    if (this.layout === "force") for (const e of this.edges) {
      if (!vis.has(e.s) || !vis.has(e.t)) continue;
      const a = this.toScreen(this.pos.get(e.s)), b = this.toScreen(this.pos.get(e.t));
      if (pointInPolygon((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, poly)) {
        for (const id of [e.s, e.t]) if (!ids.has(id)) { ids.add(id); via.add(id); }
      }
    }
    this.applySelection(ids, via, mode);
  }

  selectLecture(key, mode) {
    const ids = key === "all" ? this.nodes.map((n) => n.id)
      : this.nodes.filter((n) => n.lectures.includes(Number(key))).map((n) => n.id);
    this.applySelection(ids, new Set(), mode, key);
  }

  // ---------------------------------------------------------------- 当たり判定
  _radius(n) { return (3.2 + 5.5 * Math.sqrt(n.weight)) * Math.min(1.8, Math.max(0.75, Math.sqrt(this.view.scale / 600))); }

  hit(sx, sy) {
    const vis = this.visibleIds();
    let best = null, bestD = 1e9;
    for (const id of vis) {
      const n = this.byId.get(id);
      const [x, y] = this.toScreen(this.pos.get(id));
      const d = Math.hypot(sx - x, sy - y);
      if (d < Math.max(8, this._radius(n) + 4) && d < bestD) { best = { type: "node", id }; bestD = d; }
    }
    if (best || this.layout !== "force") return best;
    for (const e of this.edges) {
      if (!vis.has(e.s) || !vis.has(e.t)) continue;
      const a = this.toScreen(this.pos.get(e.s)), b = this.toScreen(this.pos.get(e.t));
      const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
      const d = Math.hypot(sx - mx, sy - my);
      if (d < 6 && d < bestD) { best = { type: "edge", id: e.id }; bestD = d; }
    }
    return best;
  }

  // ---------------------------------------------------------------- 描画
  draw() {
    if (this.raf) return;
    this.raf = requestAnimationFrame(() => { this.raf = null; this._draw(); });
  }

  _draw() {
    if (!this.view || !this.w) return;
    const ctx = this.ctx, vis = this.visibleIds();
    ctx.clearRect(0, 0, this.w, this.h);

    // 時系列: 講義回の列の補助線（見出しは点の上に重ねて最後に描く: _drawAxisLabels）
    if (this.layout === "timeline") {
      ctx.strokeStyle = "rgba(255,255,255,0.075)";
      for (const n of this.data.lectures) {
        const p = this.data.positions.timeline_axis[String(n)];
        if (p === undefined) continue;
        const [x] = this.toScreen([p, 0]);
        ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, this.h); ctx.stroke();
      }
    }

    const hasSel = this.selected.size > 0;
    const hov = this.hover && this.hover.type === "node" ? this.hover.id : null;
    const hovEdge = this.hover && this.hover.type === "edge" ? this.edges.find((e) => e.id === this.hover.id) : null;
    const focus = hov ? new Set([hov, ...this.neighbors.get(hov)]) : null;

    // 辺
    for (const e of this.edges) {
      if (!vis.has(e.s) || !vis.has(e.t)) continue;
      const a = this.toScreen(this.pos.get(e.s)), b = this.toScreen(this.pos.get(e.t));
      let alpha = 0.16, color = COLORS.edge, width = 1;
      if (hasSel) alpha = this.selected.has(e.s) && this.selected.has(e.t) ? 0.42 : 0.05;
      if (focus) { const on = e.s === hov || e.t === hov; alpha = on ? 0.85 : 0.04; color = on ? COLORS.accent : COLORS.edge; width = on ? 1.4 : 1; }
      if (hovEdge && hovEdge.id === e.id) { alpha = 0.95; color = COLORS.accent; width = 2; }
      ctx.strokeStyle = rgba(color, alpha); ctx.lineWidth = width;
      ctx.setLineDash(e.external ? EXTERNAL_DASH : []);       // 外部資料（Wikipedia）で補った関係は点線
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke();
      ctx.setLineDash([]);
      if (this.layout === "force" && this.view.scale > 900 && !focus) {                        // 拡大したら辺の中点を見せる
        ctx.fillStyle = rgba(color, Math.min(0.6, alpha + 0.15));
        ctx.beginPath(); ctx.arc((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, 1.8, 0, Math.PI * 2); ctx.fill();
      }
    }

    // 点
    const order = [...vis].sort((a, b) => this.byId.get(a).weight - this.byId.get(b).weight);
    for (const id of order) {
      const n = this.byId.get(id);
      const [x, y] = this.toScreen(this.pos.get(id));
      const r = this._radius(n);
      let alpha = 0.92;
      if (hasSel && !this.selected.has(id)) alpha = 0.22;
      if (focus) alpha = focus.has(id) ? 1 : 0.12;
      const c = n.asked ? COLORS.asked : COLORS.unasked;
      if (this.selected.has(id) || id === hov) {
        ctx.save();
        ctx.shadowColor = rgba(COLORS.accent, 0.75); ctx.shadowBlur = 14;
        ctx.fillStyle = rgba(c, alpha);
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
        ctx.restore();
        ctx.strokeStyle = rgba(COLORS.accent, id === hov ? 1 : 0.9); ctx.lineWidth = 2;
        ctx.beginPath(); ctx.arc(x, y, r + 3, 0, Math.PI * 2); ctx.stroke();
      } else {
        ctx.fillStyle = rgba(c, alpha);
        ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      }
    }

    // ラベル: 重みの高い順に、重ならない位置にだけ置く。拡大するほど多く出る
    ctx.font = "12px -apple-system, 'Hiragino Sans', 'Noto Sans JP', sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const placed = [...vis].map((id) => { const [x, y] = this.toScreen(this.pos.get(id)); const r = this._radius(this.byId.get(id)); return [x - r, y - r, x + r, y + r]; });
    const want = (id) => id === hov || (focus && focus.has(id)) || this.selected.has(id);
    const cand = [...vis].sort((a, b) => (want(b) - want(a)) || (this.byId.get(b).weight - this.byId.get(a).weight));
    const zoomBudget = Math.max(8, Math.round(vis.size * Math.min(1, this.view.scale / 1400)));
    let shown = 0;
    for (const id of cand) {
      const forced = want(id);
      if (!forced && shown >= zoomBudget) continue;
      const n = this.byId.get(id);
      const [x, y] = this.toScreen(this.pos.get(id));
      const r = this._radius(n);
      const w = ctx.measureText(n.label).width + 6, h = 15;
      const box = [x - w / 2, y + r + 3, x + w / 2, y + r + 3 + h];
      if (placed.some((b) => !(box[2] < b[0] || b[2] < box[0] || box[3] < b[1] || b[3] < box[1]))) continue;
      placed.push(box);
      shown += 1;
      let alpha = 0.78;
      if (hasSel && !this.selected.has(id)) alpha = 0.25;
      if (focus) alpha = focus.has(id) ? 1 : 0.1;
      ctx.fillStyle = rgba(COLORS.text, alpha);
      ctx.fillText(n.label, x, y + r + 4);
    }

    // 選択中の図形
    if (this.drag && (this.drag.kind === "lasso" || this.drag.kind === "rect")) {
      const pts = this.drag.kind === "lasso" ? this.drag.path : this._rectPoly(this.drag);
      if (pts.length > 1) {
        ctx.save();
        ctx.setLineDash([5, 4]);
        ctx.strokeStyle = rgba(COLORS.accent, 0.95); ctx.lineWidth = 1.5;
        ctx.fillStyle = this.drag.mode === "sub" ? "rgba(120,140,255,0.08)" : rgba(COLORS.accent, 0.08);
        ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]);
        for (const p of pts.slice(1)) ctx.lineTo(p[0], p[1]);
        ctx.closePath(); ctx.fill(); ctx.stroke();
        ctx.restore();
      }
    }
    this._drawAxisLabels(focus ? this.byId.get(hov) : null);
  }

  // 「講義回」の配置の列の見出し（#44）。縦位置はツールバーのすぐ下に固定し、横位置は列に合わせて動かす。
  // 隣の見出しと重なるときは、重ならなくなるまで間引く（2 つに 1 つ、3 つに 1 つ…）。
  // 描いた見出しの位置は data-axis に出す（テスト用）。
  _drawAxisLabels(hoverNode) {
    if (this.layout !== "timeline") { delete this.el.dataset.axis; return; }
    const ctx = this.ctx;
    const tb = this.root.querySelector(".qm-toolbar");
    const y = tb.offsetTop + tb.offsetHeight + 8;
    ctx.save();
    ctx.font = "600 11px -apple-system, 'Hiragino Sans', sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const items = [];
    for (const n of this.data.lectures) {
      const p = this.data.positions.timeline_axis[String(n)];
      if (p === undefined) continue;
      const text = this.T.lecture_short.replace("{n}", n);
      items.push({ n, text, x: this.toScreen([p, 0])[0], w: ctx.measureText(text).width + 10 });
    }
    items.sort((a, b) => a.x - b.x);
    let step = 1;
    const fits = (k) => items.every((it, i) => i < k || items[i].x - items[i - k].x >= (items[i].w + items[i - k].w) / 2 + 4);
    while (step < items.length && !fits(step)) step += 1;
    // 明るくする列: 点に乗っていればその点の講義回、そうでなければマウスの下（いちばん近い列。列の間隔の半分以内）
    const hot = new Set(hoverNode ? hoverNode.lectures.map(String) : []);
    if (!hoverNode && this.mouseX != null && items.length) {
      const gap = items.length > 1 ? (items[items.length - 1].x - items[0].x) / (items.length - 1) : Infinity;
      const near = items.reduce((b, it) => (Math.abs(it.x - this.mouseX) < Math.abs(b.x - this.mouseX) ? it : b));
      if (Math.abs(near.x - this.mouseX) <= gap / 2) hot.add(String(near.n));
    }
    const drawn = [];
    items.forEach((it, i) => {
      if (i % step !== 0) return;               // 間引いた見出しは、明るくする列でも描かない（重なるため）
      if (it.x + it.w / 2 < 0 || it.x - it.w / 2 > this.w) return;
      const on = hot.has(String(it.n));
      ctx.fillStyle = on ? rgba(COLORS.accent, 0.28) : "rgba(23,22,28,0.72)";
      ctx.beginPath(); ctx.roundRect(it.x - it.w / 2, y - 2, it.w, 17, 8); ctx.fill();
      ctx.fillStyle = on ? "#fff" : "rgba(226,224,236,0.7)";
      ctx.fillText(it.text, it.x, y + 1);
      drawn.push({ n: it.n, x: Math.round(it.x), y: Math.round(y - 2), w: Math.round(it.w), h: 17 });
    });
    ctx.restore();
    this.el.dataset.axis = JSON.stringify({ step, labels: drawn });
  }

  _rectPoly(d) { const [x0, y0] = d.start, [x1, y1] = d.cur; return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]; }

  // ---------------------------------------------------------------- ツールチップ
  _showTooltip(sx, sy) {
    const T = this.T;
    if (!this.hover) { this.tooltip.style.display = "none"; return; }
    let html;
    if (this.hover.type === "node") {
      const n = this.byId.get(this.hover.id);
      const lec = n.lectures.map((x) => T.lecture_fmt.replace("{n}", x)).join("・");
      // 解答中は定義を出さない（定義問題の答えになる: #42）
      const def = this.data.hide_definitions ? `<div class="hidden-def">${esc(T.def_hidden)}</div>` : `<div>${esc(n.definition)}</div>`;
      html = `<b>${esc(n.label)}</b>${n.asked ? `<span class="tag">${esc(T.asked)}</span>` : ""}${def}<small>${esc(lec)}</small>`;
    } else {
      const e = this.edges.find((x) => x.id === this.hover.id);
      html = `<b>${esc(this.byId.get(e.s).label)}</b> <span class="rel">${esc(e.label)}</span> <b>${esc(this.byId.get(e.t).label)}</b>` +
        (e.external ? `<div class="ext">${esc(T.external_rel)}</div>` +
          (e.article ? `<small class="ext-src">${esc(T.external_src)}: ${esc(e.article)}</small>` : "") : "") +
        `<small>${esc(T.lecture_fmt.replace("{n}", e.lecture))} p.${esc(e.page)}</small>`;
    }
    this.tooltip.innerHTML = html;
    this.tooltip.style.display = "block";
    const tw = this.tooltip.offsetWidth, th = this.tooltip.offsetHeight;
    this.tooltip.style.left = Math.min(this.w - tw - 8, sx + 14) + "px";
    this.tooltip.style.top = Math.min(this.h - th - 8, sy + 14) + "px";
  }

  // ---------------------------------------------------------------- 入力
  _setTool(t) { this.tool = t; this._renderChrome(); this._cursor(); }

  _cursor() {
    const panning = this.drag && this.drag.kind === "pan";
    this.el.dataset.cursor = panning ? "grabbing" : (this.space || this.tool === "hand") ? "grab" : "cross";
  }

  _bind() {
    const tb = this.root.querySelector(".qm-toolbar");
    tb.addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      if (b.dataset.tool) this._setTool(b.dataset.tool);
      if (b.dataset.layout) this.setLayout(b.dataset.layout);
      const r = this.el.getBoundingClientRect();
      if (b.dataset.act === "zoomin") this.zoomAt(this.w / 2, this.h / 2, 1.35);
      if (b.dataset.act === "zoomout") this.zoomAt(this.w / 2, this.h / 2, 1 / 1.35);
      if (b.dataset.act === "fit") this.fit(true);
      if (b.dataset.act === "clear") this.applySelection([], new Set(), "replace");
      b.blur();
      this.el.focus({ preventScroll: true });
    });
    tb.querySelector(".qm-density input").addEventListener("input", (ev) => {
      this.density = Number(ev.target.value);
      this._renderChrome();
      this.draw();
    });
    this.root.querySelector(".qm-lectures").addEventListener("click", (ev) => {
      const b = ev.target.closest("button");
      if (!b) return;
      this.selectLecture(b.dataset.lec, this._mode(ev));
      b.blur();
      this.el.focus({ preventScroll: true });
    });

    const c = this.canvas;
    c.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 && ev.button !== 1) return;
      c.setPointerCapture(ev.pointerId);
      this.el.focus({ preventScroll: true });
      const p = this._pt(ev);
      const pan = ev.button === 1 || this.space || this.tool === "hand";
      this.drag = { kind: pan ? "pan" : this.tool, start: p, cur: p, path: [p], mode: this._mode(ev), moved: false, view: { ...this.view } };
      this._cursor();
    });
    c.addEventListener("pointermove", (ev) => {
      const p = this._pt(ev);
      if (!this.drag) {
        const h = this.hit(p[0], p[1]);
        const changed = JSON.stringify(h) !== JSON.stringify(this.hover);
        this.hover = h;
        this.mouseX = p[0];
        this._showTooltip(p[0], p[1]);
        if (changed || this.layout === "timeline") this.draw();   // 時系列ではマウスの下の列の見出しを明るくする
        this.el.dataset.hover = h ? "1" : "";
        return;
      }
      const d = this.drag;
      if (Math.hypot(p[0] - d.start[0], p[1] - d.start[1]) > 3) d.moved = true;
      d.cur = p;
      d.mode = this._mode(ev);
      if (d.kind === "pan") {
        this.view = { ...d.view, tx: d.view.tx + p[0] - d.start[0], ty: d.view.ty + p[1] - d.start[1] };
      } else if (d.kind === "lasso") {
        d.path.push(p);
      }
      this.hover = null; this.tooltip.style.display = "none";
      this.draw();
    });
    const end = (ev) => {
      if (!this.drag) return;
      const d = this.drag;
      this.drag = null;
      this._cursor();
      if (!d.moved) {                                     // クリック
        // 何もない所のクリックでは選択を変えない（全部外すのは Esc と × だけ。#41）。
        // 点・辺のクリックは CLICK_DELAY 待ってから反映し、その間にダブルクリック（拡大）が来たら取り消す。
        // 待っている間に次のクリックが来たら、ダブルクリックの 2 回目とみなして両方とも選択に使わない。
        if (this._clickTimer) { clearTimeout(this._clickTimer); this._clickTimer = null; this.draw(); return; }
        if (ev.detail >= 2) { this.draw(); return; }
        const h = this.hit(d.start[0], d.start[1]);
        const mode = this._mode(ev);
        if (h && (h.type === "node" || h.type === "edge")) {
          this._clickTimer = setTimeout(() => {
            this._clickTimer = null;
            if (h.type === "node") this.applySelection([h.id], new Set(), mode);
            else {
              const e = this.edges.find((x) => x.id === h.id);
              if (e) this.applySelection([e.s, e.t], new Set([e.s, e.t]), mode);
            }
            this.draw();
          }, CLICK_DELAY);
        }
        this.draw();
        return;
      }
      if (d.kind === "lasso" && d.path.length > 2) this.selectByShape(d.path, this._mode(ev));
      if (d.kind === "rect") this.selectByShape(this._rectPoly(d), this._mode(ev));
      this.draw();
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", () => { this.drag = null; this._cursor(); this.draw(); });
    c.addEventListener("pointerleave", () => { this.mouseX = null; if (!this.drag) { this.hover = null; this.tooltip.style.display = "none"; this.draw(); } });
    c.addEventListener("dblclick", (ev) => {
      if (this._clickTimer) { clearTimeout(this._clickTimer); this._clickTimer = null; }   // ダブルクリックは拡大だけ
      const p = this._pt(ev);
      this.zoomAt(p[0], p[1], 1.8);
    });

    // ホイール: ピンチ（ctrl 付き）と 2 本指の上下はズーム。スペースを押している間は移動
    const lecRow = this.root.querySelector(".qm-lectures");
    lecRow.addEventListener("scroll", () => this._lecEdges());
    this.el.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      // 講義回ボタンの列の上では、地図をズームせずに列を横に送る（#43）
      if (ev.target.closest && ev.target.closest(".qm-lectures")) {
        lecRow.scrollLeft += Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
        return;
      }
      const p = this._pt(ev);
      if (this.space && !ev.ctrlKey) {
        this.view = { ...this.view, tx: this.view.tx - ev.deltaX, ty: this.view.ty - ev.deltaY };
        this.draw();
        return;
      }
      const k = ev.ctrlKey ? 0.012 : 0.0022;
      const dy = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
      this.zoomAt(p[0], p[1], Math.exp(-dy * k));
    }, { passive: false });

    // キーボード: スペースで一時的に手のひら。入力欄に文字を打っているときは奪わない
    const typing = () => {
      const a = document.activeElement;
      return a && a !== this.el && (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.isContentEditable);
    };
    this._onKeyDown = (ev) => {
      if (typing()) return;
      if (ev.code === "Space") {
        ev.preventDefault();
        const a = document.activeElement;
        if (a && a !== document.body && a !== this.el && a.blur) a.blur();   // ボタンが押されてしまうのを防ぐ
        if (!this.space) { this.space = true; this._cursor(); }
        return;
      }
      if (!this._inView()) return;
      if (ev.key === "h" || ev.key === "H") this._setTool("hand");
      else if (ev.key === "l" || ev.key === "L") this._setTool("lasso");
      else if (ev.key === "m" || ev.key === "M") this._setTool("rect");
      else if (ev.key === "0") this.fit(true);
      else if (ev.key === "Escape") this.applySelection([], new Set(), "replace");
    };
    this._onKeyUp = (ev) => {
      if (ev.code === "Space" && this.space) { ev.preventDefault(); this.space = false; this._cursor(); }
    };
    this._onBlur = () => { if (this.space) { this.space = false; this._cursor(); } };
    document.addEventListener("keydown", this._onKeyDown, true);
    document.addEventListener("keyup", this._onKeyUp, true);
    window.addEventListener("blur", this._onBlur);
    this._cursor();
  }

  _inView() { const r = this.el.getBoundingClientRect(); return r.bottom > 0 && r.top < window.innerHeight; }
  _pt(ev) { const r = this.canvas.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }

  destroy() {
    document.removeEventListener("keydown", this._onKeyDown, true);
    document.removeEventListener("keyup", this._onKeyUp, true);
    window.removeEventListener("blur", this._onBlur);
    this._resizeObserver.disconnect();
  }
}

export default function (component) {
  const { data, setStateValue, parentElement } = component;
  let app = parentElement.__qmMap;
  if (!app) {
    const root = document.createElement("div");
    root.className = "qm-root";
    parentElement.appendChild(root);
    app = new MapApp(root, setStateValue);
    parentElement.__qmMap = app;
    window.__qmMapApp = app;          // テストから状態を読むため
  }
  app.setStateValue = setStateValue;
  app.update(data);
  return () => {};
}
