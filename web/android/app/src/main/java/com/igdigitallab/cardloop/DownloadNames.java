package com.igdigitallab.cardloop;

import java.io.ByteArrayOutputStream;
import java.nio.charset.Charset;
import java.nio.charset.StandardCharsets;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * The file name a server put in its Content-Disposition header.
 *
 * Plain Java on purpose (no android.* import), so the JVM unit tests cover it. It exists because
 * `URLUtil.guessFileName` cannot be trusted with a name: on API 34 it reads exactly
 * `attachment; filename="…"` and nothing else (`filename*` included), and when it gives up it
 * names the file after the URL — `/api/fs/raw?path=…` arrived on the phone as `raw.bin`.
 */
final class DownloadNames {

    private DownloadNames() {}

    // RFC 5987: filename*=<charset>'<lang>'<percent-encoded>
    private static final Pattern EXTENDED =
            Pattern.compile("(?i)(?:^|;)\\s*filename\\*\\s*=\\s*([^';\\s]*)'[^']*'([^;\\s]+)");
    private static final Pattern QUOTED =
            Pattern.compile("(?i)(?:^|;)\\s*filename\\s*=\\s*\"((?:[^\"\\\\]|\\\\.)*)\"");
    private static final Pattern TOKEN =
            Pattern.compile("(?i)(?:^|;)\\s*filename\\s*=\\s*([^\";\\s][^;]*)");

    /** The name from ``header``, preferring the RFC 5987 form; null when it names nothing usable. */
    static String fromContentDisposition(String header) {
        if (header == null) return null;
        Matcher m = EXTENDED.matcher(header);
        if (m.find()) {
            String name = clean(percentDecode(m.group(2), m.group(1)));
            if (name != null) return name;
        }
        m = QUOTED.matcher(header);
        if (m.find()) {
            String name = clean(m.group(1).replaceAll("\\\\(.)", "$1"));
            if (name != null) return name;
        }
        m = TOKEN.matcher(header);
        if (m.find()) return clean(m.group(1).trim());
        return null;
    }

    /** Strip any path a hostile header smuggles in, and the characters no file name may hold. */
    static String clean(String name) {
        if (name == null) return null;
        int slash = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
        String base = name.substring(slash + 1);
        StringBuilder out = new StringBuilder(base.length());
        for (int i = 0; i < base.length(); i++) {
            char c = base.charAt(i);
            if (c >= 0x20 && c != 0x7f) out.append(c);
        }
        String result = out.toString().trim();
        if (result.isEmpty() || result.equals(".") || result.equals("..")) return null;
        return result;
    }

    /** RFC 3986 percent-decoding (no `+` → space: that is a form-encoding rule, not this one). */
    static String percentDecode(String value, String charsetName) {
        Charset charset;
        try {
            charset = charsetName == null || charsetName.isEmpty()
                    ? StandardCharsets.UTF_8 : Charset.forName(charsetName);
        } catch (Exception e) {
            charset = StandardCharsets.UTF_8;
        }
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        int i = 0;
        while (i < value.length()) {
            if (value.charAt(i) == '%' && i + 2 < value.length()) {
                int hi = Character.digit(value.charAt(i + 1), 16);
                int lo = Character.digit(value.charAt(i + 2), 16);
                if (hi >= 0 && lo >= 0) {
                    bytes.write((hi << 4) | lo);
                    i += 3;
                    continue;
                }
            }
            int cp = value.codePointAt(i);
            byte[] raw = new String(Character.toChars(cp)).getBytes(charset);
            bytes.write(raw, 0, raw.length);
            i += Character.charCount(cp);
        }
        return new String(bytes.toByteArray(), charset);
    }
}
