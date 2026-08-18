"use strict";

// ---------------------------------------------------------------- 状態
// マスク矩形・ページ範囲・奇数偶数は「ファイルごと」に settings で保持する。
// 塗りつぶし色のみ全ファイル共通。表示中のファイル＝編集対象。
const state = {
  docs: [],        // {id, name, pages}
  settings: {},    // docId -> {rects:[{x0,y0,x1,y1}], range:"", parity:"all"}
  refId: null,     // 表示・編集中のPDF id
  page: 1,         // 表示中ページ（1始まり）
  preview: false,  // マスク後プレビュー
  drag: null,      // 新規矩形ドラッグ中 {x0,y0,x1,y1}
  action: null,    // 操作中 {type:"draw"|"move"|"resize", ...}
  selectedIndex: -1, // 選択中の矩形（ハンドルでサイズ変更可能）
  hoverIndex: -1,  // 一覧でホバー中の矩形
  clipboard: null, // 矩形のコピー＆貼り付け用 {rects:[...], sourceId}
};

function ensureSettings(id) {
  if (!state.settings[id]) state.settings[id] = { rects: [], range: "", parity: "all" };
  return state.settings[id];
}
function cur() { return state.refId === null ? null : ensureSettings(state.refId); }
function curRects() { const s = cur(); return s ? s.rects : []; }

// ---------------------------------------------------------------- 要素
const $ = (id) => document.getElementById(id);
const els = {
  dropzone: $("dropzone"), fileInput: $("file-input"), pickBtn: $("pick-btn"),
  docList: $("doc-list"), clearDocs: $("clear-docs"),
  rectList: $("rect-list"), clearRects: $("clear-rects"),
  rectCopy: $("rect-copy"), rectPaste: $("rect-paste"),
  pageRange: $("page-range"), parity: $("parity"), copyAll: $("copy-all"),
  presetSelect: $("preset-select"), presetLoad: $("preset-load"),
  presetLoadAll: $("preset-load-all"),
  presetDelete: $("preset-delete"), presetName: $("preset-name"), presetSave: $("preset-save"),
  applyBtn: $("apply-btn"), results: $("results"), openOutput: $("open-output"),
  quitBtn: $("quit-btn"),
  refDoc: $("ref-doc"), prevPage: $("prev-page"), nextPage: $("next-page"),
  pageIndicator: $("page-indicator"), previewToggle: $("preview-toggle"),
  stage: $("stage"), emptyMsg: $("empty-msg"), pageWrap: $("page-wrap"),
  pageImg: $("page-img"), overlay: $("overlay"), toast: $("toast"),
};
const ctx = els.overlay.getContext("2d");

// ---------------------------------------------------------------- 通知
let toastTimer = null;
function toast(msg, isError = false) {
  els.toast.textContent = msg;
  els.toast.className = "toast" + (isError ? " error" : "");
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 3500);
}

// ---------------------------------------------------------------- API
async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `通信エラー (${res.status})`);
  return data;
}

// ---------------------------------------------------------------- PDF読み込み
async function uploadFiles(fileList) {
  const files = [...fileList].filter((f) => /\.pdf$/i.test(f.name));
  if (!files.length) { toast("PDFファイルを選択してください", true); return; }
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  toast(`${files.length} ファイルを読み込み中…`);
  try {
    const data = await api("/api/upload", { method: "POST", body: form });
    state.docs = data.docs;
    state.docs.forEach((d) => ensureSettings(d.id));
    if (data.errors.length) toast(`読み込めないファイル: ${data.errors.join(", ")}`, true);
    else toast(`${data.added.length} ファイルを読み込みました`);
    if (state.refId === null && state.docs.length) {
      state.refId = state.docs[0].id;
      state.page = 1;
    }
    syncInputsFromCur();
    renderAll();
    loadPageImage();
  } catch (e) { toast(e.message, true); }
}

async function removeDoc(id) {
  const data = await api(`/api/docs/${id}`, { method: "DELETE" });
  state.docs = data.docs;
  delete state.settings[id];
  if (state.refId === id) {
    state.refId = state.docs.length ? state.docs[0].id : null;
    state.page = 1;
    state.selectedIndex = -1;
    syncInputsFromCur();
    loadPageImage();
  }
  renderAll();
}

