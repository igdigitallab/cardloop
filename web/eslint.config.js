import js from '@eslint/js'
import tseslint from '@typescript-eslint/eslint-plugin'
import tsparser from '@typescript-eslint/parser'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import globals from 'globals'

export default [
  { ignores: ['dist', '.eslintrc.cjs'] },
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsparser,
      parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
      globals: { ...globals.browser, ...globals.es2020 },
    },
    plugins: {
      '@typescript-eslint': tseslint,
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...js.configs.recommended.rules,
      ...tseslint.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Allow any for now — codebase has legacy any usage
      '@typescript-eslint/no-explicit-any': 'off',
      // TypeScript handles undefined checks — no-undef creates false positives for
      // TS types (RequestInit, React JSX) that are available via tsconfig lib/jsx settings
      'no-undef': 'off',
      // Empty catch blocks are intentional throughout codebase (silent error suppression)
      'no-empty': ['error', { allowEmptyCatch: true }],
      // react-hooks/exhaustive-deps: warn, not error — intentional omissions are documented
      'react-hooks/exhaustive-deps': 'warn',
      // Unused vars: allow _ prefix for intentional ignores
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
    },
  },
]
