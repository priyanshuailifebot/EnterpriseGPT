"use client";

import * as RadixDropdownMenu from "@radix-ui/react-dropdown-menu";
import {
  ChevronDown,
  LayoutDashboard,
  Menu,
  MessageSquare,
  PanelsTopLeft,
  Plug,
  Settings2,
  Sparkles,
  SunMedium,
  Upload,
  Workflow,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { PropsWithChildren, useMemo } from "react";

import { cn } from "@/lib/utils";
import { useThemeToggle } from "@/components/theme-provider";
import { useAuthStore } from "@/stores/authStore";
import { useUiStore } from "@/stores/uiStore";
import type { Permission } from "@/types/api";

const nav: {
  href: string;
  label: string;
  icon: typeof Workflow;
  perm?: Permission;
}[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  {
    href: "/workflows",
    label: "Workflows",
    icon: Workflow,
    perm: "workflow:read",
  },
  {
    href: "/chat",
    label: "Chat",
    icon: MessageSquare,
    perm: "workflow:read",
  },
  {
    href: "/documents",
    label: "Documents",
    icon: Upload,
    perm: "document:read",
  },
  {
    href: "/integrations",
    label: "Integrations",
    icon: Plug,
    perm: "workflow:read",
  },
  {
    href: "/analytics",
    label: "Analytics",
    icon: PanelsTopLeft,
    perm: "analytics:read",
  },
  {
    href: "/settings",
    label: "Settings",
    icon: Settings2,
    perm: "workspace:manage",
  },
];

export function AppShell({ children }: PropsWithChildren) {
  const pathname = usePathname();
  const sidebarOpen = useUiStore((s) => s.sidebarOpen);
  const setSidebarOpen = useUiStore((s) => s.setSidebarOpen);
  const toggleSidebar = useUiStore((s) => s.toggleSidebar);
  const { toggleTheme } = useThemeToggle();
  const user = useAuthStore((s) => s.user);
  const workspaces = user?.workspaces ?? [];
  const workspaceId = useAuthStore((s) => s.workspaceId);
  const setWs = useAuthStore((s) => s.setWorkspaceId);
  const logout = useAuthStore((s) => s.logout);
  const hasPerm = useAuthStore((s) => s.hasPermission);

  const visibleNav = useMemo(
    () => nav.filter((item) => !item.perm || hasPerm(item.perm)),
    [hasPerm],
  );

  const hasWs = !!(
    workspaceId &&
    workspaces.some((w) => w.workspace_id === workspaceId)
  );

  return (
    <div className="flex min-h-screen bg-slate-50 dark:bg-slate-950">
      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-60 flex-col border-r border-slate-200 bg-white px-3 py-4 transition-transform duration-200 dark:border-slate-800 dark:bg-slate-900",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <Link
          href="/dashboard"
          className="mb-6 flex items-center gap-2 px-2 font-semibold text-slate-900 dark:text-slate-100"
        >
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-600 text-white">
            <Sparkles className="h-5 w-5" />
          </span>
          EnterpriseGPT
        </Link>

        <nav className="flex flex-1 flex-col gap-0.5">
          {visibleNav.map((item) => {
            const active =
              pathname === item.href || pathname.startsWith(`${item.href}/`);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                className={cn(
                  "flex items-center gap-2 rounded-lg px-2 py-2 text-sm transition-colors",
                  active ?
                    "bg-brand-50 font-medium text-brand-800 dark:bg-brand-950/60 dark:text-brand-100"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800/80",
                )}
                onClick={() => setSidebarOpen(false)}
              >
                <Icon className="h-4 w-4 shrink-0" />
                {item.label}
              </Link>
            );
          })}
        </nav>

        {!hasWs ? (
          <p className="mt-auto rounded-lg bg-warning/15 px-2 py-2 text-xs text-amber-900 dark:text-amber-100">
            No workspace memberships found. Ask an admin to invite you.
          </p>
        ) : null}
      </aside>

      {sidebarOpen ? (
        <button
          type="button"
          aria-label="Close menu"
          className="fixed inset-0 z-30 bg-black/40"
          onClick={() => setSidebarOpen(false)}
        />
      ) : null}

      <div className="flex min-h-screen flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-slate-200 bg-white/90 px-3 py-2 backdrop-blur dark:border-slate-800 dark:bg-slate-900/90">
          <button
            type="button"
            className="inline-flex rounded-lg p-2 hover:bg-slate-100 dark:hover:bg-slate-800"
            onClick={() => toggleSidebar()}
            aria-label="Open menu"
          >
            <Menu className="h-5 w-5" />
          </button>

          <div className="flex flex-1 items-center gap-2">
            {workspaces.length > 0 ? (
              <div className="relative">
                <label className="sr-only">Workspace</label>
                <select
                  value={workspaceId ?? ""}
                  onChange={(e) => setWs(e.target.value || null)}
                  className="max-w-[200px] cursor-pointer truncate rounded-lg border border-slate-200 bg-white py-2 pl-2 pr-8 text-sm dark:border-slate-700 dark:bg-slate-900 sm:max-w-xs"
                >
                  {workspaces.map((ws) => (
                    <option key={ws.workspace_id} value={ws.workspace_id}>
                      {ws.workspace_name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
          </div>

          <button
            type="button"
            onClick={() => toggleTheme()}
            className="inline-flex rounded-lg p-2 text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800"
            aria-label="Toggle theme"
          >
            <SunMedium className="h-5 w-5" />
          </button>

          <RadixDropdownMenu.Root>
            <RadixDropdownMenu.Trigger className="inline-flex items-center gap-1 rounded-lg border border-slate-200 px-3 py-1.5 text-sm dark:border-slate-700 dark:hover:bg-slate-800">
              <span className="max-w-[140px] truncate">
                {user?.full_name ?? user?.email}
              </span>
              <ChevronDown className="h-4 w-4" />
            </RadixDropdownMenu.Trigger>
            <RadixDropdownMenu.Portal>
              <RadixDropdownMenu.Content className="z-50 min-w-[220px] rounded-xl border border-slate-200 bg-white p-1 shadow-xl dark:border-slate-700 dark:bg-slate-900">
                <RadixDropdownMenu.Item className="cursor-pointer rounded-lg px-3 py-2 text-sm outline-none hover:bg-slate-100 dark:hover:bg-slate-800">
                  <Link href="/settings">Workspace settings</Link>
                </RadixDropdownMenu.Item>
                <RadixDropdownMenu.Separator className="my-1 h-px bg-slate-200 dark:bg-slate-700" />
                <RadixDropdownMenu.Item
                  className="cursor-pointer rounded-lg px-3 py-2 text-sm outline-none hover:bg-red-50 dark:hover:bg-red-950"
                  onSelect={() =>
                    void logout().then(() => {
                      window.location.href = "/login";
                    })
                  }
                >
                  Sign out
                </RadixDropdownMenu.Item>
              </RadixDropdownMenu.Content>
            </RadixDropdownMenu.Portal>
          </RadixDropdownMenu.Root>
        </header>

        <main className="flex-1 p-4 md:p-6">{children}</main>
      </div>
    </div>
  );
}
