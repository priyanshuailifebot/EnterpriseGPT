import { create } from "zustand";

export type ThemeMode = "light" | "dark";

type UiState = {
  theme: ThemeMode;
  toggleTheme: () => void;
  setTheme: (t: ThemeMode) => void;
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;
  activeModal: string | null;
  setModal: (id: string | null) => void;
};

const THEME_LS = "egpt_ui_theme_class";

export const useUiStore = create<UiState>((set) => ({
  theme: "light",
  toggleTheme: () =>
    set((s) => {
      const next: ThemeMode = s.theme === "dark" ? "light" : "dark";
      if (typeof window !== "undefined") {
        localStorage.setItem(THEME_LS, next);
        document.documentElement.classList.toggle("dark", next === "dark");
      }
      return { theme: next };
    }),
  setTheme: (t) =>
    set(() => {
      if (typeof window !== "undefined") {
        localStorage.setItem(THEME_LS, t);
        document.documentElement.classList.toggle("dark", t === "dark");
      }
      return { theme: t };
    }),
  sidebarOpen: false,
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  toggleSidebar: () =>
    set((state) => ({ sidebarOpen: !state.sidebarOpen })),
  activeModal: null,
  setModal: (id) => set({ activeModal: id }),
}));

export function hydrateThemeClass() {
  if (typeof window === "undefined") return;
  const stored = localStorage.getItem(THEME_LS) as ThemeMode | null;
  const mode =
    stored === "dark" || stored === "light" ? stored : (
      window.matchMedia("(prefers-color-scheme: dark)").matches ?
        "dark"
      : "light"
    );
  useUiStore.getState().setTheme(mode);
}
