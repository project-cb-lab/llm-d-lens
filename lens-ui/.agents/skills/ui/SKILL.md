---
name: ui
description: Use when changing React pages, components, hooks, browser API clients, UI state, Tailwind styles or charts in this repository.
---

# UI

Every UI change in Lens goes through this contract. It exists so that humans and
agents produce the same UI the same way: primitives from `src/components/ui/`,
colors from theme tokens, charts from the shared chart layer. If you follow this
file, a new dashboard needs zero visual review round-trips on basics.

## Scope and shared behavior

Follow [workflow](../workflow/SKILL.md) for discovery,
registration and verification. Paths below are relative to the repository root.
Before adding request, polling, form, pagination, clipboard or download behavior,
inspect `src/hooks/`, `src/api/httpClient.js`, `src/utils/` and the capability map.
Keep browser transport separate from page-specific presentation and domain rules.
When an API contract changes, also apply [backend](../backend/SKILL.md).
Validate affected callers, loading/error/empty states and effect cleanup. Use the
relevant existing JS tests and `npm run build`; follow `CONTRIBUTING.md` for checks.

## File references and browser data

Follow the [storage scheme](../../../docs/design/storage-layout.md) when displaying,
uploading or downloading task files. Use backend-provided IDs/references and
existing domain download APIs; do not build server filesystem paths in the UI.
Browser downloads, in-memory conversations and recordings retain their existing
behavior unless the user requests a persistence change.

## Shared file placement and naming

Use these conventions for new shared frontend files. Extend an existing module
before introducing a new entry point.

| Type | Directory | Filename | Export |
| --- | --- | --- | --- |
| UI primitive | `src/components/ui/` | `PascalCase.jsx` | PascalCase component |
| Chart component | `src/components/ui/charts/` | `PascalCase.jsx` | PascalCase component |
| Shared presentation composition | `src/components/shared/` | `PascalCase.jsx` | PascalCase component |
| React Hook | `src/hooks/` | `usePascalCase.js` | Matching `usePascalCase` function |
| React-independent utility or browser operation | `src/utils/` | Responsibility-based `camelCase.js` | camelCase function |
| HTTP client | `src/api/` | `<scope>Client.js` | camelCase function |
| Domain logic | `src/features/<domain>/` or existing domain directory | Responsibility-based `camelCase.js` | camelCase function |
| Domain component | `src/components/<Domain>/` | `PascalCase.jsx` | PascalCase component |
| Test | Beside the tested module | `<filename>.test.js`, `.test.jsx` or `.test.ts` | Existing test conventions |

Match a component filename to its primary component export. Hooks own React
state and lifecycle; utilities must not import React or pages. Keep existing
`*Backend.js` clients in their domain; do not create a second implementation in
`src/api/` merely to match a directory convention. Preserve the language and
extension of existing modules; this rule does not require a JS/TS migration.

Examples: `FormError.jsx` / `FormError`, `useClipboard.js` / `useClipboard`,
`pagination.js` / `paginate`, `httpClient.js` / `requestJson`.
The human-facing reference is the
[shared code guide](../../../docs/refactoring/shared-code-guide.md).

## When to use

- Adding or modifying any JSX markup, Tailwind classes, colors, or chart styling.
- Creating a new dashboard or page (see the recipe at the bottom).
- Reviewing a PR that touches `src/components/` or `src/index.css`.

## The rules

1. **Use the primitives.** Buttons, badges/status chips, modals, form fields,
   panels, stat/KPI cards, empty states, spinners, page headers, toggle groups,
   chart containers/axes/tooltips come from `src/components/ui/`. Never
   re-implement one inline. If a variant you need is missing, extend the
   primitive (new `variant`/`tone`/`size`) in the same PR — don't fork it.
2. **Colors come from tokens or the chart palette.** No new hex literals in
   `src/components/` outside `src/components/ui/`. No new per-dashboard color
   arrays — chart series colors come from `CHART_SERIES`.
   Surface/ink colors use the `theme-*` token classes (below).
3. **Extract on second use.** Markup repeated in two places becomes a primitive
   (or a variant of one) in `src/components/ui/` in the same PR. Never
   copy-paste styled JSX between components.
4. **Both themes, always.** Every primitive and screen must render correctly in
   light and dark mode. Prefer token classes (theme-handled once, in
   `src/index.css`); where a tint is needed, pair light + `dark:` classes.
   Never a light-only or dark-only component. (The app currently forces dark
   mode in `App.jsx` — that does not exempt components from light support.)
