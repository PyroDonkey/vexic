import { dark } from "@clerk/themes";

/**
 * @param {string | undefined} resolvedTheme
 * @returns {typeof dark | undefined}
 */
export function clerkBaseThemeFor(resolvedTheme) {
  return resolvedTheme === "dark" ? dark : undefined;
}