async function clearDocs() {
  await api("/api/docs/clear", { method: "POST" });
  state.docs = [];
  state.settings = {};
  state.refId = null;
  state.page = 1;
  state.selectedIndex = -1;
  syncInputsFromCur();
  renderAll();
  loadPageImage();
}

// ---------------------------------------------------------------- 表示更新
function currentDoc() {
  return state.docs.find((d) => d.id === state.refId) || null;
}

// 表示中ファイルの設定値をページ範囲・奇数偶数の入力欄へ反映する
function syncInputsFromCur() {
  const s = cur();
  els.pageRange.value = s ? s.range : "";
  els.parity.value = s ? s.parity : "all";
  els.pageRange.disabled = els.parity.disabled = !s;
}

// 表示・編集対象のファイルを切り替える
function switchDoc(id) {
  if (id === state.refId) return;
  state.refId = id;
  state.page = 1;
  state.selectedIndex = -1;
  state.hoverIndex = -1;
  syncInputsFromCur();
  renderAll();
  loadPageImage();
}

function renderAll() {
  renderDocList();
  renderRectList();
  renderToolbar();
  updateApplyButton();
}

// 矩形の追加・削除・コピー後にまとめて表示を更新する
function afterRectsChange() {
  renderDocList();
  renderRectList();
  drawOverlay();
  updateApplyButton();
}

function renderDocList() {
  els.docList.innerHTML = "";
  for (const d of state.docs) {
    const li = document.createElement("li");
    if (d.id === state.refId) li.classList.add("active-doc");
    li.title = "クリックで表示・編集対象にする";
    li.onclick = () => switchDoc(d.id);
    const name = document.createElement("span");
    name.className = "doc-name";
    name.textContent = d.name;
    name.title = d.name;
    const n = ensureSettings(d.id).rects.length;
    const badge = document.createElement("span");
    badge.className = "rect-badge" + (n ? "" : " zero");
    badge.textContent = n ? `矩形${n}` : "未設定";
    const pages = document.createElement("span");
    pages.className = "doc-pages";
    pages.textContent = `${d.pages}p`;
    const btn = document.createElement("button");
    btn.className = "remove-btn";
    btn.textContent = "✕";
    btn.title = "一覧から取り除く（ファイル自体は削除されません）";
    btn.onclick = (ev) => { ev.stopPropagation(); removeDoc(d.id); };
    li.append(name, badge, pages, btn);
    els.docList.appendChild(li);
  }
  els.clearDocs.hidden = state.docs.length === 0;
}

function deleteRect(i) {
  curRects().splice(i, 1);
  if (state.selectedIndex === i) state.selectedIndex = -1;
  else if (state.selectedIndex > i) state.selectedIndex--;
  afterRectsChange();
}

function renderRectList() {
  els.rectList.innerHTML = "";
  curRects().forEach((r, i) => {
    const li = document.createElement("li");
    if (i === state.selectedIndex) li.classList.add("selected");
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    const label = document.createElement("span");
    label.className = "rect-label";
    const w = ((r.x1 - r.x0) * 100).toFixed(1);
    const h = ((r.y1 - r.y0) * 100).toFixed(1);
    label.textContent = `矩形${i + 1}（幅${w}% × 高さ${h}%）`;
    const btn = document.createElement("button");
    btn.className = "remove-btn";
    btn.textContent = "✕";
    btn.title = "この矩形を削除";
    btn.onclick = (ev) => { ev.stopPropagation(); deleteRect(i); };
    li.onclick = () => { state.selectedIndex = i; renderRectList(); drawOverlay(); };
    li.onmouseenter = () => { state.hoverIndex = i; drawOverlay(); };
    li.onmouseleave = () => { state.hoverIndex = -1; drawOverlay(); };
    li.append(swatch, label, btn);
    els.rectList.appendChild(li);
  });
  els.clearRects.hidden = curRects().length === 0;
}

function renderToolbar() {
  els.refDoc.innerHTML = "";
  for (const d of state.docs) {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = `${d.name}（${d.pages}ページ）`;
    els.refDoc.appendChild(opt);
  }
  if (state.refId !== null) els.refDoc.value = state.refId;
  const doc = currentDoc();
  els.pageIndicator.textContent = doc ? `${state.page} / ${doc.pages}` : "– / –";
  els.prevPage.disabled = !doc || state.page <= 1;
  els.nextPage.disabled = !doc || state.page >= doc.pages;
}

