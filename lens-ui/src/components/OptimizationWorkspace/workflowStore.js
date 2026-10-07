// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// Non-component exports for the Deploy Workflow: the ordered pipeline stages,
// the React context object, and the useWorkflow hook. Kept separate from the
// provider component so react-refresh stays happy.

import { createContext, useContext } from 'react';

// Workflow tabs. Benchmark is an unnumbered independent tool that becomes
// available after deployment; `view` is the Prism navigation id.
export const STAGES = [
    { id: 'define-workload', view: 'opt-define', label: 'Define Workload', step: '1', group: 'Plan', agent: false },
    { id: 'search-candidates', view: 'opt-search', label: 'Plan Deployment', step: '2', group: 'Configure', agent: true },
    { id: 'deployment', view: 'opt-deploy', label: 'Deploy Service', step: '3', group: 'Deploy', agent: false },
    { id: 'benchmark', view: 'opt-benchmark', label: 'Benchmark', group: 'Independent tool after deployment', agent: false, parallel: true },
    { id: 'performance-tco', view: 'opt-performance', label: 'Performance & TCO', step: '5', group: 'Analyze benchmark data', agent: true },
];

export const stageByView = (view) => STAGES.find((s) => s.view === view);

export const WorkflowContext = createContext(null);

export function useWorkflow() {
    const ctx = useContext(WorkflowContext);
    if (!ctx) throw new Error('useWorkflow must be used within a WorkflowProvider');
    return ctx;
}
