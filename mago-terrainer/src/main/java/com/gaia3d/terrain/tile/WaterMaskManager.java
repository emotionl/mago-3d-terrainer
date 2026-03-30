package com.gaia3d.terrain.tile;

import com.gaia3d.command.GlobalOptions;
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
 * WaterMaskManager - Water mask manager for terrain tiles.
 *
 * Supports two modes:
 * 1. Single-file mode: loads a single WBM file directly.
 * 2. Directory mode: scans all WBM files in a directory, dynamically loading
 *    based on tile geographic extent.
 *
 * Performance optimizations:
 * - LRU cache for loaded WBM data (default 8 files).
 * - Preload all WBM files relevant to the current tile.
 * - Pixel sampling queries from cached data.
 */
@Slf4j
@Getter
@Setter
public class WaterMaskManager {
    // Single-file mode
    private GaiaGeoTiffManager geoTiffManager = new GaiaGeoTiffManager();
    private ReferencedEnvelope envelope;
    private double minLon;
    private double minLat;
    private double maxLon;
    private double maxLat;
    private double lonRange;
    private double latRange;
    private boolean isLoaded = false;

    // Preloaded raster data (single-file mode)
    private byte[] waterMaskData;
    private int maskWidth;
    private int maskHeight;

    // Directory mode: WBM file map
    private boolean isDirectoryMode = false;
    private Map<String, WaterMaskFileInfo> waterMaskFileMap = new HashMap<>();

    // LRU cache: tile key -> WBM data
    private final int maxCachedWbmFiles;
    private LinkedHashMap<String, WbmData> wbmCache;

