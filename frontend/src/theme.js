// Theme handling for the PatternFly 6 shell.
//
// PatternFly 6 switches to dark by putting the `pf-v6-theme-dark` class on
// the root element — the compiled CSS targets `:root:where(.pf-v6-theme-dark)`.
// There is no data-attribute form, so this is the whole mechanism.
//
// Default is LIGHT, matching the OpenShift web console. The operator's
// choice persists in localStorage; "system" follows the OS setting.

const STORAGE_KEY = "virtvalidate.theme";
const DARK_CLASS = "pf-v6-theme-dark";

// "light" | "dark" | "system"
export const THEMES = ["light", "dark", "system"];

function prefersDark() {
  return (
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
  );
}

/** The operator's stored preference, or "light" if never set. */
export function getStoredTheme() {
  try {
    const stored = window.localStorage?.getItem(STORAGE_KEY);
    return THEMES.includes(stored) ? stored : "light";
  } catch {
    // Private-browsing / disabled storage — fall back to the default
    // rather than breaking the whole shell.
    return "light";
  }
}

/** Resolve a preference to the theme actually rendered. */
export function resolveTheme(theme) {
  return theme === "system" ? (prefersDark() ? "dark" : "light") : theme;
}

/** Apply a preference to <html> and persist it. */
export function applyTheme(theme) {
  const resolved = resolveTheme(theme);
  document.documentElement.classList.toggle(DARK_CLASS, resolved === "dark");
  try {
    window.localStorage?.setItem(STORAGE_KEY, theme);
  } catch {
    // Persisting is best-effort; the class is already applied.
  }
  return resolved;
}

/**
 * Apply the stored theme before React mounts, so there's no flash of the
 * wrong theme on load. Called from main.jsx.
 */
export function initTheme() {
  const theme = getStoredTheme();
  applyTheme(theme);
  return theme;
}

/**
 * Watch the OS setting and re-apply while the preference is "system".
 * Returns an unsubscribe function.
 */
export function subscribeToSystemTheme(onChange) {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return () => {};
  }
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const handler = () => onChange(prefersDark() ? "dark" : "light");
  mq.addEventListener("change", handler);
  return () => mq.removeEventListener("change", handler);
}
