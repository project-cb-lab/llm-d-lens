import GuideCoreMetrics from './GuideCoreMetrics.jsx';
import BenchmarkFrontier from './BenchmarkFrontier.jsx';
import { experimentPoints } from './linkedExperiment.js';

export default function BenchmarkOverview({details,runs,onSelect,selected,guideType,onInspect}) {
    const points = experimentPoints(runs);
    const run = selected || points[0];
    return <div className="space-y-6">
        <div className="flex flex-wrap items-baseline justify-between gap-4"><span className="text-xs text-slate-500">{new Set(points.map(r=>r.caseId)).size} configurations · {points.length} measured points</span></div>
        {points.length ? <>
            <GuideCoreMetrics details={details} runs={runs} selected={run} guideType={guideType} onSelect={onSelect} onInspect={onInspect}/>
            <BenchmarkFrontier runs={runs} selected={run} onSelect={onSelect}/>
        </> : <p className="py-16 text-center text-sm text-slate-500">No measured results are available yet.</p>}
    </div>;
}
