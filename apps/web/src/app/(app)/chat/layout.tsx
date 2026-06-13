import type { PropsWithChildren } from "react";
import { Suspense } from "react";

export default function ChatLayout({ children }: PropsWithChildren) {
  return (
    <Suspense
      fallback={<div className="p-6 text-sm text-slate-500">Loading chat…</div>}
    >
      {children}
    </Suspense>
  );
}
