#!/usr/bin/env python3
"""
merge_terrain.py — 合并分块 terrain tile 输出

从 chunk_work/ 目录收集所有分块生成的 terrain tile，合并到最终输出目录。
支持断点续跑、并行处理、完整日志。

策略：
  - 无冲突 tile（只有一个来源）：直接拷贝
  - 冲突 tile（多个来源）：解码 quantized-mesh，合并顶点/三角形，重新编码
  - 多源合并使用 ProcessPoolExecutor 绕过 GIL，单源拷贝使用 ThreadPoolExecutor

用法示例:
    python merge_terrain.py \
        --chunk-work-dir ./chunk_work \
        --output-dir ./output \
        --jar mago-terrainer/dist/mago-3d-terrainer-1.12.0-release.jar
"""

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path

from quantized_mesh import decode_tile, encode_tile, merge_tiles

# ============================================================
# Worker 函数（必须在顶层，供 ProcessPoolExecutor pickle）
# ============================================================


def _merge_tile_worker(args: tuple) -> tuple:
    """合并多个来源的 terrain tile。返回 (action, rel_path, error)。"""
    rel_path, source_strs, dest_dir_str = args
    dest_dir = Path(dest_dir_str)
    dest = dest_dir / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        tiles = []
        for src_str in source_strs:
            tiles.append(decode_tile(Path(src_str).read_bytes()))
        merged = merge_tiles(tiles)
        # 原子写入
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(encode_tile(merged))
        os.replace(str(tmp), str(dest))
        return "merged", rel_path, None
    except Exception as e:
        # 合并失败，拷贝第一个来源
        try:
            shutil.copy2(source_strs[0], str(dest))
        except Exception:
            pass
        return "failed", rel_path, str(e)


def _copy_tile_worker(args: tuple) -> tuple:
    """拷贝单个 terrain tile。返回 (action, rel_path, error)。"""
    src_str, dest_str = args
    dest = Path(dest_str)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src_str, dest_str)
        return "copied", "", None
    except Exception as e:
        return "failed", dest_str, str(e)


# ============================================================
# 扫描分块输出
# ============================================================


def scan_chunk_outputs(
    chunk_work_dir: Path,
) -> dict[int, dict[str, list[Path]]]:
    """扫描所有分块输出，按 depth 分组收集 tile。

    Returns:
        {depth: {rel_path: [source_paths]}}
    """
    depth_tiles: dict[int, dict[str, list[Path]]] = defaultdict(
        lambda: defaultdict(list)
    )

    for chunk_dir in sorted(chunk_work_dir.iterdir()):
        if not chunk_dir.is_dir() or not chunk_dir.name.startswith("chunk_"):
            continue
        chunk_output = chunk_dir / "output"
        if not chunk_output.exists():
            continue

        for depth_dir in chunk_output.iterdir():
            if not depth_dir.is_dir() or not depth_dir.name.isdigit():
                continue
            depth = int(depth_dir.name)
            for x_dir in depth_dir.iterdir():
                if not x_dir.is_dir() or not x_dir.name.isdigit():
                    continue
                for terrain_file in x_dir.iterdir():
                    if not terrain_file.is_file() or not terrain_file.name.endswith(
                        ".terrain"
                    ):
                        continue
                    rel_path = f"{depth_dir.name}/{x_dir.name}/{terrain_file.name}"
                    depth_tiles[depth][rel_path].append(terrain_file)

    return dict(depth_tiles)


# ============================================================
# 断点续跑（每个 depth 独立进度文件）
# ============================================================


def progress_dir(output_dir: Path) -> Path:
    return output_dir / ".merge_progress"


def manifest_path(output_dir: Path, depth: int) -> Path:
    return progress_dir(output_dir) / f"depth_{depth}.json"


def load_manifest(output_dir: Path, depth: int) -> dict | None:
    path = manifest_path(output_dir, depth)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_manifest(output_dir: Path, depth: int, manifest: dict):
    path = manifest_path(output_dir, depth)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    os.replace(str(tmp), str(path))


# ============================================================
# 日志配置
# ============================================================


def setup_logging(output_dir: Path) -> logging.Logger:
    output_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("merge_terrain")
    logger.setLevel(logging.DEBUG)

    # 文件 handler
    log_path = output_dir / "terrain-merge.log"
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    )
    logger.addHandler(fh)

    # 终端 handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    logger.addHandler(ch)

    return logger


# ============================================================
# layer.json 生成
# ============================================================


