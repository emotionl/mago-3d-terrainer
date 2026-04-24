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
import sqlite3
import signal
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from itertools import islice
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

    max_retries = 3
    for attempt in range(max_retries):
        try:
            shutil.copy2(src_str, dest_str)
            return "copied", "", None
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
            else:
                return "failed", dest_str, str(e)


def _batched(iterable, n):
    """将可迭代对象分成大小为 n 的批次。最后一批可能不足 n 个。"""
    it = iter(iterable)
    while True:
        batch = list(islice(it, n))
        if not batch:
            return
        yield batch


# ============================================================
# 扫描分块输出（SQLite 缓存）
# ============================================================

CACHE_FILENAME = ".scan_cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    depth   INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    chunk   TEXT NOT NULL,
    PRIMARY KEY (depth, rel_path, chunk)
);
CREATE INDEX IF NOT EXISTS idx_tiles_depth ON tiles(depth);

CREATE TABLE IF NOT EXISTS tile_status (
    depth    INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (depth, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_status_depth ON tile_status(depth, status);
"""


def _init_db(db_path: Path) -> sqlite3.Connection:
    """初始化 SQLite 数据库。"""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _init_tile_status(conn: sqlite3.Connection, depth: int) -> None:
    """为指定 depth 初始化 tile_status（从 tiles 表导入，仅执行一次）。"""
    conn.execute("""
        INSERT OR IGNORE INTO tile_status (depth, rel_path, status)
        SELECT DISTINCT depth, rel_path, 'pending' FROM tiles WHERE depth = ?
    """, (depth,))
    conn.commit()


def _get_depth_stats(conn: sqlite3.Connection, depth: int) -> dict[str, int]:
    """获取指定 depth 的各状态计数。"""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM tile_status WHERE depth = ? GROUP BY status",
        (depth,),
    ).fetchall()
    stats = {"pending": 0, "completed": 0, "failed": 0}
    for status, count in rows:
        stats[status] = count
    return stats


def _get_pending_relpaths(conn: sqlite3.Connection, depth: int) -> set[str]:
    """获取指定 depth 的 pending 状态 rel_path 集合。"""
    rows = conn.execute(
        "SELECT rel_path FROM tile_status WHERE depth = ? AND status = 'pending'",
        (depth,),
    ).fetchall()
    return {r[0] for r in rows}


def _mark_tiles(conn: sqlite3.Connection, depth: int, rel_paths: list[str], status: str) -> None:
    """批量更新 tile 状态。"""
    conn.executemany(
        "UPDATE tile_status SET status = ? WHERE depth = ? AND rel_path = ?",
        [(status, depth, p) for p in rel_paths],
    )


def _tile_status(conn: sqlite3.Connection, depth: int, rel_path: str) -> str | None:
    """获取单个 tile 的状态。"""
    row = conn.execute(
        "SELECT status FROM tile_status WHERE depth = ? AND rel_path = ?",
        (depth, rel_path),
    ).fetchone()
    return row[0] if row else None


def _load_from_db(
    db_path: Path,
    skip_depths: set[int],
    chunk_work_dir: Path,
    logger: logging.Logger,
) -> dict[int, dict[str, list[Path]]] | None:
    """从 SQLite 加载扫描缓存。返回 None 表示缓存不可用。"""
    if not db_path.exists():
        return None

    logger.info(f"加载扫描缓存: {db_path}")
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        logger.warning("缓存文件损坏，将重新扫描")
        return None

    try:
        # 统计可用 depth
        placeholders = ",".join("?" * len(skip_depths)) if skip_depths else "0"
        depth_rows = conn.execute(
            f"SELECT DISTINCT depth FROM tiles WHERE depth NOT IN ({placeholders})",
            list(skip_depths),
        ).fetchall()
        available_depths = [r[0] for r in depth_rows]

        if not available_depths:
            conn.close()
            return {}

        # 按 depth 加载
        depth_tiles: dict[int, dict[str, list[Path]]] = {}
        total_tiles = 0
        for depth in available_depths:
            tiles: dict[str, list[Path]] = {}
            rows = conn.execute(
                "SELECT rel_path, chunk FROM tiles WHERE depth = ?", (depth,)
            ).fetchall()
            for rel_path, chunk in rows:
                src = chunk_work_dir / chunk / "output" / rel_path
                if rel_path in tiles:
                    tiles[rel_path].append(src)
                else:
                    tiles[rel_path] = [src]
            depth_tiles[depth] = tiles
            total_tiles += len(tiles)

        conn.close()
        logger.info(
            f"缓存加载完成 — {len(available_depths)} 个 depth 层级，"
            f"{total_tiles} 个 tile"
        )
        return depth_tiles

    except Exception as e:
        conn.close()
        logger.warning(f"缓存加载失败，将重新扫描: {e}")
        return None


def scan_chunk_outputs(
    chunk_work_dir: Path,
    logger: logging.Logger | None = None,
    skip_depths: set[int] | None = None,
) -> dict[int, dict[str, list[Path]]]:
    """扫描所有分块输出，按 depth 分组收集 tile。

    Args:
        skip_depths: 已完成的 depth 集合，扫描时跳过这些层级。

    Returns:
        {depth: {rel_path: [source_paths]}}
    """
    def log_info(msg: str):
        if logger:
            logger.info(msg)
        else:
            print(msg)

    skip_depths = skip_depths or set()

    # 尝试从 SQLite 缓存加载
    cache_path = chunk_work_dir / CACHE_FILENAME
    if logger:
        cached = _load_from_db(cache_path, skip_depths, chunk_work_dir, logger)
        if cached is not None:
            return cached

    # 缓存不可用，执行扫描（边扫描边写入 SQLite）
    depth_tiles: dict[int, dict[str, list[Path]]] = defaultdict(
        lambda: defaultdict(list)
    )

    all_entries = list(chunk_work_dir.iterdir())
    chunk_dirs = sorted(
        e for e in all_entries
        if e.is_dir() and e.name.startswith("chunk_")
    )
    total_chunks = len(chunk_dirs)
    log_info(f"发现 {total_chunks} 个 chunk 目录，开始扫描...")

    # 初始化数据库，边扫描边写入
    conn = _init_db(cache_path)
    total_file_count = 0

    for i, chunk_dir in enumerate(chunk_dirs, 1):
        chunk_output = chunk_dir / "output"
        if not chunk_output.exists():
            continue

        chunk_batch = []
        file_count = 0
        for depth_dir in chunk_output.iterdir():
            if not depth_dir.is_dir() or not depth_dir.name.isdigit():
                continue
            depth = int(depth_dir.name)
            if depth in skip_depths:
                continue
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
                    chunk_batch.append((depth, rel_path, chunk_dir.name))
                    file_count += 1

        # 每个 chunk 扫描完立即写入数据库
        if chunk_batch:
            conn.executemany(
                "INSERT OR IGNORE INTO tiles (depth, rel_path, chunk) VALUES (?, ?, ?)",
                chunk_batch,
            )
            conn.commit()
        total_file_count += file_count

        if i % 50 == 0 or i == total_chunks:
            log_info(f"扫描进度: {i}/{total_chunks} chunks, 累计 {total_file_count} 个 tile")

    conn.close()
    if total_file_count:
        log_info(f"缓存已保存 ({total_file_count} 条记录)")

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
    parser.add_argument("--rescan", action="store_true", help="强制重新扫描（忽略缓存）")
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

    # --rescan 清除缓存
    if args.rescan:
        cache_path = args.chunk_work_dir / CACHE_FILENAME
        if cache_path.exists():
            logger.info("--rescan: 清除扫描缓存")
            cache_path.unlink()

    # 收集已完成的 depth，扫描时跳过
    completed_depths: set[int] = set()
    db_path = args.chunk_work_dir / CACHE_FILENAME
    if db_path.exists():
        try:
            conn_check = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            depth_rows = conn_check.execute(
                "SELECT DISTINCT depth FROM tiles"
            ).fetchall()
            for (d,) in depth_rows:
                _init_tile_status(conn_check, d)
                stats = _get_depth_stats(conn_check, d)
                if stats["pending"] == 0 and stats["failed"] == 0:
                    completed_depths.add(d)
            conn_check.close()
        except Exception:
            pass
    # 兼容旧 manifest JSON
    if args.output_dir.exists():
        for depth in range(20):
            m = load_manifest(args.output_dir, depth)
            if m and m.get("status") == "completed":
                completed_depths.add(depth)

    if completed_depths:
        logger.info(f"已完成层级: {sorted(completed_depths)}，扫描时跳过")

    # 扫描分块输出
    logger.info("扫描分块输出...")
    depth_tiles = scan_chunk_outputs(
        args.chunk_work_dir, logger,
        skip_depths=completed_depths,
    )

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

    # 打开 SQLite 用于处理状态追踪
    db_conn = _init_db(db_path)

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

        # 初始化 tile_status 并检查是否已完成
        _init_tile_status(db_conn, depth)
        stats = _get_depth_stats(db_conn, depth)

        if stats["pending"] == 0 and stats["failed"] == 0:
            total_skipped += stats["completed"]
            logger.info(
                f"[depth {depth}] 已完成，跳过 "
                f"(共 {stats['completed']} 个 tile)"
            )
            continue

        # 获取 pending 的 rel_path，过滤出待处理任务
        pending_set = _get_pending_relpaths(db_conn, depth)
        single_source = {p: s[0] for p, s in tiles.items() if len(s) == 1}
        multi_source = {p: s for p, s in tiles.items() if len(s) > 1}

        single_tasks = {p: s for p, s in single_source.items() if p in pending_set}
        multi_tasks = {p: s for p, s in multi_source.items() if p in pending_set}

        # 统计已完成中单源/多源的数量
        depth_merged = sum(
            1 for p in multi_source
            if _tile_status(db_conn, depth, p) == "completed"
        )
        depth_copied = stats["completed"] - depth_merged
        depth_failed = stats["failed"]

        total_pending = len(single_tasks) + len(multi_tasks)
        logger.info(
            f"[depth {depth}] 开始处理 — 共 {len(tiles)} 个 tile "
            f"(拷贝 {len(single_tasks)}, 合并 {len(multi_tasks)}, "
            f"已完成 {stats['completed']}, 失败 {stats['failed']})"
        )
        depth_start = time.time()

        # 保存 manifest（精简，只有元数据）
        save_manifest(args.output_dir, depth, {
            "depth": depth,
            "status": "in_progress",
            "total_tiles": len(tiles),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        # 提交任务（分批提交，避免一次性创建大量 Future 对象）
        batch_size = 500        # 进度保存间隔
        submit_batch = 10_000   # 每批提交的任务数

        with ThreadPoolExecutor(max_workers=min(16, workers * 2)) as copy_pool, \
             ProcessPoolExecutor(max_workers=workers) as merge_pool:

            processed = 0

            # --- 分批处理拷贝任务 ---
            for batch in _batched(single_tasks.items(), submit_batch):
                if interrupted:
                    break

                copy_futures = {}
                for rel_path, src in batch:
                    dest = args.output_dir / rel_path
                    fut = copy_pool.submit(
                        _copy_tile_worker, (str(src), str(dest))
                    )
                    copy_futures[fut] = rel_path

                batch_completed = []
                batch_failed = []
                for fut in as_completed(copy_futures):
                    if interrupted:
                        break
                    try:
                        action, rel_path, error = fut.result()
                    except Exception as e:
                        action, rel_path, error = "failed", "", str(e)

                    actual_path = rel_path or copy_futures.get(fut, "")

                    if action == "copied":
                        depth_copied += 1
                        batch_completed.append(actual_path)
                    else:
                        depth_failed += 1
                        batch_failed.append(actual_path)
                        if error:
                            logger.warning(f"拷贝失败 {actual_path}: {error}")

                    processed += 1
                    if processed % batch_size == 0:
                        _mark_tiles(db_conn, depth, batch_completed, "completed")
                        _mark_tiles(db_conn, depth, batch_failed, "failed")
                        db_conn.commit()
                        batch_completed.clear()
                        batch_failed.clear()
                        logger.debug(
                            f"[depth {depth}] 进度 {processed}/{total_pending}"
                        )

                # 批次结束，提交剩余
                if batch_completed:
                    _mark_tiles(db_conn, depth, batch_completed, "completed")
                if batch_failed:
                    _mark_tiles(db_conn, depth, batch_failed, "failed")
                if batch_completed or batch_failed:
                    db_conn.commit()

            # --- 分批处理合并任务 ---
            for batch in _batched(multi_tasks.items(), submit_batch):
                if interrupted:
                    break

                merge_futures = {}
                for rel_path, sources in batch:
                    fut = merge_pool.submit(
                        _merge_tile_worker,
                        (rel_path, [str(s) for s in sources], str(args.output_dir)),
                    )
                    merge_futures[fut] = rel_path

                batch_completed = []
                batch_failed = []
                for fut in as_completed(merge_futures):
                    if interrupted:
                        break
                    try:
                        action, rel_path, error = fut.result()
                    except Exception as e:
                        action, rel_path, error = "failed", "", str(e)

                    actual_path = rel_path or merge_futures.get(fut, "")

                    if action == "merged":
                        depth_merged += 1
                        batch_completed.append(actual_path)
                    else:
                        depth_failed += 1
                        batch_failed.append(actual_path)
                        if error:
                            logger.warning(f"合并失败 {actual_path}: {error}")

                    processed += 1
                    if processed % batch_size == 0:
                        _mark_tiles(db_conn, depth, batch_completed, "completed")
                        _mark_tiles(db_conn, depth, batch_failed, "failed")
                        db_conn.commit()
                        batch_completed.clear()
                        batch_failed.clear()
                        logger.debug(
                            f"[depth {depth}] 进度 {processed}/{total_pending}"
                        )

                if batch_completed:
                    _mark_tiles(db_conn, depth, batch_completed, "completed")
                if batch_failed:
                    _mark_tiles(db_conn, depth, batch_failed, "failed")
                if batch_completed or batch_failed:
                    db_conn.commit()

        # 检查最终状态
        final_stats = _get_depth_stats(db_conn, depth)
        if final_stats["failed"] > 0:
            final_status = "in_progress"
            logger.warning(
                f"[depth {depth}] 有 {final_stats['failed']} 个失败 tile，"
                f"状态保持 in_progress，下次运行将重试"
            )
        else:
            final_status = "completed"

        save_manifest(args.output_dir, depth, {
            "depth": depth,
            "status": final_status,
            "total_tiles": len(tiles),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })

        depth_elapsed = time.time() - depth_start
        logger.info(
            f"[depth {depth}] 完成 — 拷贝 {depth_copied}, "
            f"合并 {depth_merged}, 失败 {depth_failed} "
            f"({depth_elapsed:.1f}s)"
        )

        total_copied += depth_copied
        total_merged += depth_merged
        total_failed += depth_failed

    db_conn.close()

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
