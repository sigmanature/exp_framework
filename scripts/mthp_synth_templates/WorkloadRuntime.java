package com.zzhao.mthp.synthetic;

import android.app.ActivityManager;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.widget.TextView;
import android.app.Activity;
import android.app.Service;
import android.os.IBinder;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

public final class WorkloadRuntime {
    private static final String TAG = "ZZMthpSynth";
    private static final String EXTRA_PROFILE_JSON = "zz_mthp_profile_json";
    private static final String EXTRA_VMA_COUNT_SCALE = "zz_mthp_vma_count_scale";
    private static final String EXTRA_ANON_VMA_SIZE_SCALE = "zz_mthp_anon_vma_size_scale";
    private static final String EXTRA_COW_PAGES_SCALE = "zz_mthp_cow_pages_scale";
    private static final String EXTRA_FILEMAP_SIZE_SCALE = "zz_mthp_filemap_size_scale";
    private static final String EXTRA_DLOPEN_LIB_COUNT_SCALE = "zz_mthp_dlopen_lib_count_scale";
    private static final AtomicBoolean started = new AtomicBoolean(false);
    private static final List<byte[]> javaKeepAlive = new ArrayList<>();
    private static volatile JSONObject config;

    static {
        System.loadLibrary("mthpwork");
    }

    private WorkloadRuntime() {}

    public static String start(Context context, int processIndex, String processLabel, Intent intent) {
        try {
            if (!started.compareAndSet(false, true)) {
                return nativeStatus();
            }
            String json = runtimeProfileJson(context, intent);
            config = new JSONObject(json);
            startJavaChurn(config, processIndex);
            String result = nativeStart(
                    json,
                    nativeLibrarySearchPath(context),
                    context.getFilesDir().getAbsolutePath(),
                    processIndex,
                    processLabel == null ? "unknown" : processLabel);
            Log.i(TAG, "started processIndex=" + processIndex + " label=" + processLabel + " result=" + result);
            return result;
        } catch (Throwable t) {
            Log.e(TAG, "start failed", t);
            return "ERROR " + t;
        }
    }

    public static void startPeerServices(Context context, Intent sourceIntent) {
        try {
            JSONObject cfg = config;
            if (cfg == null) {
                cfg = new JSONObject(runtimeProfileJson(context, sourceIntent));
                config = cfg;
            }
            int processCount = Math.max(1, Math.min(4, cfg.optInt("process_count", 1)));
            String profileJson = cfg.toString();
            Class<?>[] services = new Class<?>[] {
                    WorkerService1.class, WorkerService2.class, WorkerService3.class
            };
            for (int i = 1; i < processCount; i++) {
                Intent intent = new Intent(context, services[i - 1]);
                intent.putExtra("process_index", i);
                intent.putExtra(EXTRA_PROFILE_JSON, profileJson);
                context.startService(intent);
            }
        } catch (Throwable t) {
            Log.e(TAG, "startPeerServices failed", t);
        }
    }

