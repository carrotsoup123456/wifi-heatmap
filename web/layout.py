# -*- coding: utf-8 -*-
"""房型（layout）模块：几种形状模板 + 手动改数值 + 自动生成测点。

形状：方形 / 长方形 / L 型 / U 型（都是轴对齐多边形；DIY 户型暂不开放）。
单一事实来源：scripts/layout.json（形状+参数+多边形+AP+房间状态）
和 scripts/sites.csv（测点）。web/app.py 与 scripts/05_train_and_plot.py 共用。

多边形约定：顶点按逆时针（y 向上），单位米，原点 = 房间左下角。
L 型 = 外接矩形挖去右上角；U 型 = 外接矩形挖去上边中间。
房间状态固定标注为「空置（空无一物）」——有障碍物的场景见 docs/ 里的计划。
"""
from __future__ import annotations

import csv
import json
import math
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_JSON = ROOT / "scripts" / "layout.json"
SITES_CSV = ROOT / "scripts" / "sites.csv"

# ---------------------------------------------------------------- 形状模板
# fields：该形状允许手动修改的参数（key/中文标签/最小/最大），前端按它渲染表单
SHAPES = [
    {"id": "square", "name": "方形房",
     "params": {"side": 5.0},
     "fields": [{"key": "side", "label": "边长（米）", "min": 2.0, "max": 20.0}]},
    {"id": "rect", "name": "长方形房（演示房间）",
     "params": {"w": 6.62, "h": 7.74},
     "fields": [{"key": "w", "label": "宽 w（米）", "min": 2.0, "max": 20.0},
                {"key": "h", "label": "长 h（米）", "min": 2.0, "max": 20.0}]},
    {"id": "lshape", "name": "L 型房",
     "params": {"w": 8.0, "h": 10.0, "cut_w": 3.0, "cut_h": 4.0},
     "fields": [{"key": "w", "label": "总宽 w（米）", "min": 3.0, "max": 20.0},
                {"key": "h", "label": "总长 h（米）", "min": 3.0, "max": 20.0},
                {"key": "cut_w", "label": "缺口宽（米）", "min": 0.5, "max": 18.0},
                {"key": "cut_h", "label": "缺口深（米）", "min": 0.5, "max": 18.0}]},
    {"id": "ushape", "name": "U 型房",
     "params": {"w": 8.0, "h": 10.0, "notch_w": 3.0, "notch_h": 4.0},
     "fields": [{"key": "w", "label": "总宽 w（米）", "min": 3.0, "max": 20.0},
                {"key": "h", "label": "总长 h（米）", "min": 3.0, "max": 20.0},
                {"key": "notch_w", "label": "凹槽宽（米）", "min": 0.5, "max": 18.0},
                {"key": "notch_h", "label": "凹槽深（米）", "min": 0.5, "max": 18.0}]},
]
SHAPE_BY_ID = {s["id"]: s for s in SHAPES}

MIN_SPACING = 0.8
MAX_GRID = 9          # 行/列各自上限（保证 P11~P99 标签合法）
MAX_TRAIN = 100       # 测点总数上限（网格 9×9 封顶 81，基本不会触到）
WALL_MARGIN = 0.30    # 测点离墙最小距离（米），人拿板子要站得开

# 不再单独设验证点：所有测点一并采集导出，
# 训练/验证/检测集的划分交给训练脚本（05 内部随机 80/20 评估）。

DEFAULT_LAYOUT = {
    "preset_id": "rect", "name": "长方形房（演示房间）",
    "shape": "rect", "params": {"w": 6.62, "h": 7.74},
    "ap_x": 0.30, "ap_y": 0.30, "spacing": 1.0,
    "empty": True, "updated_at": None,
}


# ---------------------------------------------------------------- 多边形几何
def polygon_for(shape: str, p: dict) -> list[list[float]]:
    """按形状+参数生成逆时针顶点序列（y 向上）。"""
    if shape == "square":
        s = float(p["side"])
        return [[0, 0], [s, 0], [s, s], [0, s]]
    if shape == "rect":
        w, h = float(p["w"]), float(p["h"])
        return [[0, 0], [w, 0], [w, h], [0, h]]
    if shape == "lshape":   # 外接矩形挖去右上角
        w, h, cw, ch = float(p["w"]), float(p["h"]), float(p["cut_w"]), float(p["cut_h"])
        return [[0, 0], [w, 0], [w, h - ch], [w - cw, h - ch], [w - cw, h], [0, h]]
    if shape == "ushape":   # 外接矩形挖去上边中间
        w, h, nw, nh = float(p["w"]), float(p["h"]), float(p["notch_w"]), float(p["notch_h"])
        x0, x1, y0 = (w - nw) / 2, (w + nw) / 2, h - nh
        return [[0, 0], [w, 0], [w, h], [x1, h], [x1, y0], [x0, y0], [x0, h], [0, h]]
    raise ValueError(f"未知形状：{shape}")