5. **Class composition uses `cn()`** from `src/utils/cn.js` — never template
   literals with `${}` for conditional classes.
6. **Behavior-free styling changes.** A style refactor never changes state,
   props, data flow, or copy. If you need both, separate the commits.

## Hardware-driven UI

Vendor/hardware values are data, not constants. Accelerator tabs and vendor
labels, which metrics/charts render, and per-vendor defaults (e.g. the
model-server runtime image) come from the hardware profile the backend reports —
`GET /api/v1/hardware/capabilities` and the cluster overview `hardware` summary —
through `ClusterMonitoringStack/hardwareProfilesBackend.js` and the shared
helpers in `benchmark-results/acceleratorDisplay.js`
(`acceleratorVariantForHardware`, `runtimeImageForHardware`,
`DEFAULT_RUNTIME_IMAGES`).

- Never hardcode a vendor list, an `'xpu'`/`'gpu'` literal default, a per-vendor
  image, or a device/metric name in a component.
- Derive the default from the connected hardware; on unknown hardware keep a
  neutral value — never another vendor's default.
- When hardware changes (e.g. the selected cluster), realign only between the
  known per-vendor values so a user-entered or custom value survives.
- Render a section/option only when its profile declares the data (hide telemetry
  the profile does not expose); do not hardcode supported vendors in a `<select>`.
