// VirtValidate frontend ESLint flat config (ESLint 9+).
//
// Migrated from .eslintrc.cjs per the official guide:
//   https://eslint.org/docs/latest/use/configure/migration-guide
//
// Goal hasn't changed: catch obvious bugs + common JS security pitfalls
// without drowning reviewers in style nits. Prettier-like formatting
// churn is intentionally not enforced here.

import js from "@eslint/js";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";
import security from "eslint-plugin-security";
import globals from "globals";

export default [
  // Files we should never lint.
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "eslint.config.js",
      "lighthouserc.cjs",
      "vite.config.js",
    ],
  },

  // Base recommended rules from ESLint core.
  js.configs.recommended,

  // Project source files.
  {
    files: ["src/**/*.{js,jsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: { ...globals.browser, ...globals.node },
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: { react: { version: "18" } },
    plugins: {
      react,
      "react-hooks": reactHooks,
      security,
    },
    // Compose recommended preset rule sets manually — flat config doesn't
    // pull rules transitively from `extends:` like the legacy format did.
    rules: {
      ...react.configs.recommended.rules,
      ...react.configs["jsx-runtime"].rules,
      ...reactHooks.configs.recommended.rules,
      ...security.configs.recommended.rules,

      // React 17+ JSX transform — no `import React` boilerplate needed.
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",

      // Allow leading underscores for intentionally unused parameters.
      "no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],

      // security/detect-object-injection fires on any non-literal-key
      // bracket access (obj[k]) — practically unusable in any non-trivial
      // JS codebase. Disabled here to keep the rest of the security
      // plugin's signal usable. detect-non-literal-regexp is downgraded
      // to a warning since it occasionally surfaces real ReDoS risks.
      "security/detect-object-injection": "off",
      "security/detect-non-literal-regexp": "warn",
    },
  },
];
