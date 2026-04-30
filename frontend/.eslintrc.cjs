// VirtValidate frontend ESLint config.
// Goal: catch obvious bugs + common JS security pitfalls without drowning
// reviewers in style nits. Keep this lean — Prettier-like formatting churn
// is intentionally not enforced here.

module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  parserOptions: {
    ecmaVersion: 2022,
    sourceType: "module",
    ecmaFeatures: { jsx: true },
  },
  settings: { react: { version: "18" } },
  extends: [
    "eslint:recommended",
    "plugin:react/recommended",
    "plugin:react-hooks/recommended",
    "plugin:security/recommended-legacy",
  ],
  plugins: ["react", "react-hooks", "security"],
  rules: {
    // React 17+ JSX transform — no `import React` boilerplate needed.
    "react/react-in-jsx-scope": "off",
    "react/prop-types": "off",
    // Allow leading underscores for intentionally unused parameters.
    "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    // security plugin signal-to-noise: a handful of rules tend to false-fire
    // on innocuous code; downgrade those to warnings rather than disable.
    "security/detect-object-injection": "warn",
    "security/detect-non-literal-regexp": "warn",
  },
  ignorePatterns: ["dist", "node_modules", "lighthouserc.cjs", ".eslintrc.cjs"],
};