function updateApplyButton() {
  const any = state.docs.some((d) => ensureSettings(d.id).rects.length > 0);
  els.applyBtn.disabled = !any;
}

// ---------------------------------------------------------------- ページ画像
function loadPageImage() {
  const doc = currentDoc();
  if (!doc) {
    els.pageWrap.hidden = true;
    els.emptyMsg.hidden = false;
    return;
  }
  els.emptyMsg.hidden = true;
  els.pageWrap.hidden = false;
  els.pageImg.src = `/api/page/${doc.id}/${state.page}`;
}

els.pageImg.addEventListener("load", () => { syncOverlaySize(); drawOverlay(); });
window.addEventListener("resize", () => { syncOverlaySize(); drawOverlay(); });

function syncOverlaySize() {
  const w = els.pageImg.clientWidth;
  const h = els.pageImg.clientHeight;
  if (!w || !h) return;
  const dpr = window.devicePixelRatio || 1;
  els.overlay.width = Math.round(w * dpr);
  els.overlay.height = Math.round(h * dpr);
  els.overlay.style.width = w + "px";
  els.overlay.style.height = h + "px";
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

// ---------------------------------------------------------------- 矩形描画
function maskColor() {
  return document.querySelector('input[name="color"]:checked').value;
}

function drawOverlay() {
  const w = els.pageImg.clientWidth;
  const h = els.pageImg.clientHeight;
  ctx.clearRect(0, 0, w, h);
  const drawRect = (r, i, temp) => {
    const x = r.x0 * w, y = r.y0 * h;
    const rw = (r.x1 - r.x0) * w, rh = (r.y1 - r.y0) * h;
    if (state.preview && !temp) {
      ctx.fillStyle = maskColor() === "black" ? "#000" : "#fff";
      ctx.fillRect(x, y, rw, rh);
      ctx.strokeStyle = "rgba(100,116,139,.6)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x, y, rw, rh);
    } else {
      const sel = i === state.selectedIndex;
      const hot = sel || i === state.hoverIndex;
      ctx.fillStyle = hot ? "rgba(37,99,235,.35)" : "rgba(220,38,38,.25)";
      ctx.fillRect(x, y, rw, rh);
      ctx.strokeStyle = hot ? "#2563eb" : "#dc2626";
      ctx.lineWidth = 2;
      ctx.setLineDash(temp ? [6, 4] : []);
      ctx.strokeRect(x, y, rw, rh);
      ctx.setLineDash([]);
      if (!temp) {
        ctx.fillStyle = hot ? "#2563eb" : "#dc2626";
        ctx.font = "bold 13px sans-serif";
        const badge = String(i + 1);
        ctx.fillRect(x, y, 20, 18);
        ctx.fillStyle = "#fff";
        ctx.fillText(badge, x + 7, y + 13);
      }
      if (sel && !temp) drawHandles(r, w, h);
    }
  };
  curRects().forEach((r, i) => drawRect(normalizeRect(r), i, false));
  if (state.drag) drawRect(normalizeRect(state.drag), -1, true);
}

// 選択中矩形の8つのリサイズハンドル（四隅＋各辺の中央）
function handlePoints(r) {
  const mx = (r.x0 + r.x1) / 2, my = (r.y0 + r.y1) / 2;
  return [
    [r.x0, r.y0], [mx, r.y0], [r.x1, r.y0],
    [r.x0, my], [r.x1, my],
    [r.x0, r.y1], [mx, r.y1], [r.x1, r.y1],
  ];
}

function drawHandles(r, w, h) {
  const s = 8;
  for (const [hx, hy] of handlePoints(r)) {
    const x = hx * w, y = hy * h;
    ctx.fillStyle = "#fff";
    ctx.strokeStyle = "#2563eb";
    ctx.lineWidth = 1.5;
    ctx.fillRect(x - s / 2, y - s / 2, s, s);
    ctx.strokeRect(x - s / 2, y - s / 2, s, s);
  }
}

function normalizeRect(r) {
  return {
    x0: Math.min(r.x0, r.x1), y0: Math.min(r.y0, r.y1),
    x1: Math.max(r.x0, r.x1), y1: Math.max(r.y0, r.y1),
  };
}

// ---------------------------------------------------------------- ポインタ操作
// 空白でドラッグ=新規矩形 / 矩形クリック=選択 / ハンドルドラッグ=サイズ変更 /
// 選択矩形の内側ドラッグ=移動
const MIN_SIZE = 0.005;
const HANDLE_TOL = 8; // ハンドルの当たり判定(表示px)
const CURSORS = {
  n: "ns-resize", s: "ns-resize", e: "ew-resize", w: "ew-resize",
  ne: "nesw-resize", sw: "nesw-resize", nw: "nwse-resize", se: "nwse-resize",
};

function pointerNorm(ev) {
  const b = els.overlay.getBoundingClientRect();
  return {
    x: Math.min(1, Math.max(0, (ev.clientX - b.left) / b.width)),
    y: Math.min(1, Math.max(0, (ev.clientY - b.top) / b.height)),
  };
}

// 矩形 r の縁のどのハンドル上か（"nw","n","ne","w","e","sw","s","se" / null）
function handleAt(r, p) {
  const w = els.pageImg.clientWidth, h = els.pageImg.clientHeight;
  if (!w || !h) return null;
  const tx = HANDLE_TOL / w, ty = HANDLE_TOL / h;
  const nearX0 = Math.abs(p.x - r.x0) <= tx, nearX1 = Math.abs(p.x - r.x1) <= tx;
  const nearY0 = Math.abs(p.y - r.y0) <= ty, nearY1 = Math.abs(p.y - r.y1) <= ty;
  const inX = p.x >= r.x0 - tx && p.x <= r.x1 + tx;
  const inY = p.y >= r.y0 - ty && p.y <= r.y1 + ty;
  let hnd = "";
  if (nearY0 && inX) hnd += "n";
  else if (nearY1 && inX) hnd += "s";
  if (nearX0 && inY) hnd += "w";
  else if (nearX1 && inY) hnd += "e";
  return hnd || null;
}

function hitTest(p) {
  const rects = curRects();
  // 選択中矩形のハンドルを最優先
  if (state.selectedIndex >= 0 && rects[state.selectedIndex]) {
    const hnd = handleAt(rects[state.selectedIndex], p);
    if (hnd) return { type: "handle", index: state.selectedIndex, handle: hnd };
  }
  // 重なっている場合は後から追加した（上に描かれる）矩形を優先
  for (let i = rects.length - 1; i >= 0; i--) {
    const r = rects[i];
    if (p.x >= r.x0 && p.x <= r.x1 && p.y >= r.y0 && p.y <= r.y1) {
      return { type: "inside", index: i };
    }
  }
  return null;
}

els.overlay.addEventListener("pointerdown", (ev) => {
  if (state.preview) { toast("プレビュー中は編集できません（プレビューを解除してください）"); return; }
  if (!currentDoc()) return;
  ev.preventDefault();
  els.overlay.setPointerCapture(ev.pointerId);
  const p = pointerNorm(ev);
  const hit = hitTest(p);
  if (hit && hit.type === "handle") {
    state.action = { type: "resize", handle: hit.handle };
  } else if (hit && hit.type === "inside") {
    state.selectedIndex = hit.index;
    state.action = { type: "move", start: p, orig: { ...curRects()[hit.index] } };
  } else {
    state.selectedIndex = -1;
    state.action = { type: "draw" };
    state.drag = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
  }
  renderRectList();
  drawOverlay();
});

els.overlay.addEventListener("pointermove", (ev) => {
  const p = pointerNorm(ev);
  if (!state.action) {
    // 操作していない間はカーソル形状でヒントを出す
    if (state.preview) { els.overlay.style.cursor = "default"; return; }
    const hit = hitTest(p);
    els.overlay.style.cursor =
      !hit ? "crosshair" : hit.type === "handle" ? CURSORS[hit.handle] : "move";
    return;
  }
  if (state.action.type === "draw") {
    state.drag.x1 = p.x;
    state.drag.y1 = p.y;
  } else if (state.action.type === "resize") {
    const r = curRects()[state.selectedIndex];
    const hnd = state.action.handle;
    if (hnd.includes("w")) r.x0 = Math.min(Math.max(0, p.x), r.x1 - MIN_SIZE);
    if (hnd.includes("e")) r.x1 = Math.max(Math.min(1, p.x), r.x0 + MIN_SIZE);
    if (hnd.includes("n")) r.y0 = Math.min(Math.max(0, p.y), r.y1 - MIN_SIZE);
    if (hnd.includes("s")) r.y1 = Math.max(Math.min(1, p.y), r.y0 + MIN_SIZE);
  } else if (state.action.type === "move") {
    const a = state.action;
    const rw = a.orig.x1 - a.orig.x0, rh = a.orig.y1 - a.orig.y0;
    const nx = Math.min(Math.max(0, a.orig.x0 + (p.x - a.start.x)), 1 - rw);
    const ny = Math.min(Math.max(0, a.orig.y0 + (p.y - a.start.y)), 1 - rh);
    const r = curRects()[state.selectedIndex];
    r.x0 = nx; r.y0 = ny; r.x1 = nx + rw; r.y1 = ny + rh;
  }
  drawOverlay();
});

function endPointerAction() {
  if (!state.action) return;
  if (state.action.type === "draw" && state.drag) {
    const r = normalizeRect(state.drag);
    state.drag = null;
    if (r.x1 - r.x0 > MIN_SIZE && r.y1 - r.y0 > MIN_SIZE) {
      curRects().push(r);
      state.selectedIndex = curRects().length - 1; // 追加した矩形をそのまま選択
    }
  }
  state.action = null;
  afterRectsChange();
}

els.overlay.addEventListener("pointerup", endPointerAction);
els.overlay.addEventListener("pointercancel", endPointerAction);

// ---------------------------------------------------------------- イベント
els.pickBtn.onclick = () => els.fileInput.click();
els.fileInput.onchange = () => { uploadFiles(els.fileInput.files); els.fileInput.value = ""; };

["dragenter", "dragover"].forEach((t) =>
  els.dropzone.addEventListener(t, (e) => { e.preventDefault(); els.dropzone.classList.add("dragover"); })
);
["dragleave", "drop"].forEach((t) =>
  els.dropzone.addEventListener(t, (e) => { e.preventDefault(); els.dropzone.classList.remove("dragover"); })
);
els.dropzone.addEventListener("drop", (e) => uploadFiles(e.dataTransfer.files));

els.clearDocs.onclick = clearDocs;
els.clearRects.onclick = () => {
  const s = cur();
  if (!s) return;
  s.rects = [];
  state.selectedIndex = -1;
  afterRectsChange();
};

// ---------------------------------------------------------------- 矩形のコピー＆貼り付け
// 選択中の矩形（未選択なら表示中ファイルの全矩形）をコピーし、
// 別ファイルへ切り替えて「同じ位置・同じサイズ」で貼り付ける。
function updatePasteButton() {
  const n = state.clipboard ? state.clipboard.rects.length : 0;
  els.rectPaste.disabled = !n;
  els.rectPaste.textContent = n ? `貼り付け（${n}件）` : "貼り付け";
}

function copyRects() {
  const s = cur();
  if (!s || !s.rects.length) { toast("コピーする矩形がありません", true); return; }
  const selected = state.selectedIndex >= 0 && s.rects[state.selectedIndex];
  const src = selected ? [s.rects[state.selectedIndex]] : s.rects;
  state.clipboard = { rects: src.map((r) => ({ ...r })), sourceId: state.refId };
  updatePasteButton();
  toast(selected
    ? `選択中の矩形${state.selectedIndex + 1} をコピーしました（別ファイルへ切り替えて貼り付け）`
    : `このファイルの矩形${src.length}件をコピーしました（別ファイルへ切り替えて貼り付け）`);
}

function pasteRects() {
  const s = cur();
  if (!s) { toast("貼り付け先のファイルがありません", true); return; }
  if (!state.clipboard || !state.clipboard.rects.length) {
    toast("コピーされた矩形がありません（先に「コピー」してください）", true);
    return;
  }
  // 同じファイルへの貼り付けは複製として少しずらす（重なり防止）
  const sameDoc = state.clipboard.sourceId === state.refId;
  const OFF = 0.02;
  for (const r of state.clipboard.rects) {
    const w = r.x1 - r.x0, h = r.y1 - r.y0;
    let x0 = r.x0, y0 = r.y0;
    if (sameDoc) {
      x0 = Math.min(Math.max(0, x0 + OFF), 1 - w);
      y0 = Math.min(Math.max(0, y0 + OFF), 1 - h);
    }
    s.rects.push({ x0, y0, x1: x0 + w, y1: y0 + h });
  }
  state.selectedIndex = s.rects.length - 1;
  afterRectsChange();
  toast(sameDoc
    ? `矩形${state.clipboard.rects.length}件を複製しました（少しずらして配置）`
    : `矩形${state.clipboard.rects.length}件を同じ位置に貼り付けました`);
}

els.rectCopy.onclick = copyRects;
els.rectPaste.onclick = pasteRects;

// ページ範囲・奇数偶数の入力は表示中ファイルの設定へ直接反映する
els.pageRange.addEventListener("input", () => { const s = cur(); if (s) s.range = els.pageRange.value; });
els.parity.addEventListener("change", () => { const s = cur(); if (s) s.parity = els.parity.value; });

// 表示中ファイルの設定（矩形・ページ範囲・奇数偶数）を他の全ファイルへコピー
els.copyAll.onclick = () => {
  const s = cur();
  const doc = currentDoc();
  if (!s || !doc) { toast("コピー元のファイルがありません", true); return; }
  const others = state.docs.filter((d) => d.id !== doc.id);
  if (!others.length) { toast("コピー先のファイルがありません（読み込みが1ファイルのみ）", true); return; }
  const msg = `「${doc.name}」の設定（矩形${s.rects.length}件・ページ範囲・奇数偶数）を\n` +
    `他の ${others.length} ファイルへコピーします。\n各ファイルの既存の設定は置き換えられます。よろしいですか？`;
  if (!confirm(msg)) return;
  for (const d of others) {
    state.settings[d.id] = {
      rects: s.rects.map((r) => ({ ...r })),
      range: s.range,
      parity: s.parity,
    };
  }
  afterRectsChange();
  toast(`${others.length} ファイルへ設定をコピーしました`);
};

els.refDoc.onchange = () => switchDoc(Number(els.refDoc.value));
els.prevPage.onclick = () => { if (state.page > 1) { state.page--; renderToolbar(); loadPageImage(); } };
els.nextPage.onclick = () => {
  const doc = currentDoc();
  if (doc && state.page < doc.pages) { state.page++; renderToolbar(); loadPageImage(); }
};
els.previewToggle.onchange = () => { state.preview = els.previewToggle.checked; drawOverlay(); };
document.querySelectorAll('input[name="color"]').forEach((el) => (el.onchange = drawOverlay));

// キーボード: ←→ページ送り / Delete・Backspace 選択矩形の削除 / Esc 選択解除 /
// ⌘C(Ctrl+C) 矩形コピー / ⌘V(Ctrl+V) 貼り付け
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && (e.key === "c" || e.key === "C")) {
    // 画面上の文字列を選択中のときは通常のテキストコピーを優先する
    if (String(window.getSelection())) return;
    e.preventDefault();
    copyRects();
    return;
  }
  if (mod && (e.key === "v" || e.key === "V")) {
    e.preventDefault();
    pasteRects();
    return;
  }
  if (e.key === "ArrowLeft") els.prevPage.click();
  if (e.key === "ArrowRight") els.nextPage.click();
  if ((e.key === "Delete" || e.key === "Backspace") && state.selectedIndex >= 0) {
    e.preventDefault();
    deleteRect(state.selectedIndex);
  }
  if (e.key === "Escape" && state.selectedIndex >= 0) {
    state.selectedIndex = -1;
    renderRectList();
    drawOverlay();
  }
});

