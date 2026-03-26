package com.gaia3d.terrain.tile;

import com.gaia3d.terrain.tile.geotiff.GaiaGeoTiffManager;
import com.gaia3d.terrain.types.WaterMaskType;
import lombok.Getter;
import lombok.Setter;
import lombok.extern.slf4j.Slf4j;
import org.geotools.geometry.jts.ReferencedEnvelope;

import java.awt.image.Raster;
import java.io.File;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * WaterMaskManager - 水面掩码管理器
 *
 * 支持两种模式：
 * 1. 单文件模式：直接加载单个WBM文件
 * 2. 目录模式：扫描目录下所有WBM文件，按瓦片地理范围动态加载
 *
 * 性能优化：
 * - LRU缓存已加载的WBM数据（默认缓存8个文件）
 * - 预加载瓦片涉及的所有WBM文件
 * - 按像素采样时从已缓存数据中查询
 */
@Slf4j
@Getter
@Setter
public class WaterMaskManager {
    // 单一文件模式
    private GaiaGeoTiffManager geoTiffManager = new GaiaGeoTiffManager();
    private ReferencedEnvelope envelope;
    private double minLon;
    private double minLat;
    private double maxLon;
    private double maxLat;
    private double lonRange;
    private double latRange;
    private boolean isLoaded = false;

    // 预加载的栅格数据（单文件模式）
    private int[] waterMaskData;
    private int maskWidth;
    private int maskHeight;

    // 目录模式：WBM文件映射
    private boolean isDirectoryMode = false;
    private Map<String, WaterMaskFileInfo> waterMaskFileMap = new HashMap<>();

    // LRU缓存：瓦片key -> WBM数据
    private static final int MAX_CACHED_WBM_FILES = 8;
    private LinkedHashMap<String, WbmData> wbmCache = new LinkedHashMap<>(16, 0.75f, true) {
        @Override
        protected boolean removeEldestEntry(Map.Entry<String, WbmData> eldest) {
            return size() > MAX_CACHED_WBM_FILES;
        }
    };

    // 当前瓦片预加载的WBM数据列表
    private List<WbmData> currentTileWbmDataList = new ArrayList<>();

    // WBM文件信息
    private static class WaterMaskFileInfo {
        String filePath;
        int minLat;   // 纬度度数（整数）
        int minLon;   // 经度度数（整数）
        int maxLat;
        int maxLon;
        // 地理范围（使用文件名解析，瓦片命名是1度对齐的）
        double wbmMinLon;
        double wbmMinLat;
        double wbmMaxLon;
        double wbmMaxLat;

        WaterMaskFileInfo(String filePath, int minLat, int minLon) {
            this.filePath = filePath;
            this.minLat = minLat;
            this.minLon = minLon;
            this.maxLat = minLat + 1;  // 1度瓦片
            this.maxLon = minLon + 1;
            // 文件名表示的瓦片范围（闭合下界，开放上界）
            this.wbmMinLon = minLon;
            this.wbmMaxLon = maxLon;
            this.wbmMinLat = minLat;
            this.wbmMaxLat = maxLat;
        }

        boolean contains(double lon, double lat) {
            return lon >= wbmMinLon && lon < wbmMaxLon && lat >= wbmMinLat && lat < wbmMaxLat;
        }

        boolean intersects(double tileMinLon, double tileMinLat, double tileMaxLon, double tileMaxLat) {
            return !(tileMaxLon <= wbmMinLon || tileMinLon >= wbmMaxLon ||
                     tileMaxLat <= wbmMinLat || tileMinLat >= wbmMaxLat);
        }
    }

    // WBM数据（包含地理范围和栅格数据）
    private static class WbmData {
        String filePath;
        int minLat;   // 来自文件名
        int minLon;   // 来自文件名
        int maxLat;
        int maxLon;
        double lonRange;
        double latRange;
        int[] data;
        int width;
        int height;

        // 地理范围（闭合下界，开放上界）
        double wbmMinLon;
        double wbmMaxLon;
        double wbmMinLat;
        double wbmMaxLat;

        boolean contains(double lon, double lat) {
            return lon >= wbmMinLon && lon < wbmMaxLon && lat >= wbmMinLat && lat < wbmMaxLat;
        }

        boolean isWater(double lon, double lat) {
            if (!contains(lon, lat)) {
                return false;
            }
            // 计算归一化坐标 (0-1)
            double unitaryX = (lon - wbmMinLon) / lonRange;
            double unitaryY = 1.0 - (lat - wbmMinLat) / latRange;
            // 计算像素坐标
            int col = (int) Math.floor(unitaryX * width);
            int row = (int) Math.floor(unitaryY * height);
            // 确保在有效范围内
            col = Math.max(0, Math.min(col, width - 1));
            row = Math.max(0, Math.min(row, height - 1));
            // 获取值
            int index = row * width + col;
            return data[index] > 0;
        }
    }

