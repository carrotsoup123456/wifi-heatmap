#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
00_draw_site_map.py —— 生成一张“房间分区与测点位置图”，打印出来照着摆板子。

输入：  scripts/sites.csv
输出：  outputs/采集点位图.png

用法：
    tools/espectre/.venv/bin/python scripts/00_draw_site_map.py
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
SITES_CSV = PROJECT_ROOT / "scripts" / "sites.csv"
OUT_PNG = PROJECT_ROOT / "outputs" / "采集点位图.png"

ROOM_W = 6.62
ROOM_H = 7.74
N_COL = 4
N_ROW = 5
AP_X, AP_Y = 0.30, 0.30


def main() -> int:
    import matplotlib
    matplotlib.rcParams["font.sans-serif"] = [
        "PingFang SC", "Heiti SC", "Songti SC", "Arial Unicode MS", "DejaVu Sans",
    ]
    matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    with SITES_CSV.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    fig, ax = plt.subplots(figsize=(8.6, 10.0), dpi=150)

    # ---- 房间地板 ----
    ax.add_patch(Rectangle((0, 0), ROOM_W, ROOM_H, facecolor="#f7f7f4",
                           edgecolor="black", linewidth=2.5, zorder=1))

    # ---- 4×5 区块网格 ----
    for c in range(1, N_COL):
        x = ROOM_W * c / N_COL
        ax.plot([x, x], [0, ROOM_H], color="#9aa0a6", linewidth=1.0,
                linestyle=(0, (6, 5)), zorder=2)
    for r in range(1, N_ROW):
        y = ROOM_H * r / N_ROW
        ax.plot([0, ROOM_W], [y, y], color="#9aa0a6", linewidth=1.0,
                linestyle=(0, (6, 5)), zorder=2)

    # ---- 区块编号（画在格子右下角）----
    for r in range(N_ROW):
        for c in range(N_COL):
            bx = ROOM_W * (c + 1) / N_COL - 0.12
            by = ROOM_H * r / N_ROW + 0.12
            ax.text(bx, by, f"R{r+1}C{c+1}", fontsize=9, color="#5f6368",
                    ha="right", va="bottom", zorder=3)

    # ---- 测点 ----
    for row in rows:
        x, y = float(row["x_m"]), float(row["y_m"])
        pid = row["point_id"]
        is_train = row["role"] == "train"
        if is_train:
            ax.scatter(x, y, s=150, marker="o", facecolor="#1a73e8",
                       edgecolor="white", linewidths=1.8, zorder=6)
            ax.annotate(pid, (x, y), textcoords="offset points", xytext=(0, 13),
                        ha="center", fontsize=11, fontweight="bold", color="#174ea6", zorder=7)
        else:
            ax.scatter(x, y, s=190, marker="^", facecolor="#e8710a",
                       edgecolor="white", linewidths=1.8, zorder=6)
            ax.annotate(pid, (x, y), textcoords="offset points", xytext=(0, 15),
                        ha="center", fontsize=11, fontweight="bold", color="#b06000",
                        zorder=7,
                        bbox=dict(facecolor="white", alpha=0.9, edgecolor="none", pad=1.5))

    # ---- 路由器 ----
    ax.scatter([AP_X], [AP_Y], s=800, marker="*", facecolor="#d93025",
               edgecolor="white", linewidths=1.5, zorder=8)
    ax.annotate("路由器", (AP_X, AP_Y), textcoords="offset points", xytext=(18, 4),
                fontsize=11, fontweight="bold", color="#b3261e", zorder=9)

    # ---- 尺寸标注 ----
    ax.annotate("", xy=(0, -0.35), xytext=(ROOM_W, -0.35),
                arrowprops=dict(arrowstyle="<->", color="black", lw=1.2))
    ax.text(ROOM_W / 2, -0.55, f"{ROOM_W} m", ha="center", fontsize=11)
    ax.annotate("", xy=(-0.35, 0), xytext=(-0.35, ROOM_H),
                arrowprops=dict(arrowstyle="<->", color="black", lw=1.2))
    ax.text(-0.62, ROOM_H / 2, f"{ROOM_H} m", va="center", rotation=90, fontsize=11)

    # ---- 图例 ----
    from matplotlib.lines import Line2D
    legend_items = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor="#1a73e8",
               markeredgecolor="white", markersize=12,
               label="训练点 P11–P54（20 个，摆在区块正中心）"),
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#e8710a",
               markeredgecolor="white", markersize=12,
               label="验证点 V01–V05（5 个，藏在格子交界处，用来考模型）"),
        Line2D([0], [0], marker="*", color="w", markerfacecolor="#d93025",
               markeredgecolor="white", markersize=18, label="路由器（固定在墙角）"),
    ]
    ax.legend(handles=legend_items, loc="upper center",
              bbox_to_anchor=(0.5, -0.06), fontsize=10, frameon=False, ncol=1)

    ax.set_xlim(-1.1, ROOM_W + 0.5)
    ax.set_ylim(-1.1, ROOM_H + 0.5)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(f"房间分区与测点位置图（{ROOM_W} m × {ROOM_H} m，共 25 个测点）",
                 fontsize=14, pad=14)

    fig.tight_layout()
    OUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT_PNG)
    plt.close(fig)
    print(f"点位图 -> {OUT_PNG.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