// ---------------------------------------------------------------- 実行
els.applyBtn.onclick = async () => {
  els.applyBtn.disabled = true;
  els.applyBtn.textContent = "処理中…";
  els.results.innerHTML = "";
  try {
    // ファイルごとの設定を送る（矩形未設定のファイルはサーバー側でスキップ扱い）
    const files = state.docs.map((d) => {
      const s = ensureSettings(d.id);
      return { id: d.id, rects: s.rects, range: s.range, parity: s.parity };
    });
    const data = await api("/api/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ color: maskColor(), files }),
    });
    let okCount = 0, skipCount = 0, ngCount = 0, warnCount = 0;
    for (const r of data.results) {
      const div = document.createElement("div");
      if (r.skipped) {
        skipCount++;
        div.className = "skip";
        div.textContent = `− ${r.name}: 矩形未設定のためスキップ`;
      } else if (r.ok) {
        // 「処理した」ではなく「検査に通った」を出す。書き出したPDFを読み直して
        // 消えているか確かめた結果が r.check に入っている
        const c = r.check || {};
        const out = `<br><span class="out-name">→ output/${escapeHtml(r.output)}</span>`;
        const detail = (c.reasons || []).length
          ? `<br><span class="check-reason">${c.reasons.map(escapeHtml).join("<br>")}</span>`
          : "";
        if (c.verdict === "ng") {
          ngCount++;
          div.className = "ng";
          div.innerHTML = `✕ ${escapeHtml(r.name)}（${r.pagesMasked}ページ処理・<b>検査で消え残りを検出</b>）${out}${detail}`;
        } else if (c.verdict === "warn") {
          warnCount++;
          div.className = "warn";
          div.innerHTML = `⚠ ${escapeHtml(r.name)}（${r.pagesMasked}ページ処理・要確認）${out}${detail}`;
        } else {
          okCount++;
          div.className = "ok";
          div.innerHTML = `✓ ${escapeHtml(r.name)}（${r.pagesMasked}ページ・検査済み）${out}`;
        }
      } else {
        div.className = "ng";
        div.textContent = `✕ ${r.name}: ${r.error}`;
      }
      els.results.appendChild(div);
    }
    if (okCount || ngCount || warnCount) {
      const note = document.createElement("div");
      note.className = "warn-note";
      // 機械が見ているのは「指定した範囲の中が消えたか」だけ。範囲の外にある
      // 氏名（別欄の保護者署名など）や、そもそも置き場所を間違えた矩形は
      // 判定できない。ここを黙っていると「✓＝安全」と誤解される
      note.innerHTML =
        "⚠ 検査は<b>指定した範囲の中だけ</b>を見ています。押さえるべき欄がすべて入っているかは、" +
        "できあがったPDFを目で確かめてください。<br>" +
        "⚠ マスク前の元ファイルはこのツールでは移動・削除されません。所定の場所への隔離、または不要なら削除を忘れずに。";
      els.results.appendChild(note);
    }
    if (ngCount) {
      toast(`消え残りを検出: ${ngCount} ファイル。結果欄を確認してください`, true);
    } else {
      toast(
        `完了: ${okCount} ファイルを検査まで通しました` +
        (warnCount ? `（要確認 ${warnCount} 件）` : "") +
        (skipCount ? `（矩形未設定 ${skipCount} 件はスキップ）` : "")
      );
    }
  } catch (e) {
    toast(e.message, true);
  } finally {
    els.applyBtn.textContent = "マスキング実行（読み込んだ全PDF）";
    updateApplyButton();
  }
};

