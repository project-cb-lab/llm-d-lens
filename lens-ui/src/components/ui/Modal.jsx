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

import React, { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { cn } from '../../utils/cn';

const SIZES = {
    sm: 'max-w-md',
    md: 'max-w-lg',
    lg: 'max-w-2xl',
    xl: 'max-w-4xl',
};

// "drawer" panels use their own width scale (they're full-height, so a
// max-w meant for a centered dialog reads too narrow/too wide) -- roughly
// one size step up from the centered SIZES map.
const DRAWER_SIZES = {
    sm: 'max-w-lg',
    md: 'max-w-xl',
    lg: 'max-w-3xl',
    xl: 'max-w-5xl',
};

export function Modal({
    isOpen,
    onClose,
    title,
    subtitle,
    size = 'md',
    footer,
    closeOnBackdrop = true,
    closeOnEscape = true,
    className,
    inline = false,
    // 'center' (default) is the classic centered dialog; 'drawer' slides in
    // from the right edge of the screen as a full-height panel. Drawer
    // panels never close on a backdrop click, regardless of
    // `closeOnBackdrop` -- the only ways out are the explicit close
    // button/Escape/an in-panel Cancel action.
    variant = 'center',
    // Drawers normally still render a full-screen dimmed backdrop (it just
    // doesn't close on click). Pass `overlay={false}` for a drawer that
    // should leave the rest of the page fully visible and interactive
    // (e.g. a live log panel the user keeps open while working elsewhere).
    overlay = true,
    children,
}) {
    const isDrawer = variant === 'drawer' && !inline;
    const showOverlay = !inline && (!isDrawer || overlay);
    const [entered, setEntered] = useState(false);

    useEffect(() => {
        if (!isOpen || !closeOnEscape) return undefined;
        const onKeyDown = (e) => {
            if (e.key === 'Escape') onClose?.();
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
    }, [isOpen, closeOnEscape, onClose]);

    useEffect(() => {
        if (!isDrawer || !isOpen) return undefined;
        // Mount off-screen first, then flip a class on the next frame so
        // the transform transition actually animates instead of snapping in.
        const raf = window.requestAnimationFrame(() => setEntered(true));
        return () => {
            window.cancelAnimationFrame(raf);
            setEntered(false);
        };
    }, [isDrawer, isOpen]);

    if (!isOpen) return null;

    const content = (
        <div className={inline ? 'w-full' : 'fixed inset-0 z-[200] flex items-center justify-center p-4 pointer-events-none'}>
            {showOverlay && <div
                className="absolute inset-0 bg-slate-950/60 backdrop-blur-sm pointer-events-auto"
                onClick={isDrawer ? undefined : (closeOnBackdrop ? onClose : undefined)}
                aria-hidden="true"
            />}
            <div
                role={inline ? undefined : 'dialog'}
                aria-modal={inline ? undefined : 'true'}
                aria-label={typeof title === 'string' ? title : undefined}
                className={cn(
                    'relative w-full bg-theme-card border border-theme-border shadow-2xl',
                    !inline && 'pointer-events-auto',
                    isDrawer
                        ? cn(
                            'fixed inset-y-0 right-0 flex h-full max-h-none flex-col rounded-none border-y-0 border-r-0 transition-transform duration-300 ease-out',
                            DRAWER_SIZES[size],
                            entered ? 'translate-x-0' : 'translate-x-full'
                        )
                        : cn(inline ? 'min-h-[calc(100vh-8rem)] rounded-2xl' : 'max-h-[85vh] flex flex-col rounded-2xl', inline ? 'max-w-none' : SIZES[size]),
                    className
                )}
            >
                {(title || onClose) && (
                    <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-4 border-b border-theme-border shrink-0">
                        <div className="min-w-0">
                            {typeof title === 'string' ? (
                                <h3 className="text-base font-bold text-theme-text">{title}</h3>
                            ) : (
                                title
                            )}
                            {subtitle && <p className="text-xs text-theme-muted mt-1">{subtitle}</p>}
                        </div>
                        {onClose && (
                            <button
                                onClick={onClose}
                                aria-label="Close"
                                className="p-1.5 rounded-lg text-slate-400 hover:text-slate-900 dark:hover:text-white hover:bg-slate-200/60 dark:hover:bg-slate-800 transition-colors"
                            >
                                <X className="w-4 h-4" />
                            </button>
                        )}
                    </div>
                )}
                <div className={inline ? 'px-6 py-5' : 'flex-1 min-h-0 px-6 py-4 overflow-y-auto custom-scrollbar'}>{children}</div>
                {footer && (
                    <div className="flex items-center justify-end gap-2 px-6 py-4 border-t border-theme-border shrink-0">
                        {footer}
                    </div>
                )}
            </div>
        </div>
    );

    if (inline) return content;
    return createPortal(
        content,
        document.body
    );
}
