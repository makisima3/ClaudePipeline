package space.agentpipeline.android;

import android.Manifest;
import android.app.*;
import android.app.job.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import org.json.*;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.function.BooleanSupplier;

/** Системная фоновая сверка. Курсор относится к установке сервера, а не к выбранному проекту. */
public class ResultJob extends JobService {
    static final int JOB_ID = 2101;
    static final String CHANNEL = "task_results";
    static final Object LOCK = new Object();
    volatile boolean stopped;

    static SharedPreferences prefs(Context c) { return c.getSharedPreferences("connection", MODE_PRIVATE); }

    static JSONObject get(String base, String path) throws Exception {
        HttpURLConnection conn = (HttpURLConnection) new URL(base + path).openConnection();
        conn.setConnectTimeout(12000); conn.setReadTimeout(15000); conn.setInstanceFollowRedirects(false);
        conn.setRequestProperty("Connection", "close");
        try {
            if (conn.getResponseCode() != 200) throw new IOException("Сервер ответил ошибкой");
            try (InputStream in = conn.getInputStream(); ByteArrayOutputStream out = new ByteArrayOutputStream()) {
                byte[] buf = new byte[8192]; int n;
                while ((n = in.read(buf)) != -1) { out.write(buf, 0, n); if (out.size() > 8 * 1024 * 1024) throw new IOException("Слишком большой ответ"); }
                return new JSONObject(out.toString(StandardCharsets.UTF_8.name()));
            }
        } finally { conn.disconnect(); }
    }

    static void channel(Context c) {
        NotificationChannel channel = new NotificationChannel(CHANNEL, "Завершённые задачи", NotificationManager.IMPORTANCE_DEFAULT);
        channel.setDescription("Новые результаты всех проектов подключённого сервера");
        c.getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    static boolean allowed(Context c) {
        return (Build.VERSION.SDK_INT < 33 || c.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED)
            && c.getSystemService(NotificationManager.class).areNotificationsEnabled()
            && c.getSystemService(NotificationManager.class).getNotificationChannel(CHANNEL).getImportance() != NotificationManager.IMPORTANCE_NONE;
    }

    static void schedule(Context c) {
        channel(c);
        JobScheduler jobs = c.getSystemService(JobScheduler.class);
        if (!prefs(c).getBoolean("notifications", false) || prefs(c).getString("server", "").isEmpty()) { jobs.cancel(JOB_ID); return; }
        if (jobs.getPendingJob(JOB_ID) == null) {
            int status = jobs.schedule(new JobInfo.Builder(JOB_ID, new ComponentName(c, ResultJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY).setPeriodic(15 * 60 * 1000L)
                .setPersisted(true).build());
            if (status != JobScheduler.RESULT_SUCCESS) prefs(c).edit().putString("status", "Android не разрешил фоновую проверку. Откройте настройки приложения.").apply();
        }
    }

    static boolean sync(Context c) {
        return sync(c, () -> false);
    }

    static boolean sync(Context c, BooleanSupplier cancelled) {
        synchronized (LOCK) {
            SharedPreferences p = prefs(c);
            if (!p.getBoolean("notifications", false) || !allowed(c)) return true;
            String base = p.getString("server", ""), identity = p.getString("identity", "");
            if (base.isEmpty() || identity.isEmpty()) return true;
            String key = "cursor." + identity;
            long cursor = p.getLong(key, 0);
            try {
                // Ограничиваем длительность одного задания; следующая сверка продолжит постранично.
                for (int batch = 0; batch < 5; batch++) {
                    if (cancelled.getAsBoolean()) return false;
                    JSONObject page = get(base, "/api/results?after=" + cursor + "&limit=100");
                    if (!identity.equals(page.getString("installation_id"))) {
                        p.edit().putString("status", "По этому адресу другая база. Подключитесь заново в настройках.").apply(); return false;
                    }
                    JSONArray items = page.getJSONArray("items");
                    for (int i = 0; i < items.length(); i++) {
                        if (cancelled.getAsBoolean() || !p.getBoolean("notifications", false) || !allowed(c)) return false;
                        JSONObject r = items.getJSONObject(i);
                        long id = r.getLong("id");
                        if (id <= cursor) continue;
                        // Резервируем до notify: авария процесса не повторит уже показанный результат.
                        // В узком окне аварии уведомление может быть пропущено, сама история хранится на сервере.
                        if (!p.edit().putLong(key, id).commit()) return false;
                        cursor = id;
                        Intent open = new Intent(c, MainActivity.class).setData(Uri.parse("agentpipeline://result/" + identity + "/" + id))
                            .putExtra("result_id", id).putExtra("identity", identity).addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
                        PendingIntent click = PendingIntent.getActivity(c, 0, open, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
                        String title = r.getString("project_name") + " #" + r.getInt("task_number") + " · " + r.getString("title");
                        String text = r.getString("text");
                        Notification n = new Notification.Builder(c, CHANNEL).setSmallIcon(R.drawable.ic_notification)
                            .setContentTitle(title).setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text))
                            .setContentIntent(click).setAutoCancel(true).setOnlyAlertOnce(true)
                            .setCategory(Notification.CATEGORY_STATUS).build();
                        c.getSystemService(NotificationManager.class).notify(identity + ":" + id, 1, n);
                    }
                    p.edit().putLong("last_check", System.currentTimeMillis()).putString("status", "Связь с сервером проверена").apply();
                    if (!page.getBoolean("has_more")) return true;
                }
                return true;
            } catch (Exception e) {
                p.edit().putString("status", "Нет связи с сервером. Android повторит проверку; история сохранена.").apply(); return false;
            }
        }
    }

    @Override public boolean onStartJob(JobParameters params) {
        stopped = false;
        new Thread(() -> { boolean ok = sync(this, () -> stopped); if (!stopped) jobFinished(params, !ok); }, "result-sync").start();
        return true;
    }
    @Override public boolean onStopJob(JobParameters params) { stopped = true; return true; }
}
