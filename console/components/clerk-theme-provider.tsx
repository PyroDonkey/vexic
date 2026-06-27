"use client";

import { ClerkProvider } from "@clerk/nextjs";
import { useTheme } from "next-themes";
import { type ReactNode } from "react";

import { clerkBaseThemeFor } from "@/lib/clerk-theme.mjs";

export function ClerkThemeProvider({ children }: { children: ReactNode }): ReactNode {
  const { resolvedTheme } = useTheme();
  const baseTheme = clerkBaseThemeFor(resolvedTheme);

  return (
    <ClerkProvider appearance={baseTheme ? { baseTheme } : undefined}>{children}</ClerkProvider>
  );
}
