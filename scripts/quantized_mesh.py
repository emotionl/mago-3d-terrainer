"""
quantized_mesh.py — Quantized-Mesh 1.0 解码/编码/合并

实现 Cesium quantized-mesh terrain tile 格式的读写和合并。
格式参考: https://github.com/CesiumGS/quantized-mesh

Binary layout (all little-endian):
  Header:     88 bytes (CenterXYZ f64*3, MinMaxHeight f32*2, BoundingSphere f64*4, HorizonOcclusion f64*3)
  Vertices:   vertexCount(u32) + u[] + v[] + height[]  (zigzag-delta encoded uint16)
  Triangles:  triangleCount(u32) + indices[] (high-water-mark encoded uint16/uint32)
  Edges:      west/south/east/north counts(u32) + indices[](uint16/uint32)
  Extensions: extensionId(u8) + length(u32) + data[]
"""

import struct
from dataclasses import dataclass, field
from typing import Optional


def zigzag_decode(n: int) -> int:
    """uint16 zigzag decode."""
    return (n >> 1) ^ -(n & 1)


def zigzag_encode(n: int) -> int:
    """int zigzag encode to uint16."""
    return (n << 1) ^ (n >> 31)


def decode_high_water_mark(encoded: list[int]) -> list[int]:
    """解码 high-water-mark 编码的索引。"""
    decoded = []
    highest = 0
    for code in encoded:
        idx = highest - code
        decoded.append(idx)
        if code == 0:
            highest += 1
    return decoded


def encode_high_water_mark(decoded: list[int]) -> list[int]:
    """编码 high-water-mark 索引。"""
    encoded = []
    highest = 0
    for idx in decoded:
        code = highest - idx
        encoded.append(code)
        if code == 0:
            highest += 1
    return encoded


@dataclass
class QuantizedMeshHeader:
    center_x: float = 0.0
    center_y: float = 0.0
    center_z: float = 0.0
    minimum_height: float = 0.0
    maximum_height: float = 0.0
    bs_center_x: float = 0.0
    bs_center_y: float = 0.0
    bs_center_z: float = 0.0
    bs_radius: float = 0.0
    hop_x: float = 0.0
    hop_y: float = 0.0
    hop_z: float = 0.0


@dataclass
class QuantizedMeshTile:
    header: QuantizedMeshHeader = field(default_factory=QuantizedMeshHeader)
    vertex_count: int = 0
    u: list[int] = field(default_factory=list)
    v: list[int] = field(default_factory=list)
    height: list[int] = field(default_factory=list)
    triangle_count: int = 0
    triangle_indices: list[int] = field(default_factory=list)
    west_indices: list[int] = field(default_factory=list)
    south_indices: list[int] = field(default_factory=list)
    east_indices: list[int] = field(default_factory=list)
    north_indices: list[int] = field(default_factory=list)
    extensions: list[tuple[int, bytes]] = field(default_factory=list)