    private static void startJavaChurn(JSONObject cfg, int processIndex) {
        int javaLiveMb = cfg.optInt("java_live_mb", 0);
        int objectKb = Math.max(4, cfg.optInt("java_object_kb", 64));
        int churnMs = cfg.optInt("java_churn_ms", 1000);
        int gcPeriodMs = Math.max(0, cfg.optInt("gc_period_ms", 0));
        if (processIndex > 0) {
            javaLiveMb = Math.max(0, javaLiveMb / 3);
        }
        long requestedBytes = (long) javaLiveMb * 1024L * 1024L;
        long heapCapBytes = Math.max(16L * 1024L * 1024L, Runtime.getRuntime().maxMemory() * 3L / 4L);
        final int targetBytes = (int) Math.min((long) Integer.MAX_VALUE, Math.min(requestedBytes, heapCapBytes));
        if (requestedBytes > targetBytes) {
            Log.i(TAG, "cap java_live_mb from " + javaLiveMb + " to " + (targetBytes / 1024 / 1024) + " due to app heap limit");
        }
        final int allocBytes = objectKb * 1024;
        final int sleepMs = churnMs;
        final int gcMs = gcPeriodMs;
        Thread t = new Thread(() -> {
            long lastGc = System.currentTimeMillis();
            int salt = 1;
            while (true) {
                synchronized (javaKeepAlive) {
                    try {
                        while (totalBytesLocked() < targetBytes) {
                            byte[] arr = new byte[allocBytes];
                            for (int i = 0; i < arr.length; i += 4096) {
                                arr[i] = (byte) (salt++);
                            }
                            javaKeepAlive.add(arr);
                        }
                    } catch (OutOfMemoryError oom) {
                        Log.w(TAG, "java churn hit heap limit; keeping " + (totalBytesLocked() / 1024 / 1024) + " MiB", oom);
                        return;
                    }
                    if (churnMs <= 0) {
                        Log.i(TAG, "java fill_once process=" + processIndex + " live_bytes=" + totalBytesLocked());
                        return;
                    }
                    int drop = javaKeepAlive.size() / 8;
                    for (int i = 0; i < drop && !javaKeepAlive.isEmpty(); i++) {
                        javaKeepAlive.remove(0);
                    }
                }
                if (gcMs > 0 && System.currentTimeMillis() - lastGc > gcMs) {
                    System.gc();
                    lastGc = System.currentTimeMillis();
                }
                try {
                    Thread.sleep(sleepMs);
                } catch (InterruptedException ignored) {
                }
            }
        }, "mthp-java-churn-" + processIndex);
        t.setDaemon(true);
        t.start();
    }

    private static String nativeLibrarySearchPath(Context context) {
        String[] abis = Build.SUPPORTED_ABIS;
        String abi = abis.length > 0 ? abis[0] : "x86_64";
        return context.getApplicationInfo().sourceDir + "!/lib/" + abi;
    }

    private static int totalBytesLocked() {
        long total = 0;
        for (byte[] arr : javaKeepAlive) {
            total += arr.length;
        }
        return total > Integer.MAX_VALUE ? Integer.MAX_VALUE : (int) total;
    }

