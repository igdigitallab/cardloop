package com.igdigitallab.cardloop;

import android.app.Activity;
import android.app.DownloadManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.WebView;
import android.widget.Toast;

/**
 * Everything that should leave the app.
 *
 * Cardloop is a WebView wrapper around a self-hosted cockpit, so "inside the app" means
 * exactly one origin: the instance the operator pointed it at. Anything else — a link an
 * agent pasted, a deep link, a file to save — belongs to the phone, not to us. Before this,
 * `allowNavigation: ['*']` (needed so the WebView may redirect to an arbitrary instance)
 * also meant every external link hijacked the app window, and downloads did nothing at all
 * because a bare WebView has no downloader.
 */
final class ExternalLinks {

    private ExternalLinks() {}

    /** The bundled shell's own host, before the operator's instance is chosen. */
    private static final String SHELL_HOST = "localhost";

    /**
     * Is ``url`` part of the instance currently loaded in ``view``?
     *
     * Deliberately derived from the live page rather than from stored config: the instance
     * URL is entered at runtime (ServerSetup), so the page we are on IS the source of truth.
     * Unknown or shell-stage hosts answer "ours" — never eject the app from its own boot.
     */
    static boolean isInstanceUrl(WebView view, Uri url) {
        String current = view != null ? view.getUrl() : null;
        if (current == null) return true;
        String currentHost = Uri.parse(current).getHost();
        if (currentHost == null || SHELL_HOST.equalsIgnoreCase(currentHost)) return true;
        return currentHost.equalsIgnoreCase(url.getHost());
    }

    /**
     * Hand a URL to the system: the default browser for http(s), the owning app for a deep
     * link (tg:, mailto:, market:). Returns true when it left the app, so the caller can
     * report the navigation as handled and leave the cockpit exactly where it was.
     */
    static boolean openExternally(Context context, Uri url) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, url);
            intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            context.startActivity(intent);
            return true;
        } catch (ActivityNotFoundException e) {
            toast(context, "No app can open " + url.getScheme() + " links");
            return true;   // still handled: loading it in the WebView is not the fallback we want
        } catch (Exception e) {
            return false;  // let the WebView try, rather than swallowing the click
        }
    }

    /**
     * Save a file with the system DownloadManager: it lands in Downloads, shows real progress
     * in the notification shade, and an APK installs straight from that notification.
     *
     * The cookie matters. Cardloop's media route (`/api/projects/{id}/media/{file}`) is behind
     * the auth middleware, and DownloadManager is a separate process with no session — without
     * copying the WebView's cookie onto the request it would fetch a 401 error page and save
     * THAT as the file.
     */
    static void enqueueDownload(Activity activity, WebView view, String url, String userAgent,
                                String contentDisposition, String mimeType) {
        Uri uri = Uri.parse(url);
        if (!isInstanceUrl(view, uri)) {
            openExternally(activity, uri);   // someone else's file — someone else's browser
            return;
        }
        String filename = URLUtil.guessFileName(url, contentDisposition, mimeType);
        try {
            DownloadManager.Request request = new DownloadManager.Request(uri);
            String cookie = CookieManager.getInstance().getCookie(url);
            if (cookie != null) request.addRequestHeader("Cookie", cookie);
            if (userAgent != null) request.addRequestHeader("User-Agent", userAgent);
            request.setMimeType(mimeType);
            request.setTitle(filename);
            request.setDescription("Cardloop");
            request.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                // Public Downloads needs no permission from Android 10 on; below that it would
                // demand WRITE_EXTERNAL_STORAGE, so older phones get the app-private dir instead
                // — still reachable from the completed-download notification.
                request.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, filename);
            } else {
                request.setDestinationInExternalFilesDir(activity, Environment.DIRECTORY_DOWNLOADS, filename);
            }
            DownloadManager dm = (DownloadManager) activity.getSystemService(Context.DOWNLOAD_SERVICE);
            if (dm == null) {
                openExternally(activity, uri);
                return;
            }
            dm.enqueue(request);
            toast(activity, "Downloading " + filename);
        } catch (Exception e) {
            toast(activity, "Download failed: " + e.getMessage());
        }
    }

    /** A target=_blank / window.open destination: our own origin loads here, the rest leaves. */
    static void handlePopup(Activity activity, WebView view, Uri url) {
        if (isInstanceUrl(view, url)) {
            // Our own origin: load it in the main WebView. An attachment URL never actually
            // navigates — it fires the download listener instead, so the cockpit stays put.
            view.loadUrl(url.toString());
        } else {
            openExternally(activity, url);
        }
    }

    private static void toast(Context context, String message) {
        try {
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show();
        } catch (Exception ignored) {
            // A toast is never worth crashing a download over.
        }
    }
}
