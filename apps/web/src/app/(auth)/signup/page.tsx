"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { UserPlus } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { AuthRedirectIfAuthed } from "@/components/auth/auth-guard";
import { getErrorMessage } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

const signupSchema = z
  .object({
    full_name: z
      .string()
      .trim()
      .min(1, "Enter your name")
      .max(255, "Name is too long"),
    email: z.string().email("Enter a valid email"),
    password: z
      .string()
      .min(8, "Use at least 8 characters")
      .max(128, "Password is too long"),
    confirmPassword: z.string().min(1, "Confirm your password"),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });

type SignupFields = z.infer<typeof signupSchema>;

const inputClass =
  "mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm shadow-sm outline-none transition " +
  "placeholder:text-slate-400 focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 " +
  "dark:border-slate-600 dark:bg-slate-950 dark:placeholder:text-slate-500";

export default function SignupPage() {
  const router = useRouter();
  const qp = useSearchParams();
  const register = useAuthStore((s) => s.register);
  const [formError, setFormError] = useState<string | null>(null);

  const nextRaw = qp.get("next");
  const next =
    nextRaw && nextRaw.startsWith("/") && !nextRaw.startsWith("//") ?
      nextRaw
    : "/dashboard";

  const form = useForm<SignupFields>({
    resolver: zodResolver(signupSchema),
    defaultValues: {
      full_name: "",
      email: "",
      password: "",
      confirmPassword: "",
    },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    setFormError(null);
    try {
      await register({
        email: values.email.trim(),
        password: values.password,
        full_name: values.full_name.trim(),
      });
      router.replace(next);
    } catch (err) {
      setFormError(getErrorMessage(err));
    }
  });

  return (
    <AuthRedirectIfAuthed>
      <div className="space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-600 text-white shadow-md">
            <UserPlus className="h-6 w-6" aria-hidden />
          </div>
          <header className="space-y-1">
            <h1 className="text-2xl font-semibold tracking-tight text-slate-900 dark:text-slate-100">
              Create your account
            </h1>
            <p className="text-sm text-slate-600 dark:text-slate-400">
              Get a workspace and start building WorkFlow™ automations.
            </p>
          </header>
        </div>

        <form className="space-y-4" noValidate onSubmit={onSubmit}>
          <div>
            <label
              htmlFor="full_name"
              className="block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              Full name
            </label>
            <input
              id="full_name"
              autoComplete="name"
              placeholder="Ada Lovelace"
              {...form.register("full_name")}
              className={inputClass}
            />
            {form.formState.errors.full_name ? (
              <p className="mt-1 text-xs text-error">
                {form.formState.errors.full_name.message}
              </p>
            ) : null}
          </div>

          <div>
            <label
              htmlFor="email"
              className="block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              Work email
            </label>
            <input
              id="email"
              autoComplete="email"
              type="email"
              placeholder="you@company.com"
              {...form.register("email")}
              className={inputClass}
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
              autoComplete="new-password"
              placeholder="At least 8 characters"
              {...form.register("password")}
              className={inputClass}
            />
            {form.formState.errors.password ? (
              <p className="mt-1 text-xs text-error">
                {form.formState.errors.password.message}
              </p>
            ) : null}
          </div>

          <div>
            <label
              htmlFor="confirmPassword"
              className="block text-sm font-medium text-slate-700 dark:text-slate-300"
            >
              Confirm password
            </label>
            <input
              id="confirmPassword"
              type="password"
              autoComplete="new-password"
              {...form.register("confirmPassword")}
              className={inputClass}
            />
            {form.formState.errors.confirmPassword ? (
              <p className="mt-1 text-xs text-error">
                {form.formState.errors.confirmPassword.message}
              </p>
            ) : null}
          </div>

          {formError ? (
            <p className="text-sm text-error" role="alert">
              {formError}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={form.formState.isSubmitting}
            className="w-full rounded-lg bg-brand-600 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-brand-700 disabled:opacity-60"
          >
            {form.formState.isSubmitting ? "Creating account…" : "Create account"}
          </button>
        </form>

        <p className="text-center text-sm text-slate-600 dark:text-slate-400">
          Already have an account?{" "}
          <Link
            href={next === "/dashboard" ? "/login" : `/login?next=${encodeURIComponent(next)}`}
            className="font-semibold text-brand-700 underline decoration-brand-700/30 underline-offset-2 hover:text-brand-800 dark:text-brand-400 dark:hover:text-brand-300"
          >
            Sign in
          </Link>
        </p>

        <p className="text-center text-xs text-slate-500 dark:text-slate-400">
          <Link href="/" className="underline hover:text-slate-700 dark:hover:text-slate-300">
            Back to home
          </Link>
        </p>
      </div>
    </AuthRedirectIfAuthed>
  );
}