els.openOutput.onclick = () => api("/api/open-output", { method: "POST" }).catch((e) => toast(e.message, true));

// ------------------------------------------------------- 画面を閉じたら終了する
// この画面がアプリ本体の窓口なので、閉じたらアプリも終わる。
// ・一定間隔で「開いています」と伝える（心拍）
// ・閉じるときに合図を送る（再読み込みも同じ合図になるが、サーバー側が数秒待って
//   心拍が戻れば終了を取り消すので問題ない）
const CLIENT_ID = Math.random().toString(36).slice(2) + Date.now().toString(36);
const BEAT_MS = 5000;
let quitting = false;

function beat() {
  if (quitting) return;
  fetch("/api/heartbeat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ client_id: CLIENT_ID }),
    keepalive: true,
  }).catch(() => { /* 終了直後の失敗は無視 */ });
}
beat();
setInterval(beat, BEAT_MS);

// pagehide はタブを閉じる・別ページへ移動する・再読み込みのいずれでも起きる。
// sendBeacon は画面が消えたあとでも送り届けてくれる（fetchでは間に合わない）
window.addEventListener("pagehide", () => {
  const body = JSON.stringify({ client_id: CLIENT_ID });
  navigator.sendBeacon("/api/bye", new Blob([body], { type: "application/json" }));
});