def decode_tile(data: bytes) -> QuantizedMeshTile:
    """从二进制数据解码一个 quantized-mesh tile。"""
    tile = QuantizedMeshTile()
    pos = 0

    # Header (88 bytes)
    hdr_fmt = "<ddd ff dddd ddd"
    hdr_size = struct.calcsize(hdr_fmt)
    vals = struct.unpack_from(hdr_fmt, data, pos)
    pos += hdr_size

    tile.header = QuantizedMeshHeader(
        center_x=vals[0], center_y=vals[1], center_z=vals[2],
        minimum_height=vals[3], maximum_height=vals[4],
        bs_center_x=vals[5], bs_center_y=vals[6], bs_center_z=vals[7],
        bs_radius=vals[8],
        hop_x=vals[9], hop_y=vals[10], hop_z=vals[11],
    )

    # Vertex data
    vertex_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    tile.vertex_count = vertex_count

    # u (zigzag delta)
    u_raw = list(struct.unpack_from(f"<{vertex_count}H", data, pos))
    pos += vertex_count * 2
    prev = 0
    tile.u = []
    for raw in u_raw:
        curr = prev + zigzag_decode(raw)
        tile.u.append(curr)
        prev = curr

    # v (zigzag delta)
    v_raw = list(struct.unpack_from(f"<{vertex_count}H", data, pos))
    pos += vertex_count * 2
    prev = 0
    tile.v = []
    for raw in v_raw:
        curr = prev + zigzag_decode(raw)
        tile.v.append(curr)
        prev = curr

    # height (zigzag delta)
    h_raw = list(struct.unpack_from(f"<{vertex_count}H", data, pos))
    pos += vertex_count * 2
    prev = 0
    tile.height = []
    for raw in h_raw:
        curr = prev + zigzag_decode(raw)
        tile.height.append(curr)
        prev = curr

    # Triangle indices
    triangle_count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    tile.triangle_count = triangle_count
    idx_count = triangle_count * 3

    if vertex_count > 65536:
        idx_raw = list(struct.unpack_from(f"<{idx_count}I", data, pos))
        pos += idx_count * 4
    else:
        idx_raw = list(struct.unpack_from(f"<{idx_count}H", data, pos))
        pos += idx_count * 2

    tile.triangle_indices = decode_high_water_mark(idx_raw)

    # Edge indices
    def read_edge_indices():
        nonlocal pos
        count = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if vertex_count > 65536:
            indices = list(struct.unpack_from(f"<{count}I", data, pos))
            pos += count * 4
        else:
            indices = list(struct.unpack_from(f"<{count}H", data, pos))
            pos += count * 2
        return indices

    tile.west_indices = read_edge_indices()
    tile.south_indices = read_edge_indices()
    tile.east_indices = read_edge_indices()
    tile.north_indices = read_edge_indices()

    # Extensions (read all remaining)
    while pos < len(data):
        if pos + 5 > len(data):
            break
        ext_id = struct.unpack_from("<B", data, pos)[0]
        pos += 1
        ext_len = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        if pos + ext_len > len(data):
            break
        ext_data = data[pos:pos + ext_len]
        pos += ext_len
        tile.extensions.append((ext_id, ext_data))

    return tile


def encode_tile(tile: QuantizedMeshTile) -> bytes:
    """将 QuantizedMeshTile 编码为二进制数据。"""
    parts = []

    # Header
    h = tile.header
    parts.append(struct.pack("<ddd", h.center_x, h.center_y, h.center_z))
    parts.append(struct.pack("<ff", h.minimum_height, h.maximum_height))
    parts.append(struct.pack("<dddd", h.bs_center_x, h.bs_center_y, h.bs_center_z, h.bs_radius))
    parts.append(struct.pack("<ddd", h.hop_x, h.hop_y, h.hop_z))

    # Vertex count
    vc = tile.vertex_count
    parts.append(struct.pack("<I", vc))

    # u (zigzag delta)
    def encode_zigzag_delta(values):
        result = []
        prev = 0
        for val in values:
            diff = val - prev
            result.append(zigzag_encode(diff) & 0xFFFF)
            prev = val
        return result

    u_enc = encode_zigzag_delta(tile.u)
    parts.append(struct.pack(f"<{vc}H", *u_enc))

    v_enc = encode_zigzag_delta(tile.v)
    parts.append(struct.pack(f"<{vc}H", *v_enc))

    h_enc = encode_zigzag_delta(tile.height)
    parts.append(struct.pack(f"<{vc}H", *h_enc))

    # Triangle count
    tc = tile.triangle_count
    parts.append(struct.pack("<I", tc))

    # Triangle indices (high-water-mark encoded)
    idx_enc = encode_high_water_mark(tile.triangle_indices)
    idx_count = tc * 3
    if vc > 65536:
        parts.append(struct.pack(f"<{idx_count}I", *idx_enc))
    else:
        parts.append(struct.pack(f"<{idx_count}H", *idx_enc))

    # Edge indices
    def write_edge(indices):
        parts.append(struct.pack("<I", len(indices)))
        if vc > 65536:
            parts.append(struct.pack(f"<{len(indices)}I", *indices))
        else:
            parts.append(struct.pack(f"<{len(indices)}H", *indices))

    write_edge(tile.west_indices)
    write_edge(tile.south_indices)
    write_edge(tile.east_indices)
    write_edge(tile.north_indices)

    # Extensions
    for ext_id, ext_data in tile.extensions:
        parts.append(struct.pack("<B", ext_id))
        parts.append(struct.pack("<I", len(ext_data)))
        parts.append(ext_data)

    return b"".join(parts)


