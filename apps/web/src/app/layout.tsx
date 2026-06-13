import type { Metadata, Viewport } from "next";

import { Providers } from "./providers";
import "@/styles/globals.css";

export const metadata: Metadata = {
  title: {
    default: "EnterpriseGPT",
    template: "%s · EnterpriseGPT",
  },
  description:
    "Turn natural-language commands into agentic workflows. Powered by Dynamiq.",
  applicationName: "EnterpriseGPT",
  authors: [{ name: "EnterpriseGPT Team" }],
  keywords: [
    "EnterpriseGPT",
    "Dynamiq",
    "Agentic Workflows",
    "AI Platform",
    "Automation",
  ],
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FFFFFF" },
    { media: "(prefers-color-scheme: dark)", color: "#0F172A" },
  ],
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen font-sans">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
