// Shared visual style for the model-service pages so the admin "Model services"
// view and the user-facing pages (My services / API keys / Usage) match.
export const CARD = 'relative overflow-hidden border border-slate-800/80 rounded-2xl bg-gradient-to-br from-slate-900/90 via-slate-900/50 to-slate-950/90 shadow-2xl backdrop-blur-xl';

export const TABLE_HEAD = 'px-3 py-2 text-left text-[11px] font-medium uppercase tracking-wide text-slate-500';
export const TABLE_ROW = 'border-t border-slate-800/80';
export const CELL = 'px-3 py-2 text-sm text-slate-300';
export const CELL_MUTED = 'px-3 py-2 text-xs text-slate-400';

// Masked API-key label: literal prefix + last-4 fingerprint. Legacy tokens that
// stored the old prefix hint ("lens-mk-") have no recoverable suffix, so they
// render as just "lens-mk-****" until regenerated.
export function maskedKeyHint(hint) {
    const suffix = hint && !hint.startsWith('lens-mk') ? hint : '';
    return `lens-mk-****${suffix}`;
}
