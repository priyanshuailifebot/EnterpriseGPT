"use client";

import { useRouter } from "next/navigation";

import { WorkflowBuilder } from "@/components/workflow/WorkflowBuilder";

export default function NewWorkflowPage() {
  const router = useRouter();

  return (
    <div className="mx-auto w-full max-w-[1600px] space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-900 dark:text-slate-50">
          New Workflow
        </h1>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Describe automation in prose, tighten the blueprint, visualize the DAG,
          then save back to Dynamiq-backed execution.
        </p>
      </div>
      <WorkflowBuilder onWorkflowSaved={(id) => router.push(`/workflows/${id}`)} />
    </div>
  );
}
