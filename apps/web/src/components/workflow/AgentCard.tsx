"use client";

import * as RadixDropdownMenu from "@radix-ui/react-dropdown-menu";
import { ArrowRight, GripVertical } from "lucide-react";
import { Fragment, useCallback, useEffect, useState } from "react";

import { resolveProviderForSlug } from "@/components/workflow/integration-icons";
import { cn } from "@/lib/utils";
import type { AgentDefinition } from "@/types/api";

export interface AgentCardProps {
  agent: AgentDefinition;
  index: number;
  allAgents: AgentDefinition[];
  humanCheckpoint: boolean;
  toolsCatalog: string[];
  onChange: (next: AgentDefinition) => void;
  onToggleCheckpoint: (agentId: string, checked: boolean) => void;
  onRemove: (agentId: string) => void;
}

export function AgentCard({
  agent,
  index,
  allAgents,
  humanCheckpoint,
  toolsCatalog,
  onChange,
  onToggleCheckpoint,
  onRemove,
}: AgentCardProps) {
  const [name, setName] = useState(agent.name);
  const [role, setRole] = useState(agent.role);
  const [instructions, setInstructions] = useState(agent.instructions);

  useEffect(() => {
    setName(agent.name);
    setRole(agent.role);
    setInstructions(agent.instructions);
  }, [agent.id, agent.name, agent.role, agent.instructions]);

  const commitTextFields = useCallback(() => {
    onChange({
      ...agent,
      name,
      role,
      instructions,
    });
  }, [agent, name, role, instructions, onChange]);

  const depsMeta = agent.depends_on.map((did) =>
    allAgents.find((x) => x.id === did),
  );

  return (
    <div
      className={cn(
        "rounded-2xl border bg-white p-4 shadow-sm dark:bg-slate-900/70",
        humanCheckpoint ?
          "border-dashed border-amber-400/80 ring-2 ring-amber-200/40 dark:border-amber-500/70"
        : "border-slate-200 dark:border-slate-800",
        agent.is_parallel && "ring-2 ring-brand-200 dark:ring-brand-900",
      )}
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <GripVertical className="h-4 w-4 text-slate-400" />
          <span className="text-xs uppercase text-slate-500">#{index + 1}</span>
          {agent.is_parallel ? (
            <span className="rounded-full bg-brand-50 px-2 py-0.5 text-[10px] font-semibold text-brand-800 dark:bg-brand-950 dark:text-brand-100">
              Parallel-capable tier
            </span>
          ) : null}
        </div>
        <button
          type="button"
          className="text-xs text-red-600 hover:underline dark:text-red-400"
          onClick={() => onRemove(agent.id)}
        >
          Remove
        </button>
      </div>

      <label className="text-xs font-medium text-slate-600 dark:text-slate-300">
        Agent name
        <input
          value={name}
          className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-950"
          onChange={(e) => setName(e.target.value)}
          onBlur={commitTextFields}
        />
      </label>

      <label className="mt-3 block text-xs font-medium text-slate-600 dark:text-slate-300">
        Role / persona
        <input
          value={role}
          className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-950"
          onChange={(e) => setRole(e.target.value)}
          onBlur={commitTextFields}
        />
      </label>

      <label className="mt-3 block text-xs font-medium text-slate-600 dark:text-slate-300">
        Instructions
        <textarea
          rows={4}
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          onBlur={commitTextFields}
          className="mt-1 w-full rounded-lg border border-slate-200 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-950"
        />
      </label>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-500">Depends on:</span>
        {agent.depends_on.length === 0 ? (
          <span className="text-xs text-slate-400">none (entry node)</span>
        ) : (
          depsMeta.map((d, i) => (
            <Fragment key={`${agent.id}-${d?.id ?? i}`}>
              {i ? <ArrowRight className="h-3 w-3 text-slate-400" /> : null}
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs dark:bg-slate-800">
                {d?.name ?? "?"}
              </span>
            </Fragment>
          ))
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="text-xs text-slate-500">Tools:</span>
        {agent.tools.length === 0 ? (
          <span className="text-xs text-slate-400">none</span>
        ) : (
          agent.tools.map((t: string) => {
            const prov = resolveProviderForSlug(t);
            return (
              <span
                key={t}
                title={prov ? `${prov.label} · ${t}` : t}
                className={cn(
                  "inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px]",
                  prov ? `${prov.bg} ${prov.fg}` :
                    "bg-brand-50 text-brand-800 dark:bg-brand-950 dark:text-brand-100",
                )}
              >
                {prov ? prov.icon : null}
                {t}
              </span>
            );
          })
        )}

        <RadixDropdownMenu.Root>
          <RadixDropdownMenu.Trigger className="rounded-full border border-dashed border-slate-300 px-2 py-1 text-[11px] dark:border-slate-600">
            + tool
          </RadixDropdownMenu.Trigger>
          <RadixDropdownMenu.Portal>
            <RadixDropdownMenu.Content className="z-[60] max-h-72 min-w-[200px] overflow-auto rounded-xl border bg-white p-1 text-xs shadow-xl dark:border-slate-700 dark:bg-slate-950">
              {toolsCatalog.length === 0 ? (
                <div className="px-2 py-1 text-slate-500">Loading tools…</div>
              ) : (
                toolsCatalog.map((t) => (
                  <RadixDropdownMenu.Item
                    key={t}
                    className="cursor-pointer rounded-lg px-2 py-2 outline-none hover:bg-slate-100 dark:hover:bg-slate-900"
                    onSelect={() =>
                      onChange({
                        ...agent,
                        tools: agent.tools.includes(t) ?
                          agent.tools
                        : [...agent.tools, t],
                      })
                    }
                  >
                    {t}
                  </RadixDropdownMenu.Item>
                ))
              )}
            </RadixDropdownMenu.Content>
          </RadixDropdownMenu.Portal>
        </RadixDropdownMenu.Root>

        <label className="ml-auto inline-flex cursor-pointer items-center gap-2 text-xs text-slate-600 dark:text-slate-300">
          <input
            type="checkbox"
            className="h-4 w-4 accent-brand-600"
            checked={humanCheckpoint}
            onChange={(e) => onToggleCheckpoint(agent.id, e.target.checked)}
          />
          Human checkpoint
        </label>
      </div>
    </div>
  );
}
