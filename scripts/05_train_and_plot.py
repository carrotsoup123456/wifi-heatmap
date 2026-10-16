#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
05_train_and_plot.py —— 用稀疏测点推测整个房间的 RSS 分布，并画成热力图。

输入：  outputs/测点汇总表.csv     （由 04_build_dataset.py 生成）
输出：  outputs/热力图_预测.png
        outputs/热力图_对比.png
        outputs/模型评估报告.md

三个模型同场竞技，用来回答“机器学习到底值不值得用”：
    1. IDW      反距离加权，纯几何插值，不需要训练
    2. 路径损耗  只用“到路由器的距离”，拟合一个物理公式
    3. GPR      高斯过程回归，小样本下最强的机器学习方法，还能给不确定度

用法：
    tools/espectre/.venv/bin/python scripts/05_train_and_plot.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover
    sys.exit("缺少 numpy。请用 tools/espectre/.venv/bin/python 运行本脚本。")

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------- 配置区（房间几何由 scripts/layout.json 统一管理） ----------------
def _load_layout() -> dict:
    """读 scripts/layout.json（网站「房型导入」写入）；缺失时回退历史默认值。"""
    import json
    lay = {"room_w": 6.62, "room_h": 7.74, "ap_x": 0.30, "ap_y": 0.30,
           "name": "演示房间", "empty": True, "polygon": None}
    try:
        raw = json.loads((PROJECT_ROOT / "scripts" / "layout.json").read_text(encoding="utf-8"))
        for k in lay:
            if k in raw:
                lay[k] = raw[k]
    except Exception:  # noqa: BLE001
        pass
    if not lay.get("polygon"):  # 旧版 layout.json 没有多边形 → 按矩形回退
        w, h = lay["room_w"], lay["room_h"]
        lay["polygon"] = [[0, 0], [w, 0], [w, h], [0, h]]
    return lay

_LAY = _load_layout()
ROOM_W = _LAY["room_w"]          # 外接矩形宽（x 方向，米）
ROOM_H = _LAY["room_h"]          # 外接矩形长（y 方向，米）
AP_X, AP_Y = _LAY["ap_x"], _LAY["ap_y"]  # 路由器摆放位置（原点=路由器所在的墙角）
ROOM_NAME = _LAY["name"]
ROOM_EMPTY = bool(_LAY["empty"])  # 空置（空无一物）还是后续的有障碍物场景
ROOM_POLY = _LAY["polygon"]      # 房间多边形（L 型/U 型时用于热力图抠缺口）
GRID_N = 120           # 热力图分辨率
# -----------------------------------------------------------

POINT_CSV = PROJECT_ROOT / "outputs" / "测点汇总表.csv"
OUT_HEAT = PROJECT_ROOT / "outputs" / "热力图_预测.png"
OUT_CMP = PROJECT_ROOT / "outputs" / "热力图_对比.png"
OUT_MD = PROJECT_ROOT / "outputs" / "模型评估报告.md"
VALUE_COL = "rssi_median"