def bounding_box(poly: list[list[float]]) -> tuple[float, float]:
    return max(v[0] for v in poly), max(v[1] for v in poly)


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    """射线法。边界上算在内。"""
    n = len(poly)
    inside = False
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        # 边界判断
        dx, dy = x2 - x1, y2 - y1
        cross = (x - x1) * dy - (y - y1) * dx
        if abs(cross) < 1e-9 and min(x1, x2) - 1e-9 <= x <= max(x1, x2) + 1e-9 \
                and min(y1, y2) - 1e-9 <= y <= max(y1, y2) + 1e-9:
            return True
        if (y1 > y) != (y2 > y):
            xin = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
            if x < xin:
                inside = not inside
    return inside


def dist_to_boundary(x: float, y: float, poly: list[list[float]]) -> float:
    """到最近边的距离（米）。"""
    best = math.inf
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
        px, py = x1 + t * dx, y1 + t * dy
        best = min(best, math.hypot(x - px, y - py))
    return best


# ---------------------------------------------------------------- layout 存取
def _normalize(lay: dict) -> dict:
    """补全缺省字段；旧版 layout.json（无 shape，有 room_w/room_h）按长方形兼容。"""
    out = dict(DEFAULT_LAYOUT)
    for k in ("preset_id", "name", "shape", "params", "ap_x", "ap_y",
              "spacing", "empty", "updated_at"):
        if k in lay and lay[k] is not None:
            out[k] = lay[k]
    if "shape" not in lay and ("room_w" in lay or "room_h" in lay):
        out["shape"] = "rect"
        out["params"] = {"w": lay.get("room_w", 6.62), "h": lay.get("room_h", 7.74)}
    if out["shape"] not in SHAPE_BY_ID:
        out["shape"] = DEFAULT_LAYOUT["shape"]
        out["params"] = dict(DEFAULT_LAYOUT["params"])
    # 参数缺项用模板默认值补齐
    base = dict(SHAPE_BY_ID[out["shape"]]["params"])
    base.update({k: float(v) for k, v in (out.get("params") or {}).items()})
    out["params"] = base
    poly = polygon_for(out["shape"], base)
    out["polygon"] = [[round(x, 3), round(y, 3)] for x, y in poly]
    out["room_w"], out["room_h"] = (round(v, 2) for v in bounding_box(poly))
    return out