    // 文件名解析模式: Copernicus_DSM_COG_10_N26_00_E115_00_WBM.tif
    private static final Pattern WBM_FILE_PATTERN = Pattern.compile(
            ".*_N(\\d+)_00_E(\\d+)_00_WBM\\.tif$",
            Pattern.CASE_INSENSITIVE);

    public void loadWaterMask(String filePath) {
        if (filePath == null || filePath.isEmpty()) {
            log.warn("Water mask file path is not provided.");
            return;
        }

        File file = new File(filePath);
        if (!file.exists()) {
            log.warn("Water mask file does not exist: {}", filePath);
            return;
        }

        if (file.isDirectory()) {
            loadWaterMaskDirectory(filePath);
        } else {
            loadWaterMaskFile(filePath);
        }
    }

    private void loadWaterMaskDirectory(String dirPath) {
        File dir = new File(dirPath);
        File[] files = dir.listFiles((d, name) -> name.toLowerCase().endsWith(".tif"));

        if (files == null || files.length == 0) {
            log.warn("No .tif files found in water mask directory: {}", dirPath);
            return;
        }

        this.isDirectoryMode = true;
        this.waterMaskFileMap.clear();
        this.wbmCache.clear();

        for (File file : files) {
            WaterMaskFileInfo info = parseWbmFileName(file.getAbsolutePath());
            if (info != null) {
                String key = info.minLat + "_" + info.minLon;
                this.waterMaskFileMap.put(key, info);
                log.debug("Registered WBM file: {} -> lat[{},{}] lon[{},{}]",
                    info.filePath, info.minLat, info.maxLat, info.minLon, info.maxLon);
            }
        }

        log.info("Loaded {} WBM files from directory: {}", this.waterMaskFileMap.size(), dirPath);
        this.isLoaded = true;
    }

    private WaterMaskFileInfo parseWbmFileName(String filePath) {
        Matcher matcher = WBM_FILE_PATTERN.matcher(filePath);
        if (matcher.matches()) {
            int lat = Integer.parseInt(matcher.group(1));
            int lon = Integer.parseInt(matcher.group(2));
            return new WaterMaskFileInfo(filePath, lat, lon);
        }
        log.warn("Failed to parse WBM filename: {}", filePath);
        return null;
    }

    private void loadWaterMaskFile(String filePath) {
        this.isDirectoryMode = false;

        // 先检查缓存
        WbmData cached = wbmCache.get(filePath);
        if (cached != null) {
            this.waterMaskData = cached.data;
            this.maskWidth = cached.width;
            this.maskHeight = cached.height;
            this.minLon = cached.wbmMinLon;
            this.minLat = cached.wbmMinLat;
            this.maxLon = cached.wbmMaxLon;
            this.maxLat = cached.wbmMaxLat;
            this.lonRange = cached.lonRange;
            this.latRange = cached.latRange;
            this.isLoaded = true;
            log.debug("Using cached WBM data: {}", filePath);
            return;
        }

        // 从文件名解析坐标
        WaterMaskFileInfo info = parseWbmFileName(filePath);
        if (info == null) {
            log.warn("Cannot parse WBM filename for single file mode: {}", filePath);
            return;
        }

        // 加载新文件
        org.geotools.coverage.grid.GridCoverage2D coverage = geoTiffManager.loadGeoTiffGridCoverage2D(filePath);
        org.geotools.geometry.jts.ReferencedEnvelope env = coverage.getEnvelope2D();

        org.joml.Vector2i size = geoTiffManager.getGridCoverage2DSize(filePath);

        this.minLon = info.wbmMinLon;
        this.maxLon = info.wbmMaxLon;
        this.minLat = info.wbmMinLat;
        this.maxLat = info.wbmMaxLat;
        this.lonRange = env.getMaxX() - env.getMinX();
        this.latRange = env.getMaxY() - env.getMinY();
        this.maskWidth = size.x;
        this.maskHeight = size.y;

        // 预加载栅格数据到内存
        Raster raster = coverage.getRenderedImage().getData();
        this.waterMaskData = raster.getPixels(0, 0, maskWidth, maskHeight, (int[]) null);

        // 加入缓存
        WbmData wbmData = new WbmData();
        wbmData.filePath = filePath;
        wbmData.minLon = info.minLon;
        wbmData.minLat = info.minLat;
        wbmData.maxLon = info.maxLon;
        wbmData.maxLat = info.maxLat;
        wbmData.wbmMinLon = info.wbmMinLon;
        wbmData.wbmMaxLon = info.wbmMaxLon;
        wbmData.wbmMinLat = info.wbmMinLat;
        wbmData.wbmMaxLat = info.wbmMaxLat;
        wbmData.lonRange = this.lonRange;
        wbmData.latRange = this.latRange;
        wbmData.data = this.waterMaskData;
        wbmData.width = this.maskWidth;
        wbmData.height = this.maskHeight;
        wbmCache.put(filePath, wbmData);

        this.isLoaded = true;
        log.info("Water mask loaded from: {} ({}x{} pixels)", filePath, maskWidth, maskHeight);
    }

