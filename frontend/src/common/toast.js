// Shared react-hot-toast configuration.
//
// Every component used to declare its own local `TOAST_OPTS` (10 near-
// identical copies with drifting colors). This is the single source.
//
// Colors come from PatternFly tokens rather than hex literals so toasts
// follow the light/dark theme instead of pinning one palette.

export const TOAST_OPTS = {
  duration: 5000,
  style: {
    background: "var(--pf-t--global--background--color--floating--default)",
    color: "var(--pf-t--global--text--color--regular)",
    border: "1px solid var(--pf-t--global--border--color--default)",
    borderRadius: "var(--pf-t--global--border--radius--small)",
    boxShadow: "var(--pf-t--global--box-shadow--md)",
    fontSize: "var(--pf-t--global--font--size--body--default)",
    maxWidth: 480,
  },
};

export const TOASTER_PROPS = {
  position: "bottom-right",
  toastOptions: TOAST_OPTS,
};
