"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  PropsWithChildren,
  useEffect,
  useState,
} from "react";

import { useAuthStore } from "@/stores/authStore";

export function AuthGuard({ children }: PropsWithChildren) {
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const boot = useAuthStore((s) => s.isBootstrapping);
  const hydrateUser = useAuthStore((s) => s.hydrateUser);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    let gone = false;
    void (async () => {
      await hydrateUser();
      if (!gone) setChecked(true);
    })();
    return () => {
      gone = true;
    };
  }, [hydrateUser]);

  useEffect(() => {
    if (!checked || boot) return;
    if (!user) {
      router.replace(`/login?next=${encodeURIComponent(window.location.pathname)}`);
    }
  }, [boot, checked, router, user]);

  if (!checked || boot || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 dark:bg-slate-950">
        <div className="h-10 w-10 animate-pulse rounded-full bg-brand-200 dark:bg-brand-900" />
      </div>
    );
  }

  return children;
}

export function AuthRedirectIfAuthed({ children }: PropsWithChildren) {
  const user = useAuthStore((s) => s.user);
  const boot = useAuthStore((s) => s.isBootstrapping);
  const hydrateUser = useAuthStore((s) => s.hydrateUser);
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    void hydrateUser().finally(() => setReady(true));
  }, [hydrateUser]);

  useEffect(() => {
    if (!ready || boot) return;
    if (user) router.replace("/dashboard");
  }, [boot, ready, router, user]);

  if (!ready || boot || user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-slate-50 dark:bg-slate-950">
        <div className="h-10 w-10 animate-pulse rounded-full bg-brand-200 dark:bg-brand-900" />
      </div>
    );
  }

  return children;
}

export function LoginLink() {
  return (
    <Link
      href="/login"
      className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-medium text-white hover:bg-brand-700"
    >
      Sign in
    </Link>
  );
}