def load_layout() -> dict:
    try:
        raw = json.loads(LAYOUT_JSON.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    return _normalize(raw)


def save_layout(lay: dict) -> None:
    LAYOUT_JSON.write_text(json.dumps(lay, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- 校验与生成
def validate(shape: str, params: dict, ap_x, ap_y, spacing) -> str | None:
    """返回 None 表示合法，否则返回中文错误信息。"""
    sh = SHAPE_BY_ID.get(shape)
    if not sh:
        return "未知房型"
    try:
        spacing = float(spacing)
        ap_x, ap_y = float(ap_x), float(ap_y)
        p = {f["key"]: float(params.get(f["key"])) for f in sh["fields"]}
    except (TypeError, ValueError):
        return "数值格式不对：请把参数填全、填数字"
    for f in sh["fields"]:
        v = p[f["key"]]
        if not (f["min"] <= v <= f["max"]):
            return f"{f['label']}要在 {f['min']}~{f['max']} 之间"
    if shape == "lshape" and not (p["cut_w"] < p["w"] - 0.5 and p["cut_h"] < p["h"] - 0.5):
        return "L 型缺口要比总尺寸小（至少留 0.5 米墙体）"
    if shape == "ushape" and not (p["notch_w"] < p["w"] - 1.0 and p["notch_h"] < p["h"] - 0.5):
        return "U 型凹槽要比总尺寸小（两侧至少各留 0.5 米）"
    if spacing < MIN_SPACING:
        return f"测点间距不能小于 {MIN_SPACING} 米（太密了采不完）"
    poly = polygon_for(shape, p)
    if not point_in_polygon(ap_x, ap_y, poly):
        return "路由器(AP) 不在房间内（别放进缺口里）"
    return None


def gen_points(poly: list[list[float]], spacing: float) -> list[dict]:
    """在外接矩形上按「等分区块取中心」布网格，丢掉落在房间外/贴墙的点。

    标签 P<行><列> 沿用网格序号（被丢掉的点位留空号，正好体现房间形状）。
    所有点都是采集点（不再单独布验证点）。
    """
    w, h = bounding_box(poly)
    cols = max(2, min(MAX_GRID, round(w / spacing)))
    rows = max(2, min(MAX_GRID, round(h / spacing)))
    while True:
        pts = []
        for r in range(rows):
            for c in range(cols):
                x = round((c + 0.5) * w / cols + 1e-9, 2)
                y = round((r + 0.5) * h / rows + 1e-9, 2)
                if not point_in_polygon(x, y, poly):
                    continue
                if dist_to_boundary(x, y, poly) < WALL_MARGIN - 1e-9:
                    continue
                pts.append({
                    "point_id": f"P{r + 1}{c + 1}",
                    "block": f"R{r + 1}C{c + 1}",
                    "role": "train",
                    "x": x, "y": y,
                    "note": f"第{r + 1}行第{c + 1}列区块中心",
                })
        if len(pts) <= MAX_TRAIN or (cols <= 2 and rows <= 2):
            break
        if cols >= rows and cols > 2:
            cols -= 1
        else:
            rows -= 1
    return pts


def preview(shape: str, params: dict, spacing) -> dict:
    lay = _normalize({"shape": shape, "params": params})
    train = gen_points(lay["polygon"], float(spacing))
    return {
        "ok": True, "train": train,
        "n_train": len(train), "n_total": len(train),
        "polygon": lay["polygon"], "room_w": lay["room_w"], "room_h": lay["room_h"],
    }


def _current_points() -> list[dict]:
    try:
        with SITES_CSV.open(encoding="utf-8") as fh:
            return [{"point_id": r["point_id"], "x": float(r["x_m"]), "y": float(r["y_m"])}
                    for r in csv.DictReader(fh)]
    except Exception:  # noqa: BLE001
        return []


def same_as_current(train: list[dict]) -> bool:
    """新生成的测点与当前 sites.csv 是否完全一致（id+坐标，顺序无关）。"""
    cur = _current_points()
    new = [{"point_id": p["point_id"], "x": p["x"], "y": p["y"]} for p in train]
    key = lambda p: (p["point_id"], p["x"], p["y"])  # noqa: E731
    return bool(cur) and sorted(cur, key=key) == sorted(new, key=key)


def apply(preset_id: str, shape: str, params: dict, ap_x, ap_y, spacing,
          keep_data: bool = False) -> tuple[dict, bool]:
    """应用新房型：写 layout.json；测点变了才重生成 sites.csv。

    keep_data=True 且新测点与当前完全一致（典型场景：只改了路由器坐标）时，
    不动 sites.csv，返回 (layout, True) 让调用方保留已采数据。
    """
    err = validate(shape, params, ap_x, ap_y, spacing)
    if err:
        raise ValueError(err)
    sh = SHAPE_BY_ID[shape]
    p = {f["key"]: round(float(params[f["key"]]), 2) for f in sh["fields"]}
    lay = _normalize({
        "preset_id": preset_id or shape, "name": sh["name"],
        "shape": shape, "params": p,
        "ap_x": round(float(ap_x), 2), "ap_y": round(float(ap_y), 2),
        "spacing": round(float(spacing), 2),
        "empty": True,   # 房型导入只表示「空置房间」；障碍物场景暂不建模
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    train = gen_points(lay["polygon"], lay["spacing"])
    kept = bool(keep_data) and same_as_current(train)
    save_layout(lay)
    if not kept:
        with SITES_CSV.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["point_id", "block", "role", "x_m", "y_m", "note"])
            for pt in train:
                w.writerow([pt["point_id"], pt["block"], pt["role"], pt["x"], pt["y"], pt["note"]])
    lay["n_train"], lay["n_val"] = len(train), 0
    return lay, kept
