"use client";

import { ClerkProvider } from "@clerk/nextjs";
import { useTheme } from "next-themes";
import { useEffect, useState, type ReactNode } from "react";

import { clerkBaseThemeFor } from "@/lib/clerk-theme.mjs";

export function ClerkThemeProvider({ children }: { children: ReactNode }): ReactNode {
  const [mounted, setMounted] = useState(false);
  const { resolvedTheme } = useTheme();

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) {
    return null;
  }

  return (
    <ClerkProvider appearance={{ baseTheme: clerkBaseThemeFor(resolvedTheme) }}>{children}</ClerkProvider>
  );
}
