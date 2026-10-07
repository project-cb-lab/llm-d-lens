import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { DeleteConfirmation } from './DeleteConfirmation';
import { DeleteAIProviderModal } from '../AIProviders/DeleteAIProviderModal';
import { DeleteModelCacheModal } from '../ModelCache/DeleteModelCacheModal';
import { DeleteStorageVolumeModal } from '../StorageManagement/DeleteStorageVolumeModal';
import { DeleteDeploymentModal } from '../DeploymentManagement/DeleteDeploymentModal';

// Capture the composition under React's server renderer without mounting a portal.
function capture(Component, props) {
    let view;
    function Capture() { view = Component(props); return null; }
    renderToStaticMarkup(<Capture />);
    return view;
}

test('legacy consent retains cancel/delete decisions and small dialog', () => {
    const decisions = [];
    const view = capture(DeleteConfirmation, { description: 'Remove saved result?', onDecision: value => decisions.push(value) });
    assert.equal(view.props.size, 'sm');
    assert.equal(view.props.title, 'Confirm deletion');
    const [cancel, confirm] = view.props.footer.props.children;
    cancel.props.onClick(); confirm.props.onClick(); view.props.onClose();
    assert.deepEqual(decisions, [false, true, false]);
    assert.match(renderToStaticMarkup(view.props.children), /Remove saved result\?/);
});

test('controlled confirmation preserves form association, errors, and all pending close locks', () => {
    const onCancel = () => {};
    const onSubmit = () => {};
    const confirmRef = { current: null };
    for (const pending of [false, true]) {
        const view = capture(DeleteConfirmation, { title: 'Delete resource', formId: 'domain-delete', onCancel, onSubmit, pending, error: 'Try again', confirmRef, children: <p>Domain warning</p> });
        assert.equal(view.props.onClose, pending ? undefined : onCancel);
        assert.equal(view.props.closeOnEscape, !pending);
        assert.equal(view.props.closeOnBackdrop, !pending);
        const [cancel, confirm] = view.props.footer.props.children;
        assert.equal(cancel.props.disabled, pending);
        assert.equal(confirm.props.disabled, pending);
        assert.equal(confirm.props.isLoading, pending);
        assert.equal(confirm.props.form, 'domain-delete');
        assert.equal(confirm.props.type, 'submit');
        assert.equal(confirm.props.ref, confirmRef);
        assert.equal(view.props.children.props.onSubmit, onSubmit);
        const html = renderToStaticMarkup(view.props.children);
        assert.match(html, /id="domain-delete"/);
        assert.match(html, /Domain warning/);
        assert.match(html, /role="alert"[^>]*>.*Try again/);
    }
});

test('typed confirmation disabling and custom storage actions remain caller owned', () => {
    const view = capture(DeleteConfirmation, { confirmDisabled: true });
    assert.equal(view.props.footer.props.children[1].props.disabled, true);
    const footer = <button>Keep files</button>;
    assert.equal(capture(DeleteConfirmation, { footer }).props.footer, footer);
    const deployment = capture(DeleteDeploymentModal, { deployment: { execution_id: 'run-1', namespace: 'ns' } });
    assert.equal(deployment.props.confirmDisabled, true);
    assert.equal(deployment.props.formId, 'delete-deployment-form');
    assert.match(renderToStaticMarkup(deployment.props.children), /Also delete the Kubernetes namespace/);
});

test('resource deletions preserve warnings, retry after failure and success lock', async () => {
    for (const [Component, resource, formId, warning] of [
        [DeleteAIProviderModal, { provider: { name: 'Provider' } }, 'delete-ai-provider-form', /default planning behavior/],
        [DeleteModelCacheModal, { entry: { cachePath: '/models/example' } }, 'delete-model-cache-form', /Other cached models/],
    ]) {
        let calls = 0;
        const view = capture(Component, { ...resource, onDelete: async () => { if (++calls === 1) throw new Error('retry'); } });
        assert.equal(view.props.formId, formId);
        assert.match(renderToStaticMarkup(view.props.children), warning);
        const event = { preventDefault() {} };
        await view.props.onSubmit(event);
        await view.props.onSubmit(event);
        await view.props.onSubmit(event);
        assert.equal(calls, 2);
    }
});

test('storage with cache records requires the file choice before deleting', async () => {
    for (const count of [0, 2]) {
        const calls = [];
        const view = capture(DeleteStorageVolumeModal, { volume: { id: 'volume', kind: 'nfs', modelCacheCount: count }, onDelete: keep => calls.push(keep) });
        assert.equal(view.props.confirmLabel, count ? 'Continue' : 'Delete storage volume');
        await view.props.onSubmit({ preventDefault() {} });
        assert.deepEqual(calls, count ? [] : [false]);
    }
});


test('deployment deletion retains main cluster-name display with raw-ID fallback', () => {
    const deployment = { execution_id: 'run-1', cluster_id: 'cluster-1', namespace: 'ns' };
    for (const [clusterNameById, expected] of [[{ 'cluster-1': 'Production cluster' }, 'Production cluster'], [{}, 'cluster-1']]) {
        const view = capture(DeleteDeploymentModal, { deployment, clusterNameById });
        const html = renderToStaticMarkup(view.props.children);
        assert.match(html, new RegExp(expected));
        assert.equal((html.match(/Execution ID/g) || []).length, 1);
        assert.doesNotMatch(html, /<form/);
    }
});
