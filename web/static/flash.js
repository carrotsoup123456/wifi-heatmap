/* 一键烧录 + 一键配网（WebSerial：esptool-js + Improv Serial）
   仅在 Chrome / Edge 可用；无硬件时所有按钮保持可点但会给出友好报错。 */
import { ESPLoader, Transport } from "./vendor/esptool-js-bundle.js";

const $ = (id) => document.getElementById(id);

/* ---------- UI 小工具 ---------- */
function setStatus(el, msg, kind) {
  el.textContent = msg;
  el.className = "flash-note" + (kind ? " note-" + kind : " muted");
}
function setProgress(pct) {
  const wrap = $("flash-progress-wrap");
  if (pct == null) { wrap.hidden = true; return; }
  wrap.hidden = false;
  $("flash-progress-fill").style.width = Math.round(pct * 100) + "%";
}
function setBusy(btn, busy, busyText) {
  if (busy) { btn.dataset.text = btn.textContent; btn.textContent = busyText; btn.disabled = true; }
  else { btn.textContent = btn.dataset.text || btn.textContent; btn.disabled = false; }
}

const espTerm = { clean() {}, write() {}, writeLine() {} };

/* ---------- 固件清单 + 校验 ---------- */
let _manifest = null;
async function manifest() {
  if (!_manifest) {
    const r = await fetch("/static/firmware/firmware.json");
    if (!r.ok) throw new Error("固件清单加载失败");
    _manifest = await r.json();
  }
  return _manifest;
}
// 芯片名归一化：兼容带封装/修订后缀的名字，如 "ESP32-C6 (QFN40)" → "esp32c6"
// 顺序注意：长型号在前，避免 "esp32c6…" 被 "esp32" 前缀抢先匹配
const _KNOWN_CHIPS = ["esp32c3", "esp32c5", "esp32c6", "esp32s2", "esp32s3", "esp32"];
const chipKey = (name) => {
  const k = (name || "").toLowerCase().replace(/[^a-z0-9]/g, "");
  return _KNOWN_CHIPS.find((x) => k.startsWith(x)) || k;
};

async function fetchFirmware(key, chipName) {
  const man = await manifest();
  const meta = man.chips[key];
  if (!meta) {
    const supported = Object.values(man.chips).map((c) => c.label).join(" / ");
    throw new Error("识别到的是「" + chipName + "」，不在支持列表里（支持：" + supported +
      "）。注意：ESP32-C2/ESP8684、H2、P4 这些型号没有 CSI 功能，无法用于采集，需要换 C3/C6/S3 等板子。");
  }
  const r = await fetch("/static/firmware/" + meta.file);
  if (!r.ok) throw new Error("固件文件读取失败");
  const buf = await r.arrayBuffer();
  if (buf.byteLength !== meta.size) throw new Error("固件文件大小不对，请刷新重试");
  const digest = await crypto.subtle.digest("SHA-256", buf);
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (hex !== meta.sha256) throw new Error("固件校验和不符，文件可能损坏");
  return { data: new Uint8Array(buf), version: man.version };
}

/* ---------- 一键烧录 ---------- */
async function oneClickFlash() {
  const btn = $("btn-flash"), st = $("flash-status");
  if (!("serial" in navigator)) {
    setStatus(st, "此浏览器不支持网页烧录（需要 Chrome / Edge）。可改用下面「备用」里的官方烧录器。", "err");
    return;
  }
  let transport = null;
  setBusy(btn, true, "⏳ 烧录中…");
  try {
    setStatus(st, "在弹窗里选择你的板子对应的串口…", "muted");
    const port = await navigator.serial.requestPort();
    transport = new Transport(port, false);
    const loader = new ESPLoader({ transport, baudrate: 115200, terminal: espTerm });

    setStatus(st, "连接板子、识别芯片…", "muted");
    setProgress(0);
    const chipName = await loader.main();
    setStatus(st, "识别到 " + chipName + "，读取并校验固件…", "muted");

    const fw = await fetchFirmware(chipKey(chipName), chipName);
    setStatus(st, "正在烧录 " + chipName + "（固件 v" + fw.version + "），不要拔线…", "muted");
    await loader.writeFlash({
      fileArray: [{ data: fw.data, address: 0x0 }],
      flashSize: "keep", flashMode: "keep", flashFreq: "keep",
      eraseAll: true, compress: true,
      reportProgress: (_i, written, total) => setProgress(written / total),
    });

    setStatus(st, "重启板子…", "muted");
    await loader.after("hard_reset");
    setProgress(null);
    setStatus(st, "✓ 烧录完成！等板子重启几秒后，在下面填 Wi-Fi 名和密码，点「一键配网」。", "ok");
    $("prov-box").classList.add("prov-glow");
    setTimeout(() => $("prov-box").classList.remove("prov-glow"), 3000);
    $("prov-ssid").focus();
  } catch (e) {
    setProgress(null);
    const msg = String((e && e.message) || e);
    if (/No port selected|user didn't select|NotAllowedError/i.test(msg)) {
      setStatus(st, "已取消选择串口。准备好后重新点「一键烧录」。", "muted");
    } else if (/Failed to connect|Timed out|sync|boot/i.test(msg)) {
      setStatus(st, "连不上板子：按住 BOOT → 点按 RESET → 松开 BOOT，再重新点「一键烧录」。", "err");
    } else {
      setStatus(st, "烧录失败：" + msg, "err");
    }
  } finally {
    if (transport) { try { await transport.disconnect(); } catch (_) {} }
    setBusy(btn, false);
  }
}

