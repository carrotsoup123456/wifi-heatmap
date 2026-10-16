#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_build_dataset.py —— 把散落的 .npz 原始采集文件，整理成能直接喂给模型的两张表。

输入：  data/points/<测点ID>/*.npz        （由 02 / 03 脚本采集并归档）
输出：
        data/points_features.csv         文件级明细，一行 = 一个 npz
        outputs/测点汇总表.csv            测点级汇总，一行 = 一个测点（建模型用这张）

用法（务必用 ESPectre 的虚拟环境里的 python）：
    tools/espectre/.venv/bin/python scripts/04_build_dataset.py
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit(
        "缺少 numpy。请用虚拟环境里的解释器运行：\n"
        "    tools/espectre/.venv/bin/python scripts/04_build_dataset.py"
    )

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POINTS_DIR = PROJECT_ROOT / "data" / "points"
SITES_CSV = PROJECT_ROOT / "scripts" / "sites.csv"
FILE_CSV = PROJECT_ROOT / "data" / "points_features.csv"
POINT_CSV = PROJECT_ROOT / "outputs" / "测点汇总表.csv"

# 认为无效的 RSSI 读数（设备未上报时会填 0）
RSSI_MIN_DBM = -120.0
RSSI_MAX_DBM = -1.0

# 一个测点至少要有的有效包数，低于此值判为可疑
MIN_PACKETS = 500


def load_sites() -> dict:
    sites = {}
    if not SITES_CSV.exists():
        return sites
    with SITES_CSV.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            sites[row["point_id"].upper()] = row
    return sites


def parse_point_id(npz_path: Path) -> str:
    """文件名形如 p11_esp32c6_64sc_<device>_<时间戳>_0001.npz，取第一段作为测点ID。"""
    name = npz_path.name
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*?)_(esp32|unknown)", name, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return name.split("_", 1)[0].upper()


def scalar(data, key, default=None):
    if key not in data.files:
        return default
    value = data[key]
    try:
        return value.item()
    except (AttributeError, ValueError):
        return value


def analyse_npz(npz_path: Path) -> dict:
    """读一个 npz，返回该文件的统计量；出错则返回 {'error': ...}。"""
    try:
        with np.load(npz_path, allow_pickle=False) as data:
            files = set(data.files)
            csi = data["csi_data"].astype(np.float32) if "csi_data" in files else None
            n_packets = int(csi.shape[0]) if csi is not None else 0
            num_sc = int(scalar(data, "num_subcarriers", 0) or 0)

            rec = {
                "file": npz_path.name,
                "point_id": parse_point_id(npz_path),
                "n_packets": n_packets,
                "num_subcarriers": num_sc,
                "duration_ms": float(scalar(data, "duration_ms", 0.0) or 0.0),
                "chip": str(scalar(data, "chip", "") or ""),
                "device_id": str(scalar(data, "device_id", "") or ""),
                "frontend": str(scalar(data, "frontend", "") or ""),
                "firmware": str(scalar(data, "firmware_version", "") or ""),
                "collected_at": str(scalar(data, "collected_at", "") or ""),
                "requested_pps": float(scalar(data, "requested_pps", 0.0) or 0.0),
                "effective_pps": float(scalar(data, "effective_pps", 0.0) or 0.0),
                "error": "",
            }

            # ---- 1) 设备直接上报的 RSSI（首选）----
            has_rssi = False
            if "rssi_dbm" in files:
                rssi = data["rssi_dbm"].astype(np.float32)
                rssi = rssi[(rssi > RSSI_MIN_DBM) & (rssi < RSSI_MAX_DBM)]
                if rssi.size > 0:
                    has_rssi = True
                    rec["rssi_source"] = "device_rssi_dbm"
                    rec["rssi_median"] = float(np.median(rssi))
                    rec["rssi_mean"] = float(np.mean(rssi))
                    rec["rssi_std"] = float(np.std(rssi))
                    rec["rssi_p10"] = float(np.percentile(rssi, 10))
                    rec["rssi_p90"] = float(np.percentile(rssi, 90))
                    rec["rssi_n"] = int(rssi.size)

            # ---- 2) 从 CSI 幅度算的功率代理（兜底 / 交叉验证）----
            if csi is not None and csi.size > 0:
                q = csi[:, 0::2]
                i = csi[:, 1::2]
                amp = np.sqrt(i * i + q * q)              # [N, SC]
                pkt_power = np.mean(amp * amp, axis=1)     # [N]
                pkt_power = np.maximum(pkt_power, 1e-12)
                proxy = 10.0 * np.log10(pkt_power)
                rec["csi_power_median"] = float(np.median(proxy))
                rec["csi_power_std"] = float(np.std(proxy))
                rec["csi_amp_mean"] = float(np.mean(amp))
                rec["csi_subcarrier_mean"] = ";".join(
                    f"{v:.3f}" for v in amp.mean(axis=0)
                )
                if not has_rssi:
                    rec["rssi_source"] = "csi_power_proxy"
                    rec["rssi_median"] = float(np.median(proxy))
                    rec["rssi_mean"] = float(np.mean(proxy))
                    rec["rssi_std"] = float(np.std(proxy))
                    rec["rssi_p10"] = float(np.percentile(proxy, 10))
                    rec["rssi_p90"] = float(np.percentile(proxy, 90))
                    rec["rssi_n"] = int(proxy.size)

            # ---- 3) 噪声底（可选）----
            if "noise_floor_dbm" in files:
                nf = data["noise_floor_dbm"].astype(np.float32)
                nf = nf[(nf > RSSI_MIN_DBM) & (nf < 0)]
                if nf.size > 0:
                    rec["noise_floor_median"] = float(np.median(nf))

            if "channel" in files:
                ch = data["channel"]
                if ch.size:
                    rec["channel"] = int(np.bincount(ch.astype(np.int64)).argmax())

            if n_packets < MIN_PACKETS:
                rec["error"] = f"有效包数偏少({n_packets})"
            return rec
    except Exception as exc:  # noqa: BLE001
        return {"file": npz_path.name, "point_id": parse_point_id(npz_path), "error": f"读取失败: {exc}"}


def main() -> int:
    sites = load_sites()
    npz_files = sorted(POINTS_DIR.glob("*/*.npz"))
    if not npz_files:
        print(f"没有在 {POINTS_DIR} 下找到任何 .npz 文件。")
        print("请先运行采集脚本，例如：")
        print("    bash scripts/02_collect_point.sh P11 <设备IP>")
        return 1

    print(f"发现 {len(npz_files)} 个采集文件，开始解析...\n")

    records = []
    for path in npz_files:
        rec = analyse_npz(path)
        rec["point_id_final"] = rec.get("point_id", "?")
        records.append(rec)

    # ---------- 写文件级明细 ----------
    file_fields = [
        "point_id", "file", "n_packets", "num_subcarriers", "duration_ms",
        "rssi_source", "rssi_median", "rssi_mean", "rssi_std", "rssi_p10", "rssi_p90", "rssi_n",
        "csi_power_median", "csi_power_std", "csi_amp_mean",
        "noise_floor_median", "channel",
        "chip", "device_id", "frontend", "firmware", "collected_at",
        "requested_pps", "effective_pps", "csi_subcarrier_mean", "error",
    ]
    FILE_CSV.parent.mkdir(parents=True, exist_ok=True)
    with FILE_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=file_fields, extrasaction="ignore")
        writer.writeheader()
        for rec in records:
            writer.writerow({k: rec.get(k, "") for k in file_fields})

    # ---------- 聚合到测点级 ----------
    by_point: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        by_point[rec.get("point_id", "?")].append(rec)

    point_rows = []
    problems = []
    for pid in sorted(by_point):
        recs = by_point[pid]
        good = [r for r in recs if not r.get("error")]
        if not good:
            problems.append(f"{pid}: 全部 {len(recs)} 个文件解析失败")
            continue

        def agg(key, fn=np.median):
            vals = [r[key] for r in good if key in r and isinstance(r[key], (int, float))]
            return fn(vals) if vals else None

        site = sites.get(pid, {})
        total_packets = sum(r.get("n_packets", 0) for r in good)
        row = {
            "point_id": pid,
            "block": site.get("block", ""),
            "role": site.get("role", ""),
            "x_m": site.get("x_m", ""),
            "y_m": site.get("y_m", ""),
            "n_files": len(good),
            "n_files_bad": len(recs) - len(good),
            "n_packets": total_packets,
            "rssi_source": good[0].get("rssi_source", ""),
            "rssi_median": round(agg("rssi_median"), 3) if agg("rssi_median") is not None else "",
            "rssi_mean": round(agg("rssi_mean"), 3) if agg("rssi_mean") is not None else "",
            "rssi_std": round(agg("rssi_std"), 4) if agg("rssi_std") is not None else "",
            "rssi_p10": round(agg("rssi_p10"), 3) if agg("rssi_p10") is not None else "",
            "rssi_p90": round(agg("rssi_p90"), 3) if agg("rssi_p90") is not None else "",
            "csi_power_median": round(agg("csi_power_median"), 3) if agg("csi_power_median") is not None else "",
            "csi_power_std": round(agg("csi_power_std"), 4) if agg("csi_power_std") is not None else "",
            "csi_amp_mean": round(agg("csi_amp_mean"), 4) if agg("csi_amp_mean") is not None else "",
            "noise_floor_median": round(agg("noise_floor_median"), 2) if agg("noise_floor_median") is not None else "",
            "channel": agg("channel", lambda v: int(v[0])) if agg("channel", lambda v: int(v[0])) is not None else "",
            "device_ids": ";".join(sorted({str(r.get("device_id", "")) for r in good if r.get("device_id")})),
            "collected_at_first": min((r.get("collected_at", "") for r in good if r.get("collected_at")), default=""),
            "collected_at_last": max((r.get("collected_at", "") for r in good if r.get("collected_at")), default=""),
        }
        point_rows.append(row)

        if row["n_packets"] < MIN_PACKETS * len(good):
            problems.append(f"{pid}: 平均每文件包数偏少，建议重采")

    point_fields = list(point_rows[0].keys()) if point_rows else []
    POINT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with POINT_CSV.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=point_fields)
        writer.writeheader()
        writer.writerows(point_rows)

    # ---------- 控制台报告 ----------
    print("=" * 68)
    print(f" 测点数: {len(point_rows)} / 25      文件数: {len(npz_files)}")
    rssi_src = {r["rssi_source"] for r in point_rows if r["rssi_source"]}
    print(f" RSSI 来源: {', '.join(sorted(rssi_src)) or '无'}")
    print("=" * 68)
    print(f"{'测点':<6}{'区块':<8}{'角色':<10}{'文件':>5}{'包数':>9}{'RSSI中位':>11}{'标准差':>9}")
    print("-" * 68)
    for row in point_rows:
        print(
            f"{row['point_id']:<6}{str(row['block']):<8}{str(row['role']):<10}"
            f"{row['n_files']:>5}{row['n_packets']:>9}{str(row['rssi_median']):>11}{str(row['rssi_std']):>9}"
        )
    print("-" * 68)

    missing = [p for p in sites if p not in by_point]
    if missing:
        print(f"\n尚未采集的测点 ({len(missing)} 个): {' '.join(sorted(missing))}")
    if problems:
        print("\n需要注意:")
        for p in problems:
            print(f"  - {p}")

    print(f"\n文件级明细 -> {FILE_CSV.relative_to(PROJECT_ROOT)}")
    print(f"测点级汇总 -> {POINT_CSV.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
