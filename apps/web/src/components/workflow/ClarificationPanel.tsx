"use client";

import * as RadioGroup from "@radix-ui/react-radio-group";
import * as Tooltip from "@radix-ui/react-tooltip";
import { Info } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type FormEvent,
} from "react";

import { cn } from "@/lib/utils";
import type { ClarificationQuestion } from "@/types/api";

export type ClarificationPanelProps = {
  questions: ClarificationQuestion[];
  roundNumber: number;
  originalPrompt: string;
  pendingAnswers: Record<string, string | string[]>;
  onAnswerChange: (questionId: string, value: string | string[]) => void;
  onSubmit: () => void;
  onUseDefaults: () => void;
  isSubmitting?: boolean;
};

const MAX_ROUNDS = 3;
const OTHER_SENTINEL = "__other__";

function isOtherOption(opt: string): boolean {
  const t = opt.trim().toLowerCase();
  return t === "other" || t === "others" || t === "other (specify)";
}

function DebouncedTextAnswer({
  questionId,
  initial,
  onAnswerChange,
}: {
  questionId: string;
  initial: string;
  onAnswerChange: (q: string, v: string | string[]) => void;
}) {
  const [value, setValue] = useState(initial);

  useEffect(() => {
    setValue(initial);
  }, [initial]);

  useEffect(() => {
    const t = window.setTimeout(() => {
      onAnswerChange(questionId, value);
    }, 320);
    return () => window.clearTimeout(t);
  }, [value, questionId, onAnswerChange]);

  return (
    <textarea
      value={value}
      onChange={(e) => setValue(e.target.value)}
      rows={3}
      className="w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none ring-brand-500/30 focus:ring-2 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
      placeholder="Type your answer…"
    />
  );
}

