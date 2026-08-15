// Test environment setup.
//
// jsdom doesn't implement the handful of browser APIs PatternFly's
// responsive components reach for, so they're stubbed here. Without
// matchMedia in particular, every page that mounts the layout throws.

import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

if (!window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  });
}

if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// jsdom has no layout engine, so scrollTo is a no-op rather than a throw.
window.scrollTo = window.scrollTo ?? (() => {});
