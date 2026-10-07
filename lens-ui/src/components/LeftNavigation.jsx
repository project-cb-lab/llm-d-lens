import React, { useState, useEffect } from 'react';
import { Database, Webcam, Rocket, Gauge, Network, HardDrive, Download, Plug, Bot, ChevronDown,
    Users, UserCog, ShieldCheck, KeyRound, Monitor, ScrollText, Menu, Boxes, Activity, Waypoints
} from 'lucide-react';
import { cn } from '../utils/cn';
import SimulationIcon from './SimulationIcon';
import { useAuth } from '../features/auth/useAuth';
import { ChangePasswordModal } from '../features/auth/ChangePasswordModal';

const MENU_GROUPS = [
    {
        id: 'services',
        title: "Services",
        items: [
            { id: 'model-market', label: 'Model market', icon: Database, view: 'model-market', iconColor: 'text-white' },
            { id: 'optimization-deployments', label: 'Deployments', icon: Rocket, view: 'optimization-deployments', iconColor: 'text-white' },
            { id: 'admin-model-service', label: 'Model services', icon: Boxes, view: 'admin/model-service', iconColor: 'text-white', badge: 'Exp' },
            { id: 'admin-usage', label: 'Usage reports', icon: Activity, view: 'admin/usage', iconColor: 'text-white' },
            { id: 'optimization-evaluate', label: 'Evaluation', icon: Gauge, view: 'optimization-evaluate', iconColor: 'text-white' },
            { id: 'optimization-simulate', label: 'Simulation', icon: SimulationIcon, view: 'optimization-simulate', iconColor: 'text-white' },
            { id: 'playground-chat', label: 'Lens Assistant', icon: Bot, view: 'playground', iconColor: 'text-white', badge: 'Exp' }
        ]
    },
    {
        id: 'user-services',
        title: "User services",
        items: [
            { id: 'usage', label: 'My usage', icon: Activity, view: 'usage', iconColor: 'text-white' },
            { id: 'api-keys', label: 'API keys', icon: KeyRound, view: 'api-keys', iconColor: 'text-white' },
            { id: 'model-service', label: 'My services', icon: Waypoints, view: 'model-service', iconColor: 'text-white', badge: 'Exp' }
        ]
    },
    {
        id: 'resources',
        title: "Resources",
        items: [
            { id: 'clusters', label: 'Clusters', icon: Network, view: 'clusters', iconColor: 'text-white' },
            { id: 'storage', label: 'Storage', icon: HardDrive, view: 'storage-management', iconColor: 'text-white' },
            { id: 'model-cache', label: 'Model cache', icon: Download, view: 'model-cache', iconColor: 'text-white' },
            { id: 'ai-providers', label: 'External providers', icon: Plug, view: 'ai-providers', iconColor: 'text-white' },
            { id: 'cluster-monitoring-stack', label: 'Observability', icon: Webcam, view: 'cluster-monitoring-stack', iconColor: 'text-white' }
        ]
    },
    {
        id: 'administration',
        title: "Administration",
        items: [
            { id: 'admin-users', label: 'Users', icon: Users, view: 'admin/users', iconColor: 'text-white' },
            { id: 'admin-groups', label: 'Groups', icon: UserCog, view: 'admin/groups', iconColor: 'text-white' },
            { id: 'admin-roles', label: 'Roles', icon: ShieldCheck, view: 'admin/roles', iconColor: 'text-white' },
            { id: 'admin-identity-providers', label: 'Identity providers', icon: KeyRound, view: 'admin/identity-providers', iconColor: 'text-white', badge: 'Exp' },
            { id: 'admin-master-key', label: 'Master key', icon: KeyRound, view: 'admin/master-key', iconColor: 'text-white' },
            { id: 'admin-audit', label: 'Audit log', icon: ScrollText, view: 'admin/audit', iconColor: 'text-white' },
            { id: 'admin-sessions', label: 'Sessions', icon: Monitor, view: 'admin/sessions', iconColor: 'text-white' }
        ]
    }
];

// Groups that start collapsed. Everything else opens with the sidebar.
const DEFAULT_COLLAPSED_GROUPS = [];
const GROUP_COLLAPSE_KEY = 'prism_nav_collapsed_groups';