    private static String readAsset(Context context, String name) throws Exception {
        try (InputStream in = context.getAssets().open(name);
             ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) >= 0) {
                out.write(buf, 0, n);
            }
            return out.toString(StandardCharsets.UTF_8.name());
        }
    }

    private static String runtimeProfileJson(Context context, Intent intent) throws Exception {
        if (intent != null) {
            String overrideJson = intent.getStringExtra(EXTRA_PROFILE_JSON);
            if (overrideJson != null && !overrideJson.isEmpty()) {
                return overrideJson;
            }
        }
        JSONObject cfg = new JSONObject(readAsset(context, "profile.json"));
        applyRuntimeScales(cfg, intent);
        return cfg.toString();
    }

    private static void applyRuntimeScales(JSONObject cfg, Intent intent) throws Exception {
        float vmaCountScale = runtimeFloatExtra(intent, EXTRA_VMA_COUNT_SCALE, 1.0f);
        float anonScale = runtimeFloatExtra(intent, EXTRA_ANON_VMA_SIZE_SCALE, 1.0f);
        float cowScale = runtimeFloatExtra(intent, EXTRA_COW_PAGES_SCALE, 1.0f);
        float filemapScale = runtimeFloatExtra(intent, EXTRA_FILEMAP_SIZE_SCALE, 1.0f);
        float dlopenScale = runtimeFloatExtra(intent, EXTRA_DLOPEN_LIB_COUNT_SCALE, 1.0f);
        if (vmaCountScale < 0.0f) {
            vmaCountScale = 1.0f;
        }
        if (!(anonScale > 0.0f)) {
            anonScale = 1.0f;
        }
        if (cowScale < 0.0f) {
            cowScale = 1.0f;
        }
        if (!(filemapScale > 0.0f)) {
            filemapScale = 1.0f;
        }
        if (dlopenScale < 0.0f) {
            dlopenScale = 1.0f;
        }

        boolean changed = false;
        if (vmaCountScale != 1.0f) {
            int oldVmaCount = Math.max(0, cfg.optInt("vma_count", 0));
            int scaledVmaCount = scaledCount(oldVmaCount, vmaCountScale);
            cfg.put("vma_count_unscaled_runtime", oldVmaCount);
            cfg.put("runtime_vma_count_scale", vmaCountScale);
            cfg.put("vma_count", scaledVmaCount);
            refreshAnonResidentFields(cfg);
            changed = true;
        }

        if (anonScale != 1.0f) {
            int oldPagesPerVma = Math.max(1, cfg.optInt("touch_pages_per_vma", Math.max(1, cfg.optInt("vma_size_kb", 64) / 4)));
            int scaledPages = (int) (oldPagesPerVma * anonScale);
            scaledPages = Math.max(4, (scaledPages / 4) * 4);
            cfg.put("touch_pages_per_vma_unscaled_runtime", oldPagesPerVma);
            cfg.put("vma_size_kb_unscaled_runtime", oldPagesPerVma * 4);
            cfg.put("touch_pages_per_vma", scaledPages);
            cfg.put("vma_size_kb", scaledPages * 4);
            cfg.put("runtime_anon_vma_size_scale", anonScale);
            refreshAnonResidentFields(cfg);
            changed = true;
        }

        if (cowScale != 1.0f) {
            int oldCowPages = Math.max(0, cfg.optInt("cow_pages_per_child", 0));
            int scaledCowPages = Math.max(0, Math.round(oldCowPages * cowScale));
            int residentPages = Math.max(0, cfg.optInt("vma_count", 0)) * Math.max(1, cfg.optInt("touch_pages_per_vma", 1));
            cfg.put("cow_pages_per_child_unscaled_runtime", oldCowPages);
            cfg.put("runtime_cow_pages_scale", cowScale);
            if (scaledCowPages > residentPages) {
                cfg.put("cow_pages_per_child_capped_from_runtime", scaledCowPages);
                scaledCowPages = residentPages;
            }
            cfg.put("cow_pages_per_child", scaledCowPages);
            cfg.put("cow_total_mb", Math.max(0, cfg.optInt("fork_children", 0)) * scaledCowPages * 4 / 1024);
            changed = true;
        }

        if (filemapScale != 1.0f) {
            int oldFilemapMb = Math.max(0, cfg.optInt("filemap_file_mb", 0));
            int scaledFilemapMb = oldFilemapMb == 0 ? 0 : Math.max(1, Math.round(oldFilemapMb * filemapScale));
            cfg.put("filemap_file_mb_unscaled_runtime", oldFilemapMb);
            cfg.put("runtime_filemap_size_scale", filemapScale);
            cfg.put("filemap_file_mb", scaledFilemapMb);
            changed = true;
        }

        if (dlopenScale != 1.0f) {
            int oldDlopenCount = Math.max(0, cfg.optInt("dlopen_lib_count", 0));
            int scaledDlopenCount = scaledCount(oldDlopenCount, dlopenScale);
            cfg.put("dlopen_lib_count_unscaled_runtime", oldDlopenCount);
            cfg.put("runtime_dlopen_lib_count_scale", dlopenScale);
            cfg.put("dlopen_lib_count", scaledDlopenCount);
            changed = true;
        }

        if (changed) {
            Log.i(TAG, "runtime_scales vma_count_scale=" + vmaCountScale
                    + " anon_vma_size_scale=" + anonScale
                    + " cow_pages_scale=" + cowScale
                    + " filemap_size_scale=" + filemapScale
                    + " dlopen_lib_count_scale=" + dlopenScale
                    + " vma_count=" + cfg.optInt("vma_count", 0)
                    + " vma_size_kb=" + cfg.optInt("vma_size_kb", 0)
                    + " anon_full_fault_pages=" + cfg.optInt("anon_full_fault_pages", 0)
                    + " dlopen_lib_count=" + cfg.optInt("dlopen_lib_count", 0)
                    + " cow_pages_per_child=" + cfg.optInt("cow_pages_per_child", 0)
                    + " filemap_file_mb=" + cfg.optInt("filemap_file_mb", 0));
        }
    }

    private static int scaledCount(int oldCount, float scale) {
        if (oldCount <= 0 || scale == 0.0f) {
            return 0;
        }
        return Math.max(1, Math.round(oldCount * scale));
    }

    private static void refreshAnonResidentFields(JSONObject cfg) throws Exception {
        int vmaCount = Math.max(0, cfg.optInt("vma_count", 0));
        int pagesPerVma = Math.max(1, cfg.optInt("touch_pages_per_vma", Math.max(1, cfg.optInt("vma_size_kb", 64) / 4)));
        int anonPages = vmaCount * pagesPerVma;
        cfg.put("anon_full_fault_pages", anonPages);
        cfg.put("anon_full_fault_mb", anonPages * 4 / 1024);
        cfg.put("parent_touch_pages", anonPages);
        cfg.put("parent_touch_mb", anonPages * 4 / 1024);
    }

    private static float runtimeFloatExtra(Intent intent, String key, float defaultValue) {
        if (intent == null || !intent.hasExtra(key)) {
            return defaultValue;
        }
        Bundle extras = intent.getExtras();
        if (extras == null) {
            return defaultValue;
        }
        Object value = extras.get(key);
        if (value instanceof Number) {
            return ((Number) value).floatValue();
        }
        if (value instanceof String) {
            try {
                return Float.parseFloat((String) value);
            } catch (NumberFormatException ignored) {
                return defaultValue;
            }
        }
        return defaultValue;
    }

    private static native String nativeStart(String json, String nativeLibraryDir, String filesDir, int processIndex, String processLabel);
    private static native String nativeStatus();

    public static class MainActivity extends Activity {
        @Override
        protected void onCreate(Bundle state) {
            super.onCreate(state);
            TextView tv = new TextView(this);
            tv.setTextSize(14);
            tv.setText("MTHP synthetic workload starting...\n" + getPackageName());
            setContentView(tv);
            new Thread(() -> {
                String result = WorkloadRuntime.start(this, 0, getProcessNameCompat(this), getIntent());
                WorkloadRuntime.startPeerServices(this, getIntent());
                new Handler(Looper.getMainLooper()).post(() -> tv.setText(result + "\n" + nativeStatus()));
            }, "mthp-main-start").start();
        }
    }

    public static class BaseWorkerService extends Service {
        protected int serviceIndex() { return 1; }

        @Override
        public void onCreate() {
            super.onCreate();
        }

        @Override
        public int onStartCommand(Intent intent, int flags, int startId) {
            WorkloadRuntime.start(this, intent == null ? serviceIndex() : intent.getIntExtra("process_index", serviceIndex()), getProcessNameCompat(this), intent);
            return START_STICKY;
        }

        @Override
        public IBinder onBind(Intent intent) { return null; }
    }

    public static class WorkerService1 extends BaseWorkerService { protected int serviceIndex() { return 1; } }
    public static class WorkerService2 extends BaseWorkerService { protected int serviceIndex() { return 2; } }
    public static class WorkerService3 extends BaseWorkerService { protected int serviceIndex() { return 3; } }

    private static String getProcessNameCompat(Context context) {
        int pid = android.os.Process.myPid();
        ActivityManager am = (ActivityManager) context.getSystemService(Context.ACTIVITY_SERVICE);
        if (am != null) {
            List<ActivityManager.RunningAppProcessInfo> processes = am.getRunningAppProcesses();
            if (processes != null) {
                for (ActivityManager.RunningAppProcessInfo info : processes) {
                    if (info.pid == pid) {
                        return info.processName;
                    }
                }
            }
        }
        return context.getPackageName();
    }
}
