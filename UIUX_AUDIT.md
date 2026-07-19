# CLEAR25 — UI/UX Audit (Phase 0)

_Audited: live site `https://clear25.xyz` (deployed `main`, reflecting the 2026-07-18 paper-source-of-truth commit) + local code on branch `feature/wind-integration`. Desktop 1440px and mobile 375px, signed out._

> **Note on the original brief:** several of the 10 "known issues" were written against an earlier build. This audit records the **current** state — some are already partly fixed, some have shifted, and a few new ones surfaced. Each finding is tagged with the brief's issue number where it maps.

Baseline screenshots captured (in `.playwright-mcp/`): `audit-landing-desktop-full.png`, `audit-landing-mobile-hero.png`, `audit-dashboard-desktop-top.png`, `audit-dashboard-mobile-top.png`, `audit-dashboard-map-desktop.png`, `audit-signin-desktop.png`.

---

## Severity-ranked findings

### CRITICAL — credibility / correctness (fix first)

| # | Finding | Evidence | Brief issue |
|---|---------|----------|-------------|
| C1 | **Zeroed stats still flash `0` on first paint / no-JS / crawlers.** `landing.py` renders the template with **no context** — every headline figure lives only in `data-target` attributes and is written by JS count-up (`animateCount`, landing.html ~2170). Hero (87.5%/100%/24.7h/87h @1661–1675), "by the numbers" strip (36M/479/21/8 @1802–1814), and bento bf-stat (87.5% @2053) all initialize as literal `0`/`0%`/`0h`. Count-up only fires on scroll-into-view, so a warning system shows "0% accuracy" before JS runs. | landing.html:1658–1679, 1799–1818, 2053; landing.py:9 | #2 (still open) |
| C2 | **API docs example payload is geographically wrong.** Example station is **"UST Manila"** with `"city": "Toronto"` and Toronto coordinates; the Overview even says the API covers **"Metro Manila."** Appears in both `/live` and `/stations` examples. | api_docs.html:329, 446–449, 483–486 | #7 (shifted: was "Abbotsford", now Manila) |
| C3 | **Development-breakdown table doesn't add up.** Rule 2 TP=33 + Rule 3 TP=55, but "Combined System" TP=**26**; FP 0+2 vs combined **3**. Two large green headline percentages sit in the same Results section: **87.5%** (2024 accuracy) and **90.9%** (dev precision). | index.html:461–492 | #7 |

### HIGH — core UX of the emergency tool

| # | Finding | Evidence | Brief issue |
|---|---------|----------|-------------|
| H1 | **No design tokens for the 5-level scale.** No `--alert-*` custom properties exist. Level colors come from the backend as raw hex applied **inline** everywhere, and CSS "glow" styling matches on `[style*="#22c55e"]` attribute selectors — fragile (breaks if a hex or letter-case changes). `.badge` has layout but no color; no icons anywhere (color+label only). | style.css:2158–2177; app-render.js:26,84,88,95,105; app-map.js:116,149 | #3, WS3 |
| H2 | **VERY HIGH (`#ef4444`) + white text is ~3.3:1 — fails AA** for normal text. Several surfaces encode severity by **color alone** (stat-worst number, map dots/lines, card borders). | evaluate.py:22; app-render.js:105; app-map.js:155,176 | WS8, "never color alone" |
| H3 | **No async-state system.** `.skeleton` class is defined but **dead** (never referenced). Ad-hoc strings only: "Waiting for data", "Loading live data... or click Run Demo". **No stale-data indicator** (age computed to text but never thresholded), **no retry button** on any error path (one error is swallowed silently, app.js:125). | app-render.js:63,72; index.html:234–237; app.js:98–125; style.css:2180 | #3 |
| H4 | **Mega-page, no real routes.** All six views (dashboard/map/research/feedback/api/billing) ship in one document and toggle via `data-tab` class-swap. **Deep-linking is broken** — `/dashboard/#tab-map` does nothing; no per-view URL, title, or scroll position; research paper + API docs + billing aren't linkable or crawlable as pages. | index.html:52–111; app.js:30–41 | #4 (mechanism is class-swap, not hash) |
| H5 | **City cards lack lead time and health action.** Cards show level + predicted PM2.5 + rule + ECCC/PurpleAir chips, but **no lead-time and no plain-language health action** — the two things a stressed user needs. No per-card last-updated (one global line only). | index.html:173–186; app-render.js:49–99 | WS1 |
| H6 | **Station "table" is not a semantic table.** It's `div`-based `.table-head`/`.table-body` (10 cols: City, ID, Station, Distance, Dir, Tier, PM2.5, Predicted, Level, Lead Time). No `th`/`scope`, **no sorting, no city filter, not CSS-sticky**, row badges colored but row not tinted. Mobile = **column hiding**, not card collapse. | index.html:221–237; style.css:1025–1045, 2339–2346 | #6, WS8 |
| H7 | **No `prefers-reduced-motion` anywhere.** Zero matches in CSS. Infinite marker pulse, staggered card fade-ins, toast slides, hover transforms all run unconditionally. | style.css (none) | WS8, design direction |

### MEDIUM