const ITEM_THEMES = {
    'optimization-deployments': {
        activeBg: 'bg-gradient-to-r from-cyan-950/20 via-sky-950/10 to-slate-950/20 border-cyan-500/20 text-cyan-300',
        activeIcon: 'bg-gradient-to-br from-cyan-500 to-sky-500 text-white shadow-[0_0_15px_rgba(6,182,212,0.35)]',
        indicator: 'bg-gradient-to-b from-cyan-400 to-sky-500 shadow-[0_0_8px_rgba(6,182,212,0.6)]'
    },
    'optimization-workspace': {
        activeBg: 'bg-gradient-to-r from-emerald-950/20 via-teal-950/10 to-slate-950/20 border-emerald-500/20 text-emerald-300',
        activeIcon: 'bg-gradient-to-br from-emerald-500 to-teal-500 text-white shadow-[0_0_15px_rgba(16,185,129,0.35)]',
        indicator: 'bg-gradient-to-b from-emerald-400 to-teal-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]'
    },
    'opt-simulation': {
        activeBg: 'bg-gradient-to-r from-violet-950/20 via-fuchsia-950/10 to-slate-950/20 border-violet-500/20 text-violet-300',
        activeIcon: 'bg-gradient-to-br from-violet-500 to-fuchsia-500 text-white shadow-[0_0_15px_rgba(139,92,246,0.35)]',
        indicator: 'bg-gradient-to-b from-violet-400 to-fuchsia-500 shadow-[0_0_8px_rgba(139,92,246,0.6)]'
    }
};