def generate_layer_json(
    jar_path: Path,
    output_dir: Path,
    heap: str,
    extra_args: list[str],
    logger: logging.Logger,
) -> int:
    logger.info("开始生成 layer.json...")
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
    logger.debug(f"命令: {' '.join(cmd)}")

    log_path = output_dir / "layer_json_gen.log"
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.run(
            cmd, stdout=log_file, stderr=subprocess.STDOUT, text=True
        )

    if proc.returncode != 0:
        logger.error(f"layer.json 生成失败 (exit code {proc.returncode})")
        logger.info(f"日志: {log_path}")
        logger.info(
            f"手动运行: java -jar {jar_path} -j -i {output_dir} -o {output_dir} -wm"
        )
        return proc.returncode

    layer_json = output_dir / "layer.json"
    if layer_json.exists():
        with open(layer_json, "r") as f:
            lj = json.load(f)
        bounds = lj.get("bounds", [])
        if bounds:
            logger.info(
                f"bounds: [{bounds[0]:.2f}, {bounds[1]:.2f}, {bounds[2]:.2f}, {bounds[3]:.2f}]"
            )
    logger.info("layer.json 生成完成")
    return 0


# ============================================================
# CLI
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="合并分块 terrain tile 输出（支持断点续跑）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--chunk-work-dir",
        type=Path,
        default=Path("./chunk_work"),
        help="分块工作目录 (default: ./chunk_work)",
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path, help="最终合并输出目录"
    )
    parser.add_argument(
        "--jar", required=True, type=Path, help="Java JAR 路径（用于生成 layer.json）"
    )
    parser.add_argument("--heap", default="16g", help="JVM 堆内存 (default: 16g)")
    parser.add_argument("--java-opts", default="", help="额外 Java 参数")
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="并行 worker 数 (default: CPU 核数)",
    )
    parser.add_argument("--clean", action="store_true", help="清除输出目录重新开始")
    parser.add_argument("--skip-layer-json", action="store_true", help="跳过 layer.json 生成")
    return parser