- A per-vendor default map is acceptable only when it is selected by discovered
  hardware and has a neutral fallback; extend the profile rather than adding
  entries blindly. Follow [the hardware rule](../../../AGENTS.md#hardware-is-profile-driven-never-hardcoded).

## Periodic refresh

Pages that show server state must keep it fresh on a timer — never require a full
page reload to see changes made elsewhere.

- Use `usePolling` (`src/hooks/usePolling.js`): it clears the interval on unmount,
  pauses while the tab is hidden and refreshes immediately when it becomes
  visible again, and skips a tick while the previous request is still running.
- Default is 5s for status/list data (`usePolling(load)`); use 10–30s only for
  expensive or rarely-changing data. Poll continuously so changes from another
  tab/session or backend automation appear; gate with `enabled` when there is
  nothing to watch (e.g. only while a download is in flight).
- Refresh **quietly**: keep the current rows on screen and reuse the page's
  `load({ quiet: true })` pattern instead of flipping the full-page loading state
  or clearing data.
- A refresh must never disturb the user: keep filters, form input and open
  dialogs; do not reset the selected row or close a modal from a poll.
- Specialized loops with cancellation/streaming semantics (port-forward status,
  live logs, elapsed clocks) may keep their own timer, but must still pause when
  hidden, avoid overlapping calls and clean up on unmount.

## Theme tokens

Defined in `src/index.css`; single source of color truth. Dark values apply under
the `.dark` class on `<html>`.

| Tailwind class | CSS variable | Use for |
|---|---|---|
| `bg-theme-bg` | `--bg-primary` | Page background |
| `bg-theme-card` | `--bg-card` | Cards, panels, modals, tooltips |
| `border-theme-border` | `--border-color` | All hairline borders |
| `text-theme-text` | `--text-primary` | Headings, values, primary ink |
| `text-theme-muted` | `--text-muted` | Secondary/label ink |
| — | `--brand-accent` | Brand emerald (buttons/links via primitives) |

Accent tints (status chips, focus rings, active states) live inside the
primitives — call sites pick a `tone`/`variant`, never a color class.

## Primitive quick reference

All from `import { ... } from './ui'` (or the relative path to `src/components/ui`).

| Primitive | Key props | Replaces |
|---|---|---|
| `Button` | `variant: primary\|secondary\|ghost\|danger\|dangerOutline\|outline\|link`, `size: xs\|sm\|md\|icon`, `isLoading` | hand-rolled `<button className="px-4 py-2 …">` |
| `Badge` | `tone: neutral\|brand\|success\|info\|warning\|danger\|violet`, `size: xs\|sm\|md` | ad-hoc chip spans |
| `StatusChip` | `status: staged\|processing\|in_review\|approved\|rejected\|…` (auto tone+label+dot) | status color ternaries |
| `Modal` | `isOpen, onClose, title, subtitle, size: sm\|md\|lg\|xl, footer, closeOnBackdrop, closeOnEscape` | `fixed inset-0` overlays |
| `Input/Select/Textarea/Checkbox/Label` | `error`, standard DOM props | ad-hoc form field styling |
| `Panel` | `title, actions, padding: none\|sm\|md` | `bg-white dark:bg-slate-800 rounded-xl border …` shells |
| `StatCard` | `icon, title, value, details[], onClick, active` | KPI cards (clickable = filter card) |
| `EmptyState` | `icon, title, message, action` | "No data" markup |
| `Spinner` / `LoadingState` | `size` / `label, fullPage` | inline `animate-spin` loaders; `fullPage` = pre-data dashboard shell |
| `PageHeader` | `title, subtitle, badge, onNavigateBack, onToggleMobileNav, actions` | dashboard header chrome |
| `ShareLinkButton` | — (copies URL + "Link copied!" toast) | per-dashboard share buttons |
| `ToggleGroup` | `options[{value,label}], value, onChange, fullWidth` | metric/mode pill selectors (single-select) |

## Chart rules

- Wrap every chart in `ChartContainer` (`title`, filter controls in `actions`,
  one row above the plot).
- Use Recharts axes with explicit units, domains and tick formatting. Allow
  room for labels and padding so marks are not clipped at the plot edges.
- Series colors: `CHART_SERIES` in fixed order — emerald, sky, amber, violet,
  pink. Assign by entity, never by rank: a series keeps its color when filters
  change the series count. More than 5 series → fold into "Other" or use small
  multiples; never invent a 6th hue. The palette is CVD-validated for both
  themes; do not edit it without re-validating.
- Status colors in charts come from `CHART_STATUS` and are reserved for state —
  never used as an extra series color; always paired with a label.
- **One axis.** Never two y-scales on one chart. Two measures of different
  scale → two charts or index to a common base.
- **Canonical metric selectors.** Every latency/throughput chart selector uses
  the same option sets, labels, and order across dashboards:
  - Latency: `NTPOT | TPOT | TTFT | ITL | E2E` (values `ntpot | tpot | ttft |
    itl | e2e`).
  - Throughput: `Output | Input | Total | QPS` (values `output | input |
    total | qps`).
  - Stats: `Mean | P50 | P90 | P99`.
  Omit options the data lacks — never render a selectable metric that can't be
  plotted — but keep the canonical order and labels for the ones present. Do
  not invent synonyms ("Request latency" → `E2E`; "tok/s" variants → the
  canonical four).
- **Bar marks**: `radius={[6, 6, 0, 0]}`, `isAnimationActive={false}`,
  `barCategoryGap="25%"`; `maxBarSize` 80 for single-stat bars, 50 for grouped
  stat bars. Give Recharts tooltips a visible cursor and correct stacking order.
- Tooltips: build on `ChartTooltip` + `ChartTooltipRow` (`opacity` for stat
  rows). The swatch carries series identity; text wears text tokens, never
  the series color.
- ≥2 series → render a `ChartLegend` (entries of `{label, color}`, colors from
  the palette), placed directly below the plot inside the `ChartContainer`;
  a single series needs none (the title names it).

## Review checklist (ratchet)

Run on every UI PR — these greps must return nothing for changed files outside
`src/components/ui/`:

```bash
git diff main --name-only -- 'src/components/*.jsx' 'src/components/**/*.jsx' | grep -v '^src/components/ui/' | \
  xargs grep -nE '#[0-9a-fA-F]{6}|fixed inset-0|animate-spin' -- 2>/dev/null
```

- [ ] No new hex literals or inline `animate-spin` outside `src/components/ui/`.
- [ ] No new `fixed inset-0` DIALOGS outside `src/components/ui/` — dialogs use
      `Modal`. (Legitimate non-dialog uses exist: invisible popover click-away
      layers and slide-over drawer backdrops. A hit must be one of those, with
      the pattern named in the PR description.)
- [ ] No template-literal class conditionals where `cn()` fits.
- [ ] New repeated markup extracted into `src/components/ui/`.
- [ ] Charts: fixed-order palette, one axis, `ChartContainer` shell, tooltip on
      the shared shell, legend present for ≥2 series.
- [ ] No hardcoded vendor/hardware literal (`'xpu'`/`'gpu'`, a vendor image, a
      device/metric name) driving a default or label — it comes from the hardware
      profile (see "Hardware-driven UI").
- [ ] Renders correctly in both themes (toggle `.dark` on `<html>` to check).
