"use client"

import { useTheme } from "next-themes"
import { Toaster as Sonner, type ToasterProps } from "sonner"

function Toaster({ ...props }: ToasterProps) {
  const { theme = "system" } = useTheme()

  return <Sonner richColors theme={theme as ToasterProps["theme"]} {...props} />
}

export { Toaster }
