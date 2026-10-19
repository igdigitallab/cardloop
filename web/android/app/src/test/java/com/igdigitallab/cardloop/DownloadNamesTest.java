package com.igdigitallab.cardloop;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import org.junit.Test;

public class DownloadNamesTest {

    @Test
    public void readsThePlainForm() {
        assertEquals("1792449905_report.pdf",
                DownloadNames.fromContentDisposition("attachment; filename=\"1792449905_report.pdf\""));
        assertEquals("notes.md", DownloadNames.fromContentDisposition("attachment; filename=notes.md"));
        assertEquals("a; b.txt", DownloadNames.fromContentDisposition("attachment; filename=\"a; b.txt\""));
        assertEquals("say \"hi\".txt",
                DownloadNames.fromContentDisposition("attachment; filename=\"say \\\"hi\\\".txt\""));
    }

    @Test
    public void readsTheEncodedFormTheFilesTabSends() {
        // the shape fs_browser.attachment_disposition emits for a name that cannot travel plain
        assertEquals("Отчёт за октябрь.pdf", DownloadNames.fromContentDisposition(
                "attachment; filename=\"download.pdf\"; filename*=UTF-8''"
                        + "%D0%9E%D1%82%D1%87%D1%91%D1%82%20%D0%B7%D0%B0%20%D0%BE%D0%BA%D1%82%D1%8F%D0%B1%D1%80%D1%8C.pdf"));
        // the old header: filename* alone — API 34's URLUtil turned this into raw.bin
        assertEquals("dltest-fs-report.pdf",
                DownloadNames.fromContentDisposition("attachment; filename*=UTF-8''dltest-fs-report.pdf"));
        assertEquals("a+b.txt", DownloadNames.fromContentDisposition("attachment; filename*=utf-8''a+b.txt"));
    }

    @Test
    public void neverHandsBackAPath() {
        assertEquals("passwd", DownloadNames.fromContentDisposition("attachment; filename=\"../../etc/passwd\""));
        assertEquals("x.bat", DownloadNames.fromContentDisposition("attachment; filename*=UTF-8''..%5C..%5Cx.bat"));
        assertEquals("ab.txt", DownloadNames.fromContentDisposition("attachment; filename*=UTF-8''a%0D%0Ab.txt"));
    }

    @Test
    public void namesNothingWhenThereIsNothingToName() {
        assertNull(DownloadNames.fromContentDisposition(null));
        assertNull(DownloadNames.fromContentDisposition("attachment"));
        assertNull(DownloadNames.fromContentDisposition("inline"));
        assertNull(DownloadNames.fromContentDisposition("attachment; filename=\"..\""));
        assertNull(DownloadNames.fromContentDisposition("attachment; filename=\"\""));
    }
}
