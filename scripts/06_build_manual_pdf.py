#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
06_build_manual_pdf.py —— 把 docs/操作手册.html 打包成 PDF。

做两件事：
  1. 把 HTML 里的 {{IMG_xxx}} 占位符替换成图片的 base64 内嵌数据
     （内嵌后 PDF 不依赖任何外部文件路径，不会出现丢图）
  2. 调用本机 Chrome 的 headless 模式打印成 PDF

输出：outputs/Wi-Fi热力图_数据采集操作手册.pdf

用法：
    python3 scripts/06_build_manual_pdf.py
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTML_SRC = PROJECT_ROOT / "docs" / "操作手册.html"
HTML_TMP = PROJECT_ROOT / ".deepworks" / "tmp" / "_manual_inline.html"
PDF_OUT = PROJECT_ROOT / "outputs" / "Wi-Fi热力图_数据采集操作手册.pdf"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

# 占位符 -> 候选图片路径（按顺序取第一个存在的）
IMAGES = {
    "{{IMG_SITE_MAP}}": ["outputs/采集点位图.png"],
    "{{IMG_HEATMAP}}": ["outputs/示例_热力图_预测.png", "outputs/热力图_预测.png"],
    "{{IMG_COMPARE}}": ["outputs/示例_三模型对比.png", "outputs/热力图_对比.png"],
}


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if Path(path).exists():
            return path
    sys.exit(
        "找不到 Chrome。PDF 生成需要 Chrome（Safari 不支持命令行打印）。\n"
        "安装：brew install --cask google-chrome"
    )


def main() -> int:
    if not HTML_SRC.exists():
        sys.exit(f"找不到源文件 {HTML_SRC}")

    html = HTML_SRC.read_text(encoding="utf-8")
    total_bytes = 0

    for placeholder, candidates in IMAGES.items():
        found = None
        for rel in candidates:
            p = PROJECT_ROOT / rel
            if p.exists():
                found = p
                break
        if found is None:
            print(f"  [!] {placeholder} 没有对应图片，替换为空")
            html = html.replace(placeholder, "")
            continue
        data = base64.b64encode(found.read_bytes()).decode("ascii")
        total_bytes += len(data)
        html = html.replace(placeholder, f"data:image/png;base64,{data}")
        print(f"  [ok] 内嵌 {found.name}  ({found.stat().st_size // 1024} KB)")

    if "{{IMG_" in html:
        leftover = html.count("{{IMG_")
        print(f"  [!] 仍有 {leftover} 个占位符未替换")

    HTML_TMP.parent.mkdir(parents=True, exist_ok=True)
    HTML_TMP.write_text(html, encoding="utf-8")
    print(f"  [ok] 合成 HTML：{HTML_TMP.relative_to(PROJECT_ROOT)} "
          f"({HTML_TMP.stat().st_size // 1024} KB)")

    chrome = find_chrome()
    PDF_OUT.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--virtual-time-budget=20000",
        f"--print-to-pdf={PDF_OUT}",
        HTML_TMP.as_uri(),
    ]
    print("  [..] 正在用 Chrome 打印 PDF...")
    proc = subprocess.run(cmd, capture_output=True, text=True)

    if not PDF_OUT.exists():
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:], file=sys.stderr)
        sys.exit("PDF 生成失败。")

    size_kb = PDF_OUT.stat().st_size // 1024
    print(f"\n完成 -> {PDF_OUT.relative_to(PROJECT_ROOT)}  ({size_kb} KB)")
    if size_kb < 20:
        print("  [!] 文件偏小，可能没渲染出内容，请打开检查。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
