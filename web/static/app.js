/* ===== Wi-Fi 热力图 · 一键采集平台（前端） ===== */

const PHASE_TEXT = {
  idle:       ["待 命", "badge-idle"],
  ready:      ["就 绪", "badge-ready"],
  collecting: ["采集中", "badge-collecting"],
  collected:  ["采集完成", "badge-done"],
  building:   ["整理中…", "badge-busy"],
  plotting:   ["出图中…", "badge-busy"],
  done:       ["全 部 完 成", "badge-done"],
};

/* Apple 风画布配色 */
const C = {
  ink:    "#1d1d1f",
  muted:  "#86868b",
  grid:   "#e8e8ed",
  room:   "#fafafc",
  border: "#d2d2d7",
  todo:   "#c7c7cc",
  now:    "#0a84ff",
  done:   "#30d158",
  val:    "#ff9f0a",
  ap:     "#ff453a",
  lineA:  "#0a84ff",
  lineB:  "#30d158",
  canvasBg: "#ffffff",
};

let lastState = null;

/* ---------- API ---------- */
async function api(path, opts) {
  const res = await fetch(path, opts);
  return res.json();
}
async function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

/* ---------- 模式 / 设备 ---------- */
async function setMode(mode) {
  if (lastState && lastState.mode !== mode) {
    if (lastState.phase === "collecting") {
      const ok = confirm(
        `正在采集第 ${lastState.batch_index + 1} 批。\n` +
        `切换模式会丢弃本批正在采的半成品数据，正在跑的点回到「未采集」；\n` +
        `已采完的点和数据会保存，下次切回来从这里继续。确定切换？`
      );
      if (!ok) return;
    } else if (lastState.phase === "building" || lastState.phase === "plotting") {
      alert("正在整理/出图，跑完再切换模式");
      return;
    }
  }
  await post("/api/mode", { mode, two_boards: document.getElementById("two-boards").checked });
  refresh(true);
}
async function setBoards() {
  await post("/api/mode", { mode: lastState?.mode || "demo", two_boards: document.getElementById("two-boards").checked });
  refresh(true);
}
async function discoverDevices() {
  const list = document.getElementById("device-list");
  list.innerHTML = '<p class="muted">正在发现设备…</p>';
  const data = await api("/api/devices");
  renderDevices(data.devices || []);
  refresh(true);
}
function renderDevices(devices) {
  const list = document.getElementById("device-list");
  if (!devices.length) {
    list.innerHTML = '<p class="muted">未发现设备（真实模式请先烧录配网；演示模式会显示虚拟板）</p>';
    return;
  }
  list.innerHTML = devices.map((d, i) => `
    <div class="device-card">
      <span class="dev-icon">◉</span>
      <div>
        <div class="dev-name">${d.name || "esp32c6"}</div>
        <div class="dev-ip">${d.ip || ""}</div>
      </div>
      <span class="dev-tag">板 ${i === 0 ? "A" : "B"}</span>
    </div>`).join("");
}

/* ---------- 采集控制 ---------- */
async function startRound() {
  const round = document.getElementById("round-name").value || "r1";
  if (!lastState) await refresh(true);   // 页面刚加载时先拿状态，避免误判模式
  if (lastState && lastState.mode === "demo") {
    // 演示模式：一键跑完（自动连采 25 批 → 整理 → 出图）
    await post("/api/collect/auto", { round });
  } else {
    await post("/api/collect/start", { round });
  }
  refresh(true);
}
async function nextBatch() { await post("/api/collect/next"); refresh(true); }
async function stopBatch() { await post("/api/collect/stop"); refresh(true); }

