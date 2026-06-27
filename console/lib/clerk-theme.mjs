import { dark } from "@clerk/themes";

export function clerkBaseThemeFor(resolvedTheme) {
  return resolvedTheme === "dark" ? dark : undefined;
}
