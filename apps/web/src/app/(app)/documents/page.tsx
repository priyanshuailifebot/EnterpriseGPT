"use client";

import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useCallback, useEffect, useState } from "react";

import { api, getErrorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/authStore";
import type {
  CitedAnswerOut,
  DocumentListResponse,
  DocumentUploadResponse,
  DocumentStatusOut,
} from "@/types/api";

const CORPUS_POLL_MS = 3000;
const INGEST_POLL_MS = 3000;
const INGEST_MAX_POLLS = 200;

type UploadJobRow = {
  clientId: string;
  filename: string;
  /** 0–100 while bytes are uploading */
  uploadPct: number;
  phase: "uploading" | "ingesting" | "indexed" | "error";
  documentId: string | null;
  statusLabel: string;
  error?: string;
};

function ProgressBar({ value, indeterminate }: { value: number; indeterminate?: boolean }) {
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-200 dark:bg-slate-800">
      {indeterminate ?
        <div
          className="h-full w-[55%] animate-pulse rounded-full bg-brand-600"
          role="progressbar"
          aria-label="Indexing"
        />
      : <div
          className="h-full rounded-full bg-brand-600 transition-[width] duration-300"
          style={{ width: `${Math.min(100, Math.max(0, value))}%` }}
          role="progressbar"
          aria-valuenow={Math.round(Math.min(100, Math.max(0, value)))}
          aria-valuemin={0}
          aria-valuemax={100}
        />
      }
    </div>
  );
}

