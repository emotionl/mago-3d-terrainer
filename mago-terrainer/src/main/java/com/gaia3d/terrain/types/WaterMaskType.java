package com.gaia3d.terrain.types;

public enum WaterMaskType {
    NONE,       // 不启用水面掩码
    ALL_LAND,   // 0x00
    ALL_WATER,  // 0xFF
    MIXED       // 65536 bytes (256x256 grid)
}
