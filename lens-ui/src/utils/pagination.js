/** Page an already-filtered collection. Filtering and reset policy stay in callers. */
export function paginate(items, page, pageSize) {
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    const currentPage = Math.max(0, Math.min(page, totalPages - 1));
    const offset = currentPage * pageSize;
    return {
        totalPages,
        currentPage,
        pageStart: items.length === 0 ? 0 : offset + 1,
        pageEnd: Math.min(items.length, offset + pageSize),
        pagedItems: items.slice(offset, offset + pageSize),
    };
}
