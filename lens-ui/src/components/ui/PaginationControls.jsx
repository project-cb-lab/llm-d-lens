import React, { useId } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from './Button';
import { Select } from './FormControls';
import { cn } from '../../utils/cn';

/** Optional summary uses the caller's total (including server-paginated totals). */
export function PaginationControls({ page, totalPages, onPageChange, total, pageStart, pageEnd,
    itemLabel = 'items', formatValue = String, pageSize, pageSizeOptions, onPageSizeChange, pageSizeId, className }) {
    const generatedId = useId();
    const selectId = pageSizeId || generatedId;
    const controls = <>
        {onPageSizeChange && <>
            <label htmlFor={selectId} className="text-theme-muted">Rows per page</label>
            <Select id={selectId} className="h-8 w-20 text-xs" value={String(pageSize)}
                onChange={event => { onPageSizeChange(Number(event.target.value)); onPageChange(0); }}>
                {pageSizeOptions.map(size => <option key={size} value={size}>{size}</option>)}
            </Select>
        </>}
        <Button variant="secondary" size="sm" disabled={page <= 0}
            onClick={() => onPageChange(Math.max(0, page - 1))} aria-label="Previous page">
            <ChevronLeft size={14} />
        </Button>
        <span className="min-w-20 text-center text-theme-muted">Page {page + 1} of {totalPages}</span>
        <Button variant="secondary" size="sm" disabled={page + 1 >= totalPages}
            onClick={() => onPageChange(Math.max(0, Math.min(totalPages - 1, page + 1)))} aria-label="Next page">
            <ChevronRight size={14} />
        </Button>
    </>;
    if (total === undefined) return controls;
    return <div className={cn('flex flex-col gap-3 border-t border-theme-border px-1 pt-4 text-xs text-theme-muted sm:flex-row sm:items-center sm:justify-between', className)}>
        <span>Showing {formatValue(pageStart)}–{formatValue(pageEnd)} of {formatValue(total)} {itemLabel}</span>
        <div className="flex items-center gap-2">{controls}</div>
    </div>;
}