| # | Finding | Evidence | Brief issue |
|---|---------|----------|-------------|
| M1 | **Landing has no live status.** The "is my air safe" answer is absent from the landing page — under the hero is a static marketing stat grid, no per-city alert strip. | landing.html:1658–1679 | WS2 |
| M2 | **Nav duplicated (landing only).** Desktop nav (landing.html:1596–1623) and mobile menu (1624–1631) are separate hardcoded blocks, link list maintained twice. (Dashboard is fine — single sidebar → bottom bar via CSS.) | landing.html:1596–1631 | #5 |
| M3 | **"Open Dashboard" ×4 on landing** (nav @1613, mobile menu @1630, hero @1643, bottom CTA @2106) — no single primary CTA per section. | landing.html | #9 |
| M4 | **Run Demo ×2, no persistent "Demo data" badge.** Demo vs live is only transient status text; demo readings can be mistaken for live. `/api/live/` can silently serve `demo_preview`. | index.html:190–196, 252–258; app-render.js:134 | #8 |
| M5 | **"Montréal/Montreal" inconsistency.** Landing 100% accented; index.html:383 prose "Montreal, …Quebec" plain; api_docs user-visible "Montreal". | index.html:383; api_docs.html:491 | #10 |
| M6 | **No sticky TOC in research view; summary stats have no tooltips.** research-nav scrolls away; "Worst Predicted / Stations Reporting / Earliest Warning" have no explanatory `title`/info. | index.html:286–294, 200–217 | WS5, WS1 |
| M7 | **API docs: no copy buttons, no line numbers; hand-rolled span highlighting.** Billing crypto checkout redirects immediately with no pre-redirect step restating "what happens next" + 30-day term (that copy exists only passively lower on the page). | api_docs.html:333–539; billing.html:635–668 | WS7 |
| M8 | **Rule 1 distance disagrees:** 100–650 km (landing @1860, methodology @371) vs 100–600 km (dev table @463) vs "100–600+ km" (taglines). | landing/index | #7 |

### LOW

| # | Finding | Evidence |
|---|---------|----------|
| L1 | Billing badge has no `default:` fallback — empty `current_plan` → blank badge + unmatched `badge-` class (not a raw-`{{}}` leak; the brief's `default:"FREE"` string does **not** exist anymore). | billing.html:425 |
| L2 | No skip link; interactive controls use `all:unset` relying on one global `:focus-visible` ring; some inputs `outline:none` with border-only focus; no semantic landmarks in layout. | style.css:93–97, 148, 575–579 |
| L3 | Sign-in is a near-default allauth page, visually disconnected from the product. | /accounts/login/ |
| L4 | Alert bands overlap at integer edges (MODERATE max=60/HIGH min=60; 80; 120) — cosmetic in copy; left intact in `evaluate.py` (validated core). | evaluate.py:16–26 |

**Already resolved vs. the brief (no action):** billing `{{ …|default:"FREE" }}` leak (gone), old "97.8% precision" / "440 stations" conflicts (gone), dashboard nav duplication (never existed there). No template-syntax leaks found on any audited page.

---

## Authoritative 5-level scale (from `evaluate.py`, the source of truth)

| Level | Band (µg/m³) | Current bg | Current text | AA on current pair? |
|-------|-------------|-----------|-------------|---------------------|
| LOW | 0–20 | `#22c55e` | black | ✅ ~ 7:1 |
| MODERATE | 21–60 | `#eab308` | black | ✅ ~ 10:1 |
| HIGH | 60–80 | `#f97316` | black | ✅ ~ 7:1 |
| VERY HIGH | 80–120 | `#ef4444` | white | ❌ ~ 3.3:1 |
| EXTREME | 120+ | `#7f1d1d` | white | ✅ ~ 9:1 |

Health messages and lead-time ranges are also defined in `evaluate.py`. **Constraint honored:** the token work will not change bands, thresholds, or detection logic — only the presentation-layer color/text pairings (fixing VERY HIGH) and adding icons + a documented badge component.

---

## Prioritized implementation plan

1. **Rendering + correctness (C1–C3, M5, M8):** server-render all stats (progressive-enhancement count-up on top); fix the Manila API example; reconcile the dev-table arithmetic and Rule-1 distance; normalize Montréal.
2. **Design tokens + badge component (H1, H2):** define `--alert-*` custom properties once with AA text pairings + icons; ship one badge partial; replace inline/attribute-selector color usage; expose tokens to JS via data attributes rather than raw hex.
3. **Async-state system (H3):** reusable skeleton → content → empty → error-with-retry → stale-banner; apply to city cards, table, map, feedback, key lists; add stale threshold on data age.
4. **IA + nav (H4, M2):** real routes for research / API / billing (or lazy views) with titles + redirects for `#tab-*`; consolidate landing nav into one responsive component.
5. **Dashboard core (H5, H6, M4, M6):** add lead time + health action + last-updated to city cards; make the table a semantic `<table>` with sort/filter/sticky header/row coloring and mobile card collapse; persistent Demo badge; stat tooltips.
6. **Landing (M1, M3):** live per-city status strip near hero; one primary CTA per section; case-study timeline; tighten copy.
7. **Research / feedback / API / billing (M6, M7):** sticky TOC + readable measure; feedback empty/optimistic/validation states; copy buttons + corrected examples + reveal-once keys; pre-redirect checkout step.
8. **Accessibility + performance (H7, L1–L2):** `prefers-reduced-motion`, skip link, landmarks, focus, 44px targets; defer non-active JS; verify LCP on throttled mobile.
