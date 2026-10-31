package space.agentpipeline.android;

import android.Manifest;
import android.app.*;
import android.content.*;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.*;
import android.provider.Settings;
import android.view.*;
import android.webkit.*;
import android.widget.*;
import org.json.JSONObject;
import java.net.URI;
import java.text.DateFormat;
import java.util.Date;

/** Весь продукт загружается с общего сервера; здесь только возможности Android. */
public class MainActivity extends Activity {
    WebView web;
    LinearLayout root, content;
    TextView state;
    ProgressBar progress;
    ValueCallback<Uri[]> chooser;
    boolean inSettings;
    long pendingResult;
    final Handler handler = new Handler(Looper.getMainLooper());
    final Runnable foregroundSync = new Runnable() { public void run() {
        new Thread(() -> ResultJob.sync(MainActivity.this), "visible-sync").start();
        handler.postDelayed(this, 20000);
    }};

    int dp(int n) { return (int)(getResources().getDisplayMetrics().density * n); }
    TextView text(String s, int size) { TextView v = new TextView(this); v.setText(s); v.setTextSize(size); v.setTextColor(Color.rgb(236,236,236)); v.setPadding(0, dp(8), 0, dp(8)); return v; }
    Button button(String s, Runnable action) { Button b = new Button(this); b.setText(s); b.setAllCaps(false); b.setOnClickListener(v -> action.run()); return b; }
    void message(String s) { Toast.makeText(this, s, Toast.LENGTH_LONG).show(); }

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved); ResultJob.channel(this);
        root = new LinearLayout(this); root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(Color.rgb(33,33,33));
        LinearLayout toolbar = new LinearLayout(this); toolbar.setGravity(Gravity.CENTER_VERTICAL); toolbar.setPadding(dp(8),0,dp(4),0);
        state = text("AgentPipeline", 12); state.setSingleLine(true); state.setEllipsize(android.text.TextUtils.TruncateAt.END);
        toolbar.addView(state, new LinearLayout.LayoutParams(0, dp(42), 1));
        Button reload = button("↻", () -> { if (web != null && !inSettings) web.reload(); }); reload.setMinWidth(0); reload.setMinimumWidth(0); reload.setContentDescription("Обновить страницу");
        toolbar.addView(reload, new LinearLayout.LayoutParams(dp(44), dp(42)));
        Button connection = button("Подключение", this::settings); connection.setTextSize(12);
        toolbar.addView(connection, new LinearLayout.LayoutParams(dp(114), dp(42)));
        root.addView(toolbar); progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal); root.addView(progress, new LinearLayout.LayoutParams(-1, dp(2)));
        content = new LinearLayout(this); content.setOrientation(LinearLayout.VERTICAL); root.addView(content, new LinearLayout.LayoutParams(-1, 0, 1)); setContentView(root);
        readIntent(getIntent());
        if (ResultJob.prefs(this).getString("server", "").isEmpty()) settings(); else showWeb();
    }

    void readIntent(Intent intent) {
        if (intent != null && intent.hasExtra("result_id")) {
            if (ResultJob.prefs(this).getString("identity", "").equals(intent.getStringExtra("identity"))) pendingResult = intent.getLongExtra("result_id", 0);
            else message("Уведомление относится к другому подключению. Выберите исходный сервер.");
        }
    }
    @Override protected void onNewIntent(Intent i) { super.onNewIntent(i); setIntent(i); readIntent(i); showWeb(); }

    void showWeb() {
        inSettings = false; content.removeAllViews();
        if (web == null) createWeb();
        content.addView(web, new LinearLayout.LayoutParams(-1,-1));
        String base = ResultJob.prefs(this).getString("server", "");
        state.setText(Uri.parse(base).getAuthority());
        if (base.isEmpty()) { settings(); return; }
        if (pendingResult > 0) { web.loadUrl(base + "/?result_id=" + pendingResult); pendingResult = 0; }
        else if (web.getUrl() == null || !sameOrigin(web.getUrl())) web.loadUrl(base + "/");
        ResultJob.schedule(this);
    }

    boolean sameOrigin(String url) {
        try { URI a = new URI(url), b = new URI(ResultJob.prefs(this).getString("server", ""));
            return a.getScheme().equals(b.getScheme()) && a.getHost().equals(b.getHost()) && a.getPort() == b.getPort();
        } catch (Exception e) { return false; }
    }

    void createWeb() {
        web = new WebView(this); WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true); s.setDomStorageEnabled(true); s.setAllowFileAccess(false); s.setAllowContentAccess(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW); s.setSupportMultipleWindows(true); s.setMediaPlaybackRequiresUserGesture(true);
        web.addJavascriptInterface(new Object() { @JavascriptInterface public void settings() { runOnUiThread(() -> MainActivity.this.settings()); } }, "AgentPipelineAndroid");
        web.setWebViewClient(new WebViewClient() {
            @Override public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) { return navigate(request.getUrl().toString()); }
            @Override public void onPageFinished(WebView v, String url) { state.setText(Uri.parse(url).getAuthority()); }
            @Override public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                if (r.isForMainFrame()) { state.setText("Нет связи · нажмите ↻"); message("Сервер недоступен. Проверьте адрес, USB/VPN и работу компьютера. Нажмите ↻ для повтора."); }
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override public void onProgressChanged(WebView v, int n) { progress.setProgress(n); progress.setVisibility(n < 100 ? View.VISIBLE : View.GONE); }
            @Override public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (chooser != null) chooser.onReceiveValue(null); chooser = callback;
                try { startActivityForResult(params.createIntent(), 10); } catch (ActivityNotFoundException e) { chooser.onReceiveValue(null); chooser = null; message("Не найден выбор файлов"); }
                return true;
            }
            @Override public boolean onCreateWindow(WebView v, boolean dialog, boolean gesture, Message result) {
                if (!gesture) return false;
                WebView popup = new WebView(MainActivity.this);
                popup.setWebViewClient(new WebViewClient() { @Override public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                    String url = r.getUrl().toString();
                    if (!navigate(url) && sameOrigin(url)) web.loadUrl(url);
                    popup.destroy(); return true;
                }});
                ((WebView.WebViewTransport) result.obj).setWebView(popup); result.sendToTarget(); return true;
            }
            @Override public boolean onJsAlert(WebView v, String url, String text, JsResult r) { new AlertDialog.Builder(MainActivity.this).setMessage(text).setPositiveButton("ОК", (d,w)->r.confirm()).setOnCancelListener(d->r.cancel()).show(); return true; }
            @Override public boolean onJsConfirm(WebView v, String url, String text, JsResult r) { new AlertDialog.Builder(MainActivity.this).setMessage(text).setPositiveButton("Да", (d,w)->r.confirm()).setNegativeButton("Нет", (d,w)->r.cancel()).setOnCancelListener(d->r.cancel()).show(); return true; }
            @Override public boolean onJsPrompt(WebView v, String url, String text, String value, JsPromptResult r) {
                EditText input = new EditText(MainActivity.this); input.setText(value);
                new AlertDialog.Builder(MainActivity.this).setMessage(text).setView(input).setPositiveButton("Сохранить", (d,w)->r.confirm(input.getText().toString())).setNegativeButton("Отмена", (d,w)->r.cancel()).setOnCancelListener(d->r.cancel()).show(); return true;
            }
        });
        web.setDownloadListener((url, ua, disposition, type, length) -> download(url, disposition, type));
    }

    boolean navigate(String url) {
        Uri uri = Uri.parse(url);
        if (!("http".equals(uri.getScheme()) || "https".equals(uri.getScheme()))) return true;
        if (sameOrigin(url)) return false;
        try { startActivity(new Intent(Intent.ACTION_VIEW, uri)); } catch (ActivityNotFoundException e) { message("Нет приложения для этой ссылки"); }
        return true;
    }

    void download(String url, String disposition, String type) {
        if (!sameOrigin(url)) { navigate(url); return; }
        try {
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            String filename = URLUtil.guessFileName(url, disposition, type);
            // filename* содержит русские имена; Android URLUtil не во всех версиях понимает RFC 5987.
            if (disposition != null && disposition.contains("filename*=UTF-8''")) filename = Uri.decode(disposition.split("filename\\*=UTF-8''", 2)[1]);
            filename = filename.replaceAll("[\\\\/:*?\"<>|]", "_");
            req.setTitle(filename).setMimeType(type).setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
            getSystemService(DownloadManager.class).enqueue(req); message("Файл сохраняется в Загрузки");
        } catch (Exception e) { message("Не удалось скачать файл. Проверьте связь и свободное место."); }
    }

    void settings() {
        inSettings = true; content.removeAllViews(); progress.setVisibility(View.GONE);
        ScrollView scroll = new ScrollView(this); LinearLayout box = new LinearLayout(this); box.setOrientation(LinearLayout.VERTICAL); box.setPadding(dp(20),dp(12),dp(20),dp(24)); scroll.addView(box); content.addView(scroll);
        box.addView(text("Подключение к трекеру", 24));
        box.addView(text("Используется тот же сервер, что у сайта. Проекты, задачи, диалоги и результаты общие.", 15));
        EditText address = new EditText(this); address.setSingleLine(true); address.setInputType(android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_VARIATION_URI);
        address.setHint("http://адрес-компьютера:8765"); address.setText(ResultJob.prefs(this).getString("server", "http://127.0.0.1:8765")); box.addView(address);
        TextView status = text("", 14); box.addView(status);
        Button connect = button("Подключиться", () -> connect(address.getText().toString(), status)); box.addView(connect);
        box.addView(text("Сервер по умолчанию доступен только на ПК. Через USB: включите отладку, выполните на ПК adb reverse tcp:8765 tcp:8765 и оставьте адрес http://127.0.0.1:8765. Для другого адреса нужна уже настроенная доступность сервера. HTTP используйте только в доверенной сети/USB/VPN; у базового трекера нет входа по паролю.", 13));
        box.addView(text("Уведомления о результатах", 22));
        Switch enabled = new Switch(this); enabled.setText("Фоновая проверка всех проектов"); enabled.setChecked(ResultJob.prefs(this).getBoolean("notifications", false)); box.addView(enabled);
        enabled.setOnCheckedChangeListener((v, checked) -> {
            ResultJob.prefs(this).edit().putBoolean("notifications", checked).apply();
            if (checked && Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 20);
            ResultJob.schedule(this);
            if (checked) new Thread(() -> ResultJob.sync(this)).start();
        });
        box.addView(text(ResultJob.allowed(this) ? "Системное разрешение выдано" : "Уведомления запрещены системой. Разрешите их ниже.", 14));
        box.addView(button("Разрешения Android", () -> startActivity(new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS).putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName()))));
        box.addView(text("При свёрнутом приложении и выключенном экране Android проверяет новые результаты примерно раз в 15 минут. Экономия заряда, Doze и прошивка могут задержать проверку на часы. Мгновенная доставка не гарантируется. После принудительной остановки откройте приложение снова. История восстанавливается после связи; старые результаты при первом подключении не создают уведомления.", 14));
        box.addView(button("Настройки батареи", () -> startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))));
        long last = ResultJob.prefs(this).getLong("last_check", 0);
        box.addView(text(ResultJob.prefs(this).getString("status", "Проверок ещё не было") + (last > 0 ? "\nПоследняя проверка: " + DateFormat.getDateTimeInstance().format(new Date(last)) : ""), 13));
        box.addView(button("Проверить результаты сейчас", () -> {
            if (!ResultJob.prefs(this).getBoolean("notifications", false) || !ResultJob.allowed(this)) { message("Включите фоновую проверку и разрешите уведомления Android."); return; }
            new Thread(() -> { boolean ok = ResultJob.sync(this); runOnUiThread(() -> message(ok ? "Новые результаты проверены" : "Нет связи. Проверьте подключение.")); }).start();
        }));
        if (!ResultJob.prefs(this).getString("server", "").isEmpty()) box.addView(button("Вернуться к задачам", this::showWeb));
        state.setText("AgentPipeline · подключение");
    }

    void connect(String input, TextView status) {
        final String base;
        try { URI u = new URI(input.trim());
            if (!("http".equals(u.getScheme()) || "https".equals(u.getScheme())) || u.getHost() == null || u.getRawUserInfo() != null
                || u.getRawQuery() != null || u.getRawFragment() != null || !(u.getPath().isEmpty() || u.getPath().equals("/"))) throw new Exception();
            base = new URI(u.getScheme(), null, u.getHost(), u.getPort(), null, null, null).toString();
        } catch (Exception e) { status.setText("Введите http://адрес:порт или https://адрес без пути и пароля."); return; }
        status.setText("Проверяю сервер…");
        new Thread(() -> {
            synchronized (ResultJob.LOCK) {
                try {
                    JSONObject meta = ResultJob.get(base, "/api/results/meta");
                    if (meta.getInt("version") != 1) throw new Exception();
                    String identity = meta.getString("installation_id");
                    android.content.SharedPreferences p = ResultJob.prefs(this);
                    android.content.SharedPreferences.Editor edit = p.edit().putString("server", base).putString("identity", identity);
                    if (!p.contains("cursor." + identity)) edit.putLong("cursor." + identity, meta.getLong("latest_id"));
                    if (!edit.commit()) throw new Exception();
                    runOnUiThread(() -> { if (web != null) { web.destroy(); web = null; } pendingResult = 0; showWeb(); });
                } catch (Exception e) { runOnUiThread(() -> status.setText("Сервер недоступен или ещё не обновлён. Проверьте адрес, USB/VPN и версию трекера; повторите подключение.")); }
            }
        }, "connect").start();
    }

    @Override public void onActivityResult(int request, int result, Intent data) { super.onActivityResult(request,result,data); if (request == 10 && chooser != null) { chooser.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(result, data)); chooser = null; } }
    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] grants) { super.onRequestPermissionsResult(request,permissions,grants); ResultJob.schedule(this); if (inSettings) settings(); }
    @Override protected void onResume() { super.onResume(); ResultJob.schedule(this); handler.post(foregroundSync); }
    @Override protected void onPause() { handler.removeCallbacks(foregroundSync); super.onPause(); }
    @Override public void onBackPressed() {
        if (inSettings) { if (!ResultJob.prefs(this).getString("server", "").isEmpty()) showWeb(); else super.onBackPressed(); return; }
        if (web != null) web.evaluateJavascript("typeof App !== 'undefined' && App.androidBack()", value -> {
            if (!"true".equals(value)) { if (web.canGoBack()) web.goBack(); else moveTaskToBack(true); }
        }); else super.onBackPressed();
    }
    @Override protected void onDestroy() { if (chooser != null) chooser.onReceiveValue(null); if (web != null) { content.removeView(web); web.destroy(); } super.onDestroy(); }
}
