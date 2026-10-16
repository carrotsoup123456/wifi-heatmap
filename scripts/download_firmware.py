#!/usr/bin/env python3
"""从 ESPectre 官方 Release 下载各芯片固件，并按 firmware.json 校验 sha256/大小。

固件为 ESPectre 项目（GPLv3）的预编译二进制，体积约 24MB，不随本仓库分发；
需要「一键烧录」功能时运行一次本脚本即可：

    python3 scripts/download_firmware.py
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

FIRMWARE_DIR = Path(__file__).resolve().parent.parent / "web" / "static" / "firmware"
META_FILE = FIRMWARE_DIR / "firmware.json"
RELEASE_URL = "https://github.com/francescopace/espectre/releases/download/v{version}/{file}"


def main() -> int:
    meta = json.loads(META_FILE.read_text(encoding="utf-8"))
    version = meta["version"]
    ok = True
    for chip, info in meta["chips"].items():
        dest = FIRMWARE_DIR / info["file"]
        if dest.exists():
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
            if digest == info["sha256"] and dest.stat().st_size == info["size"]:
                print(f"[跳过] {chip}: 已存在且校验通过")
                continue
            print(f"[重下] {chip}: 已有文件校验失败，重新下载")
        url = RELEASE_URL.format(version=version, file=info["file"])
        print(f"[下载] {chip}: {url}")
        try:
            data = urllib.request.urlopen(url, timeout=120).read()
        except Exception as e:  # noqa: BLE001
            print(f"  下载失败: {e}")
            ok = False
            continue
        digest = hashlib.sha256(data).hexdigest()
        if len(data) != info["size"] or digest != info["sha256"]:
            print(f"  校验失败（size={len(data)}, sha256={digest[:16]}…），未写入")
            ok = False
            continue
        dest.write_bytes(data)
        print(f"  完成 -> {dest}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
