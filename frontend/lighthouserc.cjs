// Lighthouse CI config — gates accessibility at WCAG-AA-friendly 0.9 to
// match the federal-friendly stance documented in README.md. Performance
// and SEO are reported but not gated; we only fail the build on a11y.
module.exports = {
  ci: {
    collect: {
      // Build is produced by `npm run build`; preview serves dist/ on 4173.
      startServerCommand: "npm run preview -- --port 4173 --strictPort",
      startServerReadyPattern: "Local:",
      url: ["http://localhost:4173/"],
      numberOfRuns: 1,
      settings: {
        // Headless Chrome is fine for a11y audits; sandboxing gets in the
        // way of Ubuntu runners, so disable it here only.
        chromeFlags: "--no-sandbox --headless",
      },
    },
    assert: {
      assertions: {
        "categories:accessibility": ["error", { minScore: 0.9 }],
      },
    },
    upload: {
      // No credentials needed; produces a one-time public report URL the
      // workflow logs so reviewers can click straight into the run.
      target: "temporary-public-storage",
    },
  },
};
