package com.igdigitallab.cardloop;

import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Message;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;
import androidx.activity.OnBackPressedCallback;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.BridgeWebChromeClient;
import com.getcapacitor.BridgeWebViewClient;
import org.json.JSONObject;

public class MainActivity extends BridgeActivity {
    private static final int NOTIFICATION_PERMISSION_REQUEST_CODE = 1;
    /** Back at the app boundary asks for confirmation inside this window instead of exiting. */
    private static final long EXIT_CONFIRM_WINDOW_MS = 2500;

    private long lastBackAt = 0L;
    /** An intent that arrived before the cockpit finished loading, replayed on page ready. */
    private String pendingIntentJs = null;
    private boolean pageReady = false;

    // Android 13+ requires this runtime permission before the app (or its WebView, via the
    // web Notification/Push APIs) can post anything to the system tray. A bare WebView never
    // triggers the OS prompt on its own, so request it once up front.
    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(
                    this,
                    new String[] {Manifest.permission.POST_NOTIFICATIONS},
                    NOTIFICATION_PERMISSION_REQUEST_CODE);
        }
        wireExternalLinks();
        wireBackNavigation();
        routeIntent(getIntent());
    }

    /** A share or a launcher shortcut can arrive while the app is already running. */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        routeIntent(intent);
    }

    /**
     * Back should walk out of the UI before it walks out of the app.
     *
     * Every dismissible layer in the cockpit (modal, lightbox, mobile drawer, the tab you came
     * from) owns one history entry — see useBackDismiss — so `canGoBack()` is the single
     * question worth asking: if the WebView has somewhere to go, Back belongs to the web app.
     * Only at the true boundary do we leave, and then not on the first press: the operator's
     * complaint was that one stray Back threw them out of a live session.
     */
    private void wireBackNavigation() {
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                Bridge bridge = getBridge();
                WebView webView = bridge != null ? bridge.getWebView() : null;
                if (webView != null && webView.canGoBack()) {
                    webView.goBack();
                    return;
                }
                long now = System.currentTimeMillis();
                if (now - lastBackAt < EXIT_CONFIRM_WINDOW_MS) {
                    finish();
                    return;
                }
                lastBackAt = now;
                Toast.makeText(MainActivity.this, "Press back again to leave Cardloop",
                        Toast.LENGTH_SHORT).show();
            }
        });
    }

    /**
     * Hand a share or a launcher shortcut to the web app as a `cops:intent` event.
     *
     * This is the part a PWA cannot do: appear in the Android share sheet and in the
     * long-press menu on the launcher icon. The payload is queued when it arrives before the
     * cockpit is on screen (cold start from the share sheet is exactly that case) and replayed
     * from onPageFinished.
     */
    private void routeIntent(Intent intent) {
        if (intent == null) return;
        String kind = null;
        String value = null;

        if (Intent.ACTION_SEND.equals(intent.getAction())) {
            String text = intent.getStringExtra(Intent.EXTRA_TEXT);
            String subject = intent.getStringExtra(Intent.EXTRA_SUBJECT);
            if (text != null && !text.isEmpty()) {
                kind = "share";
                value = (subject != null && !subject.isEmpty() && !text.contains(subject))
                        ? subject + "\n" + text
                        : text;
            }
        } else if (intent.hasExtra("cops_shortcut")) {
            kind = "shortcut";
            value = intent.getStringExtra("cops_shortcut");
        }
        if (kind == null || value == null) return;

        try {
            JSONObject detail = new JSONObject();
            detail.put("kind", kind);
            detail.put("value", value);
            String js = "window.dispatchEvent(new CustomEvent('cops:intent',{detail:"
                    + detail.toString() + "}))";
            if (pageReady) evaluate(js);
            else pendingIntentJs = js;
        } catch (Exception ignored) {
            // A malformed share is not worth crashing the app over.
        }
    }

    private void evaluate(String js) {
        Bridge bridge = getBridge();
        WebView webView = bridge != null ? bridge.getWebView() : null;
        if (webView != null) webView.evaluateJavascript(js, null);
    }

    /**
     * Keep the app on the cockpit and give everything else to the phone.
     *
     * Capacitor already opens off-origin URLs externally — but only for hosts outside
     * `server.allowNavigation`, and ours is `['*']` because the instance URL is chosen at
     * runtime and cannot be listed at build time. So the whitelist that lets the WebView
     * reach the operator's own server also swallowed every link an agent ever pasted. The
     * three hooks below re-draw that line at "the origin currently loaded", which is the
     * only definition that survives a runtime-chosen server.
     */
    private void wireExternalLinks() {
        Bridge bridge = this.getBridge();
        if (bridge == null) return;
        WebView webView = bridge.getWebView();
        if (webView == null) return;

        // 1. Ordinary clicks. Both the bridge's reference and the WebView's must be replaced:
        //    the bridge hands its own client to the WebView during load(), and keeps using
        //    the field afterwards.
        BridgeWebViewClient client = new CardloopWebViewClient(bridge);
        bridge.setWebViewClient(client);
        webView.setWebViewClient(client);

        // 2. target=_blank and window.open — without multiple-window support the WebView
        //    silently drops them, which is why the chat's 📎 download link did nothing at all.
        webView.getSettings().setSupportMultipleWindows(true);
        webView.setWebChromeClient(new CardloopWebChromeClient(bridge));

        // 3. Anything the server sends as an attachment.
        webView.setDownloadListener((url, userAgent, contentDisposition, mimeType, contentLength) ->
                ExternalLinks.enqueueDownload(this, webView, url, userAgent, contentDisposition, mimeType));
    }

    private class CardloopWebViewClient extends BridgeWebViewClient {
        CardloopWebViewClient(Bridge bridge) {
            super(bridge);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            pageReady = true;
            if (pendingIntentJs != null) {
                String js = pendingIntentJs;
                pendingIntentJs = null;
                // The listeners live in React components; give the tree a tick to mount.
                view.postDelayed(() -> evaluate(js), 400);
            }
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri url = request.getUrl();
            String scheme = url.getScheme();

            // Sub-frames are page content, not navigation — an ad iframe is not a link click.
            if (!request.isForMainFrame() || scheme == null) {
                return super.shouldOverrideUrlLoading(view, request);
            }
            // Deep links (tg:, mailto:, market:, intent:) are for their apps, always.
            if (!scheme.equals("http") && !scheme.equals("https")) {
                return ExternalLinks.openExternally(MainActivity.this, url)
                        || super.shouldOverrideUrlLoading(view, request);
            }
            // Our own instance stays in the app.
            if (ExternalLinks.isInstanceUrl(view, url)) {
                return super.shouldOverrideUrlLoading(view, request);
            }
            // No user gesture = the app navigating itself. This is the ServerSetup redirect
            // from the bundled shell onto the operator's instance (location.replace), and
            // ejecting THAT to a browser would leave the app permanently on a blank shell.
            if (!request.hasGesture()) {
                return super.shouldOverrideUrlLoading(view, request);
            }
            return ExternalLinks.openExternally(MainActivity.this, url);
        }
    }

    private class CardloopWebChromeClient extends BridgeWebChromeClient {
        CardloopWebChromeClient(Bridge bridge) {
            super(bridge);
        }

        @Override
        public boolean onCreateWindow(WebView view, boolean isDialog, boolean isUserGesture, Message resultMsg) {
            // The destination is not passed in — the documented way to learn it is to let a
            // throwaway WebView receive the navigation and report the URL it was handed.
            WebView probe = new WebView(view.getContext());
            probe.setWebViewClient(new WebViewClient() {
                @Override
                public boolean shouldOverrideUrlLoading(WebView probeView, WebResourceRequest request) {
                    ExternalLinks.handlePopup(MainActivity.this, view, request.getUrl());
                    probeView.destroy();
                    return true;
                }
            });
            ((WebView.WebViewTransport) resultMsg.obj).setWebView(probe);
            resultMsg.sendToTarget();
            return true;
        }
    }
}