def setup_chinese_font() -> None:
    import matplotlib
    matplotlib.rcParams["font.sans-serif"] = [
        "PingFang SC", "Heiti SC", "Songti SC", "Arial Unicode MS", "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False


def load_points() -> tuple[np.ndarray, np.ndarray, list[dict]]:
    if not POINT_CSV.exists():
        sys.exit(
            f"找不到 {POINT_CSV}。请先运行：\n"
            "    tools/espectre/.venv/bin/python scripts/04_build_dataset.py"
        )
    rows = []
    with POINT_CSV.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            if not row.get("x_m") or not row.get(VALUE_COL):
                continue
            rows.append(
                {
                    "point_id": row["point_id"],
                    "role": row.get("role", ""),
                    "x": float(row["x_m"]),
                    "y": float(row["y_m"]),
                    "v": float(row[VALUE_COL]),
                }
            )
    if not rows:
        sys.exit("汇总表里没有可用的测点数据。")

    xy = np.array([[r["x"], r["y"]] for r in rows])
    v = np.array([r["v"] for r in rows])
    return xy, v, rows


def dist_to_ap(xy: np.ndarray) -> np.ndarray:
    d = np.sqrt((xy[:, 0] - AP_X) ** 2 + (xy[:, 1] - AP_Y) ** 2)
    return np.maximum(d, 0.3)   # 避免 0 距离


# ---------------- 模型 1：反距离加权 ----------------
def idw_predict(xy_train: np.ndarray, v_train: np.ndarray, xy_q: np.ndarray, power: float = 2.0) -> np.ndarray:
    out = np.empty(len(xy_q))
    for i, q in enumerate(xy_q):
        d = np.sqrt(((xy_train - q) ** 2).sum(axis=1))
        w = 1.0 / np.maximum(d, 1e-6) ** power
        out[i] = float((w * v_train).sum() / w.sum())
    return out


# ---------------- 模型 2：路径损耗拟合 ----------------
def pathloss_fit(xy_train: np.ndarray, v_train: np.ndarray) -> tuple[float, float]:
    d = dist_to_ap(xy_train)
    A = np.column_stack([np.ones_like(d), 10.0 * np.log10(d)])
    coef, *_ = np.linalg.lstsq(A, v_train, rcond=None)
    return float(coef[0]), float(coef[1])


def pathloss_predict(xy_q: np.ndarray, p0: float, n: float) -> np.ndarray:
    # 拟合式 v = p0 + n·(10log10 d)，最小二乘给出的 n 本身为负（衰减）
    d = dist_to_ap(xy_q)
    return p0 + n * 10.0 * np.log10(d)


# ---------------- 模型 3：高斯过程回归 ----------------
def make_gpr():
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

    kernel = (
        ConstantKernel(1.0, (1e-3, 1e4))
        * RBF(length_scale=2.0, length_scale_bounds=(0.3, 50.0))
        + WhiteKernel(noise_level=1.0, noise_level_bounds=(1e-3, 1e2))
    )
    return GaussianProcessRegressor(
        kernel=kernel, normalize_y=True, n_restarts_optimizer=5, random_state=0
    )


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    e = y_pred - y_true
    mae = float(np.mean(np.abs(e)))
    rmse = float(np.sqrt(np.mean(e ** 2)))
    ss_res = float(np.sum(e ** 2))
    ss_tot = float(np.sum((y_true - y_true.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    return {"MAE": mae, "RMSE": rmse, "R2": r2}


def main() -> int:
    setup_chinese_font()
    import matplotlib.pyplot as plt

    xy, v, rows = load_points()
    train_idx = [i for i, r in enumerate(rows) if r["role"] == "train"]
    valid_idx = [i for i, r in enumerate(rows) if r["role"] == "validate"]

    if len(train_idx) < 4:
        sys.exit(f"测点太少（{len(train_idx)} 个），至少要 4 个才能建模。")

    # 没有单独验证点时：训练脚本内部随机留出 ~20% 做评估（可复现，seed 固定）
    split_note = ""
    if not valid_idx and len(train_idx) >= 10:
        rng = np.random.RandomState(42)
        n_hold = max(3, round(len(train_idx) * 0.2))
        hold = set(rng.choice(len(train_idx), size=n_hold, replace=False).tolist())
        valid_idx = [i for j, i in enumerate(train_idx) if j in hold]
        train_idx = [i for j, i in enumerate(train_idx) if j not in hold]
        split_note = "（脚本内部随机 80/20 划分）"
    elif not valid_idx:
        split_note = "（测点不足 10 个，全部用于建模，无留出评估）"

    Xtr, vtr = xy[train_idx], v[train_idx]
    unit = "dBm" if "device_rssi_dbm" in open(POINT_CSV, encoding="utf-8-sig").read() else "dB(相对)"

    print("=" * 68)
    print(f" 测点 {len(rows)} 个 → 训练 {len(train_idx)} 个 / 留出验证 {len(valid_idx)} 个{split_note}    单位 {unit}")
    print("=" * 68)

    # ---------- 留一交叉验证（在训练点上评估泛化能力）----------
    loo = {"IDW": [], "路径损耗": [], "GPR": []}
    for i in range(len(Xtr)):
        mask = np.ones(len(Xtr), dtype=bool)
        mask[i] = False
        Xa, va = Xtr[mask], vtr[mask]
        q = Xtr[i:i + 1]

        loo["IDW"].append(idw_predict(Xa, va, q)[0])
        p0, n = pathloss_fit(Xa, va)
        loo["路径损耗"].append(pathloss_predict(q, p0, n)[0])
        gpr = make_gpr()
        gpr.fit(Xa, va)
        loo["GPR"].append(float(gpr.predict(q)[0]))

    print(f"\n【留一交叉验证】在 {len(Xtr)} 个训练点上轮流“藏一个、猜一个”")
    print(f"{'模型':<12}{'MAE':>10}{'RMSE':>10}{'R2':>10}")
    print("-" * 68)
    loo_results = {}
    for name, preds in loo.items():
        m = metrics(vtr, np.array(preds))
        loo_results[name] = m
        print(f"{name:<12}{m['MAE']:>10.3f}{m['RMSE']:>10.3f}{m['R2']:>10.3f}")

    # ---------- 用全部训练点训练，去预测 5 个验证点 ----------
    p0, n = pathloss_fit(Xtr, vtr)
    gpr = make_gpr()
    gpr.fit(Xtr, vtr)

    holdout_results = {}
    if valid_idx:
        Xv, vv = xy[valid_idx], v[valid_idx]
        preds_holdout = {
            "IDW": idw_predict(Xtr, vtr, Xv),
            "路径损耗": pathloss_predict(Xv, p0, n),
            "GPR": gpr.predict(Xv),
        }
        print(f"\n【留出验证】用 {len(train_idx)} 个训练点建模，去猜 {len(valid_idx)} 个没参与建模的位置{split_note}")
        print(f"{'模型':<12}{'MAE':>10}{'RMSE':>10}{'R2':>10}")
        print("-" * 68)
        for name, p in preds_holdout.items():
            m = metrics(vv, np.array(p))
            holdout_results[name] = m
            print(f"{name:<12}{m['MAE']:>10.3f}{m['RMSE']:>10.3f}{m['R2']:>10.3f}")

        print("\n每个留出点的实测 vs 预测（GPR）")
        print(f"{'测点':<8}{'坐标(m)':<18}{'实测':>9}{'预测':>9}{'误差':>9}")
        print("-" * 68)
        for k, i in enumerate(valid_idx):
            r = rows[i]
            coord = f"({r['x']:.2f}, {r['y']:.2f})"
            pred_v = preds_holdout["GPR"][k]
            print(
                f"{r['point_id']:<8}{coord:<18}"
                f"{r['v']:>9.3f}{pred_v:>9.3f}{pred_v - r['v']:>9.3f}"
            )
    else:
        print("\n[提示] 测点不足 10 个，全部用于建模；多采一些点后训练脚本会自动随机留出 20% 做评估。")

    # ---------- 画热力图 ----------
    gx = np.linspace(0, ROOM_W, GRID_N)
    gy = np.linspace(0, ROOM_H, GRID_N)
    GX, GY = np.meshgrid(gx, gy)
    grid = np.column_stack([GX.ravel(), GY.ravel()])

    grid_gpr = gpr.predict(grid).reshape(GX.shape)
    grid_idw = idw_predict(Xtr, vtr, grid).reshape(GX.shape)
    grid_pl = pathloss_predict(grid, p0, n).reshape(GX.shape)

    # 非矩形房型（L 型/U 型）：外接矩形网格里落在房间外的格子置 NaN（图上留空）
    from matplotlib.path import Path as MplPath
    inside = MplPath(ROOM_POLY).contains_points(grid).reshape(GX.shape)
    for g in (grid_gpr, grid_idw, grid_pl):
        g[~inside] = np.nan
    poly_x = [p[0] for p in ROOM_POLY] + [ROOM_POLY[0][0]]
    poly_y = [p[1] for p in ROOM_POLY] + [ROOM_POLY[0][1]]

    vmin, vmax = float(v.min()), float(v.max())
    pad = max(1.0, (vmax - vmin) * 0.15)

    fig, ax = plt.subplots(figsize=(7.5, 8.6), dpi=150)
    cs = ax.contourf(GX, GY, grid_gpr, levels=40, cmap="turbo",
                     vmin=vmin - pad, vmax=vmax + pad)
    ax.contour(GX, GY, grid_gpr, levels=12, colors="k", linewidths=0.4, alpha=0.4)
    fig.colorbar(cs, ax=ax, shrink=0.85, label=f"RSS ({unit})")
    ax.plot(poly_x, poly_y, color="#222", linewidth=2, zorder=8)  # 房间轮廓

    for i, r in enumerate(rows):
        marker = "o" if r["role"] == "train" else "^"
        edge = "white" if r["role"] == "train" else "red"
        ax.scatter(r["x"], r["y"], c="black", s=46, marker=marker,
                   edgecolors=edge, linewidths=1.5, zorder=5)
        ax.annotate(r["point_id"], (r["x"], r["y"]), textcoords="offset points",
                    xytext=(6, 5), fontsize=7, color="black", zorder=6)

    ax.scatter([AP_X], [AP_Y], c="red", s=220, marker="*", zorder=7, label="路由器(AP)")
    ax.legend(loc="lower right", fontsize=9)
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.set_title(f"房间 RSS 分布预测（GPR，{len(train_idx)} 个测点重建）", fontsize=13)
    ax.set_xlim(0, ROOM_W)
    ax.set_ylim(0, ROOM_H)
    ax.set_aspect("equal")
    fig.tight_layout()
    OUT_HEAT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_HEAT)
    plt.close(fig)

    # 三模型对比图
    fig, axes = plt.subplots(1, 3, figsize=(16, 6.4), dpi=140)
    for ax_, gridv, title in zip(
        axes, [grid_idw, grid_pl, grid_gpr], ["IDW 几何插值", "路径损耗拟合", "GPR 机器学习"]
    ):
        cs = ax_.contourf(GX, GY, gridv, levels=40, cmap="turbo",
                          vmin=vmin - pad, vmax=vmax + pad)
        ax_.scatter(Xtr[:, 0], Xtr[:, 1], c="black", s=30, marker="o",
                    edgecolors="white", linewidths=1.2)
        if valid_idx:
            ax_.scatter(xy[valid_idx, 0], xy[valid_idx, 1], c="red", s=60, marker="^",
                        edgecolors="white", linewidths=1.2)
        ax_.scatter([AP_X], [AP_Y], c="red", s=180, marker="*")
        ax_.plot(poly_x, poly_y, color="#222", linewidth=1.8, zorder=8)  # 房间轮廓
        ax_.set_title(title, fontsize=12)
        ax_.set_xlim(0, ROOM_W)
        ax_.set_ylim(0, ROOM_H)
        ax_.set_aspect("equal")
        ax_.set_xlabel("x (m)")
    axes[0].set_ylabel("y (m)")
    fig.colorbar(cs, ax=axes, shrink=0.8, label=f"RSS ({unit})")
    fig.suptitle("三种方法推测同一个房间的 RSS 分布（黑点=训练点，红三角=留出验证点）", fontsize=13)
    fig.savefig(OUT_CMP, bbox_inches="tight")
    plt.close(fig)

    # ---------- 写评估报告 ----------
    lines = ["# RSS 重建模型评估报告", ""]
    lines.append(f"- 房间：{ROOM_NAME}（外接 {ROOM_W} m × {ROOM_H} m）")
    lines.append(f"- 房间状态：{'空置（空无一物）' if ROOM_EMPTY else '有障碍物（未建模）'}")
    lines.append(f"- 路由器位置：({AP_X}, {AP_Y}) m")
    lines.append(f"- 测点：共 {len(rows)} 个 → 训练 {len(train_idx)} 个 / 留出验证 {len(valid_idx)} 个{split_note}")
    lines.append(f"- 数值单位：{unit}")
    lines.append(f"- 路径损耗拟合：P(d) = {p0:.2f} {n:+.2f} · 10log10(d)")
    lines.append("")
    lines.append("## 留一交叉验证（训练集内部泛化能力）")
    lines.append("")
    lines.append("| 模型 | MAE | RMSE | R² |")
    lines.append("| --- | --- | --- | --- |")
    for name, m in loo_results.items():
        lines.append(f"| {name} | {m['MAE']:.3f} | {m['RMSE']:.3f} | {m['R2']:.3f} |")
    lines.append("")
    if holdout_results:
        lines.append(f"## 留出验证（预测 {len(valid_idx)} 个未参与建模的位置{split_note}）")
        lines.append("")
        lines.append("| 模型 | MAE | RMSE | R² |")
        lines.append("| --- | --- | --- | --- |")
        for name, m in holdout_results.items():
            lines.append(f"| {name} | {m['MAE']:.3f} | {m['RMSE']:.3f} | {m['R2']:.3f} |")
        lines.append("")
    best = min(holdout_results or loo_results, key=lambda k: (holdout_results or loo_results)[k]["MAE"])
    lines.append(f"## 结论")
    lines.append("")
    lines.append(f"在当前数据量下，**{best}** 的平均绝对误差最小。")
    lines.append("")
    lines.append("> 注意：留出集是训练脚本随机划分的（seed 固定、可复现）；")
    lines.append("> 增加采集轮次（不同时段重复采集）比换模型更能提升精度。")
    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n热力图       -> {OUT_HEAT.relative_to(PROJECT_ROOT)}")
    print(f"三模型对比图 -> {OUT_CMP.relative_to(PROJECT_ROOT)}")
    print(f"评估报告     -> {OUT_MD.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
