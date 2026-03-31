#!/usr/bin/env python3
"""
terrain_chunker.py — 分块处理 GeoTIFF 地形数据

将大量 GeoTIFF 文件按经纬度范围分块，逐块调用 mago-3d-terrainer Java 工具，
最后合并所有 terrain tile 输出。

核心原理：
  - 所有分块生成全深度 tile（不做深度过滤）
  - 合并时：无冲突 tile 直接移动，冲突 tile（同一路径来自多个分块）通过
    quantized-mesh 二进制裁剪合并（合并顶点/三角形/边界）
  - 这样保证低深度 tile 包含完整的区域数据

用法示例:
    python terrain_chunker.py \
        --dem-dir /path/to/dem/ \
        --wbm-dir /path/to/wbm/ \
        --output-dir /path/to/output/ \
        --jar mago-terrainer/dist/mago-3d-terrainer-1.12.0-release.jar

    python terrain_chunker.py \
        --dem-dir input/dem --wbm-dir input/wbm \
        --output-dir output/ \
        --jar mago-terrainer/dist/mago-3d-terrainer-1.12.0-release.jar \
        --lat 3-4 --lon 73-74
"""

import argparse
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from quantized_mesh import decode_tile, encode_tile, merge_tiles

# ============================================================
# 文件名解析
# ============================================================

# DEM 文件有两种命名格式: Copernicus_DSM_10_ 和 Copernicus_DSM_COG_10_
DEM_PATTERN = re.compile(
    r"Copernicus_DSM_(?:COG_)?10_N(\d+)_00_E(\d+)_00_DEM\.tif$", re.IGNORECASE
)
WBM_PATTERN = re.compile(
    r"Copernicus_DSM_COG_10_N(\d+)_00_E(\d+)_00_WBM\.tif$", re.IGNORECASE
)


def parse_geo_coords(filename: str, pattern: re.Pattern) -> Optional[tuple[int, int]]:
    m = pattern.match(os.path.basename(filename))
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def scan_files(directory: Path, pattern: re.Pattern) -> dict[tuple[int, int], Path]:
    result = {}
    if not directory.exists():
        return result
    for f in directory.iterdir():
        if not f.is_file():
            continue
        coords = parse_geo_coords(f.name, pattern)
        if coords:
            result[coords] = f
    return result


# ============================================================
# 分块网格
# ============================================================


@dataclass
class Chunk:
    index: int
    lat_start: int
    lat_end: int  # inclusive
    lon_start: int
    lon_end: int  # inclusive
    dem_count: int = 0
    wbm_count: int = 0

    @property
    def label(self) -> str:
        return f"chunk_{self.index:02d}_lat{self.lat_start}-{self.lat_end}_lon{self.lon_start}-{self.lon_end}"

    def contains(self, lat: int, lon: int) -> bool:
        return self.lat_start <= lat <= self.lat_end and self.lon_start <= lon <= self.lon_end


def generate_chunks(
    lat_range: tuple[int, int],
    lon_range: tuple[int, int],
    chunk_size: int,
    dem_files: dict[tuple[int, int], Path],
    wbm_files: dict[tuple[int, int], Path],
) -> list[Chunk]:
    lat_min, lat_max = lat_range
    lon_min, lon_max = lon_range

    chunks = []
    idx = 0
    lat = lat_min
    while lat <= lat_max:
        lat_end = min(lat + chunk_size - 1, lat_max)
        lon = lon_min
        while lon <= lon_max:
            lon_end = min(lon + chunk_size - 1, lon_max)
            chunk = Chunk(
                index=idx,
                lat_start=lat,
                lat_end=lat_end,
                lon_start=lon,
                lon_end=lon_end,
            )
            dem_count = sum(1 for (la, lo) in dem_files if chunk.contains(la, lo))
            wbm_count = sum(1 for (la, lo) in wbm_files if chunk.contains(la, lo))
            chunk.dem_count = dem_count
            chunk.wbm_count = wbm_count
            if dem_count > 0:
                chunks.append(chunk)
                idx += 1
            lon += chunk_size
        lat += chunk_size
    return chunks