els.quitBtn.onclick = async () => {
  if (!confirm("スミケシを終了します。よろしいですか？")) return;
  try {
    await api("/api/quit", { method: "POST" });
    quitting = true;
    document.body.innerHTML =
      '<div class="quit-screen"><h1>スミケシを終了しました</h1>' +
      "<p>このタブは閉じてかまいません。</p></div>";
    window.close();  // 自動で開いたタブは閉じられることがある
  } catch (e) {
    toast(e.message, true);
  }
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------- プリセット
async function refreshPresets(selectName) {
  try {
    const data = await api("/api/presets");
    els.presetSelect.innerHTML = '<option value="">（保存済みプリセット）</option>';
    for (const p of data.presets) {
      const opt = document.createElement("option");
      opt.value = p.name;
      opt.textContent = `${p.name}（矩形${p.rects.length}件・${p.saved || ""}）`;
      opt.dataset.preset = JSON.stringify(p);
      els.presetSelect.appendChild(opt);
    }
    if (selectName) els.presetSelect.value = selectName;
  } catch (e) { toast(e.message, true); }
}

els.presetSave.onclick = async () => {
  const name = els.presetName.value.trim();
  const s = cur();
  if (!name) { toast("プリセット名を入力してください", true); return; }
  if (!s || !s.rects.length) { toast("保存する矩形がありません（表示中のファイルに矩形を作成してください）", true); return; }
  try {
    const data = await api("/api/presets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        rects: s.rects,
        color: maskColor(),
        range: s.range,
        parity: s.parity,
      }),
    });
    toast(`プリセット「${data.name}」を保存しました`);
    els.presetName.value = "";
    refreshPresets(data.name);
  } catch (e) { toast(e.message, true); }
};