export default function LeftNavigation({ currentView, onNavigate, isMobileOpen, isExpanded: controlledExpanded, onExpandedChange }) {
    const { canView, principal, logout } = useAuth();
    const [accountMenuOpen, setAccountMenuOpen] = useState(false);
    const [showChangePassword, setShowChangePassword] = useState(false);
    const [uncontrolledExpanded, setUncontrolledExpanded] = useState(() => {
        const saved = localStorage.getItem('prism_sidebar_expanded');
        return saved !== null ? saved === 'true' : true;
    });
    const isExpanded = controlledExpanded ?? uncontrolledExpanded;
    const setIsExpanded = (nextExpanded) => {
        const nextValue = typeof nextExpanded === 'function' ? nextExpanded(isExpanded) : nextExpanded;
        if (controlledExpanded === undefined) setUncontrolledExpanded(nextValue);
        onExpandedChange?.(nextValue);
    };

    const [collapsedGroups, setCollapsedGroups] = useState(() => {
        try {
            const saved = JSON.parse(localStorage.getItem(GROUP_COLLAPSE_KEY));
            if (Array.isArray(saved)) return new Set(saved);
        } catch {
            // Ignore malformed preferences and fall back to the defaults.
        }
        return new Set(DEFAULT_COLLAPSED_GROUPS);
    });

    useEffect(() => {
        localStorage.setItem('prism_sidebar_expanded', isExpanded);
    }, [isExpanded]);

    useEffect(() => {
        localStorage.setItem(GROUP_COLLAPSE_KEY, JSON.stringify([...collapsedGroups]));
    }, [collapsedGroups]);

    const toggleGroup = (groupId) => {
        setCollapsedGroups((current) => {
            const next = new Set(current);
            if (next.has(groupId)) next.delete(groupId);
            else next.add(groupId);
            return next;
        });
    };

    const handleItemClick = (view, disabled) => {
        if (!disabled) {
            onNavigate(view);
        }
    };

    const visibleGroups = MENU_GROUPS
        .map(group => ({
        ...group,
        items: group.items.filter(item => !item.disabled && canView(item.view))
    })).filter(group => group.items.length > 0);

    return (
        <>
        <aside className={cn(
            'fixed top-20 left-4 h-[calc(100vh-6rem)]',
            isMobileOpen ? 'flex' : 'hidden md:flex',
            'flex-col border border-slate-900/65 bg-slate-950/50 backdrop-blur-xl rounded-3xl transition-all duration-300 z-50 shadow-[0_20px_50px_rgba(0,0,0,0.5)]',
            isExpanded ? 'w-64' : 'w-20'
        )}>



            {/* Navigation Items */}
            <div className="flex-1 overflow-y-auto overflow-x-visible py-6 flex flex-col gap-8 px-3 no-scrollbar">
                {visibleGroups.map((group, gIdx) => {
                    // Group collapse state is shared between the labelled and the icon-only
                    // sidebar, so collapsing a group hides its icons in both modes.
                    const isGroupCollapsed = collapsedGroups.has(group.id);
                    return (
                    <div key={group.id ?? gIdx} className="flex flex-col gap-1">
                        {/* Group Header with Stable Vertical Footprint */}
                        {group.title && (
                            isExpanded ? (
                                <button
                                    type="button"
                                    onClick={() => toggleGroup(group.id)}
                                    aria-expanded={!isGroupCollapsed}
                                    aria-controls={`nav-group-${group.id}`}
                                    className="group/header flex items-center gap-1.5 text-[10px] text-slate-500 uppercase tracking-widest px-3 mb-2.5 font-bold h-4 w-full hover:text-slate-300 transition-colors cursor-pointer"
                                >
                                    <ChevronDown className={cn('w-3 h-3 shrink-0 transition-transform duration-200', isGroupCollapsed && '-rotate-90')} />
                                    <span className="truncate">{group.title}</span>
                                </button>
                            ) : (
                                <button
                                    type="button"
                                    onClick={() => toggleGroup(group.id)}
                                    aria-expanded={!isGroupCollapsed}
                                    aria-controls={`nav-group-${group.id}`}
                                    aria-label={`${isGroupCollapsed ? 'Expand' : 'Collapse'} ${group.title}`}
                                    title={group.title}
                                    className="group/header relative mb-2.5 flex h-4 items-center justify-center px-3 text-slate-600 hover:text-slate-300 transition-colors cursor-pointer"
                                >
                                    {gIdx > 0 ? <div className="absolute left-[10px] w-[36px] h-[1px] bg-slate-800/80 shrink-0" /> : null}
                                    <ChevronDown className={cn('relative w-3 h-3 shrink-0 transition-transform duration-200 bg-slate-950 rounded-full', isGroupCollapsed && '-rotate-90')} />
                                </button>
                            )
                        )}

                        <div id={`nav-group-${group.id}`} className={cn('flex flex-col gap-1', isGroupCollapsed && 'hidden')}>
                        {group.items.map((item) => {
                            const Icon = item.icon;
                            const isActive = currentView === item.view;

                            return (
                                <React.Fragment key={item.id}>
                                    {item.separator && (
                                        <div className="my-2 border-t border-slate-900/60 mx-3" />
                                    )}
                                    <button
                                        onClick={() => handleItemClick(item.view, item.disabled)}
                                        aria-disabled={item.disabled}
                                        title={!isExpanded ? item.label : undefined}
                                        className={cn(
                                            'group relative flex items-center gap-4 px-3 py-2.5 rounded-2xl transition-all duration-300 w-full text-left font-normal border',
                                            isActive
                                                ? (ITEM_THEMES[item.view]?.activeBg || 'bg-gradient-to-r from-cyan-950/20 via-blue-950/10 to-slate-950/20 border-cyan-500/20 text-cyan-300 shadow-[inset_0_1px_1px_rgba(255,255,255,0.02)]')
                                                : 'border-transparent text-slate-400 hover:bg-slate-900/30 hover:text-white cursor-pointer'
                                        )}
                                    >
                                        {/* Active Side Indicator */}
                                        {isActive && (
                                            <div className={cn('absolute left-1 top-3.5 bottom-3.5 w-1 rounded-full', ITEM_THEMES[item.view]?.indicator || 'bg-gradient-to-b from-cyan-400 to-blue-500')} />
                                        )}

                                        <div className={cn(
                                            'p-1.5 rounded-xl transition-all duration-300',
                                            isActive
                                                ? (ITEM_THEMES[item.view]?.activeIcon || 'bg-gradient-to-br from-cyan-500 to-blue-500 text-white shadow-[0_0_15px_rgba(6,182,212,0.35)]')
                                                : cn('bg-transparent group-hover:brightness-125', item.iconColor || 'text-slate-400')
                                        )}>
                                            <Icon className="w-5 h-5 shrink-0" />
                                        </div>

                                        {isExpanded && (
                                            <div className="flex flex-1 items-center justify-between truncate">
                                                <span className="flex items-center gap-1.5 min-w-0">
                                                    <span className={cn('text-sm tracking-wide truncate', isActive ? 'text-white font-medium' : 'font-normal')}>
                                                        {item.label}
                                                    </span>
                                                    {item.badge && (
                                                        <span className="text-[9px] text-amber-300 font-mono px-1.5 py-0.5 rounded bg-amber-950/40 border border-amber-800/50 shrink-0 tracking-wider">
                                                            {item.badge}
                                                        </span>
                                                    )}
                                                </span>

                                                {item.disabled && (
                                                    <span className="text-[9px] text-slate-500 font-mono px-2 py-0.5 rounded bg-slate-950 border border-slate-900/80 shrink-0 tracking-wider">
                                                        Coming soon
                                                    </span>
                                                )}
                                            </div>
                                        )}

                                        {/* Tooltip when Collapsed */}
                                        {!isExpanded && (
                                            <div className="absolute left-16 top-1/2 -translate-y-1/2 px-3 py-1.5 bg-slate-900 border border-slate-800/60 text-white text-xs font-medium rounded-lg invisible group-hover:visible shadow-xl z-[99999] whitespace-nowrap flex items-center gap-2">
                                                {item.label}
                                                {item.badge && <span className="text-[10px] text-amber-300 font-mono">({item.badge})</span>}
                                                {item.disabled && <span className="text-[10px] text-slate-500 font-mono">(Coming soon)</span>}
                                            </div>
                                        )}
                                    </button>
                                </React.Fragment>
                            );
                        })}
                        </div>
                    </div>
                    );
                })}
            </div>

            {principal && (
                <div className={cn(
                    'relative border-t border-slate-900/65 py-3 flex items-center gap-2 text-xs text-slate-400 shrink-0',
                    isExpanded ? 'px-4 justify-between' : 'px-3 justify-center'
                )}>
                    {accountMenuOpen && (
                        <div className="fixed inset-0 z-40" onClick={() => setAccountMenuOpen(false)} aria-hidden="true" />
                    )}
                    <button
                        type="button"
                        onClick={() => setAccountMenuOpen((open) => !open)}
                        className="relative z-50 flex min-w-0 items-center gap-2"
                        title={principal.username}
                        aria-haspopup="menu"
                        aria-expanded={accountMenuOpen}
                    >
                        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-slate-700 bg-slate-900 text-sm font-semibold uppercase text-slate-100">
                            {(principal.username || '?').slice(0, 1)}
                        </span>
                        {isExpanded && <span className="truncate text-sm text-slate-200">{principal.username}</span>}
                    </button>
                    {isExpanded && (
                        <button
                            type="button"
                            onClick={() => setAccountMenuOpen((open) => !open)}
                            className="relative z-50 rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-900/50 hover:text-white"
                            aria-label="Account menu"
                            aria-haspopup="menu"
                            aria-expanded={accountMenuOpen}
                        >
                            <Menu size={16} />
                        </button>
                    )}
                    {accountMenuOpen && (
                        <div
                            role="menu"
                            className={cn(
                                'absolute bottom-full z-50 mb-2 w-44 overflow-hidden rounded-xl border border-slate-800 bg-slate-900 shadow-2xl',
                                isExpanded ? 'right-4' : 'left-3'
                            )}
                        >
                            <button
                                type="button"
                                role="menuitem"
                                onClick={() => { setAccountMenuOpen(false); setShowChangePassword(true); }}
                                className="block w-full px-3 py-2 text-left text-xs text-slate-300 transition-colors hover:bg-slate-800 hover:text-white"
                            >
                                Change password
                            </button>
                            <button
                                type="button"
                                role="menuitem"
                                onClick={() => { setAccountMenuOpen(false); logout(); }}
                                className="block w-full border-t border-slate-800 px-3 py-2 text-left text-xs text-rose-300 transition-colors hover:bg-slate-800"
                            >
                                Sign out
                            </button>
                        </div>
                    )}
                </div>
            )}

            {/* Fixed Bottom-Left Toggle */}
            <div className="mt-auto border-t border-slate-900/65 px-4 py-4 flex items-center justify-start bg-slate-950/20 shrink-0 rounded-b-3xl">
                <button
                    onClick={() => setIsExpanded(!isExpanded)}
                    className="h-8 w-8 rounded-xl font-mono text-slate-500 hover:text-white hover:bg-slate-900/50 transition-all flex items-center justify-center cursor-pointer text-xs font-bold border border-slate-900/60 hover:border-slate-800/65"
                    title={isExpanded ? "Collapse Sidebar" : "Expand Sidebar"}
                >
                    {isExpanded ? "<|" : "|>"}
                </button>
            </div>
        </aside>
        {showChangePassword && (
            <ChangePasswordModal
                onClose={() => setShowChangePassword(false)}
                onSaved={() => setShowChangePassword(false)}
            />
        )}
        </>
    );
}
