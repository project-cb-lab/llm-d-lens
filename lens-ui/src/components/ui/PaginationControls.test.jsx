import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { PaginationControls } from './PaginationControls';

test('pagination shows correct page and clamps first/last navigation', () => {
    for (const [page, totalPages] of [[0,1],[0,3],[1,3],[2,3]]) {
        const changes = [];
        let view;
        function Capture() { view = PaginationControls({ page, totalPages, onPageChange: n => changes.push(n) }); return null; }
        renderToStaticMarkup(<Capture />);
        const [,previous,,next] = view.props.children;
        assert.equal(previous.props.disabled, page === 0);
        assert.equal(next.props.disabled, page + 1 >= totalPages);
        previous.props.onClick(); next.props.onClick();
        assert.deepEqual(changes, [Math.max(0,page-1),Math.min(totalPages-1,page+1)]);
        assert.match(renderToStaticMarkup(<PaginationControls page={page} totalPages={totalPages} onPageChange={() => {}} />), new RegExp(`Page ${page+1} of ${totalPages}`));
    }
});

test('form errors hide when empty and expose one accessible alert', async () => {
    const { FormError } = await import('./FormError.jsx');
    assert.equal(renderToStaticMarkup(<FormError message="" />), '');
    const html = renderToStaticMarkup(<FormError message="Failed" />);
    assert.equal((html.match(/role="alert"/g) || []).length, 1);
    assert.match(html, /Failed/);
});


test('summary preserves server total and page-size selection resets the page', () => {
    const changes = [];
    let view;
    function Capture() {
        view = PaginationControls({ page: 2, totalPages: 21, total: 410, pageStart: 41, pageEnd: 60,
            itemLabel: 'tasks', pageSize: 20, pageSizeOptions: [10, 20, 50], pageSizeId: 'tasks-size',
            onPageChange: page => changes.push(['page', page]), onPageSizeChange: size => changes.push(['size', size]) });
        return null;
    }
    renderToStaticMarkup(<Capture />);
    assert.match(renderToStaticMarkup(view), /Showing 41–60 of 410 tasks/);
    const sizeControls = view.props.children[1].props.children.props.children[0];
    const select = sizeControls.props.children[1];
    assert.equal(select.props.id, 'tasks-size');
    select.props.onChange({ target: { value: '50' } });
    assert.deepEqual(changes, [['size', 50], ['page', 0]]);
});