function selectedPreset() {
  const opt = els.presetSelect.selectedOptions[0];
  if (!opt || !opt.dataset.preset) { toast("読み込むプリセットを選択してください", true); return null; }
  return JSON.parse(opt.dataset.preset);
}

function presetToSettings(p) {
  return {
    rects: (p.rects || []).map((r) => ({ ...r })),
    range: p.range || "",
    parity: p.parity || "all",
  };
}

function applyPresetColor(p) {
  document.querySelector(`input[name="color"][value="${p.color || "black"}"]`).checked = true;
}

els.presetLoad.onclick = () => {
  const p = selectedPreset();
  if (!p) return;
  if (state.refId === null) { toast("PDFを読み込んでから実行してください", true); return; }
  state.settings[state.refId] = presetToSettings(p);
  state.selectedIndex = -1;
  applyPresetColor(p);
  syncInputsFromCur();
  afterRectsChange();
  toast(`プリセット「${p.name}」を表示中のファイルへ読み込みました`);
};

els.presetLoadAll.onclick = () => {
  const p = selectedPreset();
  if (!p) return;
  if (!state.docs.length) { toast("PDFを読み込んでから実行してください", true); return; }
  for (const d of state.docs) state.settings[d.id] = presetToSettings(p);
  state.selectedIndex = -1;
  applyPresetColor(p);
  syncInputsFromCur();
  afterRectsChange();
  toast(`プリセット「${p.name}」を全 ${state.docs.length} ファイルへ読み込みました`);
};

els.presetDelete.onclick = async () => {
  const opt = els.presetSelect.selectedOptions[0];
  if (!opt || !opt.value) { toast("削除するプリセットを選択してください", true); return; }
  if (!confirm(`プリセット「${opt.value}」を削除しますか？\n（trash フォルダに退避されます）`)) return;
  try {
    await api(`/api/presets/${encodeURIComponent(opt.value)}`, { method: "DELETE" });
    toast("削除しました（trash/ に退避）");
    refreshPresets();
  } catch (e) { toast(e.message, true); }
};

// ---------------------------------------------------------------- 初期化
async function init() {
  refreshPresets();
  try {
    const data = await api("/api/docs");
    state.docs = data.docs;
    state.docs.forEach((d) => ensureSettings(d.id));
    if (state.docs.length) {
      state.refId = state.docs[0].id;
      state.page = 1;
      loadPageImage();
    }
  } catch (e) { /* サーバー未接続時は無視 */ }
  syncInputsFromCur();
  updatePasteButton();
  renderAll();
}
init();