# ============================================================
# 主流程
# ============================================================


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.chunk_work_dir.exists():
        print(f"错误: 分块工作目录不存在: {args.chunk_work_dir}")
        sys.exit(1)
    if not args.jar.exists():
        print(f"错误: JAR 文件不存在: {args.jar}")
        sys.exit(1)

    extra_args = args.java_opts.split() if args.java_opts else []
    workers = args.workers or os.cpu_count() or 4

    # 清理
    if args.clean and args.output_dir.exists():
        print(f"清除输出目录: {args.output_dir}")
        shutil.rmtree(args.output_dir)

    logger = setup_logging(args.output_dir)
    logger.info("=" * 60)
    logger.info("merge_terrain.py 启动")
    logger.info(f"分块目录: {args.chunk_work_dir}")
    logger.info(f"输出目录: {args.output_dir}")
    logger.info(f"JAR: {args.jar}")
    logger.info(f"并行 workers: {workers}")

    # 扫描分块输出
    logger.info("扫描分块输出...")
    depth_tiles = scan_chunk_outputs(args.chunk_work_dir)

    if not depth_tiles:
        logger.error("未找到任何 terrain tile")
        sys.exit(1)

    total_tiles = sum(len(tiles) for tiles in depth_tiles.values())
    conflict_tiles = sum(
        1 for tiles in depth_tiles.values() for s in tiles.values() if len(s) > 1
    )
    logger.info(
        f"共 {len(depth_tiles)} 个 depth 层级，{total_tiles} 个 tile，"
        f"{conflict_tiles} 个冲突合并"
    )

    # 中断处理
    interrupted = False

    def signal_handler(sig, frame):
        nonlocal interrupted
        logger.info("中断信号收到，等待当前处理完成后退出...")
        interrupted = True

    original_handler = signal.signal(signal.SIGINT, signal_handler)

    # ================================================================
    # 按 depth 逐层处理
    # ================================================================
    overall_start = time.time()
    total_copied = 0
    total_merged = 0
    total_failed = 0
    total_skipped = 0

    for depth in sorted(depth_tiles.keys()):
        if interrupted:
            logger.info("处理被中断，已保存进度，可重新运行继续")
            break

        tiles = depth_tiles[depth]
        single_source = {p: s[0] for p, s in tiles.items() if len(s) == 1}
        multi_source = {p: s for p, s in tiles.items() if len(s) > 1}

        # 检查断点
        manifest = load_manifest(args.output_dir, depth)
        if manifest and manifest.get("status") == "completed":
            skipped_c = manifest.get("copied", 0)
            skipped_m = manifest.get("merged", 0)
            skipped_f = manifest.get("failed", 0)
            total_skipped += skipped_c + skipped_m + skipped_f
            logger.info(
                f"[depth {depth}] 已完成，跳过 "
                f"(拷贝 {skipped_c}, 合并 {skipped_m}, 失败 {skipped_f})"
            )
            continue

        # 恢复已完成的 tile
        completed_paths = set()
        if manifest and manifest.get("status") == "in_progress":
            completed_paths = set(manifest.get("completed_paths", []))

        # 过滤已完成的 tile
        single_tasks = {
            p: s for p, s in single_source.items() if p not in completed_paths
        }
        multi_tasks = {
            p: s for p, s in multi_source.items() if p not in completed_paths
        }

        new_completed = list(completed_paths)
        depth_copied = len(completed_paths & set(single_source.keys()))
        depth_merged = len(completed_paths & set(multi_source.keys()))
        depth_failed = 0

        logger.info(
            f"[depth {depth}] 开始处理 — 共 {len(tiles)} 个 tile "
            f"(拷贝 {len(single_tasks)}, 合并 {len(multi_tasks)}, "
            f"已完成 {len(completed_paths)})"
        )
        depth_start = time.time()

        # 初始化 manifest
        if not manifest:
            manifest = {
                "depth": depth,
                "status": "in_progress",
                "total_tiles": len(tiles),
                "copied": 0,
                "merged": 0,
                "failed": 0,
                "completed_paths": [],
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_manifest(args.output_dir, depth, manifest)

        # 提交任务
        batch_size = 500

        with ThreadPoolExecutor(max_workers=min(16, workers * 2)) as copy_pool, \
             ProcessPoolExecutor(max_workers=workers) as merge_pool:

            # 拷贝任务
            copy_futures = {}
            for rel_path, src in single_tasks.items():
                dest = args.output_dir / rel_path
                fut = copy_pool.submit(
                    _copy_tile_worker, (str(src), str(dest))
                )
                copy_futures[fut] = rel_path

            # 合并任务
            merge_futures = {}
            for rel_path, sources in multi_tasks.items():
                fut = merge_pool.submit(
                    _merge_tile_worker,
                    (rel_path, [str(s) for s in sources], str(args.output_dir)),
                )
                merge_futures[fut] = rel_path

            # 收集结果
            processed = 0
            for fut in as_completed(
                list(copy_futures.keys()) + list(merge_futures.keys())
            ):
                if interrupted:
                    break
                try:
                    action, rel_path, error = fut.result()
                except Exception as e:
                    action, rel_path, error = "failed", "", str(e)

                if action == "copied":
                    depth_copied += 1
                elif action == "merged":
                    depth_merged += 1
                else:
                    depth_failed += 1
                    if error:
                        logger.warning(f"合并失败 {rel_path}: {error}")

                # 记录完成
                actual_path = rel_path or copy_futures.get(fut) or merge_futures.get(fut, "")
                if actual_path:
                    new_completed.append(actual_path)

                processed += 1
                # 批量保存进度
                if processed % batch_size == 0:
                    manifest.update({
                        "status": "in_progress",
                        "copied": depth_copied,
                        "merged": depth_merged,
                        "failed": depth_failed,
                        "completed_paths": new_completed,
                    })
                    save_manifest(args.output_dir, depth, manifest)
                    logger.debug(
                        f"[depth {depth}] 进度 {processed}/{len(single_tasks) + len(multi_tasks)}"
                    )

        # 保存最终 manifest
        manifest.update({
            "status": "completed",
            "copied": depth_copied,
            "merged": depth_merged,
            "failed": depth_failed,
            "completed_paths": new_completed,
        })
        save_manifest(args.output_dir, depth, manifest)

        depth_elapsed = time.time() - depth_start
        logger.info(
            f"[depth {depth}] 完成 — 拷贝 {depth_copied}, "
            f"合并 {depth_merged}, 失败 {depth_failed} "
            f"({depth_elapsed:.1f}s)"
        )

        total_copied += depth_copied
        total_merged += depth_merged
        total_failed += depth_failed

    signal.signal(signal.SIGINT, original_handler)

    overall_elapsed = time.time() - overall_start
    logger.info("=" * 60)
    logger.info(
        f"合并完成: 拷贝 {total_copied}, 合并 {total_merged}, "
        f"失败 {total_failed}, 跳过 {total_skipped} ({overall_elapsed:.1f}s)"
    )

    if interrupted:
        logger.info("处理被中断。可重新运行以继续未完成的层级。")
        sys.exit(1)

    if total_failed > 0:
        logger.warning(f"存在 {total_failed} 个合并失败的 tile（已使用首个来源替代）")

    if args.skip_layer_json:
        logger.info("跳过 layer.json 生成")
    else:
        generate_layer_json(
            args.jar, args.output_dir, args.heap, extra_args, logger
        )

    # 统计总 tile 数
    total_tiles = sum(
        1
        for d in args.output_dir.iterdir()
        if d.is_dir() and d.name.isdigit()
        for _ in d.rglob("*.terrain")
    )
    logger.info(f"全部完成! 共 {total_tiles} 个 terrain 文件")


if __name__ == "__main__":
    main()
