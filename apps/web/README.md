# EnterpriseGPT Web

Next.js 14 (App Router) frontend for EnterpriseGPT.

## Local development

```bash
pnpm install
pnpm dev
# open http://localhost:3000
```

## Project layout

```
apps/web/
├── src/
│   ├── app/              # App Router routes (layout, page, providers)
│   ├── components/       # Reusable UI components
│   ├── hooks/            # Custom React hooks
│   ├── lib/              # API client, utilities
│   ├── stores/           # Zustand stores
│   └── styles/           # Global Tailwind styles
├── public/
├── tailwind.config.ts
├── next.config.mjs
├── tsconfig.json
└── Dockerfile
```

## Useful scripts

```bash
pnpm dev          # dev server with HMR
pnpm build        # production build (Next.js standalone)
pnpm lint         # ESLint via next lint
pnpm type-check   # tsc --noEmit
pnpm test         # vitest unit tests
pnpm test:e2e     # Playwright tests
```
