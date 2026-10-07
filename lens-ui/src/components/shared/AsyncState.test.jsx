import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { AsyncState } from './AsyncState';

test('state precedence is loading, error, empty, content with domain slots', () => {
    const props = { loadingContent: <p>loading-slot</p>, errorContent: <p>error-slot</p>, emptyContent: <button>Add provider</button>, children: <p>records</p> };
    assert.equal(renderToStaticMarkup(<AsyncState {...props} loading error="failure" empty />), '<p>loading-slot</p>');
    assert.equal(renderToStaticMarkup(<AsyncState {...props} error="failure" empty />), '<p>error-slot</p>');
    assert.equal(renderToStaticMarkup(<AsyncState {...props} empty />), '<button>Add provider</button>');
    assert.equal(renderToStaticMarkup(<AsyncState {...props} />), '<p>records</p>');
});

test('default states retain retry and empty labels', () => {
    assert.match(renderToStaticMarkup(<AsyncState error={new Error('Unavailable')} onRetry={() => {}} />), /Unavailable.*Retry/);
    assert.match(renderToStaticMarkup(<AsyncState empty emptyTitle="No records" />), /No records/);
});
