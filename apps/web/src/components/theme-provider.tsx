"use client";

import { ReactNode, useEffect } from "react";

import { useUiStore } from "@/stores/uiStore";

export function ThemeProvider({ children }: { children: ReactNode }) {
  const theme = useUiStore((s) => s.theme);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  return children;
}

export function useThemeToggle() {
  const theme = useUiStore((s) => s.theme);
  const toggleTheme = useUiStore((s) => s.toggleTheme);
  const setTheme = useUiStore((s) => s.setTheme);
  return { theme, toggleTheme, setTheme, resolvedTheme: theme };
}
