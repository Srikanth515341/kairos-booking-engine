# Kairos frontend

React + TypeScript (Vite) — see the repo root [`README.md`](../README.md) for how to run
this alongside the backend, and [`CLAUDE.md`](../CLAUDE.md) for what's built.

```bash
npm install
cp .env.example .env
npm run dev          # http://localhost:5173

npm run typecheck    # tsc -b, strict mode
npm run lint         # eslint . (flat config, typescript-eslint strict + stylistic)
npm run format:check # prettier --check .
npm run test         # vitest run
npm run build        # tsc -b && vite build
```

## Layout

```
src/
├── api/       # the HTTP client — idempotency-key + X-Request-Id generation,
│              # 503-as-retry, error.code-based error types (client.ts, errors.ts)
├── auth/      # session state (AuthProvider/useAuth), dev-mock + real-OIDC-redirect
│              # login, ProtectedRoute
├── layout/    # app shell (nav bar, page frame)
├── pages/     # route-level components
└── test/      # Vitest setup (jest-dom matchers)
```