/* ---------- Improv Serial 一键配网 ---------- */
const IMPROV_MAGIC = [73, 77, 80, 82, 79, 86]; // "IMPROV"

function improvPacket(type, data) {
  const body = [1, type, data.length, ...data]; // version=1
  const pkt = [...IMPROV_MAGIC, ...body];
  const chk = pkt.reduce((a, b) => a + b, 0) & 0xff;
  return new Uint8Array([...pkt, chk]);
}
const rpc = (cmd, data = []) => improvPacket(0x03, [cmd, data.length, ...data]);

function wifiRpcPayload(ssid, pass) {
  const enc = new TextEncoder();
  const s = enc.encode(ssid), p = enc.encode(pass);
  return [s.length, ...s, p.length, ...p];
}

/* 从字节流里找下一个 IMPROV 包；返回 null 表示数据还不够 */
function nextPacket(buf) {
  let i = -1;
  outer: for (let k = 0; k + 6 <= buf.length; k++) {
    for (let m = 0; m < 6; m++) if (buf[k + m] !== IMPROV_MAGIC[m]) continue outer;
    i = k; break;
  }
  if (i < 0) return { buf: buf.slice(-5), pkt: null };
  if (i > 0) buf = buf.slice(i);
  if (buf.length < 9) return { buf, pkt: null };
  const len = buf[8];
  if (buf.length < 10 + len) return { buf, pkt: null };
  const raw = buf.slice(0, 10 + len);
  const chk = raw.slice(0, -1).reduce((a, b) => a + b, 0) & 0xff;
  const rest = buf.slice(10 + len);
  if (chk !== raw[raw.length - 1]) return { buf: rest, pkt: null }; // 丢包继续扫
  return { buf: rest, pkt: { type: raw[7], data: raw.slice(9, 9 + len) } };
}

async function improvProvision() {
  const btn = $("btn-provision"), st = $("prov-status");
  const ssid = $("prov-ssid").value.trim(), pass = $("prov-pass").value;
  if (!ssid) { setStatus(st, "先填 Wi-Fi 名（必须是 2.4GHz，不要用 5G）。", "err"); return; }
  if (!("serial" in navigator)) {
    setStatus(st, "此浏览器不支持网页配网（需要 Chrome / Edge）。", "err");
    return;
  }
  let port = null, reader = null, writer = null;
  setBusy(btn, true, "⏳ 配网中…");
  try {
    setStatus(st, "在弹窗里选择板子串口（和烧录时是同一个）…", "muted");
    port = await navigator.serial.requestPort();
    await port.open({ baudRate: 115200 });
    reader = port.readable.getReader();
    writer = port.writable.getWriter();

    let buf = [], state = 0, sentCreds = false, urls = [];
    const deadline = Date.now() + 40000;
    const askState = async () => { await writer.write(rpc(0x02)); };
    await askState();
    let lastAsk = Date.now();

    while (Date.now() < deadline) {
      const read = await Promise.race([
        reader.read(),
        new Promise((r) => setTimeout(() => r(null), 500)),
      ]);
      if (read && read.value) buf = buf.concat([...read.value]);
      let r;
      while ((r = nextPacket(buf)).pkt) {
        buf = r.buf;
        const { type, data } = r.pkt;
        if (type === 0x01) {           // Current state
          state = data[0];
          if (state === 0x02 && !sentCreds) {
            setStatus(st, "板子已就绪，发送 Wi-Fi 信息…", "muted");
            await writer.write(rpc(0x01, wifiRpcPayload(ssid, pass)));
            sentCreds = true;
          } else if (state === 0x03) {
            setStatus(st, "板子正在连接 Wi-Fi…", "muted");
          }
        } else if (type === 0x02) {      // Error state
          if (data[0] === 0x03) throw new Error("板子连不上这个 Wi-Fi：检查名称/密码，并确认是 2.4GHz 网络");
          if (data[0] !== 0x00) throw new Error("配网出错（代码 0x" + data[0].toString(16) + "）");
        } else if (type === 0x04) {      // RPC result：可能的设备 URL
          const dec = new TextDecoder();
          let p = 2;
          while (p < data.length) { const l = data[p]; const s = dec.decode(new Uint8Array(data.slice(p + 1, p + 1 + l))); if (s) urls.push(s); p += 1 + l; }
        }
        if (state === 0x04) break;
      }
      if (state === 0x04) break;
      if (Date.now() - lastAsk > 3000 && !sentCreds) { await askState(); lastAsk = Date.now(); }
    }
    if (state !== 0x04) throw new Error("等板子联网超时了：确认密码正确、是 2.4GHz，再点一次「一键配网」");
    setStatus(st, "✓ 配网成功，板子已联网！" + (urls.length ? " 设备地址：" + urls.join("、") + "。" : "") + " 现在去第 1 步点「发现设备」。", "ok");
  } catch (e) {
    const msg = String((e && e.message) || e);
    if (/No port selected|user didn't select|NotAllowedError/i.test(msg)) {
      setStatus(st, "已取消选择串口。", "muted");
    } else {
      setStatus(st, "配网失败：" + msg, "err");
    }
  } finally {
    try { if (reader) { await reader.cancel(); reader.releaseLock(); } } catch (_) {}
    try { if (writer) writer.releaseLock(); } catch (_) {}
    try { if (port) await port.close(); } catch (_) {}
    setBusy(btn, false);
  }
}

/* ---------- 接线 ---------- */
$("btn-flash").addEventListener("click", oneClickFlash);
$("btn-provision").addEventListener("click", improvProvision);
