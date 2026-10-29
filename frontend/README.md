# Frontend (review dashboard)

Scaffolded in **Stage 6**. Planned stack: **React + TypeScript + Vite** (SPA).

Why Vite SPA rather than Next.js: the backend already owns all server logic and
secrets; a static SPA is simpler to secure (no second server runtime holding
credentials), can be served by the FastAPI container or a static host, and is
enough for a single-user review dashboard.

Security rules:
- The frontend never receives OAuth tokens, API keys, or client secrets.
- All YouTube and LLM operations go through the backend API.
- Access requires sign-in with an allow-listed Google account (or IAP in Cloud Run).

Planned screens: channel status · video list & pipeline status · per-language
status grid · side-by-side metadata editor · subtitle preview/editor (RTL-aware
for Arabic) · approve/reject · per-language publishing settings · history & errors.