/* ---------- 房型导入 ---------- */
let layoutShapes = [];
async function loadLayout() {
  try {
    const r = await api("/api/layout");
    if (!r.ok) return;
    layoutShapes = r.shapes || [];
    const sel = document.getElementById("layout-shape");
    sel.innerHTML = layoutShapes.map(s =>
      `<option value="${s.id}">${s.name}</option>`).join("");
    const lay = r.layout;
    sel.value = lay.shape || "rect";
    renderLayoutParams(sel.value, lay.params);
    document.getElementById("layout-spacing").value = lay.spacing;
    document.getElementById("layout-apx").value = lay.ap_x;
    document.getElementById("layout-apy").value = lay.ap_y;
    document.getElementById("layout-current").textContent =
      `当前：${lay.name}（外接 ${lay.room_w}×${lay.room_h}m）· AP(${lay.ap_x}, ${lay.ap_y})` +
      ` · ${lay.n_train} 个测点`;
  } catch (e) { /* 后端没起来时静默 */ }
}
function renderLayoutParams(shapeId, values) {
  // 按所选形状动态渲染参数输入框；values 为空时用该形状默认值
  const sh = layoutShapes.find(x => x.id === shapeId) || layoutShapes[0];
  if (!sh) return;
  document.getElementById("layout-params").innerHTML = sh.fields.map(f => {
    const v = (values && values[f.key] != null) ? values[f.key] : sh.params[f.key];
    return `<label>${f.label}<input id="lp-${f.key}" type="number" step="0.1" ` +
      `min="${f.min}" max="${f.max}" value="${v}"></label>`;
  }).join("");
}
function onLayoutShape() {
  const sel = document.getElementById("layout-shape");
  renderLayoutParams(sel.value, null);   // 换形状恢复该形状默认尺寸
  previewLayout(true);                   // 选形状即预览
}
function layoutForm() {
  const shapeId = document.getElementById("layout-shape").value;
  const sh = layoutShapes.find(x => x.id === shapeId) || { fields: [] };
  const params = {};
  for (const f of sh.fields) {
    const el = document.getElementById(`lp-${f.key}`);
    params[f.key] = el ? parseFloat(el.value) : NaN;
  }
  return {
    shape: shapeId, params,
    spacing: parseFloat(document.getElementById("layout-spacing").value),
    ap_x: parseFloat(document.getElementById("layout-apx").value),
    ap_y: parseFloat(document.getElementById("layout-apy").value),
  };
}
async function previewLayout(quiet) {
  const r = await post("/api/layout/preview", layoutForm());
  if (!r.ok) {
    document.getElementById("layout-preview").style.display = "none";
    if (!quiet) alert(r.msg || "参数不合法");
    return;
  }
  document.getElementById("layout-preview").style.display = "";
  document.getElementById("layout-preview-text").textContent =
    `共 ${r.n_total} 个测点（全部一并采集、一并导出，训练集划分交给训练脚本）`;
  const f = layoutForm();
  drawLayoutMini(r.train, r.polygon, r.room_w, r.room_h, f.ap_x, f.ap_y);
}
async function applyLayout() {
  const body = layoutForm();
  const r0 = await post("/api/layout/preview", body);
  if (!r0.ok) { alert(r0.msg || "参数不合法"); return; }
  body.preset_id = body.shape;
  if (r0.same_points && r0.ap_changed) {
    // 点位没变、只动了路由器 → 按约定弹窗问用户
    const keep = confirm(
      "测点与当前完全一致，只改了路由器位置。\n\n" +
      "【确定】只是改坐标 → 保留已采数据\n" +
      "【取消】路由器真搬了家 → 清空重采");
    if (keep) {
      body.keep_data = true;
    } else {
      if (!confirm("确定清空演示和真实两个模式的全部已采数据吗？\n【取消】放弃本次应用")) return;
      body.keep_data = false;
    }
  } else if (r0.same_points) {
    body.keep_data = true;   // 什么实质都没变，静默保留
  } else {
    if (!confirm(`应用新房型将生成 ${r0.n_total} 个测点，并清空演示和真实两个模式的全部已采数据与进度。确定应用？`)) return;
    body.keep_data = false;
  }
  const r = await post("/api/layout/apply", body);
  if (!r.ok) { alert(r.msg || "应用失败"); return; }
  document.getElementById("layout-preview").style.display = "none";
  if (!r.kept) clearResults();
  await loadLayout();
  refresh(true);
}
function drawLayoutMini(train, polygon, w, h, apx, apy) {
  const cv = document.getElementById("layout-mini");
  const ctx = cv.getContext("2d");
  ctx.clearRect(0, 0, cv.width, cv.height);
  const pad = 24;
  const s = Math.min((cv.width - 2 * pad) / w, (cv.height - 2 * pad) / h);
  const ox = (cv.width - w * s) / 2, oy = (cv.height - h * s) / 2;
  const X = x => ox + x * s, Y = y => oy + (h - y) * s;   // y 轴向上
  // 房间多边形（L 型/U 型也能画出缺口）
  ctx.beginPath();
  (polygon || [[0, 0], [w, 0], [w, h], [0, h]]).forEach(([x, y], i) =>
    i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)));
  ctx.closePath();
  ctx.fillStyle = "#f5f5f7";
  ctx.fill();
  ctx.strokeStyle = "#86868b";
  ctx.stroke();
  for (const p of train) {
    ctx.fillStyle = "#0a84ff";
    ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 3.2, 0, 7); ctx.fill();
  }
  ctx.fillStyle = "#ff3b30";
  ctx.font = "14px sans-serif";
  ctx.fillText("★", X(apx) - 7, Y(apy) + 5);
}

