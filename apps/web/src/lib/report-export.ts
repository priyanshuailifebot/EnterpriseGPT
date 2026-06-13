"use client";

import toast from "react-hot-toast";

import { api, getErrorMessage } from "@/lib/api";

/** Trigger a file download from bytes — no pop-ups required. */
export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

export function downloadPdfBase64(base64: string, filename: string): void {
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  downloadBlob(new Blob([bytes], { type: "application/pdf" }), filename);
}

function safePdfFilename(title: string): string {
  const stem = (title || "report")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-")
    .slice(0, 80);
  const base = stem || "report";
  return base.toLowerCase().endsWith(".pdf") ? base : `${base}.pdf`;
}

/**
 * Render agent report markdown on the server and download a real PDF file.
 * Avoids ``window.open`` so browser pop-up blockers cannot interfere.
 */
export async function downloadReportAsPdf(opts: {
  title: string;
  markdown: string;
  meta?: string;
}): Promise<void> {
  const filename = safePdfFilename(opts.title);
  const toastId = toast.loading("Generating PDF…");
  try {
    const { data } = await api.post<ArrayBuffer>(
      "/api/v1/reports/pdf",
      {
        title: opts.title,
        content: opts.markdown,
      },
      { responseType: "arraybuffer" },
    );
    downloadBlob(new Blob([data], { type: "application/pdf" }), filename);
    toast.success("PDF downloaded", { id: toastId });
  } catch (error: unknown) {
    toast.error(getErrorMessage(error) || "Could not generate PDF", { id: toastId });
  }
}

export async function copyReportToClipboard(markdown: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(markdown);
    toast.success("Report copied to clipboard");
  } catch {
    toast.error("Could not copy to clipboard");
  }
}