    /**
     * 预加载瓦片涉及的所有WBM文件到currentTileWbmDataList
     */
    public void loadWaterMaskForTile(double tileMinLon, double tileMinLat, double tileMaxLon, double tileMaxLat) {
        if (!this.isDirectoryMode) {
            return;
        }

        // 清空当前列表
        currentTileWbmDataList.clear();

        // 查找所有与瓦片范围相交的WBM文件
        for (WaterMaskFileInfo info : this.waterMaskFileMap.values()) {
            if (info.intersects(tileMinLon, tileMinLat, tileMaxLon, tileMaxLat)) {
                // 加载或从缓存获取WBM数据
                WbmData wbmData = loadOrGetFromCache(info.filePath);
                if (wbmData != null) {
                    currentTileWbmDataList.add(wbmData);
                }
            }
        }

        log.debug("Preloaded {} WBM files for tile (lat[{},{}], lon[{},{}])",
            currentTileWbmDataList.size(), tileMinLat, tileMaxLat, tileMinLon, tileMaxLon);
    }

    private WbmData loadOrGetFromCache(String filePath) {
        // 先检查缓存
        WbmData cached = wbmCache.get(filePath);
        if (cached != null) {
            return cached;
        }

        // 从文件名解析坐标
        WaterMaskFileInfo info = parseWbmFileName(filePath);
        if (info == null) {
            return null;
        }

        // 加载新文件
        org.geotools.coverage.grid.GridCoverage2D coverage = geoTiffManager.loadGeoTiffGridCoverage2D(filePath);
        org.geotools.geometry.jts.ReferencedEnvelope env = coverage.getEnvelope2D();

        org.joml.Vector2i size = geoTiffManager.getGridCoverage2DSize(filePath);
        Raster raster = coverage.getRenderedImage().getData();
        int[] data = raster.getPixels(0, 0, size.x, size.y, (int[]) null);

        WbmData wbmData = new WbmData();
        wbmData.filePath = filePath;
        wbmData.minLon = info.minLon;
        wbmData.minLat = info.minLat;
        wbmData.maxLon = info.maxLon;
        wbmData.maxLat = info.maxLat;
        // 使用文件名解析的坐标（1度瓦片对齐）
        wbmData.wbmMinLon = info.wbmMinLon;
        wbmData.wbmMaxLon = info.wbmMaxLon;
        wbmData.wbmMinLat = info.wbmMinLat;
        wbmData.wbmMaxLat = info.wbmMaxLat;
        // 使用GeoTIFF envelope计算像素范围比例
        wbmData.lonRange = env.getMaxX() - env.getMinX();
        wbmData.latRange = env.getMaxY() - env.getMinY();
        wbmData.data = data;
        wbmData.width = size.x;
        wbmData.height = size.y;

        wbmCache.put(filePath, wbmData);
        log.debug("Loaded WBM file: {} ({}x{}) bounds=[{},{}]x[{},{}]",
            filePath, size.x, size.y, wbmData.wbmMinLon, wbmData.wbmMaxLon, wbmData.wbmMinLat, wbmData.wbmMaxLat);

        return wbmData;
    }

    public boolean isWater(double lonDeg, double latDeg) {
        if (!isLoaded) {
            return false;
        }

        if (this.isDirectoryMode) {
            // 目录模式：从当前瓦片的WBM数据列表中查找
            for (WbmData wbmData : currentTileWbmDataList) {
                if (wbmData.contains(lonDeg, latDeg)) {
                    return wbmData.isWater(lonDeg, latDeg);
                }
            }
            return false;
        } else {
            // 单文件模式：使用已加载的数据
            if (waterMaskData == null) {
                return false;
            }

            try {
                if (lonDeg < minLon || lonDeg > maxLon || latDeg < minLat || latDeg > maxLat) {
                    return false;
                }
                double unitaryX = (lonDeg - minLon) / lonRange;
                double unitaryY = 1.0 - (latDeg - minLat) / latRange;
                int col = (int) Math.floor(unitaryX * maskWidth);
                int row = (int) Math.floor(unitaryY * maskHeight);
                col = Math.max(0, Math.min(col, maskWidth - 1));
                row = Math.max(0, Math.min(row, maskHeight - 1));
                int index = row * maskWidth + col;
                return waterMaskData[index] > 0;
            } catch (Exception e) {
                log.debug("Error evaluating water mask at ({}, {}): {}", lonDeg, latDeg, e.getMessage());
            }
            return false;
        }
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

    /**
     * 生成256×256水面掩码网格
     * 按从北向南、从西向东排列（Cesium规范要求）
     */
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