async function checkPorts() {
  // 烧录部分：确认板子被电脑识别（只读）
  const el = document.getElementById("ports-result");
  el.textContent = "检测中…";
  try {
    const r = await api("/api/setup/ports");
    if (r.ports && r.ports.length) {
      el.textContent = "✓ 发现串口：" + r.ports.join("、") + " —— 板子已被识别，可以去烧录了";
    } else {
      el.textContent = "没发现串口：确认用的是数据线、换个 USB 口、插紧后 5 秒再点一次。";
    }
  } catch (e) {
    el.textContent = "检测失败：" + e;
  }
}
async function skipCollect() {
  // 演示模式专属：跳过采集等待，直接演示 整理 + 出图
  if (!lastState) await refresh(true);
  if (!lastState || lastState.mode !== "demo") return;
  if (lastState.phase === "collecting") {
    await post("/api/collect/skip");
  } else {
    const round = document.getElementById("round-name").value || "r1";
    await post("/api/collect/auto", { round, instant: true });
  }
  refresh(true);
}
async function resetAll()  { await post("/api/reset"); clearResults(); refresh(true); }
async function buildDataset() { await post("/api/build"); refresh(true); }
async function plotHeatmap()  { await post("/api/plot"); refresh(true); }

/* ---------- 状态渲染 ---------- */
function render(s) {
  lastState = s;

  // 模式按钮
  document.getElementById("mode-demo").classList.toggle("active", s.mode === "demo");
  document.getElementById("mode-real").classList.toggle("active", s.mode === "real");

  // 阶段徽章
  const [txt, cls] = PHASE_TEXT[s.phase] || ["?", "badge-idle"];
  const badge = document.getElementById("phase-badge");
  badge.textContent = txt;
  badge.className = "badge " + cls;

  // 批次横幅
  const banner = document.getElementById("batch-banner");
  const countEl = document.getElementById("batch-count");
  const ptsEl = document.getElementById("batch-points");
  const timerEl = document.getElementById("batch-timer");
  banner.className = "batch-banner " + (s.phase === "collecting" ? "collecting" : (s.phase === "ready" ? "ready" : "idle"));

  if (s.total_batches === 0) {
    countEl.textContent = "— / —";
    ptsEl.textContent = '点击「开始新一轮」';
    timerEl.textContent = "";
  } else if (s.phase === "collected" || s.phase === "done" || s.batch_index >= s.total_batches) {
    countEl.textContent = `${s.total_batches} / ${s.total_batches}`;
    ptsEl.innerHTML = "✓ 全部批次完成";
    timerEl.textContent = "";
  } else {
    countEl.textContent = `第 ${s.batch_index + 1} / ${s.total_batches} 批`;
    const b = s.batch || {};
    const parts = [];
    if (b.A) parts.push(`板A <span class="arrow">→</span> <span class="pt">${b.A}</span>`);
    if (b.B) parts.push(`板B <span class="arrow">→</span> <span class="pt">${b.B}</span>`);
    ptsEl.innerHTML = parts.join("&nbsp;&nbsp;") || "—";
    // 真实模式优先用采集器自报的进度
    const prg = s.progress;
    if (s.phase === "collecting" && s.mode === "real" && prg) {
      timerEl.textContent = Math.max(0, prg.duration - prg.elapsed).toFixed(0) + "s";
      timerEl.className = "batch-timer";
    } else if (s.phase === "collecting" && s.remaining != null) {
      timerEl.textContent = s.remaining.toFixed(1) + "s";
      timerEl.className = "batch-timer";
    } else if (s.phase === "ready") {
      timerEl.textContent = "✓ 已就绪";
      timerEl.className = "batch-timer done";
    } else {
      timerEl.textContent = "";
    }
  }

  // 进度条
  const pct = s.total_points ? (s.collected.length / s.total_points) * 100 : 0;
  document.getElementById("progress-bar").style.width = pct + "%";
  document.getElementById("progress-text").textContent = `已采 ${s.collected.length} / ${s.total_points} 个测点`;

  // 按钮可用性
  document.getElementById("btn-next").disabled =
    !(s.phase === "ready" || s.phase === "collected" || s.phase === "done") || s.batch_index >= s.total_batches;
  document.getElementById("btn-stop").disabled = (s.phase !== "collecting");
  document.getElementById("btn-start").disabled = (s.phase === "collecting" || s.phase === "building" || s.phase === "plotting");

  // 跳过按钮：仅演示模式；采集中 = 跳过剩余批次，空闲 = 直接秒采+出图
  const btnSkip = document.getElementById("btn-skip");
  const busy = (s.phase === "building" || s.phase === "plotting");
  btnSkip.hidden = (s.mode !== "demo" || busy);
  btnSkip.textContent = (s.phase === "collecting") ? "⏭ 跳过剩余采集" : "⏭ 跳过采集，直接出图";
  const donePhase = (s.phase === "collected" || s.phase === "done");
  const diskReady = (s.points_on_disk || 0) >= s.total_points;
  const flying = (s.phase === "collecting" || busy);
  const partialReady = s.collected.length >= 4;   // 实时保存：采够 4 个点就能先整理出图
  const buildDisabled = flying || (!donePhase && !diskReady && !partialReady);
  document.getElementById("btn-build").disabled = buildDisabled;
  document.getElementById("btn-plot").disabled = buildDisabled;

  // 模式相关的文案
  document.getElementById("btn-start").textContent =
    s.mode === "demo" ? "▶ 一键跑完（采集→整理→出图）" : "▶ 开始新一轮";
  document.getElementById("build-hint").textContent = buildDisabled
    ? (flying ? "采集/整理进行中，结束后可整理出图（数据每采完一个点就自动落盘）"
              : `采够 4 个点就能先整理出图（当前 ${s.collected.length}/${s.total_points}）`)
    : (donePhase ? "" : `已采 ${s.collected.length} 个点，可以先整理出图看看；采完 ${s.total_points} 点再出最终版`);
  document.getElementById("result-status").textContent = s.mode === "demo"
    ? "点「一键跑完」后自动完成 整理 + 出图"
    : `采完全部 ${s.total_points} 点后，先「整理数据」再「生成热力图」`;

  // 日志
  const logList = document.getElementById("log-list");
  if (s.log && s.log.length) {
    logList.innerHTML = s.log.map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join("");
    logList.scrollTop = logList.scrollHeight;
  }

  // 图
  drawMap(s);
  if (s.mode === "real") drawLiveReal(s);
  else drawLive(s);

  // 结果
  renderSummary(s);
  renderReport(s);
}

