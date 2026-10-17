#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wi-Fi 热力图 · 一键采集网站（后端）

一个本地网页，把原本要手敲的 4 条命令变成按钮：
    自检 → 采集 → 整理 → 出图

支持两种模式：
    demo  演示模式：不插板子，用模拟数据走完整流水线（给评委演示用，每批加速到 6 秒）
    real  真实模式：调用 ESPectre CLI 采集真实板子的数据

启动：
    tools/espectre/.venv/bin/python web/app.py
然后浏览器打开  http://127.0.0.1:8000
"""

from __future__ import annotations

import csv
import io
import json
import math
import random
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import layout as layout_mod

import numpy as np
from flask import Flask, jsonify, request, send_file, send_from_directory, render_template

# ---------------------------------------------------------------- 路径
ROOT = Path(__file__).resolve().parents[1]
SITES_CSV = ROOT / "scripts" / "sites.csv"
POINTS_DIR = ROOT / "data" / "points"
SESSION_DIR = ROOT / "data" / "_sessions"      # 各模式的进度快照 <mode>.json
PARK_DIR = ROOT / "data" / "_parked"           # 非活跃模式的数据目录停靠处
OWNER_FILE = SESSION_DIR / "_active_owner.json"  # 记录 data/points 当前属于哪个模式
OUTPUTS_DIR = ROOT / "outputs"
VENV_PY = ROOT / "tools" / "espectre" / ".venv" / "bin" / "python"
ESPECTRE = ROOT / "tools" / "espectre" / "espectre"
ESPECTRE_DATA = ROOT / "tools" / "espectre" / "micro-espectre" / "data"  # v3 CLI 实际写这里

ROOM_W, ROOM_H = 6.62, 7.74
AP_X, AP_Y = 0.30, 0.30
LAYOUT: dict = {}   # 当前房型（reload_layout 填充，随 state 发给前端）

BATCH_SECONDS_REAL = 60     # 真实模式每批采集秒数
BATCH_SECONDS_DEMO = 6      # 演示模式每批加速秒数

app = Flask(__name__)

# ---------------------------------------------------------------- 测点
def load_sites() -> list[dict]:
    rows = []
    with SITES_CSV.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            rows.append({
                "point_id": r["point_id"],
                "block": r["block"],
                "role": r["role"],
                "x": float(r["x_m"]),
                "y": float(r["y_m"]),
            })
    return rows

SITES = load_sites()
# 采集顺序：按 sites.csv 的顺序（不再区分训练/验证点，全部一并采集导出）
POINT_ORDER = [s["point_id"] for s in SITES]
SITE_BY_ID = {s["point_id"]: s for s in SITES}


def reload_layout() -> None:
    """从 scripts/layout.json + sites.csv 重载房间几何与测点（换房型后调用）。"""
    global ROOM_W, ROOM_H, AP_X, AP_Y, SITES, POINT_ORDER, SITE_BY_ID, LAYOUT
    LAYOUT = layout_mod.load_layout()
    ROOM_W, ROOM_H = LAYOUT["room_w"], LAYOUT["room_h"]
    AP_X, AP_Y = LAYOUT["ap_x"], LAYOUT["ap_y"]
    SITES = load_sites()
    POINT_ORDER = [s["point_id"] for s in SITES]
    SITE_BY_ID = {s["point_id"]: s for s in SITES}


reload_layout()


def make_batches(two_boards: bool = True) -> list[dict]:
    """把测点切成批次。双板：每批 2 个点；单板：每批 1 个。
    顺序取「当前生效方案」：用户自定义子集（按点击顺序）或默认全部测点。"""
    step = 2 if two_boards else 1
    order = STATE.get("custom_points") or POINT_ORDER
    batches = []
    for i in range(0, len(order), step):
        chunk = order[i:i + step]
        batches.append({"A": chunk[0], "B": chunk[1] if len(chunk) > 1 else None})
    return batches


def active_order() -> list:
    """当前生效的采集顺序：自定义子集优先，否则默认全量。"""
    return STATE.get("custom_points") or POINT_ORDER


# ---------------------------------------------------------------- 全局状态
LOCK = threading.Lock()
STATE = {
    "mode": "demo",            # demo | real
    "phase": "idle",           # idle|ready|collecting|collected|building|plotting|done
    "two_boards": False,       # 默认单板（用户只有一块板子）
    "round": "r1",
    "batch_index": 0,
    "batches": [],             # list of {"A":..,"B":..}
    "batch_start": None,       # epoch
    "batch_seconds": BATCH_SECONDS_DEMO,
    "collected": [],           # 已采完的测点 id
    "live": [],                # 演示模式实时曲线 [(t, rssA, rssB), ...]
    "progress": None,          # 真实模式采集进度 {"status","elapsed","duration","pps","drop","packets"}
    "log": [],                 # 事件日志（最近 50 条）
    "devices": [],
    "summary": None,           # 04 跑完后的汇总（行数等）
    "report": None,            # 05 跑完后的评估摘要
    "error": None,
    # 产物新鲜度标记：只有「跑完一整轮」之后再「整理/出图」，下载包才放对应产物
    "custom_points": None,     # 自定义采集方案（按点击顺序的点 id 列表）；None = 默认全部 56 点
    "round_complete_at": None, # 本轮完整采完的时刻（epoch）
    "built_at": None,          # 整理数据最近一次成功的时刻
    "plotted_at": None,        # 生成热力图最近一次成功的时刻
}
_worker_thread = None
_stop_flag = threading.Event()
_instant_flag = threading.Event()   # 演示模式"跳过"：批次倒计时瞬间完成
_current_procs: list[subprocess.Popen] = []   # 真实模式当前批次子进程（用于停止）


def log(msg: str) -> None:
    with LOCK:
        STATE["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        STATE["log"] = STATE["log"][-50:]


def set_phase(phase: str) -> None:
    with LOCK:
        STATE["phase"] = phase


# ---------------------------------------------------------------- 模式会话
# 切换模式时的约定：
#   1. 正在跑的批次停掉，半成品数据删除，正在跑的点保持「未采集」
#   2. 当前模式的进度存 data/_sessions/<mode>.json，data/points 整体泊到 _parked/<mode>/
#   3. 切回来时还原进度和数据，从「正在跑的那个点」继续
# 注意：save_session / apply_session 须在 LOCK 内调用（它们自己不再加锁）。

def save_session(mode: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "collected": list(STATE["collected"]),
        "batch_index": STATE["batch_index"],
        "two_boards": STATE["two_boards"],
        "round": STATE["round"],
        "custom_points": STATE.get("custom_points"),
        "round_complete_at": STATE.get("round_complete_at"),
        "built_at": STATE.get("built_at"),
        "plotted_at": STATE.get("plotted_at"),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (SESSION_DIR / f"{mode}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_session(mode: str) -> dict | None:
    try:
        return json.loads((SESSION_DIR / f"{mode}.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def apply_session(sess: dict) -> None:
    """把会话快照灌回 STATE（须在 LOCK 内调用）。被中断的批次没有推进 batch_index，
    所以恢复后正好停在「正在跑的那个点」，且它不在 collected 里 = 未采集状态。"""
    STATE["collected"] = [p for p in sess.get("collected", []) if p in SITE_BY_ID]
    STATE["two_boards"] = bool(sess.get("two_boards", False))
    STATE["round"] = sess.get("round", "r1")
    STATE["batches"] = make_batches(STATE["two_boards"])
    STATE["batch_index"] = min(int(sess.get("batch_index", 0)), len(STATE["batches"]))
    STATE.update({"batch_start": None, "live": [], "progress": None,
                  "summary": None, "report": None, "error": None,
                  "custom_points": sess.get("custom_points"),
                  "round_complete_at": sess.get("round_complete_at"),
                  "built_at": sess.get("built_at"),
                  "plotted_at": sess.get("plotted_at")})
    if STATE["batch_index"] < len(STATE["batches"]):
        STATE["phase"] = "ready"
    else:
        STATE["phase"] = "collected" if STATE["collected"] else "idle"


def park_data(mode: str) -> None:
    """把 data/points 整体泊到 _parked/<mode>/points（数据不丢，切回来还原）。"""
    if POINTS_DIR.exists() and any(POINTS_DIR.iterdir()):
        dst = PARK_DIR / mode / "points"
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(POINTS_DIR), str(dst))


def unpark_data(mode: str) -> None:
    src = PARK_DIR / mode / "points"
    if src.exists():
        if POINTS_DIR.exists():
            shutil.rmtree(POINTS_DIR)
        shutil.move(str(src), str(POINTS_DIR))


def write_owner(mode: str) -> None:
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    OWNER_FILE.write_text(json.dumps({"owner": mode}), encoding="utf-8")


def purge_inflight(points: list[str], since: float) -> None:
    """删除正在跑的那批写出的半成品文件（mtime >= since），保留这些点更早的历史数据。"""
    n = 0
    for p in points:
        label = p.lower()
        for base in (POINTS_DIR / label, ESPECTRE_DATA / label):
            if not base.exists():
                continue
            for f in base.glob("*.npz"):
                try:
                    if f.stat().st_mtime >= since - 1:   # 1s 容差
                        f.unlink()
                        n += 1
                except OSError:
                    pass
    if n:
        log(f"已删除正在跑批次的 {n} 个半成品数据文件")


# ---------------------------------------------------------------- 模拟数据
def simulate_rss(x: float, y: float, t: float) -> float:
    """演示用的 RSS 模型：路径损耗 + 缓慢漂移 + 快噪声。"""
    d = max(math.hypot(x - AP_X, y - AP_Y), 0.3)
    base = -30.0 - 25.0 * math.log10(d)
    drift = 0.6 * math.sin(t / 3.0)
    noise = random.gauss(0, 0.7)
    return base + drift + noise


def generate_npz(point_id: str, out_dir: Path) -> Path:
    """为一个测点生成一个模拟 npz（格式与真实采集一致）。"""
    s = SITE_BY_ID[point_id]
    d = max(math.hypot(s["x"] - AP_X, s["y"] - AP_Y), 0.3)
    rss = -30.0 - 25.0 * math.log10(d) + random.gauss(0, 0.6)

    n = 6000
    rng = np.random.default_rng(abs(hash((point_id, time.time()))) % (2**32))
    rssi = np.round(rng.normal(rss, 1.2, n)).astype(np.int16)
    nf = np.full(n, -95, dtype=np.int16)
    channel = np.ones(n, dtype=np.uint8)
    csi = rng.normal(0, 6, (n, 128)).astype(np.int8)

    label = point_id.lower()
    d_dir = out_dir / label
    d_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{label}_esp32c6_64sc_{random.getrandbits(64):016x}_{time.strftime('%Y%m%d_%H%M%S')}_{1:04d}.npz"
    tmp_name = fname + ".tmp"          # np.savez 会自动补 .npz -> 实际写出 fname + ".tmp.npz"
    np.savez_compressed(               # 先写临时名再改名：下载/整理不会读到写了一半的文件
        d_dir / tmp_name,
        csi_data=csi, num_subcarriers=np.int64(64), label=label, chip="esp32c6",
        collected_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        duration_ms=np.float64(60000.0), format_version="1.2",
        device_id=np.uint64(random.getrandbits(64)), frontend="native",
        firmware_version="demo", requested_pps=np.float64(100.0),
        effective_pps=np.float64(99.5), rssi_dbm=rssi, noise_floor_dbm=nf,
        channel=channel,
        phy_mode=np.array(["ht"] * n), ltf_type=np.array(["ht-ltf"] * n),
        channel_width=np.array(["20"] * n),
    )
    (d_dir / (tmp_name + ".npz")).replace(d_dir / fname)   # 原子改名
    return d_dir / fname


# ---------------------------------------------------------------- 采集调度
def current_batch() -> dict | None:
    with LOCK:
        if 0 <= STATE["batch_index"] < len(STATE["batches"]):
            return STATE["batches"][STATE["batch_index"]]
    return None


STATUS_RE = re.compile(
    r"STATUS:\s*(\w+).*?elapsed\s+([\d.]+)/([\d.]+)s\s*\|\s*capture\s+(\d+)"
)  # v3 CLI 格式：STATUS: RECORDING 1/1 | elapsed 4.6/5.0s | capture 464


def collect_point_real(point_id: str, ip: str, round_name: str) -> bool:
    """真实模式：调 espectre collect 采一个点，流式解析进度，最后归档 npz。成功返回 True。"""
    s = SITE_BY_ID[point_id]
    label = point_id.lower()
    desc = f"point={point_id};block={s['block']};role={s['role']};x={s['x']};y={s['y']};round={round_name}"
    cmd = [
        str(VENV_PY), str(ESPECTRE), "collect",
        "--target", ip, "--label", label,
        "--duration", str(BATCH_SECONDS_REAL), "--pps", "100",
        "--ready-stable-seconds", "0", "--detector", "lightweight",
        "--description", desc,
    ]
    log(f"真实采集 {point_id} -> {ip}")
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=str(ROOT),
    )
    with LOCK:
        _current_procs.append(proc)
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            m = STATUS_RE.search(line)
            if m:
                with LOCK:
                    STATE["progress"] = {
                        "status": m.group(1), "elapsed": float(m.group(2)),
                        "duration": float(m.group(3)), "packets": int(m.group(4)),
                        "pps": 100, "drop": 0.0,
                    }
            elif "Saved" in line or "No packets" in line or "Error" in line or "failed" in line.lower():
                log(f"[{point_id}] {line[-110:]}")
        proc.wait(timeout=BATCH_SECONDS_REAL + 120)
    finally:
        with LOCK:
            if proc in _current_procs:
                _current_procs.remove(proc)
    # v3 CLI 质量准入门（占用率/包数）不过会 exit(1)，但 npz 已落盘——有数据就算成功（告警照记）
    src = ESPECTRE_DATA / label
    has_data = src.exists() and any(src.glob("*.npz"))
    if proc.returncode != 0 and not has_data:
        log(f"[{point_id}] 采集器返回码 {proc.returncode}，且无数据文件，请检查设备/网络")
        return False
    if proc.returncode != 0:
        log(f"[{point_id}] 质量门未过但数据已保存（返回码 {proc.returncode}），按成功归档")
    # 归档：tools/espectre/data/<label>/ -> data/points/<label>/
    dst = POINTS_DIR / label
    dst.mkdir(parents=True, exist_ok=True)
    n_new = 0
    if src.exists():
        for f in src.glob("*.npz"):
            tgt = dst / f.name
            if not tgt.exists():
                tmp = tgt.with_name(tgt.name + ".tmp")   # 原子写：避免下载/整理读到写了一半的文件
                tmp.write_bytes(f.read_bytes())
                tmp.replace(tgt)
                n_new += 1
    log(f"[{point_id}] 归档 {n_new} 个数据文件")
    return True


def run_one_batch(batch: dict) -> bool:
    """采集当前这一批（demo 模拟 / real 调 CLI）。采完返回 True，被停止返回 False。"""
    with LOCK:
        STATE["batch_start"] = time.time()
        STATE["live"] = []
    set_phase("collecting")
    pts = [p for p in (batch["A"], batch["B"]) if p]
    log(f"开始第 {STATE['batch_index']+1}/{len(STATE['batches'])} 批：{' + '.join(pts)}")

    if STATE["mode"] == "demo":
        # 演示模式：加速倒计时 + 实时曲线 + 到时生成 npz
        secs = STATE["batch_seconds"]
        t0 = time.time()
        while (time.time() - t0 < secs
               and not _stop_flag.is_set()
               and not _instant_flag.is_set()):
            t = time.time() - t0
            with LOCK:
                rss_a = simulate_rss(SITE_BY_ID[batch["A"]]["x"], SITE_BY_ID[batch["A"]]["y"], t)
                rss_b = simulate_rss(SITE_BY_ID[batch["B"]]["x"], SITE_BY_ID[batch["B"]]["y"], t) if batch["B"] else None
                STATE["live"].append((round(t, 2), round(rss_a, 2), round(rss_b, 2) if rss_b is not None else None))
                STATE["live"] = STATE["live"][-240:]
            time.sleep(0.15)
        if _stop_flag.is_set():
            log("本批已停止，不计入采集结果")
            set_phase("ready")
            return False
        for p in pts:
            generate_npz(p, POINTS_DIR)
    else:
        # 真实模式：并行起 collect 子进程（单板就 1 个），流式进度见 progress
        ips = STATE.get("device_ips") or [d.get("ip", "") for d in STATE.get("devices", [])]
        with LOCK:
            STATE["progress"] = None
        results: dict = {}
        threads = []
        mapping = [("A", batch["A"]), ("B", batch["B"])]
        for idx, (key, pid) in enumerate(mapping):
            if not pid:
                continue
            ip = ips[idx] if idx < len(ips) else ""
            if not ip:
                log(f"没有板 {key} 的 IP，跳过 {pid}（先点「发现设备」）")
                continue
            th = threading.Thread(
                target=lambda p=pid, i=ip: results.__setitem__(p, collect_point_real(p, i, STATE["round"])))
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
        if _stop_flag.is_set():
            log("本批已停止，不计入采集结果")
            set_phase("ready")
            return False
        if not results:
            log("本批没有板子可用，未计入进度（先点「发现设备」确认板子在线）")
            set_phase("ready")
            return False
        pts = [p for p in pts if results.get(p)]   # 只有真正采成功的点才计入
        if not all(results.values()):
            log("本批有点位采集失败：已采到的保留，批次不推进，排查后再点「采集本批」重试")
            with LOCK:
                for p in pts:
                    if p not in STATE["collected"]:
                        STATE["collected"].append(p)
                STATE["batch_start"] = None
            save_session(STATE["mode"])
            set_phase("ready")
            return False

    with LOCK:
        for p in pts:
            if p not in STATE["collected"]:
                STATE["collected"].append(p)
        STATE["batch_index"] += 1
        STATE["batch_start"] = None
    log(f"第 {STATE['batch_index']} 批完成")
    save_session(STATE["mode"])   # 每批落盘：服务重启/崩溃后仍能恢复到最新进度
    return True


def collect_worker() -> None:
    """手动模式：只采当前这一批就退出，等用户点「采集本批」（真实模式要挪板子）。"""
    batch = current_batch()
    if batch is None:
        with LOCK:
            STATE["round_complete_at"] = STATE.get("round_complete_at") or time.time()
        set_phase("collected")
        log("一轮采集完成，全部测点已采完")
        return
    if run_one_batch(batch):
        if current_batch() is None:      # 刚采完的是最后一批：进入「采集完成」，解锁整理/出图
            with LOCK:
                STATE["round_complete_at"] = time.time()
            set_phase("collected")
            left = [p for p in active_order() if p not in STATE["collected"]]
            if left:
                log(f"一轮结束，还有 {len(left)} 个点没采到（{', '.join(left[:6])}"
                    f"{'…' if len(left) > 6 else ''}）：检查设备后可「开始新一轮」补采")
            else:
                log("一轮采集完成，全部测点已采完，可以「整理数据」了")
        else:
            set_phase("ready")   # 等用户点“下一批”


def auto_worker() -> None:
    """演示模式一键跑完：连续采完剩余批次，然后自动整理数据 + 生成热力图。"""
    while not _stop_flag.is_set():
        batch = current_batch()
        if batch is None:
            break
        if not run_one_batch(batch):
            return   # 被停止，run_one_batch 已把状态置为 ready
    if _stop_flag.is_set():
        return
    log("全部测点采完，自动开始整理数据…")
    run_script("04_build_dataset.py", "building", "collected", "summary")
    with LOCK:
        ok = (STATE.get("summary") or {}).get("ok")
    if not ok:
        log("整理数据失败，自动出图取消（错误见结果区）")
        return
    log("自动开始生成热力图…")
    run_script("05_train_and_plot.py", "plotting", "done", "report")


# ---------------------------------------------------------------- 子任务（04 / 05）
def run_script(script: str, phase: str, done_phase: str, key: str) -> None:
    set_phase(phase)
    log(f"运行 {script} ...")
    try:
        proc = subprocess.run(
            [str(VENV_PY), str(ROOT / "scripts" / script)],
            capture_output=True, text=True, timeout=600, cwd=str(ROOT),
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        with LOCK:
            STATE[key] = {"ok": proc.returncode == 0, "tail": out[-1500:]}
            if proc.returncode == 0 and key == "summary":
                STATE["built_at"] = time.time()
            elif proc.returncode == 0 and key == "report":
                STATE["plotted_at"] = time.time()
        log(f"{script} 完成" if proc.returncode == 0 else f"{script} 出错（见结果区）")
    except Exception as exc:  # noqa: BLE001
        with LOCK:
            STATE[key] = {"ok": False, "tail": str(exc)}
        log(f"{script} 异常：{exc}")
    save_session(STATE["mode"])
    set_phase(done_phase)


# ---------------------------------------------------------------- API
@app.route("/")
def index():
    return render_template("index.html", project_root=str(ROOT))


@app.route("/api/state")
def api_state():
    # 磁盘上有数据的测点数（在锁外统计；让用户用命令行采的数据也能直接在网页整理出图）
    points_on_disk = 0
    if POINTS_DIR.exists():
        points_on_disk = sum(
            1 for d in POINTS_DIR.iterdir()
            if d.is_dir() and any(d.glob("*.npz"))
        )
    with LOCK:
        # 状态自愈：批次全部跑完但 phase 停在 ready 的，直接置为 collected（解锁整理/出图）
        if (STATE["phase"] == "ready" and STATE["batches"]
                and STATE["batch_index"] >= len(STATE["batches"])):
            STATE["phase"] = "collected"
        batch = None
        if 0 <= STATE["batch_index"] < len(STATE["batches"]):
            batch = STATE["batches"][STATE["batch_index"]]
        elapsed = None
        remaining = None
        if STATE["phase"] == "collecting" and STATE["batch_start"]:
            elapsed = round(time.time() - STATE["batch_start"], 1)
            remaining = max(0.0, round(STATE["batch_seconds"] - elapsed, 1))
        return jsonify({
            "mode": STATE["mode"],
            "phase": STATE["phase"],
            "round": STATE["round"],
            "two_boards": STATE["two_boards"],
            "batch_index": STATE["batch_index"],
            "total_batches": len(STATE["batches"]),
            "batch": batch,
            "batch_seconds": STATE["batch_seconds"],
            "elapsed": elapsed,
            "remaining": remaining,
            "collected": STATE["collected"],
            "total_points": len(active_order()),
            "custom_points": STATE["custom_points"],   # None = 默认全部测点
            "points_on_disk": points_on_disk,
            "live": STATE["live"][-120:],
            "progress": STATE["progress"],
            "log": STATE["log"][-12:],
            "devices": STATE["devices"],
            "summary": STATE["summary"],
            "report": STATE["report"],
            "sites": SITES,
            "room": {"w": ROOM_W, "h": ROOM_H, "ap": [AP_X, AP_Y],
                     "polygon": LAYOUT.get("polygon"), "shape": LAYOUT.get("shape")},
            "layout": {"name": LAYOUT.get("name"), "empty": LAYOUT.get("empty", True),
                       "preset_id": LAYOUT.get("preset_id")},
        })


@app.route("/api/mode", methods=["POST"])
def api_mode():
    """切换演示/真实模式：停半成品 → 存当前进度 → 泊走数据 → 还原目标模式。"""
    data = request.get_json(force=True)
    target = "real" if data.get("mode") == "real" else "demo"
    with LOCK:
        cur = STATE["mode"]
        if STATE["phase"] in ("building", "plotting"):
            return jsonify({"ok": False, "msg": "正在整理/出图，跑完再切换"}), 409
        if target == cur:   # 没切模式，只更新板数
            if data.get("two_boards") is not None:
                STATE["two_boards"] = bool(data["two_boards"])
            return jsonify({"ok": True, "mode": cur, "unchanged": True})
        inflight, since = [], time.time()
        if STATE["phase"] == "collecting" and 0 <= STATE["batch_index"] < len(STATE["batches"]):
            b = STATE["batches"][STATE["batch_index"]]
            inflight = [p for p in (b["A"], b["B"]) if p]
            since = STATE["batch_start"] or since

    # ① 停掉正在跑的批次（锁外等采集线程退出，避免它回头改状态）
    if inflight:
        _stop_flag.set()
        with LOCK:
            procs = list(_current_procs)
        for proc in procs:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if _worker_thread:
            _worker_thread.join(timeout=8)
        _stop_flag.clear()
        _instant_flag.clear()
        purge_inflight(inflight, since)

    # ② 保存当前模式的进度，泊走它的数据目录
    with LOCK:
        save_session(cur)
    park_data(cur)

    # ③ 还原目标模式的数据目录和进度
    unpark_data(target)
    write_owner(target)
    name = "真实" if target == "real" else "演示"
    with LOCK:
        STATE["mode"] = target
        STATE["batch_seconds"] = BATCH_SECONDS_REAL if target == "real" else BATCH_SECONDS_DEMO
        STATE["devices"] = []
        STATE.pop("device_ips", None)
        sess = load_session(target)
        if sess:
            apply_session(sess)
            if STATE["phase"] == "ready":
                msg = (f"切到{name}模式：恢复上次进度，已采 {len(STATE['collected'])}/{len(POINT_ORDER)} 点，"
                       f"正在跑的点已重置为未采集，从第 {STATE['batch_index']+1} 批继续")
            else:
                msg = f"切到{name}模式：恢复上次进度（已采完，可直接整理出图）"
        else:
            STATE.update({"phase": "idle", "batch_index": 0, "batches": [], "collected": [],
                          "live": [], "progress": None, "summary": None, "report": None,
                          "batch_start": None})
            msg = f"切到{name}模式（全新开始）"
    log(msg)   # log() 自己会加锁，必须在锁外调用
    return jsonify({"ok": True, "mode": target, "restored": bool(sess)})


@app.route("/api/devices")
def api_devices():
    if STATE["mode"] == "demo":
        demo = [{"name": "esp32c6-demo-A", "ip": "192.168.1.51", "chip": "esp32c6"}]
        if STATE["two_boards"]:
            demo.append({"name": "esp32c6-demo-B", "ip": "192.168.1.52", "chip": "esp32c6"})
        with LOCK:
            STATE["devices"] = demo
        return jsonify({"ok": True, "devices": demo, "demo": True})
    devices = []
    try:
        proc = subprocess.run(
            [str(VENV_PY), str(ESPECTRE), "devices", "--json", "--timeout", "8"],
            capture_output=True, text=True, timeout=20,
        )
        raw = json.loads(proc.stdout) if proc.stdout.strip() else []
        # 官方字段是 ip_address / name / chip / frontend，统一成页面用的 name/ip/chip
        devices = [
            {"name": d.get("name") or d.get("device_id") or "esp32",
             "ip": d.get("ip_address", ""),
             "chip": d.get("chip", ""),
             "frontend": d.get("frontend", "")}
            for d in raw
        ]
        log(f"发现 {len(devices)} 台设备" if devices else "没有发现设备：确认板子已配网且与电脑在同一 Wi-Fi")
    except Exception as exc:  # noqa: BLE001
        log(f"设备发现失败：{exc}")
    with LOCK:
        STATE["devices"] = devices
    return jsonify({"ok": True, "devices": devices})


@app.route("/api/collect/start", methods=["POST"])
def api_collect_start():
    data = request.get_json(force=True) or {}
    with LOCK:
        STATE["round"] = data.get("round", STATE["round"])
        STATE["batches"] = make_batches(STATE["two_boards"])
        STATE["batch_index"] = 0
        STATE["collected"] = []
        STATE["summary"] = None
        STATE["report"] = None
        STATE["error"] = None
        STATE["round_complete_at"] = None
        STATE["built_at"] = None
        STATE["plotted_at"] = None
        if data.get("device_ips"):
            STATE["device_ips"] = data["device_ips"]
        STATE["phase"] = "ready"
    # 演示模式：清空旧数据目录，保证本轮产物干净（真实模式绝不清）
    if STATE["mode"] == "demo" and POINTS_DIR.exists():
        for child in POINTS_DIR.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
    log(f"开始新一轮 {STATE['round']}，共 {len(STATE['batches'])} 批")
    return jsonify({"ok": True, "total_batches": len(STATE["batches"])})


@app.route("/api/collect/next", methods=["POST"])
def api_collect_next():
    global _worker_thread
    with LOCK:
        # ready=正常续采；collected/done=整理或出图后继续采同一轮的剩余批次（进度不丢）
        if STATE["phase"] not in ("ready", "collected", "done"):
            return jsonify({"ok": False, "msg": f"当前状态 {STATE['phase']} 不能开始下一批"})
        if STATE["phase"] in ("collected", "done"):
            STATE["summary"] = None   # 结果已过时：清掉，等重新整理出图
            STATE["report"] = None
            STATE["round_complete_at"] = None
            STATE["built_at"] = None
            STATE["plotted_at"] = None
        if STATE["batch_index"] >= len(STATE["batches"]):
            return jsonify({"ok": False, "msg": "已全部采完"})
    _stop_flag.clear()
    _instant_flag.clear()
    _worker_thread = threading.Thread(target=collect_worker, daemon=True)
    _worker_thread.start()
    return jsonify({"ok": True})


@app.route("/api/collect/auto", methods=["POST"])
def api_collect_auto():
    """演示模式一键跑完：需要时先初始化新一轮，然后自动采完剩余批次 + 整理 + 出图。"""
    global _worker_thread
    data = request.get_json(force=True) or {}
    with LOCK:
        if STATE["mode"] != "demo":
            return jsonify({"ok": False, "msg": "一键跑完只在演示模式；真实模式要逐点挪板子，请用「采集本批」"}), 400
        if STATE["phase"] in ("collecting", "building", "plotting"):
            return jsonify({"ok": False, "msg": f"当前正在 {STATE['phase']}，等它结束"}), 409
        fresh = (not STATE["batches"]) or STATE["batch_index"] >= len(STATE["batches"])
        if fresh:
            STATE["round"] = data.get("round", STATE["round"])
            STATE["batches"] = make_batches(STATE["two_boards"])
            STATE["batch_index"] = 0
            STATE["collected"] = []
            STATE["summary"] = None
            STATE["report"] = None
            STATE["error"] = None
            STATE["round_complete_at"] = None
            STATE["built_at"] = None
            STATE["plotted_at"] = None
            STATE["phase"] = "ready"
    if fresh:
        # 全新一轮：清空演示旧数据（一键跑完只在 demo，真实模式走不到这里）
        if POINTS_DIR.exists():
            for child in POINTS_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
        log(f"一键跑完：新一轮 {STATE['round']}，共 {len(STATE['batches'])} 批，自动 采集→整理→出图")
    else:
        log(f"一键跑完：从第 {STATE['batch_index']+1} 批继续，采完后自动整理+出图")
    _stop_flag.clear()
    _instant_flag.clear()
    if data.get("instant"):
        _instant_flag.set()
        log("跳过模式：采集瞬间完成，直接演示 整理 + 出图")
    _worker_thread = threading.Thread(target=auto_worker, daemon=True)
    _worker_thread.start()
    return jsonify({"ok": True})


@app.route("/api/collect/skip", methods=["POST"])
def api_collect_skip():
    """演示模式采集中途跳过：当前批和剩余批次都瞬间完成。"""
    with LOCK:
        if STATE["mode"] != "demo":
            return jsonify({"ok": False, "msg": "只有演示模式能跳过；真实模式要真实采数据"}), 400
        if STATE["phase"] != "collecting":
            return jsonify({"ok": False, "msg": "当前不在采集中"}), 409
    _instant_flag.set()
    log("跳过剩余采集：后续测点瞬间完成")
    return jsonify({"ok": True})


@app.route("/api/collect/stop", methods=["POST"])
def api_collect_stop():
    _stop_flag.set()
    with LOCK:
        procs = list(_current_procs)
    for proc in procs:          # 真实模式：杀掉采集子进程
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    set_phase("ready")
    log("已请求停止当前批次")
    return jsonify({"ok": True})


@app.route("/api/build", methods=["POST"])
def api_build():
    with LOCK:
        if STATE["phase"] in ("collecting", "building", "plotting"):
            return jsonify({"ok": False, "msg": "正在采集/整理/出图中，等它结束再试"}), 409
    threading.Thread(target=run_script, args=("04_build_dataset.py", "building", "collected", "summary"), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/plot", methods=["POST"])
def api_plot():
    with LOCK:
        if STATE["phase"] in ("collecting", "building", "plotting"):
            return jsonify({"ok": False, "msg": "正在采集/整理/出图中，等它结束再试"}), 409
    threading.Thread(target=run_script, args=("05_train_and_plot.py", "plotting", "done", "report"), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/outputs/<path:fname>")
def api_outputs(fname):
    return send_from_directory(str(OUTPUTS_DIR), fname)


@app.route("/api/reset", methods=["POST"])
def api_reset():
    global _worker_thread
    _stop_flag.set()
    with LOCK:
        procs = list(_current_procs)
    for proc in procs:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    th = _worker_thread
    if th and th.is_alive():
        th.join(timeout=10)   # 等工作线程退出，避免它随后把 phase 改回去
    _worker_thread = None
    _stop_flag.clear()
    _instant_flag.clear()
    with LOCK:
        STATE.update({
            "phase": "idle", "batch_index": 0, "batches": [], "collected": [],
            "live": [], "progress": None, "summary": None, "report": None,
            "round_complete_at": None, "built_at": None, "plotted_at": None,
            "error": None, "batch_start": None,
        })
    log("已重置")
    return jsonify({"ok": True})


@app.route("/api/setup/ports")
def api_setup_ports():
    """列出可能的板子串口（只读，用于烧录前确认板子已被电脑识别）。"""
    import glob as _glob
    ports = []
    for pat in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*",
                "/dev/cu.wchusbserial*", "/dev/cu.SLAB_USBtoUART*"):
        ports.extend(_glob.glob(pat))
    return jsonify({"ok": True, "ports": sorted(set(ports))})


# ---------------------------------------------------------------- 房型导入
def _reset_all_modes() -> None:
    """换房型时调用：测点变了，两个模式的旧数据/进度/泊位全部失效，清空。

    前端在应用前会让用户确认；这里不再单独保留旧数据——旧测点编号与新房型
    已经不兼容，留着只会被误用。
    """
    for m in ("demo", "real"):
        f = SESSION_DIR / f"{m}.json"
        if f.exists():
            f.unlink()
        p = PARK_DIR / m
        if p.exists():
            shutil.rmtree(p)
    if POINTS_DIR.exists():
        shutil.rmtree(POINTS_DIR)
    POINTS_DIR.mkdir(parents=True, exist_ok=True)
    if OWNER_FILE.exists():
        OWNER_FILE.unlink()


@app.route("/api/layout")
def api_layout():
    lay = dict(LAYOUT)
    lay["n_train"] = sum(1 for s in SITES if s["role"] == "train")
    lay["n_val"] = sum(1 for s in SITES if s["role"] != "train")
    return jsonify({"ok": True, "layout": lay, "shapes": layout_mod.SHAPES,
                    "bounds": {"min_spacing": layout_mod.MIN_SPACING,
                               "max_train": layout_mod.MAX_TRAIN}})


@app.route("/api/layout/preview", methods=["POST"])
def api_layout_preview():
    d = request.get_json(force=True)
    err = layout_mod.validate(d.get("shape", "rect"), d.get("params") or {},
                              d.get("ap_x"), d.get("ap_y"), d.get("spacing"))
    if err:
        return jsonify({"ok": False, "msg": err}), 400
    out = layout_mod.preview(d["shape"], d["params"], d["spacing"])
    out["same_points"] = layout_mod.same_as_current(out["train"])
    try:
        out["ap_changed"] = (round(float(d["ap_x"]), 2), round(float(d["ap_y"]), 2)) \
            != (LAYOUT["ap_x"], LAYOUT["ap_y"])
    except (TypeError, ValueError):
        out["ap_changed"] = True
    return jsonify(out)


@app.route("/api/layout/apply", methods=["POST"])
def api_layout_apply():
    d = request.get_json(force=True)
    with LOCK:
        busy = STATE["phase"] in ("collecting", "building", "plotting")
    if busy:
        return jsonify({"ok": False, "msg": "采集/整理进行中，跑完或停止后再换房型"}), 409
    try:
        lay, kept = layout_mod.apply(
            d.get("preset_id", ""), d.get("shape", "rect"),
            d.get("params") or {}, d.get("ap_x"), d.get("ap_y"), d.get("spacing"),
            keep_data=bool(d.get("keep_data")))
    except ValueError as e:
        return jsonify({"ok": False, "msg": str(e)}), 400

    if kept:
        # 测点没变（只改了路由器坐标）：保留两个模式的全部数据与进度
        reload_layout()
        log(f"路由器位置已更新为 ({lay['ap_x']}, {lay['ap_y']})，测点未变、已采数据保留。"
            "提醒：现有热力图/报告基于旧位置，建议重新「整理出图」。")
        return jsonify({"ok": True, "layout": lay, "kept": True})

    _reset_all_modes()
    reload_layout()
    with LOCK:
        STATE.update({
            "phase": "idle", "batch_index": 0, "batches": [], "collected": [],
            "live": [], "progress": None, "summary": None, "report": None,
            "round_complete_at": None, "built_at": None, "plotted_at": None,
            "error": None, "batch_start": None,
        })
    save_session(STATE["mode"])
    log(f"已应用新房型：{lay['name']}（外接 {lay['room_w']}×{lay['room_h']}m，"
        f"{lay['n_train']} 个测点，空置房间）")
    return jsonify({"ok": True, "layout": lay, "kept": False})


def _points_edit_guard():
    """改采集方案前的状态检查：采集/整理/出图中、或一轮采到一半，都不允许改。"""
    if STATE["phase"] in ("collecting", "building", "plotting"):
        return "正在采集/整理/出图，结束后再改采集方案"
    if STATE["phase"] == "ready" and STATE["batch_index"] > 0:
        return "本轮已采了一部分：先采完，或点「重置」清空后再改方案"
    return None


@app.route("/api/points/custom", methods=["POST"])
def api_points_custom():
    """设置自定义采集方案：用户挑的子集，严格按点击顺序采集。"""
    data = request.get_json(force=True) or {}
    pts = data.get("points") or []
    with LOCK:
        err = _points_edit_guard()
        if err:
            return jsonify({"ok": False, "msg": err}), 409
        valid, seen = [], set()
        for p in pts:
            pid = str(p).strip().upper()
            if pid in SITE_BY_ID and pid not in seen:
                seen.add(pid)
                valid.append(pid)
        if not valid:
            return jsonify({"ok": False, "msg": "至少选 1 个有效测点"}), 400
        STATE["custom_points"] = valid
        save_session(STATE["mode"])
        n = len(valid)
    log(f"自定义采集方案：{n} 个点（{', '.join(valid[:8])}{'…' if n > 8 else ''}），"
        f"将按选择顺序采集。点开始新一轮生效")
    return jsonify({"ok": True, "count": n, "points": valid})


@app.route("/api/points/default", methods=["POST"])
def api_points_default():
    """恢复默认方案：全部测点、默认顺序。"""
    with LOCK:
        err = _points_edit_guard()
        if err:
            return jsonify({"ok": False, "msg": err}), 409
        STATE["custom_points"] = None
        save_session(STATE["mode"])
    log("已恢复默认采集方案：全部测点、默认顺序。点开始新一轮生效")
    return jsonify({"ok": True})


@app.route("/api/download")
def api_download():
    """一键下载数据包：任何时刻都能下（采集中也行）——原始采集数据全量打包；
    整理数据/热力图/报告只在「本轮完整采完后点过对应按钮」才放入，绝不混入旧产物。"""
    mem = io.BytesIO()
    n_data = 0
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        def add(path: Path, arcname: str):
            nonlocal n_data
            if path.is_file():
                z.write(path, arcname)
                n_data += 1
        # 产物只放「本轮完整采完之后」生成的：防止旧轮次的报告/热力图混进下载包
        with LOCK:
            snap = dict(STATE)
        round_done_at = snap.get("round_complete_at")
        built_fresh = bool(round_done_at and snap.get("built_at") and snap["built_at"] >= round_done_at)
        plotted_fresh = bool(round_done_at and snap.get("plotted_at") and snap["plotted_at"] >= round_done_at)
        if plotted_fresh:
            add(ROOT / "outputs/热力图_预测.png", "结果图/热力图_预测.png")
            add(ROOT / "outputs/热力图_对比.png", "结果图/热力图_对比.png")
            add(ROOT / "outputs/模型评估报告.md", "报告/模型评估报告.md")
            add(ROOT / "outputs/测点汇总表.csv", "报告/测点汇总表.csv")
        if built_fresh or plotted_fresh:
            add(ROOT / "data/points_features.csv", "整理数据/points_features.csv")
        if POINTS_DIR.exists():
            for f in sorted(POINTS_DIR.rglob("*.npz")):
                z.write(f, f"原始数据/{f.relative_to(POINTS_DIR)}")
                n_data += 1
        progress = {
            "导出时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "模式": "演示（模拟数据）" if snap["mode"] == "demo" else "真实",
            "房型": LAYOUT.get("name"), "轮次": snap.get("round"),
            "当前阶段": snap.get("phase"),
            "已采测点": snap.get("collected", []),
            "测点总数": len(active_order()),
            "整理数据": "已包含（本轮采完后整理）" if (built_fresh or plotted_fresh)
                       else "未包含：需跑完一整轮采集后点「整理数据」",
            "热力图与评估报告": "已包含（本轮采完后出图）" if plotted_fresh
                            else "未包含：需跑完一整轮采集后点「生成热力图」",
            "说明": "采集中途导出也完整：已采的点都在「原始数据」里；"
                   "整理数据/热力图只在本轮采完后点过对应按钮才会打包，绝不混入旧轮次产物。",
        }
        z.writestr("进度清单.json", json.dumps(progress, ensure_ascii=False, indent=2))
    if n_data == 0:
        return jsonify({"ok": False, "msg": "还没有任何采集数据，先采几个点再来"}), 400
    mem.seek(0)
    # 文件名：房型名_已采-总数_导出时间.zip（/ 在文件名里非法，个数比用 - 连接）
    safe_name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(LAYOUT.get("name") or "未命名房型")).strip("_")
    fname = (f"{safe_name}_{len(snap.get('collected', []))}-{len(POINT_ORDER)}"
             f"_{time.strftime('%Y%m%d_%H%M%S')}.zip")
    log(f"下载数据包：{n_data} 个数据文件 + 进度清单 -> {fname}")
    return send_file(mem, mimetype="application/zip", as_attachment=True,
                     download_name=fname)


if __name__ == "__main__":
    # 启动时恢复上次活跃模式的进度（服务重启不丢进度；data/points 归属由 owner 文件保证）
    try:
        _owner = json.loads(OWNER_FILE.read_text(encoding="utf-8")).get("owner")
    except Exception:  # noqa: BLE001
        _owner = None
    if _owner in ("demo", "real"):
        _sess = load_session(_owner)
        with LOCK:
            STATE["mode"] = _owner
            STATE["batch_seconds"] = BATCH_SECONDS_REAL if _owner == "real" else BATCH_SECONDS_DEMO
            if _sess:
                apply_session(_sess)
    print("=" * 56)
    print("  Wi-Fi 热力图 · 一键采集网站")
    print("  浏览器打开:  http://127.0.0.1:8000")
    print("  默认是【演示模式】，不插板子也能跑完整流程")
    print("=" * 56)
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
