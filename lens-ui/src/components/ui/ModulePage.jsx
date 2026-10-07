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

import React from 'react';
import { cn } from '../../utils/cn';

// The canonical page shell for workspace modules: full-bleed (no max-width
// centering, only a small left gutter that keeps content clear of the sidebar)
// on the same flat slate backdrop the benchmark browser uses. Pair with
// ModuleHeader as the first child. Do not hand-roll this per module.
export function ModulePage({ children, className, contentClassName }) {
    return (
        <div
            className={cn(
                'min-h-screen w-full bg-slate-950 text-slate-100 font-sans antialiased relative overflow-x-hidden',
                className
            )}
        >
            <main className={cn('relative z-10 w-full py-6 pl-8 pr-6', contentClassName)}>{children}</main>
        </div>
    );
}
