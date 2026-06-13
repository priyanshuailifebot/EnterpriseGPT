"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { AuthRedirectIfAuthed } from "@/components/auth/auth-guard";
import { getErrorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

const loginSchema = z.object({
  email: z.string().email("Enter a valid email"),
  password: z.string().min(1, "Password required"),
  totp_code: z.string().optional(),
});

type LoginFields = z.infer<typeof loginSchema>;

export default function LoginPage() {
  const router = useRouter();
  const qp = useSearchParams();
  const login = useAuthStore((s) => s.login);
  const [mfaRequired, setMfaRequired] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const next = qp.get("next") || "/dashboard";

  const form = useForm<LoginFields>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      email: "",
      password: "",
      totp_code: "",
    },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    setFormError(null);
    try {
      await login({
        email: values.email,
        password: values.password,
        ...(mfaRequired && values.totp_code ?
          { totp_code: values.totp_code }
        : {}),
      });
      router.replace(next.startsWith("/") ? next : "/dashboard");
    } catch (err) {
      const msg = getErrorMessage(err);
      if (
        msg.toLowerCase().includes("mfa") ||
        msg.toLowerCase().includes("totp")
      ) {
        setMfaRequired(true);
      }
      setFormError(msg);
    }
  });

  return (
    <AuthRedirectIfAuthed>
      <div className="space-y-6">
        <header className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-100">
            Welcome back
          </h1>
          <p className="text-sm text-slate-600 dark:text-slate-400">
            Sign in to build and run WorkFlow™ automations.
          </p>
        </header>

        <form className="space-y-4" noValidate onSubmit={onSubmit}>
          <div>
            <label
              htmlFor="email"
              className="block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              Email
            </label>
            <input
              id="email"
              autoComplete="email"
              {...form.register("email")}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-950"
            />
            {form.formState.errors.email ? (
              <p className="mt-1 text-xs text-error">
                {form.formState.errors.email.message}
              </p>
            ) : null}
          </div>

          <div>
            <label
              htmlFor="password"
              className="block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              Password
            </label>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              {...form.register("password")}
              className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-600 dark:bg-slate-950"
            />
            {form.formState.errors.password ? (
              <p className="mt-1 text-xs text-error">
                {form.formState.errors.password.message}
              </p>
            ) : null}
          </div>

          {mfaRequired ? (
            <div>
              <label
                htmlFor="totp"
                className="block text-sm font-medium text-slate-700 dark:text-slate-300"
              >
                Authenticator code
              </label>
              <input
                id="totp"
                inputMode="numeric"
                placeholder="••••••"
                {...form.register("totp_code")}
                className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm tracking-widest dark:border-slate-600 dark:bg-slate-950"
              />
            </div>
          ) : null}

          {formError ? (
            <p className="text-sm text-error" role="alert">
              {formError}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={form.formState.isSubmitting}
            className="w-full rounded-lg bg-brand-600 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-60"
          >
            {form.formState.isSubmitting ? "Signing in…" : "Sign in"}
          </button>
        </form>

        <p className="text-center text-sm text-slate-600 dark:text-slate-400">
          New to EnterpriseGPT?{" "}
          <Link
            href={
              next === "/dashboard" ?
                "/signup"
              : `/signup?next=${encodeURIComponent(next)}`
            }
            className="font-semibold text-brand-700 underline decoration-brand-700/30 underline-offset-2 hover:text-brand-800 dark:text-brand-400 dark:hover:text-brand-300"
          >
            Create an account
          </Link>
        </p>

        <p className="text-center text-xs text-slate-500 dark:text-slate-400">
          <Link href="/" className="underline hover:text-slate-700">
            Back to home
          </Link>
          <span aria-hidden className="mx-2">
            ·
          </span>
          <button
            type="button"
            className="underline hover:text-slate-700 disabled:opacity-50"
            disabled
            title="Self-service recovery ships in Phase 7"
          >
            Forgot password
          </button>
        </p>
      </div>
    </AuthRedirectIfAuthed>
  );
}