function escapeHtml(t) {
  return t.replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------- 点位图 ---------- */
function drawMap(s) {
  const cv = document.getElementById("map-canvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  const pad = 60;
  const { w: rw, h: rh } = s.room;
  const kx = (W - 2 * pad) / rw;
  const ky = (H - 2 * pad) / rh;
  const X = x => pad + x * kx;
  const Y = y => H - pad - y * ky;   // y 向上

  ctx.clearRect(0, 0, W, H);

  // 房间底色（支持 L 型/U 型多边形）
  const poly = (s.room.polygon && s.room.polygon.length >= 3) ? s.room.polygon : null;
  const roomPath = () => {
    ctx.beginPath();
    if (poly) {
      poly.forEach(([x, y], i) => i ? ctx.lineTo(X(x), Y(y)) : ctx.moveTo(X(x), Y(y)));
      ctx.closePath();
    } else {
      roundRect(ctx, X(0), Y(rh), rw * kx, rh * ky, 10);
    }
  };
  ctx.fillStyle = C.room;
  roomPath(); ctx.fill();

  // 区块网格：行列数按训练点推导，裁剪到房间轮廓内
  const txs = [...new Set(s.sites.filter(p => p.role === "train").map(p => p.x))];
  const tys = [...new Set(s.sites.filter(p => p.role === "train").map(p => p.y))];
  const cols = Math.max(2, txs.length), rows = Math.max(2, tys.length);
  ctx.save();
  roomPath(); ctx.clip();
  ctx.strokeStyle = C.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i <= cols; i++) {
    const gx = (rw / cols) * i;
    ctx.beginPath(); ctx.moveTo(X(gx), Y(0)); ctx.lineTo(X(gx), Y(rh)); ctx.stroke();
  }
  for (let j = 0; j <= rows; j++) {
    const gy = (rh / rows) * j;
    ctx.beginPath(); ctx.moveTo(X(0), Y(gy)); ctx.lineTo(X(rw), Y(gy)); ctx.stroke();
  }
  ctx.restore();

  // 房间边框
  ctx.strokeStyle = C.border;
  ctx.lineWidth = 2;
  roomPath(); ctx.stroke();

  // 当前批次的点
  const nowPts = [];
  if (s.phase === "collecting" || s.phase === "ready") {
    const b = s.batch || {};
    if (b.A) nowPts.push(b.A);
    if (b.B) nowPts.push(b.B);
  }

  // 测点
  for (const site of s.sites) {
    const cx = X(site.x), cy = Y(site.y);
    const isVal = site.role === "validate";
    const done = s.collected.includes(site.point_id);
    const now = nowPts.includes(site.point_id);

    let color = C.todo;
    if (done) color = C.done;
    else if (now) color = C.now;
    else if (isVal) color = C.val;

    if (now) {
      ctx.beginPath();
      ctx.arc(cx, cy, 16, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(10,132,255,.18)";
      ctx.fill();
    }

    ctx.beginPath();
    ctx.arc(cx, cy, 9, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 2;
    ctx.stroke();

    ctx.fillStyle = C.ink;
    ctx.font = "11px Menlo, monospace";
    ctx.textAlign = "center";
    ctx.fillText(site.point_id, cx, cy - 15);
  }

  // AP（左下角）
  const ax = X(s.room.ap[0]), ay = Y(s.room.ap[1]);
  ctx.fillStyle = C.ap;
  ctx.save();
  ctx.translate(ax, ay);
  ctx.beginPath();
  for (let i = 0; i < 5; i++) {
    const a = (i * 4 * Math.PI) / 5 - Math.PI / 2;
    ctx.lineTo(Math.cos(a) * 14, Math.sin(a) * 14);
  }
  ctx.closePath();
  ctx.fill();
  ctx.restore();
  ctx.fillStyle = C.ap;
  ctx.font = "bold 12px -apple-system, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("AP", ax + 18, ay + 4);

  // 原点标注
  ctx.fillStyle = C.muted;
  ctx.font = "11px -apple-system, sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("原点 = 路由器墙角", X(0) + 4, Y(0) + 18);
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* ---------- 实时曲线（演示模式） ---------- */
function drawLive(s) {
  const cv = document.getElementById("live-canvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  setLiveLegend("curves");

  const live = s.live || [];
  const hint = document.getElementById("live-hint");
  if (!live.length) {
    hint.textContent = s.phase === "collecting" ? "等待数据…" : "RSSI (dBm)";
    ctx.fillStyle = C.todo;
    ctx.font = "13px -apple-system, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(s.phase === "collecting" ? "…" : "采集时这里会显示实时信号曲线", W / 2, H / 2);
    return;
  }
  hint.textContent = `RSSI (dBm) · ${live.length} 点`;

  const a = live.map(p => p[1]).filter(v => v != null);
  const b = live.map(p => p[2]).filter(v => v != null);
  const all = a.concat(b);
  let lo = Math.min(...all), hi = Math.max(...all);
  if (hi - lo < 4) { const m = (hi + lo) / 2; lo = m - 2; hi = m + 2; }
  lo -= 1; hi += 1;

  const X = i => 40 + (i / Math.max(live.length - 1, 1)) * (W - 60);
  const Y = v => H - 24 - ((v - lo) / (hi - lo)) * (H - 44);

  ctx.strokeStyle = C.grid;
  ctx.fillStyle = C.muted;
  ctx.font = "10px Menlo, monospace";
  ctx.textAlign = "right";
  for (let g = 0; g <= 4; g++) {
    const v = lo + ((hi - lo) * g) / 4;
    const y = Y(v);
    ctx.beginPath(); ctx.moveTo(40, y); ctx.lineTo(W - 20, y); ctx.stroke();
    ctx.fillText(v.toFixed(0), 34, y + 3);
  }

  function drawSeries(getter, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    live.forEach((p, i) => {
      const v = getter(p);
      if (v == null) return;
      if (!started) { ctx.moveTo(X(i), Y(v)); started = true; }
      else ctx.lineTo(X(i), Y(v));
    });
    ctx.stroke();
    for (let i = live.length - 1; i >= 0; i--) {
      const v = getter(live[i]);
      if (v != null) {
        ctx.fillStyle = color;
        ctx.beginPath(); ctx.arc(X(i), Y(v), 3.5, 0, Math.PI * 2); ctx.fill();
        ctx.font = "bold 12px Menlo, monospace";
        ctx.textAlign = "left";
        ctx.fillText(v.toFixed(1), Math.min(X(i) + 8, W - 46), Y(v) - 8);
        break;
      }
    }
  }
  drawSeries(p => p[1], C.lineA);
  drawSeries(p => p[2], C.lineB);
}

/* ---------- 实时进度（真实模式：采集器自报的包数/pps/掉包率） ---------- */
function drawLiveReal(s) {
  const cv = document.getElementById("live-canvas");
  const ctx = cv.getContext("2d");
  const W = cv.width, H = cv.height;
  ctx.clearRect(0, 0, W, H);
  setLiveLegend("real");

  const hint = document.getElementById("live-hint");
  const prg = s.progress;
  if (!prg) {
    hint.textContent = "真实采集进度";
    ctx.fillStyle = C.todo;
    ctx.font = "13px -apple-system, sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(s.phase === "collecting" ? "正在连接设备…" : "采集时这里显示已收包数 / 速率 / 掉包率", W / 2, H / 2);
    return;
  }
  hint.textContent = `${prg.status} · 目标 ${prg.duration.toFixed(0)}s`;

  // 大包数
  ctx.fillStyle = C.ink;
  ctx.font = "800 52px -apple-system, sans-serif";
  ctx.textAlign = "center";
  ctx.fillText(String(prg.packets), W / 2, 78);
  ctx.fillStyle = C.muted;
  ctx.font = "13px -apple-system, sans-serif";
  ctx.fillText("已接收数据包", W / 2, 102);

  // 进度条（elapsed/duration）
  const pct = Math.min(1, prg.elapsed / prg.duration);
  const bx = 40, bw = W - 80, by = 130, bh = 12;
  ctx.fillStyle = C.grid;
  roundRect(ctx, bx, by, bw, bh, 6); ctx.fill();
  const grad = ctx.createLinearGradient(bx, 0, bx + bw, 0);
  grad.addColorStop(0, "#0a84ff"); grad.addColorStop(1, "#64d2ff");
  ctx.fillStyle = grad;
  roundRect(ctx, bx, by, bw * pct, bh, 6); ctx.fill();
  ctx.fillStyle = C.muted;
  ctx.font = "12px Menlo, monospace";
  ctx.textAlign = "left";
  ctx.fillText(`${prg.elapsed.toFixed(1)}s`, bx, by + 32);
  ctx.textAlign = "right";
  ctx.fillText(`${prg.duration.toFixed(0)}s`, bx + bw, by + 32);

  // pps / drop
  ctx.textAlign = "center";
  ctx.font = "600 15px -apple-system, sans-serif";
  ctx.fillStyle = prg.pps > 50 ? C.done : C.val;
  ctx.fillText(`速率 ${prg.pps} pps`, W / 2 - 90, 200);
  ctx.fillStyle = prg.drop < 5 ? C.done : C.ap;
  ctx.fillText(`掉包 ${prg.drop.toFixed(1)}%`, W / 2 + 90, 200);
}

function setLiveLegend(kind) {
  const el = document.getElementById("live-legend");
  if (kind === "real") {
    el.innerHTML = "<span>数据来自采集器实时输出</span>";
  } else {
    el.innerHTML = '<span><i class="line line-a"></i>板 A</span><span><i class="line line-b"></i>板 B</span>';
  }
}

/* ---------- 结果 ---------- */
// 结果区只在内容变化时重渲染，否则每秒轮询会反复重写 DOM、
// 热力图 <img> 带新时间戳反复下载 -> 页面频闪
let lastSummaryKey = null;
let lastReportKey = null;

function renderSummary(s) {
  const box = document.getElementById("summary-box");
  if (!s.summary) { lastSummaryKey = null; return; }
  const key = s.summary.ok + "|" + (s.summary.tail || "");
  if (key === lastSummaryKey) return;
  lastSummaryKey = key;
  if (!s.summary.ok) {
    box.innerHTML = `<pre>整理出错：\n${escapeHtml(s.summary.tail || "")}</pre>`;
    return;
  }
  box.innerHTML = `<pre>${escapeHtml(s.summary.tail || "完成")}</pre>`;
  loadSummaryTable();
}

async function loadSummaryTable() {
  try {
    const res = await fetch("/outputs/测点汇总表.csv?ts=" + Date.now());
    if (!res.ok) return;
    const text = await res.text();
    const lines = text.split("\n").filter(l => l.trim()).slice(0, 27);
    const rows = lines.map(l => l.split(","));
    let html = "<table>";
    rows.forEach((r, i) => {
      const tag = i === 0 ? "th" : "td";
      html += "<tr>" + r.slice(0, 7).map(c => `<${tag}>${escapeHtml(c)}</${tag}>`).join("") + "</tr>";
    });
    html += "</table>";
    document.getElementById("summary-box").innerHTML = html;
  } catch (e) { /* 忽略 */ }
}

function renderReport(s) {
  const box = document.getElementById("report-box");
  const heat = document.getElementById("heatmap-box");
  const status = document.getElementById("result-status");
  if (!s.report) { lastReportKey = null; return; }
  const key = s.report.ok + "|" + (s.report.tail || "");
  if (key === lastReportKey) return;
  lastReportKey = key;
  if (!s.report.ok) {
    box.innerHTML = `<pre>出图出错：\n${escapeHtml(s.report.tail || "")}</pre>`;
    return;
  }
  box.innerHTML = `<pre>${escapeHtml(s.report.tail || "完成")}</pre>`;
  const ts = Date.now();
  heat.innerHTML = `
    <img src="/outputs/热力图_预测.png?ts=${ts}" alt="热力图">
    <img src="/outputs/热力图_对比.png?ts=${ts}" alt="对比图" style="margin-top:10px">`;
  status.textContent = "✓ 全部完成：上面是模型重建的全屋 Wi-Fi 信号分布";
}

function clearResults() {
  lastSummaryKey = null;
  lastReportKey = null;
  document.getElementById("summary-box").innerHTML = '<p class="muted">尚无数据</p>';
  document.getElementById("heatmap-box").innerHTML = '<p class="muted">尚无图片</p>';
  document.getElementById("report-box").innerHTML = '<p class="muted">尚无报告</p>';
}

/* ---------- 轮询 ---------- */
async function refresh(force) {
  try {
    const s = await api("/api/state");
    render(s);
  } catch (e) { /* 后端没起来时静默 */ }
}
setInterval(refresh, 500);
refresh(true);
loadLayout();

/* ===== 使用说明弹窗 ===== */
(function () {
  const fab = document.getElementById("help-fab");
  const modal = document.getElementById("help-modal");
  const close = document.getElementById("help-close");
  if (!fab || !modal) return;
  const open = () => { modal.hidden = false; };
  const shut = () => { modal.hidden = true; };
  fab.addEventListener("click", open);
  close.addEventListener("click", shut);
  modal.addEventListener("click", (e) => { if (e.target === modal) shut(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) shut(); });
})();