export default function DocumentsPage() {
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const canUpload = useAuthStore((s) => s.hasPermission("document:upload"));

  const [items, setItems] = useState<DocumentListResponse["items"]>([]);
  const [loading, setLoading] = useState(false);
  const [uploadJobs, setUploadJobs] = useState<UploadJobRow[]>([]);

  const [ragQ, setRag] = useState("");
  const [ragBusy, setRagBusy] = useState(false);
  const [answer, setAnswer] = useState<CitedAnswerOut | null>(null);

  const reload = useCallback(async () => {
    if (!workspaceId) return;
    setLoading(true);
    try {
      const { data } = await api.get<DocumentListResponse>("/api/v1/documents", {
        params: { workspace_id: workspaceId, page_size: 100 },
      });
      setItems(data.items);
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    if (!workspaceId) return undefined;
    const id = window.setInterval(() => {
      void reload();
    }, CORPUS_POLL_MS);
    return () => window.clearInterval(id);
  }, [reload, workspaceId]);

  const patchJob = useCallback((clientId: string, patch: Partial<UploadJobRow>) => {
    setUploadJobs((rows) =>
      rows.map((r) => (r.clientId === clientId ? { ...r, ...patch } : r)),
    );
  }, []);

  const startIngestPolling = useCallback(
    (clientId: string, documentId: string) => {
      let polls = 0;
      const tick = async () => {
        if (!workspaceId) return;
        polls++;
        if (polls > INGEST_MAX_POLLS) {
          patchJob(clientId, {
            phase: "error",
            statusLabel: "Timed out waiting for indexing",
          });
          return;
        }

        try {
          const { data } = await api.get<DocumentStatusOut>(
            `/api/v1/documents/${documentId}/status`,
            { params: { workspace_id: workspaceId } },
          );
          patchJob(clientId, {
            statusLabel: data.status,
            ...(data.status === "indexed" ?
              {
                phase: "indexed",
                uploadPct: 100,
              }
            : {}),
          });

          if (data.status === "indexed") {
            void reload();
            return;
          }
          if (data.status === "error") {
            patchJob(clientId, {
              phase: "error",
              error: data.error_message ?? "Ingest failed",
              statusLabel: "error",
            });
            void reload();
            return;
          }
        } catch (e: unknown) {
          patchJob(clientId, {
            phase: "error",
            error: getErrorMessage(e),
            statusLabel: "error",
          });
          return;
        }

        window.setTimeout(tick, INGEST_POLL_MS);
      };

      void tick();
    },
    [patchJob, reload, workspaceId],
  );

  async function uploadOne(file: File) {
    if (!workspaceId || !canUpload) return;

    const clientId = crypto.randomUUID();
    const row: UploadJobRow = {
      clientId,
      filename: file.name,
      uploadPct: 0,
      phase: "uploading",
      documentId: null,
      statusLabel: "Uploading…",
    };
    setUploadJobs((r) => [...r, row]);

    const fd = new FormData();
    fd.append("file", file);

    try {
      const { data } = await api.post<DocumentUploadResponse>(
        `/api/v1/documents/upload`,
        fd,
        {
          params: { workspace_id: workspaceId },
          headers: { "Content-Type": "multipart/form-data" },
          onUploadProgress: (ev) => {
            const total = ev.total ?? 0;
            const pct =
              total > 0 ? Math.round((ev.loaded * 100) / total) : ev.loaded > 0 ? 5 : 0;
            patchJob(clientId, { uploadPct: pct, statusLabel: `Uploading… ${pct}%` });
          },
        },
      );

      patchJob(clientId, {
        uploadPct: 100,
        phase: "ingesting",
        documentId: data.document_id,
        statusLabel: data.status,
      });

      startIngestPolling(clientId, data.document_id);
    } catch (e: unknown) {
      patchJob(clientId, {
        phase: "error",
        error: getErrorMessage(e),
        statusLabel: "Upload failed",
        uploadPct: 0,
      });
    }
  }

  async function onFiles(files: FileList | null) {
    if (!files?.length || !workspaceId || !canUpload) return;
    const list = Array.from(files);
    await Promise.all(list.map((f) => uploadOne(f)));
  }

  async function rag() {
    if (!workspaceId) return;
    const question = ragQ.trim();
    if (!question) return;
    setRagBusy(true);
    try {
      const { data } = await api.post<CitedAnswerOut>(
        "/api/v1/documents/query",
        { question, top_k: 8 },
        { params: { workspace_id: workspaceId } },
      );
      setAnswer(data);
    } catch (e) {
      alert(getErrorMessage(e));
      setAnswer(null);
    } finally {
      setRagBusy(false);
    }
  }

  function dismissJob(clientId: string) {
    setUploadJobs((rows) => rows.filter((r) => r.clientId !== clientId));
  }

  if (!workspaceId) {
    return <p>Select a workspace.</p>;
  }

  return (
    <div className="mx-auto grid max-w-7xl gap-6 lg:grid-cols-[2fr,minmax(320px,1fr)]">
      <section className="space-y-4 rounded-3xl border border-slate-200 bg-white p-6 dark:border-slate-800 dark:bg-slate-950">
        <div>
          <h1 className="text-xl font-semibold text-slate-900 dark:text-slate-50">
            Documents
          </h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Upload corp knowledge, watch per-file progress, then verify indexed status.
          </p>
        </div>

        {canUpload ?
          <label
            htmlFor="file-drop"
            className="flex flex-col gap-3 rounded-2xl border-2 border-dashed border-brand-400/70 bg-brand-50/70 px-6 py-10 text-center dark:border-brand-900 dark:bg-brand-950/60"
          >
            <span className="text-sm font-medium text-brand-900 dark:text-brand-100">
              Drag PDF/DOCX/TXT/CSV/MD here or browse
            </span>
            <input
              id="file-drop"
              type="file"
              multiple
              accept=".pdf,.docx,.txt,.csv,.md"
              className="hidden"
              onChange={(e) => void onFiles(e.target.files)}
            />
          </label>
        : <p className="text-sm text-slate-500 dark:text-slate-400">
            Ask an administrator for upload permissions.
          </p>
        }

        {uploadJobs.length > 0 ?
          <div className="space-y-3 rounded-2xl border border-slate-200 bg-slate-50/80 p-4 dark:border-slate-800 dark:bg-slate-900/50">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              Active uploads
            </p>
            <ul className="space-y-4">
              {uploadJobs.map((job) => (
                <li key={job.clientId} className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-700 dark:bg-slate-950">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <p className="text-sm font-medium text-slate-900 dark:text-slate-100">
                      {job.filename}
                    </p>
                    <div className="flex items-center gap-2">
                      <span
                        className={cn(
                          "rounded-full px-2 py-0.5 text-[11px] font-semibold capitalize",
                          job.phase === "indexed" &&
                            "bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100",
                          job.phase === "error" &&
                            "bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-100",
                          (job.phase === "uploading" || job.phase === "ingesting") &&
                            "bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100",
                        )}
                      >
                        {job.phase}
                      </span>
                      {(job.phase === "indexed" || job.phase === "error") ?
                        <button
                          type="button"
                          className="text-[11px] text-slate-500 underline"
                          onClick={() => dismissJob(job.clientId)}
                        >
                          Dismiss
                        </button>
                      : null}
                    </div>
                  </div>
                  <p className="mt-1 text-xs text-slate-500">{job.statusLabel}</p>
                  {job.error ?
                    <p className="mt-1 text-xs text-red-600 dark:text-red-400">{job.error}</p>
                  : null}
                  <div className="mt-2">
                    {job.phase === "uploading" ?
                      <ProgressBar value={job.uploadPct} />
                    : job.phase === "ingesting" ?
                      <ProgressBar value={100} indeterminate />
                    : job.phase === "indexed" ?
                      <ProgressBar value={100} />
                    : <ProgressBar value={0} />
                    }
                  </div>
                </li>
              ))}
            </ul>
          </div>
        : null}

        <div>
          <h2 className="text-sm font-semibold text-slate-800 dark:text-slate-200">
            Corpus
          </h2>
          {loading ?
            <p className="text-sm text-slate-500">Loading…</p>
          : items.length === 0 ?
            <p className="text-sm text-slate-500">Nothing uploaded yet.</p>
          : <ul className="mt-3 space-y-2">
              {items.map((doc) => (
                <li
                  key={doc.id}
                  className="rounded-2xl border border-slate-200 px-4 py-3 text-sm dark:border-slate-800"
                >
                  <div className="flex justify-between gap-3">
                    <div>
                      <p className="font-medium text-slate-900 dark:text-slate-100">
                        {doc.filename}
                      </p>
                      <p className="text-xs text-slate-500">
                        {doc.file_type} · chunks {doc.chunk_count} ·{" "}
                        {new Date(doc.created_at).toLocaleString()}
                      </p>
                    </div>
                    <span className="capitalize text-xs text-slate-600 dark:text-slate-300">
                      {doc.status}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          }
        </div>
      </section>

      <aside className="space-y-4 rounded-3xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-950">
        <h2 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          RAG query
        </h2>
        <textarea
          value={ragQ}
          rows={6}
          onChange={(e) => setRag(e.target.value)}
          className="w-full rounded-2xl border border-slate-200 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
          placeholder="Ask with citations enforced…"
        />
        <button
          type="button"
          disabled={ragBusy}
          onClick={() => void rag()}
          className="w-full rounded-2xl bg-brand-600 py-2 text-sm font-semibold text-white disabled:opacity-60"
        >
          {ragBusy ? "Thinking…" : "Run query"}
        </button>

        {answer ?
          <div className="space-y-4 rounded-2xl border border-slate-100 bg-slate-50 px-4 py-3 text-sm dark:border-slate-800 dark:bg-slate-900">
            <div className="leading-relaxed">
              <Markdown remarkPlugins={[remarkGfm]}>{answer.answer}</Markdown>
            </div>
            <div className="text-xs font-semibold text-slate-600 dark:text-slate-300">
              Citations
              <div className="mt-2 flex flex-wrap gap-2">
                {answer.citations.map((c) => (
                  <span
                    key={`${c.document_id}-${c.index}`}
                    className="inline-flex rounded-full border border-brand-200 px-3 py-1 text-brand-800 dark:border-brand-700 dark:text-brand-100"
                  >
                    [{c.index}] {c.document_title}{" "}
                    <span className="opacity-60"> · p.{c.page_number}</span>
                  </span>
                ))}
              </div>
            </div>
          </div>
        : null}
      </aside>
    </div>
  );
}