def merge_tiles(tiles: list[QuantizedMeshTile]) -> QuantizedMeshTile:
    """合并多个覆盖同一区域但数据来源不同的 quantized-mesh tile。

    策略：
    1. 合并所有顶点（来自不同 chunk 的顶点在 tile 内的不同 u/v 位置）
    2. 合并三角形索引（后续 chunk 的索引偏移）
    3. 重算 min/max height，重新归一化 height 值
    4. 重算 bounding sphere
    5. 合并 edge indices
    6. Extensions 取第一个非空的 tile 的数据
    """
    if len(tiles) == 0:
        raise ValueError("No tiles to merge")
    if len(tiles) == 1:
        return tiles[0]

    # 计算合并后的 min/max height
    min_height = min(t.header.minimum_height for t in tiles)
    max_height = max(t.header.maximum_height for t in tiles)
    height_range = max_height - min_height if max_height != min_height else 1.0

    # 合并顶点和三角形
    merged = QuantizedMeshTile()
    merged.header.minimum_height = min_height
    merged.header.maximum_height = max_height

    all_u = []
    all_v = []
    all_height = []
    all_triangles = []
    all_west = []
    all_south = []
    all_east = []
    all_north = []

    vertex_offset = 0

    for tile in tiles:
        tile_min_h = tile.header.minimum_height
        tile_max_h = tile.header.maximum_height
        tile_range = tile_max_h - tile_min_h if tile_max_h != tile_min_h else 1.0

        # 重新归一化 height 到合并后的 range
        for i in range(tile.vertex_count):
            all_u.append(tile.u[i])
            all_v.append(tile.v[i])
            # 反归一化原 height → 再归一化到新 range
            orig_h = tile_min_h + (tile.height[i] / 32767.0) * tile_range
            new_h = int(round((orig_h - min_height) / height_range * 32767))
            new_h = max(0, min(32767, new_h))
            all_height.append(new_h)

        # 偏移三角形索引
        for idx in tile.triangle_indices:
            all_triangles.append(idx + vertex_offset)

        # 偏移 edge indices
        for idx in tile.west_indices:
            all_west.append(idx + vertex_offset)
        for idx in tile.south_indices:
            all_south.append(idx + vertex_offset)
        for idx in tile.east_indices:
            all_east.append(idx + vertex_offset)
        for idx in tile.north_indices:
            all_north.append(idx + vertex_offset)

        # Extensions: 取第一个有 extension 的 tile
        if not merged.extensions and tile.extensions:
            merged.extensions = tile.extensions

        vertex_offset += tile.vertex_count

    merged.vertex_count = len(all_u)
    merged.u = all_u
    merged.v = all_v
    merged.height = all_height
    merged.triangle_count = len(all_triangles) // 3
    merged.triangle_indices = all_triangles
    merged.west_indices = all_west
    merged.south_indices = all_south
    merged.east_indices = all_east
    merged.north_indices = all_north

    # 重算 header 的 center 和 bounding sphere
    # 使用第一个 tile 的 center 作为基准（同一 depth/X/Y 的 tile 地理范围相同）
    merged.header.center_x = tiles[0].header.center_x
    merged.header.center_y = tiles[0].header.center_y
    merged.header.center_z = tiles[0].header.center_z

    # Bounding sphere: 取最大的 radius（保守估计）
    merged.header.bs_center_x = tiles[0].header.bs_center_x
    merged.header.bs_center_y = tiles[0].header.bs_center_y
    merged.header.bs_center_z = tiles[0].header.bs_center_z
    merged.header.bs_radius = max(t.header.bs_radius for t in tiles)

    # Horizon occlusion point: 取第一个
    merged.header.hop_x = tiles[0].header.hop_x
    merged.header.hop_y = tiles[0].header.hop_y
    merged.header.hop_z = tiles[0].header.hop_z

    return merged