export function ClarificationPanel({
  questions,
  roundNumber,
  originalPrompt,
  pendingAnswers,
  onAnswerChange,
  onSubmit,
  onUseDefaults,
  isSubmitting = false,
}: ClarificationPanelProps) {
  // Local "Other" UI state — per-question. ``otherActive`` is whether the
  // user has checked the "Other" radio/box; ``otherText`` is the custom
  // value they're typing. We funnel the typed text into ``pendingAnswers``
  // so the backend never sees the sentinel — it sees the real answer.
  const [otherActive, setOtherActive] = useState<Record<string, boolean>>({});
  const [otherText, setOtherText] = useState<Record<string, string>>({});

  const allRequiredAnswered = useMemo(() => {
    return questions.every((q) => {
      if (!q.required) return true;
      const v = pendingAnswers[q.id];
      if (v === undefined) return false;
      if (typeof v === "string") return v.trim().length > 0;
      return v.length > 0;
    });
  }, [questions, pendingAnswers]);

  const handleFormSubmit = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      if (allRequiredAnswered && !isSubmitting) onSubmit();
    },
    [allRequiredAnswered, isSubmitting, onSubmit],
  );

  return (
    <Tooltip.Provider delayDuration={200}>
      <form
        onSubmit={handleFormSubmit}
        className="space-y-6 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm dark:border-slate-800 dark:bg-slate-900/60"
      >
        <div className="flex items-start justify-between gap-4">
          <blockquote className="flex-1 border-l-4 border-brand-400 pl-4 text-sm italic text-slate-600 dark:text-slate-400">
            {originalPrompt}
          </blockquote>
          <span className="shrink-0 rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">
            Round {roundNumber} of {MAX_ROUNDS}
          </span>
        </div>

        <div className="space-y-5">
          {questions.map((q) => (
            <div
              key={q.id}
              className="rounded-xl border border-slate-100 bg-slate-50/80 p-4 dark:border-slate-800 dark:bg-slate-950/40"
            >
              <div className="mb-3 flex items-start gap-2">
                <p className="flex-1 text-sm font-semibold text-slate-900 dark:text-slate-100">
                  {q.question}
                  {q.required ? (
                    <span className="text-error" aria-hidden>
                      {" "}
                      *
                    </span>
                  ) : null}
                </p>
                {q.why_asked ? (
                  <Tooltip.Root>
                    <Tooltip.Trigger asChild>
                      <button
                        type="button"
                        className="text-slate-400 hover:text-brand-600 dark:hover:text-brand-400"
                        aria-label="Why we ask"
                      >
                        <Info className="h-4 w-4" />
                      </button>
                    </Tooltip.Trigger>
                    <Tooltip.Portal>
                      <Tooltip.Content
                        side="top"
                        className="z-50 max-w-xs rounded-lg bg-slate-900 px-3 py-2 text-xs text-white shadow-md dark:bg-slate-100 dark:text-slate-900"
                      >
                        {q.why_asked}
                        <Tooltip.Arrow className="fill-slate-900 dark:fill-slate-100" />
                      </Tooltip.Content>
                    </Tooltip.Portal>
                  </Tooltip.Root>
                ) : null}
              </div>

              {q.type === "text" ? (
                <DebouncedTextAnswer
                  questionId={q.id}
                  initial={
                    typeof pendingAnswers[q.id] === "string"
                      ? (pendingAnswers[q.id] as string)
                      : ""
                  }
                  onAnswerChange={onAnswerChange}
                />
              ) : null}

              {q.type === "choice" && q.options?.length ? (() => {
                // Strip any LLM-emitted "Other" so we don't duplicate the one
                // we append ourselves.
                const baseOptions = q.options.filter((o) => !isOtherOption(o));
                const isOther = !!otherActive[q.id];
                const customText = otherText[q.id] ?? "";
                const radioValue = isOther
                  ? OTHER_SENTINEL
                  : typeof pendingAnswers[q.id] === "string"
                    ? (pendingAnswers[q.id] as string)
                    : "";
                return (
                  <RadioGroup.Root
                    className="flex flex-col gap-2"
                    value={radioValue}
                    onValueChange={(v) => {
                      if (v === OTHER_SENTINEL) {
                        setOtherActive((p) => ({ ...p, [q.id]: true }));
                        onAnswerChange(q.id, customText);
                      } else {
                        setOtherActive((p) => ({ ...p, [q.id]: false }));
                        onAnswerChange(q.id, v);
                      }
                    }}
                  >
                    {baseOptions.map((opt) => (
                      <label
                        key={opt}
                        className="flex cursor-pointer items-center gap-2 text-sm text-slate-700 dark:text-slate-300"
                      >
                        <RadioGroup.Item
                          value={opt}
                          className={cn(
                            "flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 bg-white",
                            "data-[state=checked]:border-brand-600 data-[state=checked]:bg-brand-600",
                          )}
                        >
                          <RadioGroup.Indicator className="h-2 w-2 rounded-full bg-white" />
                        </RadioGroup.Item>
                        {opt}
                      </label>
                    ))}
                    <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                      <RadioGroup.Item
                        value={OTHER_SENTINEL}
                        className={cn(
                          "flex h-4 w-4 items-center justify-center rounded-full border border-slate-300 bg-white",
                          "data-[state=checked]:border-brand-600 data-[state=checked]:bg-brand-600",
                        )}
                      >
                        <RadioGroup.Indicator className="h-2 w-2 rounded-full bg-white" />
                      </RadioGroup.Item>
                      Other (specify)
                    </label>
                    {isOther ? (
                      <input
                        type="text"
                        autoFocus
                        value={customText}
                        onChange={(e) => {
                          const v = e.target.value;
                          setOtherText((p) => ({ ...p, [q.id]: v }));
                          onAnswerChange(q.id, v);
                        }}
                        placeholder="Type your custom answer…"
                        className="ml-6 mt-1 w-[calc(100%-1.5rem)] rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none ring-brand-500/30 focus:ring-2 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                      />
                    ) : null}
                  </RadioGroup.Root>
                );
              })() : null}

              {q.type === "multi_choice" && q.options?.length ? (() => {
                const baseOptions = q.options.filter((o) => !isOtherOption(o));
                const selected = Array.isArray(pendingAnswers[q.id])
                  ? (pendingAnswers[q.id] as string[])
                  : [];
                const isOther = !!otherActive[q.id];
                const customText = otherText[q.id] ?? "";
                return (
                  <ul className="space-y-2">
                    {baseOptions.map((opt) => {
                      const checked = selected.includes(opt);
                      return (
                        <li key={opt}>
                          <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                            <input
                              type="checkbox"
                              checked={checked}
                              onChange={() => {
                                const next = new Set(selected);
                                if (checked) next.delete(opt);
                                else next.add(opt);
                                onAnswerChange(q.id, [...next]);
                              }}
                              className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                            />
                            {opt}
                          </label>
                        </li>
                      );
                    })}
                    <li>
                      <label className="flex cursor-pointer items-center gap-2 text-sm text-slate-700 dark:text-slate-300">
                        <input
                          type="checkbox"
                          checked={isOther}
                          onChange={() => {
                            if (isOther) {
                              // Turning Other off — drop the custom text from
                              // the answers if present.
                              const next = selected.filter(
                                (s) => s !== customText,
                              );
                              setOtherActive((p) => ({ ...p, [q.id]: false }));
                              onAnswerChange(q.id, next);
                            } else {
                              // Turning Other on — leave the array as-is. We
                              // only push the custom text once it's typed
                              // (avoids inserting "" into the answers).
                              setOtherActive((p) => ({ ...p, [q.id]: true }));
                            }
                          }}
                          className="h-4 w-4 rounded border-slate-300 text-brand-600 focus:ring-brand-500"
                        />
                        Other (specify)
                      </label>
                      {isOther ? (
                        <input
                          type="text"
                          autoFocus
                          value={customText}
                          onChange={(e) => {
                            const v = e.target.value;
                            const oldText = customText;
                            setOtherText((p) => ({ ...p, [q.id]: v }));
                            // Replace the previous custom text in the array
                            // with the new value (or add it if not present).
                            let next: string[];
                            const idx = selected.indexOf(oldText);
                            if (idx >= 0) {
                              next = [...selected];
                              if (v.trim()) next[idx] = v;
                              else next.splice(idx, 1);
                            } else if (v.trim()) {
                              next = [...selected, v];
                            } else {
                              next = selected;
                            }
                            onAnswerChange(q.id, next);
                          }}
                          placeholder="Type your custom answer…"
                          className="ml-6 mt-1 w-[calc(100%-1.5rem)] rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-900 outline-none ring-brand-500/30 focus:ring-2 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100"
                        />
                      ) : null}
                    </li>
                  </ul>
                );
              })() : null}
            </div>
          ))}
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <button
            type="submit"
            disabled={!allRequiredAnswered || isSubmitting}
            className={cn(
              "rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white shadow-sm",
              "hover:bg-brand-700 disabled:cursor-not-allowed disabled:opacity-50",
            )}
          >
            {isSubmitting ? "Submitting…" : "Submit answers"}
          </button>
          <button
            type="button"
            onClick={onUseDefaults}
            disabled={isSubmitting}
            className="rounded-lg border border-slate-200 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
          >
            Use defaults
          </button>
        </div>
      </form>
    </Tooltip.Provider>
  );
}