    public WaterMaskManager() {
        this.maxCachedWbmFiles = GlobalOptions.getInstance().getWbmCacheSize();
        this.wbmCache = new LinkedHashMap<>(16, 0.75f, true) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, WbmData> eldest) {
                return size() > maxCachedWbmFiles;
            }
        };
    }

    // Currently preloaded WBM data list for the tile (ThreadLocal for thread safety)
    private final ThreadLocal<List<WbmData>> currentTileWbmDataList = ThreadLocal.withInitial(ArrayList::new);

    // WBM file information
    private static class WaterMaskFileInfo {
        String filePath;
        int minLat;   // Latitude in degrees (integer)
        int minLon;   // Longitude in degrees (integer)
        int maxLat;
        int maxLon;
        // Geographic extent (parsed from filename, tile naming aligned to 1 degree)
        double wbmMinLon;
        double wbmMinLat;
        double wbmMaxLon;
        double wbmMaxLat;

        WaterMaskFileInfo(String filePath, int minLat, int minLon) {
            this.filePath = filePath;
            this.minLat = minLat;
            this.minLon = minLon;
            this.maxLat = minLat + 1;  // 1-degree tile
            this.maxLon = minLon + 1;
            // Tile range represented by filename (inclusive min, exclusive max)
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

    // WBM data (contains geographic extent and raster data)
    private static class WbmData {
        String filePath;
        int minLat;   // From filename
        int minLon;   // From filename
        int maxLat;
        int maxLon;
        double lonRange;
        double latRange;
        byte[] data;
        int width;
        int height;

        // Geographic extent (inclusive min, exclusive max)
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
            // Calculate normalized coordinates (0-1)
            double unitaryX = (lon - wbmMinLon) / lonRange;
            double unitaryY = 1.0 - (lat - wbmMinLat) / latRange;
            // Calculate pixel coordinates
            int col = (int) Math.floor(unitaryX * width);
            int row = (int) Math.floor(unitaryY * height);
            // Clamp to valid range
            col = Math.max(0, Math.min(col, width - 1));
            row = Math.max(0, Math.min(row, height - 1));
            // Get value
            int index = row * width + col;
            return data[index] != 0;
        }
    }

    // Filename parse pattern: Copernicus_DSM_COG_10_N26_00_E115_00_WBM.tif
    private static final Pattern WBM_FILE_PATTERN = Pattern.compile(
            ".*_N(\\d+)_00_E(\\d+)_00_WBM\\.tif$",
            Pattern.CASE_INSENSITIVE);

    /**
     * Extracts raster data as a compact byte[] (1 byte per pixel).
     * WBM data is binary (water/land), so int[] (4 bytes/pixel) is wasteful.
     */
    private static byte[] extractByteData(Raster raster, int width, int height) {
        int[] intData = raster.getPixels(0, 0, width, height, (int[]) null);
        byte[] byteData = new byte[intData.length];
        for (int i = 0; i < intData.length; i++) {
            byteData[i] = intData[i] > 0 ? (byte) 1 : (byte) 0;
        }
        return byteData;
    }

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

        // Check cache first
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

        // Parse coordinates from filename
        WaterMaskFileInfo info = parseWbmFileName(filePath);
        if (info == null) {
            log.warn("Cannot parse WBM filename for single file mode: {}", filePath);
            return;
        }

        // Load new file
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

        // Preload raster data into memory
        Raster raster = coverage.getRenderedImage().getData();
        this.waterMaskData = extractByteData(raster, maskWidth, maskHeight);

        // Dispose coverage after extracting pixel data to avoid holding duplicate copies
        geoTiffManager.disposeCoverage(filePath);

        // Add to cache
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
     * Preloads all WBM files involved in the tile and returns the list.
     * Thread-safe: returns a new list per call instead of mutating shared state.
     */
    public List<WbmData> loadWaterMaskForTile(double tileMinLon, double tileMinLat, double tileMaxLon, double tileMaxLat) {
        if (!this.isDirectoryMode) {
            return Collections.emptyList();
        }

        List<WbmData> wbmDataList = new ArrayList<>();

        // Find all WBM files intersecting with tile extent
        for (WaterMaskFileInfo info : this.waterMaskFileMap.values()) {
            if (info.intersects(tileMinLon, tileMinLat, tileMaxLon, tileMaxLat)) {
                // Load or get WBM data from cache
                WbmData wbmData = loadOrGetFromCache(info.filePath);
                if (wbmData != null) {
                    wbmDataList.add(wbmData);
                }
            }
        }

        // Also update the thread-local for backward compatibility
        this.currentTileWbmDataList.set(wbmDataList);

        log.debug("Preloaded {} WBM files for tile (lat[{},{}], lon[{},{}])",
            wbmDataList.size(), tileMinLat, tileMaxLat, tileMinLon, tileMaxLon);
        return wbmDataList;
    }

    private synchronized WbmData loadOrGetFromCache(String filePath) {
        // Check cache first
        WbmData cached = wbmCache.get(filePath);
        if (cached != null) {
            return cached;
        }

        // Parse coordinates from filename
        WaterMaskFileInfo info = parseWbmFileName(filePath);
        if (info == null) {
            return null;
        }

        // Load new file
        org.geotools.coverage.grid.GridCoverage2D coverage = geoTiffManager.loadGeoTiffGridCoverage2D(filePath);
        org.geotools.geometry.jts.ReferencedEnvelope env = coverage.getEnvelope2D();

        org.joml.Vector2i size = geoTiffManager.getGridCoverage2DSize(filePath);
        Raster raster = coverage.getRenderedImage().getData();
        byte[] data = extractByteData(raster, size.x, size.y);

        // Dispose coverage after extracting pixel data to avoid holding duplicate copies
        geoTiffManager.disposeCoverage(filePath);

        WbmData wbmData = new WbmData();
        wbmData.filePath = filePath;
        wbmData.minLon = info.minLon;
        wbmData.minLat = info.minLat;
        wbmData.maxLon = info.maxLon;
        wbmData.maxLat = info.maxLat;
        // Use coordinates parsed from filename (1-degree tile alignment)
        wbmData.wbmMinLon = info.wbmMinLon;
        wbmData.wbmMaxLon = info.wbmMaxLon;
        wbmData.wbmMinLat = info.wbmMinLat;
        wbmData.wbmMaxLat = info.wbmMaxLat;
        // Use GeoTIFF envelope to calculate pixel range ratio
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
            // Directory mode: search from current tile's WBM data list
            for (WbmData wbmData : currentTileWbmDataList.get()) {
                if (wbmData.contains(lonDeg, latDeg)) {
                    return wbmData.isWater(lonDeg, latDeg);
                }
            }
            return false;
        } else {
            // Single-file mode: use loaded data
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
                return waterMaskData[index] != 0;
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

        // Sampling check (step 64, approximately 400 sample points)
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
     * Thread-safe version: checks if a point is water using the given WBM data list.
     */
    public boolean isWater(double lonDeg, double latDeg, List<WbmData> wbmDataList) {
        if (!isLoaded) {
            return false;
        }

        if (this.isDirectoryMode) {
            for (WbmData wbmData : wbmDataList) {
                if (wbmData.contains(lonDeg, latDeg)) {
                    return wbmData.isWater(lonDeg, latDeg);
                }
            }
            return false;
        } else {
            return isWater(lonDeg, latDeg);
        }
    }

    /**
     * Thread-safe version: determines water mask type using the given WBM data list.
     */
    public WaterMaskType getWaterMaskType(double minLon, double minLat, double maxLon, double maxLat, List<WbmData> wbmDataList) {
        if (!isLoaded) {
            return WaterMaskType.NONE;
        }

        boolean hasLand = false;
        boolean hasWater = false;

        int sampleStep = 64;
        for (int y = 0; y <= sampleStep && (!hasLand || !hasWater); y++) {
            for (int x = 0; x <= sampleStep && (!hasLand || !hasWater); x++) {
                double lat = minLat + (maxLat - minLat) * y / sampleStep;
                double lon = minLon + (maxLon - minLon) * x / sampleStep;
                if (isWater(lon, lat, wbmDataList)) {
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
     * Thread-safe version: generates a 256x256 water mask grid using the given WBM data list.
     */
    public byte[] generateWaterMaskGrid(double minLon, double minLat, double maxLon, double maxLat, List<WbmData> wbmDataList) {
        byte[] grid = new byte[256 * 256];
        for (int y = 0; y < 256; y++) {
            for (int x = 0; x < 256; x++) {
                double lat = maxLat - ((double) y / 255.0) * (maxLat - minLat);
                double lon = minLon + ((double) x / 255.0) * (maxLon - minLon);
                grid[y * 256 + x] = isWater(lon, lat, wbmDataList) ? (byte) 255 : (byte) 0;
            }
        }
        return grid;
    }

    /**
     * Generates a 256x256 water mask grid.
     * Ordered from north to south, west to east (Cesium spec requirement).
     */
    public byte[] generateWaterMaskGrid(double minLon, double minLat, double maxLon, double maxLat) {
        byte[] grid = new byte[256 * 256];
        for (int y = 0; y < 256; y++) {
            for (int x = 0; x < 256; x++) {
                // y=0 is northmost, y=255 is southmost
                double lat = maxLat - ((double) y / 255.0) * (maxLat - minLat);
                // x=0 is westmost, x=255 is eastmost
                double lon = minLon + ((double) x / 255.0) * (maxLon - minLon);
                grid[y * 256 + x] = isWater(lon, lat) ? (byte) 255 : (byte) 0;
            }
        }
        return grid;
    }

    /**
     * Clears all cached WBM data and releases resources.
     */
    public void clear() {
        wbmCache.clear();
        currentTileWbmDataList.get().clear();
        waterMaskData = null;
        geoTiffManager.clear();
        isLoaded = false;
    }
}
