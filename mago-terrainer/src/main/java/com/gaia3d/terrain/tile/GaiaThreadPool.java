package com.gaia3d.terrain.tile;

import com.gaia3d.command.GlobalOptions;
import lombok.Getter;
import lombok.extern.slf4j.Slf4j;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.*;

/**
 * Reusable thread pool for parallel terrain processing.
 * Thread count is configurable via --threads CLI option.
 */
@Getter
@Slf4j
public class GaiaThreadPool {
    private static GaiaThreadPool instance;

    private final ExecutorService executorService;
    private final int threadCount;

    public GaiaThreadPool() {
        this.threadCount = GlobalOptions.getInstance().getThreadCount();
        this.executorService = Executors.newFixedThreadPool(threadCount);
        log.info("Initialized thread pool with {} threads", threadCount);
    }

    public static synchronized GaiaThreadPool getInstance() {
        if (GaiaThreadPool.instance == null) {
            GaiaThreadPool.instance = new GaiaThreadPool();
        }
        return GaiaThreadPool.instance;
    }

    /**
     * Submits all tasks and waits for completion.
     * Does NOT shut down the pool — can be called multiple times.
     */
    public <T> List<Future<T>> submitAllAndWait(List<Callable<T>> tasks) {
        List<Future<T>> futures = new ArrayList<>(tasks.size());
        try {
            for (Callable<T> task : tasks) {
                futures.add(executorService.submit(task));
            }
            for (Future<T> future : futures) {
                future.get();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new RuntimeException("Thread pool interrupted", e);
        } catch (ExecutionException e) {
            log.error("Task execution failed", e.getCause());
            throw new RuntimeException(e.getCause());
        }
        return futures;
    }

    /**
     * Submits all Runnable tasks and waits for completion.
     */
    public void submitRunnablesAndWait(List<Runnable> tasks) {
        List<Callable<Void>> callableTasks = new ArrayList<>(tasks.size());
        for (Runnable task : tasks) {
            callableTasks.add(() -> {
                task.run();
                return null;
            });
        }
        submitAllAndWait(callableTasks);
    }

    /**
     * Shuts down the thread pool. Call when all processing is complete.
     */
    public void shutdown() {
        executorService.shutdown();
        try {
            if (!executorService.awaitTermination(60, TimeUnit.SECONDS)) {
                executorService.shutdownNow();
            }
        } catch (InterruptedException e) {
            executorService.shutdownNow();
            Thread.currentThread().interrupt();
        }
        log.info("Thread pool shut down");
    }

    /**
     * Returns the thread count configured for this pool.
     */
    public int getThreadCount() {
        return threadCount;
    }

    /**
     * Resets the singleton instance. For testing purposes.
     */
    public static synchronized void reset() {
        if (instance != null) {
            instance.shutdown();
            instance = null;
        }
    }
}
