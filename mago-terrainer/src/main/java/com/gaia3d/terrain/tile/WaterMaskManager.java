package com.gaia3d.terrain.tile;

import com.gaia3d.command.GlobalOptions;
import com.gaia3d.terrain.tile.geotiff.GaiaGeoTiffManager;
import com.gaia3d.terrain.types.WaterMaskType;
import lombok.Getter;
import lombok.Setter;
import lombok.extern.slf4j.Slf4j;
import org.geotools.api.referencing.operation.TransformException;
import org.geotools.coverage.grid.GridCoverage2D;
import org.geotools.geometry.jts.ReferencedEnvelope;
import org.joml.Vector2i;

import java.awt.image.Raster;
import java.awt.image.WritableRaster;
import java.io.IOException;

@Slf4j
@Getter
@Setter
public class WaterMaskManager {
    private GridCoverage2D waterMaskCoverage;
    private ReferencedEnvelope envelope;
    private GaiaGeoTiffManager geoTiffManager;
    private Vector2i coverageSize;
    private double minLon;
    private double minLat;
    private double maxLon;
    private double maxLat;
    private double lonRange;
    private double latRange;
    private boolean isLoaded = false;

    // 预加载的栅格数据（已加载到内存）
    private int[] waterMaskData;
    private int maskWidth;
    private int maskHeight;

    public void loadWaterMask(String filePath) {
        if (filePath == null || filePath.isEmpty()) {
            log.warn("Water mask file path is not provided.");
            return;
        }

        this.geoTiffManager = new GaiaGeoTiffManager();
        this.waterMaskCoverage = geoTiffManager.loadGeoTiffGridCoverage2D(filePath);
        this.envelope = waterMaskCoverage.getEnvelope2D();

        this.minLon = envelope.getMinX();
        this.minLat = envelope.getMinY();
        this.maxLon = envelope.getMaxX();
        this.maxLat = envelope.getMaxY();
        this.lonRange = maxLon - minLon;
        this.latRange = maxLat - minLat;

        this.coverageSize = geoTiffManager.getGridCoverage2DSize(filePath);
        this.maskWidth = coverageSize.x;
        this.maskHeight = coverageSize.y;

        // 预加载栅格数据到内存
        Raster raster = waterMaskCoverage.getRenderedImage().getData();
        this.waterMaskData = raster.getPixels(0, 0, maskWidth, maskHeight, (int[]) null);

        this.isLoaded = true;
        log.info("Water mask loaded from: {} ({}x{} pixels preloaded)", filePath, maskWidth, maskHeight);
    }

    public boolean isWater(double lonDeg, double latDeg) {
        if (!isLoaded || waterMaskData == null) {
            return false;
        }

        try {
            // 检查是否在范围内
            if (lonDeg < minLon || lonDeg > maxLon || latDeg < minLat || latDeg > maxLat) {
                return false;
            }

            // 计算归一化坐标 (0-1)
            double unitaryX = (lonDeg - minLon) / lonRange;
            double unitaryY = 1.0 - (latDeg - minLat) / latRange; // 注意：Y轴方向是向上的

            // 计算像素坐标
            int col = (int) Math.floor(unitaryX * maskWidth);
            int row = (int) Math.floor(unitaryY * maskHeight);

            // 确保在有效范围内
            col = Math.max(0, Math.min(col, maskWidth - 1));
            row = Math.max(0, Math.min(row, maskHeight - 1));

            // 从预加载的数据中获取值
            int index = row * maskWidth + col;
            return waterMaskData[index] > 0; // > 0 表示水面
        } catch (Exception e) {
            log.debug("Error evaluating water mask at ({}, {}): {}", lonDeg, latDeg, e.getMessage());
        }
        return false;
    }

    public WaterMaskType getWaterMaskType(double minLon, double minLat, double maxLon, double maxLat) {
        if (!isLoaded) {
            return WaterMaskType.NONE;
        }

        boolean hasLand = false;
        boolean hasWater = false;

        // 采样检测（步长64，约400个采样点）
        int sampleStep = 64;
        for (int y = 0; y <= sampleStep && (!hasLand || !hasWater); y++) {
            for (int x = 0; x <= sampleStep && (!hasLand || !hasWater); x++) {
                double lat = minLat + (maxLat - minLat) * y / sampleStep;
                double lon = minLon + (maxLon - minLon) * x / sampleStep;
                if (isWater(lon, lat)) {
                    hasWater = true;
                } else {
                    hasLand = true;
                }
            }
        }

        if (hasWater && hasLand) return WaterMaskType.MIXED;
        else if (hasWater) return WaterMaskType.ALL_WATER;
        else return WaterMaskType.ALL_LAND;
    }

    // 按从北向南、从西向东排列（规范要求）
    public byte[] generateWaterMaskGrid(double minLon, double minLat, double maxLon, double maxLat) {
        byte[] grid = new byte[256 * 256];
        for (int y = 0; y < 256; y++) {
            for (int x = 0; x < 256; x++) {
                // y=0 是最北边，y=255 是最南边
                double lat = maxLat - ((double) y / 255.0) * (maxLat - minLat);
                // x=0 是最西边，x=255 是最东边
                double lon = minLon + ((double) x / 255.0) * (maxLon - minLon);
                grid[y * 256 + x] = isWater(lon, lat) ? (byte) 255 : (byte) 0;
            }
        }
        return grid;
    }
}