# ============================================================
# 断点续跑
# ============================================================


@dataclass
class Progress:
    chunks: dict[str, dict] = field(default_factory=dict)

    def is_completed(self, chunk_label: str) -> bool:
        info = self.chunks.get(chunk_label)
        return info is not None and info.get("status") == "completed"

    def mark_started(self, chunk_label: str):
        self.chunks[chunk_label] = {
            "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def mark_completed(self, chunk_label: str):
        if chunk_label in self.chunks:
            self.chunks[chunk_label]["status"] = "completed"
            self.chunks[chunk_label]["completed_at"] = time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        else:
            self.chunks[chunk_label] = {
                "status": "completed",
                "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

    def mark_failed(self, chunk_label: str, error: str):
        if chunk_label in self.chunks:
            self.chunks[chunk_label]["status"] = "failed"
            self.chunks[chunk_label]["error"] = error
        else:
            self.chunks[chunk_label] = {"status": "failed", "error": error}


def load_progress(path: Path) -> Progress:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            p = Progress()
            p.chunks = data.get("chunks", {})
            return p
        except (json.JSONDecodeError, OSError):
            pass
    return Progress()


def save_progress(path: Path, progress: Progress):
    data = {"chunks": progress.chunks}
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


# ============================================================
# 符号链接工作目录
# ============================================================


def create_symlink_dir(
    source_files: dict[tuple[int, int], Path],
    chunk: Chunk,
    target_dir: Path,
):
    target_dir.mkdir(parents=True, exist_ok=True)
    use_symlink = platform.system() != "Windows" or _can_symlink()

    for (lat, lon), src_path in source_files.items():
        if chunk.contains(lat, lon):
            link_path = target_dir / src_path.name
            if link_path.exists():
                continue
            if use_symlink:
                os.symlink(str(src_path.resolve()), str(link_path))
            else:
                shutil.copy2(str(src_path), str(link_path))



def _can_symlink() -> bool:
    try:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.touch()
            dst = Path(td) / "dst"
            os.symlink(str(src), str(dst))
            return True
    except OSError:
        return False


def prepare_chunk_workspace(
    chunk: Chunk,
    dem_files: dict[tuple[int, int], Path],
    wbm_files: dict[tuple[int, int], Path],
    work_dir: Path,
) -> tuple[Path, Path]:
    chunk_dir = work_dir / chunk.label
    dem_dir = chunk_dir / "dem"
    wbm_dir = chunk_dir / "wbm"

    create_symlink_dir(dem_files, chunk, dem_dir)
    create_symlink_dir(wbm_files, chunk, wbm_dir)

    return dem_dir, wbm_dir


# ============================================================
# Java 工具调用
# ============================================================


def run_terrainer(
    jar_path: Path,
    dem_input: Path,
    wbm_input: Path,
    output: Path,
    heap: str,
    extra_args: list[str],
    log_path: Path,
) -> int:
    cmd = [
        "java",
        f"-Xmx{heap}",
        "-jar",
        str(jar_path),
        "-i", str(dem_input),
        "-o", str(output),
        "-wm",
        "-wmp", str(wbm_input),
    ] + extra_args

    print(f"  命令: {' '.join(cmd)}")

    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.run(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True,
        )
    return proc.returncode



def run_layer_json_gen(
    jar_path: Path,
    output_dir: Path,
    heap: str,
    extra_args: list[str],
) -> int:
    cmd = [
        "java",
        f"-Xmx{heap}",
        "-jar",
        str(jar_path),
        "-j",
        "-i", str(output_dir),
        "-o", str(output_dir),
        "-wm",
    ] + extra_args

    print(f"  生成 layer.json 命令: {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  layer.json 生成失败:")
        print(f"    stdout: {result.stdout[-500:] if result.stdout else '(empty)'}")
        print(f"    stderr: {result.stderr[-500:] if result.stderr else '(empty)'}")
    return result.returncode


# ============================================================
# 输出合并（智能合并：无冲突直接移动，冲突 tile 做 quantized-mesh 合并）
# ============================================================


def smart_merge_outputs(
    chunk_work_dir: Path,
    final_output: Path,
) -> tuple[int, int, int]:
    """将所有分块输出合并到最终目录。

    策略：
    1. 先扫描所有分块输出，按 tile 相对路径分组
    2. 无冲突的 tile（只有一个来源）：直接移动
    3. 冲突的 tile（多个来源）：解码 quantized-mesh，合并顶点/三角形，重新编码

    Returns:
        (直接移动数, 冲突合并数, 合并失败数)
    """
    final_output.mkdir(parents=True, exist_ok=True)

    # 收集所有 tile: tile_rel_path -> list of (chunk_dir, terrain_file_path)
    tile_sources: dict[str, list[Path]] = defaultdict(list)

    for chunk_dir in sorted(chunk_work_dir.iterdir()):
        if not chunk_dir.is_dir() or not chunk_dir.name.startswith("chunk_"):
            continue
        chunk_output = chunk_dir / "output"
        if not chunk_output.exists():
            continue

        for depth_dir in chunk_output.iterdir():
            if not depth_dir.is_dir() or not depth_dir.name.isdigit():
                continue
            for x_dir in depth_dir.iterdir():
                if not x_dir.is_dir() or not x_dir.name.isdigit():
                    continue
                for terrain_file in x_dir.iterdir():
                    if not terrain_file.is_file() or not terrain_file.name.endswith(
                        ".terrain"
                    ):
                        continue
                    # 相对路径: depth/X/Y.terrain
                    rel_path = f"{depth_dir.name}/{x_dir.name}/{terrain_file.name}"
                    tile_sources[rel_path].append(terrain_file)

    moved_count = 0
    merged_count = 0
    failed_count = 0

    for rel_path, sources in tile_sources.items():
        dest = final_output / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        if len(sources) == 1:
            # 无冲突：直接移动
            shutil.move(str(sources[0]), str(dest))
            moved_count += 1
        else:
            # 冲突：quantized-mesh 合并
            try:
                tiles = []
                for src in sources:
                    data = src.read_bytes()
                    tiles.append(decode_tile(data))

                merged_tile = merge_tiles(tiles)
                merged_bytes = encode_tile(merged_tile)
                dest.write_bytes(merged_bytes)
                merged_count += 1
            except Exception as e:
                # 合并失败时，使用第一个来源的文件
                print(f"    合并失败 {rel_path}: {e}")
                shutil.move(str(sources[0]), str(dest))
                failed_count += 1

            # 清理源文件
            for src in sources:
                if src.exists():
                    src.unlink()

    return moved_count, merged_count, failed_count


# ============================================================
# CLI
# ============================================================


def parse_range(s: str) -> tuple[int, int]:
    parts = s.split("-")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"格式错误: '{s}'，应为 'start-end'，如 '3-54'")
    return int(parts[0]), int(parts[1])


def parse_chunk_index(s: str) -> list[int]:
    if "-" in s:
        parts = s.split("-")
        start, end = int(parts[0]), int(parts[1])
        return list(range(start, end + 1))
    return [int(s)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="分块处理 GeoTIFF 地形数据，逐块调用 mago-3d-terrainer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dem-dir", required=True, type=Path, help="DEM 文件目录")
    parser.add_argument("--wbm-dir", required=True, type=Path, help="WBM 文件目录")
    parser.add_argument("--output-dir", required=True, type=Path, help="最终合并输出目录")
    parser.add_argument("--jar", required=True, type=Path, help="Java JAR 路径")
    parser.add_argument("--lat", type=parse_range, default=None, help="纬度范围，如 3-54")
    parser.add_argument("--lon", type=parse_range, default=None, help="经度范围，如 73-136")
    parser.add_argument("--chunk-size", type=int, default=10, help="每块度数 (default: 10)")
    parser.add_argument("--chunk", type=parse_chunk_index, default=None, help="指定分块索引，如 3 或 3-7")
    parser.add_argument("--heap", default="16g", help="JVM 堆内存 (default: 16g)")
    parser.add_argument("--work-dir", type=Path, default=Path("./chunk_work"), help="临时工作目录")
    parser.add_argument("--java-opts", default="", help="额外 Java 参数，如 '-max 14 -th 4'")
    parser.add_argument("--dry-run", action="store_true", help="预览分块计划不执行")
    parser.add_argument("--clean", action="store_true", help="清除工作目录重新开始")
    parser.add_argument("--skip-merge", action="store_true", help="跳过合并步骤（仅处理分块）")
    return parser


# ============================================================
# 主流程
# ============================================================


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.dem_dir.exists():
        print(f"错误: DEM 目录不存在: {args.dem_dir}")
        sys.exit(1)
    if not args.jar.exists():
        print(f"错误: JAR 文件不存在: {args.jar}")
        sys.exit(1)

    extra_args = args.java_opts.split() if args.java_opts else []

    # 扫描文件
    print("=" * 60)
    print("扫描文件...")
    dem_files = scan_files(args.dem_dir, DEM_PATTERN)
    wbm_files = scan_files(args.wbm_dir, WBM_PATTERN)
    print(f"  DEM: {len(dem_files)} 个, WBM: {len(wbm_files)} 个")

    if not dem_files:
        print("错误: 未找到 DEM 文件")
        sys.exit(1)

    # 自动检测范围
    all_lats = sorted(set(lat for lat, lon in dem_files))
    all_lons = sorted(set(lon for lat, lon in dem_files))
    lat_range = args.lat if args.lat else (all_lats[0], all_lats[-1])
    lon_range = args.lon if args.lon else (all_lons[0], all_lons[-1])

    print(f"\n经纬度范围: lat {lat_range[0]}-{lat_range[1]}, lon {lon_range[0]}-{lon_range[1]}")
    print(f"分块大小: {args.chunk_size}°")

    # 生成分块
    chunks = generate_chunks(lat_range, lon_range, args.chunk_size, dem_files, wbm_files)
    if not chunks:
        print("没有找到匹配的分块")
        sys.exit(0)

    if args.chunk is not None:
        chunks = [c for c in chunks if c.index in args.chunk]
        if not chunks:
            print(f"指定的分块索引 {args.chunk} 不存在")
            sys.exit(1)

    # 显示分块计划
    print(f"\n共 {len(chunks)} 个分块:")
    print("-" * 60)
    for c in chunks:
        status = f"  DEM: {c.dem_count}, WBM: {c.wbm_count}" if args.dry_run else ""
        print(f"  [{c.index:02d}] lat {c.lat_start:3d}-{c.lat_end:3d}  "
              f"lon {c.lon_start:3d}-{c.lon_end:3d}{status}")
    print("-" * 60)

    if args.dry_run:
        print("\n[dry-run] 预览完成，未执行任何操作。")
        sys.exit(0)

    # 清理工作目录
    if args.clean and args.work_dir.exists():
        print(f"\n清理工作目录: {args.work_dir}")
        shutil.rmtree(args.work_dir)

    args.work_dir.mkdir(parents=True, exist_ok=True)

    progress_file = args.work_dir / "progress.json"
    progress = load_progress(progress_file)

    interrupted = False

    def signal_handler(sig, frame):
        nonlocal interrupted
        print("\n\n中断信号收到，等待当前分块完成后退出...")
        interrupted = True

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    # ================================================================
    # 逐块生成 terrain tile（全深度）
    # ================================================================
    print(f"\n{'=' * 60}")
    print("逐块生成 terrain tile（全深度）")
    print("=" * 60)

    completed = 0
    skipped = 0
    failed = 0
    start_time = time.time()

    for chunk in chunks:
        if interrupted:
            break

        if progress.is_completed(chunk.label):
            print(f"\n[{chunk.index:02d}] {chunk.label} — 已完成，跳过")
            skipped += 1
            continue

        print(f"\n{'=' * 60}")
        print(f"[{chunk.index:02d}] {chunk.label}")
        print(f"  范围: lat {chunk.lat_start}-{chunk.lat_end}, lon {chunk.lon_start}-{chunk.lon_end}")
        print(f"  DEM: {chunk.dem_count}, WBM: {chunk.wbm_count}")

        dem_dir, wbm_dir = prepare_chunk_workspace(
            chunk, dem_files, wbm_files, args.work_dir
        )

        chunk_output = args.work_dir / chunk.label / "output"
        log_path = args.work_dir / chunk.label / "output.log"

        progress.mark_started(chunk.label)
        save_progress(progress_file, progress)

        chunk_start = time.time()
        ret = run_terrainer(
            args.jar, dem_dir, wbm_dir, chunk_output,
            args.heap, extra_args, log_path,
        )
        chunk_elapsed = time.time() - chunk_start

        if ret == 0:
            progress.mark_completed(chunk.label)
            save_progress(progress_file, progress)
            completed += 1
            print(f"  完成 ({chunk_elapsed:.1f}s)")
        else:
            error_msg = f"exit code {ret}"
            progress.mark_failed(chunk.label, error_msg)
            save_progress(progress_file, progress)
            failed += 1
            print(f"  失败 ({error_msg})，日志: {log_path}")
            if log_path.exists():
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    tail = lines[-20:] if len(lines) > 20 else lines
                    print("  --- 日志末尾 ---")
                    for line in tail:
                        print(f"  {line.rstrip()}")
                    print("  ---")

    signal.signal(signal.SIGINT, original_handler)

    elapsed = time.time() - start_time
    print(f"\n{'=' * 60}")
    print(f"分块处理完成: 完成 {completed}, 跳过 {skipped}, 失败 {failed}")
    print(f"耗时: {elapsed:.1f}s")

    if interrupted:
        print("\n处理被中断。可重新运行以继续未完成的分块。")
        sys.exit(1)

    if failed > 0:
        print("\n存在失败的分块，请检查日志。")
        sys.exit(1)

    if args.skip_merge:
        print(f"\n{'=' * 60}")
        print("跳过合并，全部完成!")
        return

    # ================================================================
    # 智能合并输出
    # ================================================================
    print(f"\n{'=' * 60}")
    print("智能合并输出...")
    print("  策略: 无冲突 tile 直接移动，冲突 tile 进行 quantized-mesh 合并")
    print("=" * 60)

    moved, merged, failed = smart_merge_outputs(args.work_dir, args.output_dir)
    print(f"  直接移动: {moved} 个")
    print(f"  冲突合并: {merged} 个")
    if failed > 0:
        print(f"  合并失败（使用首个来源）: {failed} 个")

    # 生成 layer.json
    print("\n生成 layer.json...")
    ret = run_layer_json_gen(
        args.jar, args.output_dir, args.heap, extra_args=extra_args,
    )
    if ret == 0:
        layer_json = args.output_dir / "layer.json"
        if layer_json.exists():
            with open(layer_json, "r") as f:
                lj = json.load(f)
            bounds = lj.get("bounds", [])
            if bounds:
                print(f"  bounds: [{bounds[0]:.2f}, {bounds[1]:.2f}, {bounds[2]:.2f}, {bounds[3]:.2f}]")
            print(f"  layer.json 已生成: {layer_json}")
    else:
        print("  layer.json 生成失败，请手动运行:")
        print(f"  java -jar {args.jar} -j -i {args.output_dir} -o {args.output_dir} -wm")

    # 统计总 tile 数
    total_tiles = sum(
        1 for d in args.output_dir.iterdir()
        if d.is_dir() and d.name.isdigit()
        for x in d.rglob("*.terrain")
    )

    print(f"\n{'=' * 60}")
    print(f"全部完成! 共 {total_tiles} 个 terrain 文件")


if __name__ == "__main__":
    main()
