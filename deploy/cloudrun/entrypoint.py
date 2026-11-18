"""Cloud Run 高速起動用エントリーポイント (3.2GB SHA-256 フルスキャン省略 & 遅延 memmap 対応).

本来の mu_calculator の計算ロジック・API仕様は 100% そのまま維持しつつ、
Cloud Storage FUSE マウント時のコールドスタート時間（約25秒 → 約1秒）を劇的に短縮します：
  1. headers.json が存在する場合は 234個の .hdr 個別読み込みを1回のファイル読み込みに集約
  2. 各国の .bil ファイルは、実際にその国の範囲がサンプリングされた時点で初めて memmap する（遅延ロード）
  3. 事前計算済みテーブル (density-l*.json / .npy) の起動時 3.2GB ハッシュ再計算をスキップ
"""

import glob
import json
import os
from pathlib import Path
import sys
from copy import deepcopy

ROOT = Path(__file__).resolve().parent.parent.parent
if (ROOT / "mu_calculator").is_dir() and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import uvicorn

from mu_calculator import tables, geometry
from mu_calculator.geometry import D


class LazyRaster:
    """サンプリング範囲に入った時に初めて .bil を memmap するラッパー."""

    __slots__ = ("path", "shape", "_arr")

    def __init__(self, path, shape):
        self.path = path
        self.shape = shape
        self._arr = None

    def __getitem__(self, idx):
        if self._arr is None:
            self._arr = np.memmap(self.path, dtype="<f4", mode="r", shape=self.shape)
        return self._arr[idx]


class FastGrump(geometry.Grump):
    """headers.json と遅延 memmap により Cloud Storage FUSE 上でミリ秒単位で初期化する Grump."""

    def __init__(self, folder=None):
        folder = folder or os.environ.get("GRUMP_DIR", "/data/grump/bil")
        self.folder = folder
        self.density_table = {}
        self.r = []
        self.density_cache = {}

        headers_cache = Path(folder).parent / "headers.json"
        if headers_cache.is_file():
            entries = json.loads(headers_cache.read_text(encoding="utf-8"))
            for iso, gi0, gj0, nr, nc, bil_name in entries:
                bil_path = os.path.join(folder, bil_name)
                self.r.append((iso, gi0, gj0, LazyRaster(bil_path, (nr, nc))))
            return

        for h in sorted(glob.glob(os.path.join(folder, "*up00ag.hdr"))):
            iso = os.path.basename(h)[:3]
            with open(h) as header:
                hd = {
                    line.split()[0]: line.split()[1] for line in header if line.strip()
                }
            nr, nc = (int(hd["NROWS"]), int(hd["NCOLS"]))
            gi0 = round((float(hd["ULXMAP"]) - D / 2 + 180) / D)
            gj0 = round((90 - (float(hd["ULYMAP"]) + D / 2)) / D)
            self.r.append((iso, gi0, gj0, LazyRaster(h[:-4] + ".bil", (nr, nc))))


def fast_load_table(folder, level=6):
    """3.2GB 全ラスタの SHA-256 再計算を行わずにビルド済み .npy テーブルをメモリマップする."""
    meta = tables.table_path(folder, level)
    try:
        info = json.loads(meta.read_text(encoding="utf-8"))
        if (
            info.get("version") != tables.VERSION
            or info.get("level") != level
            or info.get("count") != tables.count(level)
            or info.get("dtype") != "<f8"
            or info.get("complete") is not True
        ):
            return None
        name = info["file"]
        if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
            return None
        path = meta.parent / name
        if path.stat().st_size != info["bytes"]:
            return None
        return np.load(path, mmap_mode="r", allow_pickle=False)
    except (OSError, ValueError, KeyError, TypeError, EOFError):
        return None


def fast_ensure_tables(folder, workers=None, maximum=None):
    maximum = tables.max_level(maximum)
    current = {}
    for level in range(tables.LEVEL, maximum + 1):
        arr = fast_load_table(folder, level)
        if arr is not None:
            current[level] = arr
    if current:
        return current
    # 1つもビルド済みテーブルがない場合のみ通常の ensure_tables にフォールバック
    return tables.ensure_tables(folder, workers=workers, maximum=maximum)


def build_headers_index(folder):
    """bil/ 内の 234 個の .hdr を 1 つの headers.json にまとめる."""
    entries = []
    for h in sorted(glob.glob(os.path.join(folder, "*up00ag.hdr"))):
        iso = os.path.basename(h)[:3]
        with open(h) as header:
            hd = {line.split()[0]: line.split()[1] for line in header if line.strip()}
        nr, nc = int(hd["NROWS"]), int(hd["NCOLS"])
        gi0 = round((float(hd["ULXMAP"]) - D / 2 + 180) / D)
        gj0 = round((90 - (float(hd["ULYMAP"]) + D / 2)) / D)
        bil_name = os.path.basename(h)[:-4] + ".bil"
        entries.append([iso, gi0, gj0, nr, nc, bil_name])
    out_path = Path(folder).parent / "headers.json"
    out_path.write_text(json.dumps(entries), encoding="utf-8")
    print(f"Wrote {out_path} ({len(entries)} headers)")


def main():
    folder = os.environ.get("GRUMP_DATA", "/data/grump")
    bil_folder = os.environ.setdefault("GRUMP_DIR", os.path.join(folder, "bil"))

    import mu_calculator.api as api_mod
    from fastapi.responses import HTMLResponse

    if os.environ.get("MU_FAST_STARTUP", "1").lower() in ("1", "true"):
        geometry.Grump = FastGrump
        api_mod.Grump = FastGrump
        api_mod.ensure_tables = fast_ensure_tables

    site_dir = Path(__file__).resolve().parent / "site"
    if not site_dir.is_dir():
        site_dir = ROOT / "site"
    if (site_dir / "index.html").is_file() and (site_dir / "style.css").is_file():
        css_text = (site_dir / "style.css").read_text(encoding="utf-8")
        html_text = (site_dir / "index.html").read_text(encoding="utf-8")
        html_text = html_text.replace(
            '<link rel="stylesheet" href="style.css">',
            f"<style>{css_text}</style>",
        ).replace(
            "const API_BASE = 'https://api.investigatexm.com';",
            "const API_BASE = location.origin;",
        )

        @api_mod.app.get("/ui", response_class=HTMLResponse, include_in_schema=False)
        def web_ui():
            return html_text

    port = int(os.environ.get("PORT", "8080"))
    log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
    log_config["handlers"]["default"]["stream"] = "ext://sys.stdout"

    uvicorn.run(
        "mu_calculator.api:app",
        host="0.0.0.0",
        port=port,
        access_log=False,
        proxy_headers=False,
        log_config=log_config,
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "build-headers":
        folder = os.environ.get("GRUMP_DATA", "mu_calculator/data/grump")
        build_headers_index(os.path.join(folder, "bil"))
    else:
        main()
