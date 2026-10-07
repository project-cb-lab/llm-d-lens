import React from 'react';
import { Label, Select } from '../ui';

export default function RemoteDeploymentTarget({
    target, setTarget,
}) {
    const cards = target.cards || [];
    const freeCards = cards.filter((card) => typeof card === 'string' || !['busy', 'allocated', 'unavailable'].includes(card.status)).length;
    return (
        <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end"><div><Label>Select cluster node</Label><Select value={target.nodeSelection || 'current-server'} onChange={(event) => setTarget({ ...target, nodeSelection: event.target.value })}><option value="current-server">Current Prism server</option></Select></div><div className="flex min-w-40 gap-4 border border-slate-800 bg-slate-950/60 px-3 py-2 text-xs"><span><span className="block text-[10px] uppercase tracking-wider text-slate-500">Cards</span><span className="font-mono text-slate-200">{cards.length}</span></span><span><span className="block text-[10px] uppercase tracking-wider text-slate-500">Free</span><span className="font-mono text-emerald-300">{freeCards}</span></span></div></div>
    );
}
