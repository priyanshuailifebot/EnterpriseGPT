"use client";

/**
 * Slide-out drawer that calls the NL augment endpoint.
 *
 * The user types an instruction like "add a Slack notification after
 * scoring", the canvas posts it to ``POST /workflows/{id}/augment``,
 * and the parent applies the returned definition to its editor store.
 *
 * The submission UX shows pending → success/error state inline; the
 * actual diff is rendered by the InteractiveCanvas once the editor
 * store accepts the proposed definition.
 */

import * as Dialog from "@radix-ui/react-dialog";
import { Sparkles, X } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";

interface AIRefineDrawerProps {
  open: boolean;
  onClose: () => void;
  /** ``true`` when the host doesn't have a saved workflow id yet. */
  disabled: boolean;
  onSubmit: (message: string) => Promise<void>;
}

export function AIRefineDrawer({
  open,
  onClose,
  disabled,
  onSubmit,
}: AIRefineDrawerProps) {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    if (!message.trim() || disabled) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(message.trim());
      setMessage("");
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Refine failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm" />
        <Dialog.Content
          className={cn(
            "fixed right-0 top-0 z-50 flex h-full w-[420px] flex-col gap-4 border-l border-slate-200 bg-white p-5 shadow-2xl",
            "dark:border-slate-800 dark:bg-slate-950",
          )}
        >
          <header className="flex items-start justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-100 text-brand-700 dark:bg-brand-950 dark:text-brand-300">
                <Sparkles className="h-4 w-4" />
              </span>
              <div>
                <Dialog.Title className="text-sm font-semibold text-slate-900 dark:text-slate-100">
                  Refine with AI
                </Dialog.Title>
                <Dialog.Description className="text-[11px] text-slate-500 dark:text-slate-400">
                  Describe the change you want. The model rewrites the
                  graph and preserves stable ids; you preview the result
                  before saving.
                </Dialog.Description>
              </div>
            </div>
            <Dialog.Close className="rounded-md p-1 text-slate-500 hover:bg-slate-100 dark:hover:bg-slate-800">
              <X className="h-4 w-4" />
            </Dialog.Close>
          </header>

          {disabled ? (
            <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-700 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
              Save the workflow at least once before refining — the
              endpoint operates on a persisted workflow id.
            </p>
          ) : null}

          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder='e.g. "Add a Slack notification after the Hiring Manager review."'
            rows={6}
            disabled={disabled || busy}
            className="w-full resize-y rounded-md border border-slate-300 bg-white px-2 py-1.5 text-[12px] text-slate-900 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
          />

          {error ? (
            <p className="rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-[11px] text-rose-700 dark:border-rose-950 dark:bg-rose-950/40 dark:text-rose-300">
              {error}
            </p>
          ) : null}

          <div className="mt-auto flex items-center justify-between gap-2">
            <p className="text-[10px] text-slate-500 dark:text-slate-400">
              Tip: be specific — name the upstream node when adding
              steps, or call out the field to change.
            </p>
            <button
              type="button"
              onClick={() => void handleSubmit()}
              disabled={disabled || busy || !message.trim()}
              className="rounded-md bg-brand-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
            >
              {busy ? "Generating…" : "Apply refinement"}
            </button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
