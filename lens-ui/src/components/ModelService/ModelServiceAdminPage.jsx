import { useCallback, useEffect, useRef, useState } from 'react';
import { Boxes, Plus, Repeat } from 'lucide-react';
import { usePolling } from '../../hooks/usePolling';
import { useNotice } from '../../hooks/useNotice';
import { useSubmission } from '../../hooks/useSubmission';
import { cn } from '../../utils/cn';
import { errorMessage } from '../../utils/errorMessage';
import { Button } from '../ui/Button';
import { Input, Select, Textarea } from '../ui/FormControls';
import { Modal } from '../ui/Modal';
import { ModuleHeader } from '../ui/ModuleHeader';
import { ModulePage } from '../ui/ModulePage';
import { SectionLabel } from '../ui/SectionLabel';
import { PermissionGate } from '../../features/auth/PermissionGate';
import {
    createGroup,
    createMember,
    deleteGroup,
    deleteMember,
    getGatewayIppConfig,
    getGatewayStatus,
    listGroups,
    listMembers,
    listPublishableDeployments,
    streamComponentLogs,
    setGatewayIppConfig,
    streamGatewayOperation,
    updateGroup,
    updateMember,
} from './modelServiceBackend';
import { DataPlaneTopology } from './DataPlaneTopology';
import { GatewayOperationLogDrawer } from './GatewayOperationLogDrawer';
import { CARD } from './modelServiceStyles';


const STATUS_DOT = {
    active: 'bg-emerald-400',
    deployed: 'bg-emerald-400',
    ready: 'bg-emerald-400',
    running: 'bg-emerald-400',
    installed: 'bg-emerald-400',
    missing: 'bg-amber-400',
    unhealthy: 'bg-rose-500',
    degraded: 'bg-amber-400',
    external: 'bg-cyan-400',
    unknown: 'bg-amber-400',
    stopped: 'bg-slate-500',
    absent: 'bg-slate-500',
    disabled: 'bg-slate-500',
};

// A model service's status combines its providers: red only when every provider
// is unhealthy; yellow (degraded) when at least one is healthy but some are red;
// green when all healthy; no members -> absent; otherwise disabled.
function aggregateMemberStatus(groupMembers) {
    if (groupMembers.length === 0) return 'absent';
    const active = groupMembers.filter((member) => member.status === 'active').length;
    const unhealthy = groupMembers.filter((member) => member.status === 'unhealthy').length;
    if (unhealthy > 0) return active > 0 ? 'degraded' : 'unhealthy';
    if (active > 0) return 'active';
    return 'disabled';
}

// Admin "Manage models": left picks a model service, right shows its members.
// Styling mirrors the clusters overview page.
export function ModelServiceAdminPage({ onToggleMobileNav }) {
    const [groups, setGroups] = useState([]);
    const [members, setMembers] = useState([]);
    const [deployments, setDeployments] = useState([]);
    const [status, setStatus] = useState({ clusters: [] });
    const [error, setError] = useState('');
    const [notice, setNotice] = useNotice(6000);
    const [groupOpen, setGroupOpen] = useState(false);
    const [memberOpen, setMemberOpen] = useState(false);
    const [groupForm, setGroupForm] = useState({ name: '', clusterId: '', displayName: '', description: '' });
    const [memberForm, setMemberForm] = useState({ groupId: '', clusterId: '', executionId: '' });
    const [selectedGroupId, setSelectedGroupId] = useState('');
    const [detail, setDetail] = useState(null);
    const [serviceEdit, setServiceEdit] = useState(null);
    const [serviceDelete, setServiceDelete] = useState(null);
    const [memberDelete, setMemberDelete] = useState(null);
    const [ippConfigEdit, setIppConfigEdit] = useState(null);
    const [operationLog, setOperationLog] = useState(null);
    const [logsTarget, setLogsTarget] = useState(null);
    const [logsLines, setLogsLines] = useState([]);
    const logsPreRef = useRef(null);
    const loadedOnceRef = useRef(false);
    const serviceSummaries = groups.map((group) => {
        const groupMembers = members.filter((member) => member.groupId === group.id);
        return {
            id: group.id,
            name: group.name,
            clusterId: group.clusterId,
            memberCount: groupMembers.length,
            status: aggregateMemberStatus(groupMembers),
            groupStatus: group.status,
        };
    });
    const memberGroups = groups
        .map((group) => {
            const groupMembers = members.filter((member) => member.groupId === group.id);
            const clusterIds = [...new Set(groupMembers.map((member) => member.clusterId))];
            return {
                id: group.id,
                name: group.name,
                clusters: clusterIds.map((clusterId) => ({
                    clusterId,
                    clusterName: status.clusters.find((cluster) => cluster.clusterId === clusterId)?.clusterName || clusterId,
                    deployments: groupMembers
                        .filter((member) => member.clusterId === clusterId)
                        .map((member) => ({
                            memberId: member.id,
                            clusterId: member.clusterId,
                            id: member.executionId,
                            name: deployments.find((item) => item.executionId === member.executionId)?.name || member.executionId,
                            status: member.status,
                            components: member.healthJson?.components || null,
                            poolName: member.poolName,
                            eppRef: member.eppRef,
                            namespace: member.targetNamespace,
                            targetService: member.targetService,
                            target: `${member.targetService}.${member.targetNamespace}:${member.targetPort}`,
                        })),
                })),
            };
        })
        .filter((group) => group.clusters.length > 0);

    const load = useCallback(async () => {
        try {
            const [groupItems, memberItems, deployItems, gatewayStatus] = await Promise.all([
                listGroups(),
                listMembers(),
                listPublishableDeployments(),
                getGatewayStatus(),
            ]);
            setGroups(groupItems);
            setMembers(memberItems);
            setDeployments(deployItems);
            setStatus(gatewayStatus);
            setError('');
        } catch (failure) {
            setError(failure?.message || 'Failed to load model services');
        } finally {
            loadedOnceRef.current = true;
        }
    }, []);

    useEffect(() => { load(); }, [load]);
    usePolling(() => load({ quiet: true }));
    useEffect(() => {
        if (groups.length === 0) return;
        if (!groups.some((group) => group.id === selectedGroupId)) {
            setSelectedGroupId(groups[0].id);
        }
    }, [groups, selectedGroupId]);

    const groupSubmission = useSubmission('Failed to save group');
    const memberSubmission = useSubmission('Failed to publish member');
    const deleteSubmission = useSubmission('Failed to delete');
    const syncSubmission = useSubmission('Failed to sync data plane');
    const toggleSubmission = useSubmission('Failed to update member');
    const serviceEditSubmission = useSubmission('Failed to update model service');
    const serviceToggleSubmission = useSubmission('Failed to update model service');
    const ippConfigSubmission = useSubmission('Failed to save IPP config');
    const gatewaySubmission = useSubmission('Failed to install gateway');

    const submitGroup = () => groupSubmission.run(async () => {
        await createGroup({
            name: groupForm.name.trim(),
            clusterId: groupForm.clusterId || undefined,
            displayName: groupForm.displayName.trim(),
            description: groupForm.description,
        });
        setGroupOpen(false);
        setGroupForm({ name: '', modelRef: '', servedName: '', baseModel: '', clusterId: '', displayName: '', description: '' });
        await load({ quiet: true });
    });

    const submitMember = () => memberSubmission.run(async () => {
        await createMember({
            groupId: memberForm.groupId,
            executionId: memberForm.executionId,
        });
        setMemberOpen(false);
        setNotice('Published. The cluster gateway and model route will sync automatically.');
        await load({ quiet: true });
    });

    const removeGroup = (group) => deleteSubmission.run(async () => {
        await deleteGroup(group.id);
        await load({ quiet: true });
    });

    // Removing the last provider leaves no row to manage, so that deletes the
    // whole model service; otherwise only this cluster's provider is removed.
    const removeProvider = () => deleteSubmission.run(async () => {
        if (!memberDelete) return;
        const providers = members.filter((item) => item.groupId === memberDelete.groupId);
        if (providers.length <= 1) await deleteGroup(memberDelete.groupId);
        else await deleteMember(memberDelete.id);
        setMemberDelete(null);
        await load({ quiet: true });
    });

    const runGatewayOperation = (payloadOrList, title) => {
        const list = Array.isArray(payloadOrList) ? payloadOrList : [payloadOrList];
        setOperationLog({ title, status: 'running', lines: [], message: '' });
        const append = (line) => setOperationLog((prev) => (prev ? { ...prev, lines: [...prev.lines, line] } : prev));
        (async () => {
            for (const payload of list) {
                await new Promise((resolve) => {
                    streamGatewayOperation(payload, (type, data) => {
                        if (type === 'log') append(data.message);
                        else if (type === 'complete') {
                            if (data.message) append(data.message);
                            resolve();
                        } else if (type === 'error') {
                            append(data.message || 'Unexpected error.');
                            resolve();
                        }
                    }).catch((error) => {
                        append(errorMessage(error, 'Log stream failed.'));
                        resolve();
                    });
                });
            }
            setOperationLog((prev) => (prev ? { ...prev, status: 'succeeded' } : prev));
            setNotice(`${title} finished`);
            load({ quiet: true });
        })();
    };

    const handleSync = () => {
        const clusterIds = [...new Set(members.map((member) => member.clusterId))].sort();
        if (clusterIds.length === 0) {
            setNotice('No clusters with members to reconcile');
            return;
        }
        runGatewayOperation(clusterIds.map((clusterId) => ({ op: 'reconcile', clusterId })), 'Reconcile data plane');
    };

    const toggleMember = (member) => toggleSubmission.run(async () => {
        await updateMember(member.id, { status: member.status === 'disabled' ? 'active' : 'disabled' });
        await load({ quiet: true });
    });


    const openIppConfig = (cluster) => {
        ippConfigSubmission.setError('');
        const clusterIdValue = cluster.clusterId;
        setIppConfigEdit({ clusterId: clusterIdValue, name: cluster.clusterName || clusterIdValue, config: '', default: '', loading: true });
        getGatewayIppConfig(clusterIdValue)
            .then((payload) => setIppConfigEdit((cur) => (
                cur && cur.clusterId === clusterIdValue
                    ? { ...cur, config: payload.config || '', default: payload.default || '', loading: false }
                    : cur
            )))
            .catch((err) => setIppConfigEdit((cur) => (
                cur ? { ...cur, loading: false, loadError: errorMessage(err) } : cur
            )));
    };

    const submitIppConfig = () => ippConfigSubmission.run(async () => {
        await setGatewayIppConfig(ippConfigEdit.clusterId, ippConfigEdit.config);
        setIppConfigEdit(null);
        setNotice('IPP config applied.');
        await load({ quiet: true });
    });

    const installGatewayForCluster = (cluster) => runGatewayOperation(
        { op: 'install-gateway', clusterId: cluster.clusterId, provider: cluster.gatewayProvider || null },
        `Install Gateway (${cluster.clusterName || cluster.clusterId})`,
    );

    const openLogs = (kind, cluster, member) => {
        const clusterName = cluster.clusterName || cluster.clusterId;
        const target = (() => {
            if (kind === 'gateway') {
                return { title: `Gateway logs — ${clusterName}`, payload: { clusterId: cluster.clusterId, component: 'gateway' } };
            }
            if (kind === 'ipp') {
                return {
                    title: `IPP logs — ${clusterName}`,
                    payload: {
                        clusterId: cluster.clusterId, component: 'ipp',
                        namespace: cluster.ippNamespace || cluster.gatewayNamespace, name: cluster.ippName || 'lens-ipp',
                    },
                };
            }
            if (kind === 'epp' && member) {
                return {
                    title: `EPP logs — ${member.name || member.eppRef}`,
                    payload: {
                        clusterId: member.clusterId || cluster.clusterId,
                        namespace: member.namespace, name: member.eppRef || member.targetService,
                    },
                };
            }
            return null;
        })();
        if (target) setLogsTarget(target);
    };

    useEffect(() => {
        if (!logsTarget) return undefined;
        setLogsLines([]);
        const controller = new AbortController();
        streamComponentLogs(logsTarget.payload, (type, data) => {
            if (type === 'log') setLogsLines((current) => [...current, data.message].slice(-500));
            else if (type === 'error') setLogsLines((current) => [...current, `[error] ${data.message}`].slice(-500));
        }, controller.signal).catch((failure) => {
            setLogsLines((current) => [...current, `[error] ${errorMessage(failure, 'log stream failed')}`]);
        });
        return () => controller.abort();
    }, [logsTarget]);

    useEffect(() => {
        const node = logsPreRef.current;
        if (node) node.scrollTop = node.scrollHeight;
    }, [logsLines]);

    const handleComponentAction = (cluster, kind, action, service, member) => {
        const clusterId = cluster.clusterId;
        if (action === 'logs') {
            openLogs(kind, cluster, member);
            return;
        }
        if (kind === 'gateway' && (action === 'start' || action === 'stop')) {
            runGatewayOperation(
                { op: action === 'stop' ? 'gateway-stop' : 'gateway-start', clusterId },
                `${action === 'stop' ? 'Stop' : 'Start'} Gateway (${cluster.clusterName || clusterId})`,
            );
            return;
        }
        if (kind === 'gateway' && action === 'install') {
            installGatewayForCluster(cluster);
            return;
        }
        if (kind === 'gateway' && action === 'uninstall') {
            runGatewayOperation({ op: 'uninstall-gateway', clusterId }, 'Uninstall Gateway');
            return;
        }
        if (kind === 'gateway' && action === 'reconcile') {
            runGatewayOperation({ op: 'reconcile', clusterId }, `Reconcile ${cluster.clusterName || clusterId}`);
            return;
        }
        if (kind === 'ipp' && action === 'install') {
            runGatewayOperation({ op: 'install-ipp', clusterId }, 'Install IPP');
            return;
        }
        if (kind === 'ipp' && action === 'uninstall') {
            runGatewayOperation({ op: 'uninstall-ipp', clusterId }, 'Uninstall IPP');
            return;
        }
        if (kind === 'ipp' && action === 'configure') {
            openIppConfig(cluster);
            return;
        }
        if (kind === 'ipp' && (action === 'start' || action === 'stop')) {
            runGatewayOperation({
                op: 'scale', clusterId, namespace: cluster.gatewayNamespace, name: 'lens-ipp',
                replicas: action === 'stop' ? 0 : 1,
            }, `${action === 'stop' ? 'Stop' : 'Start'} IPP`);
            return;
        }
        if (kind === 'epp' && member) {
            runGatewayOperation({
                op: 'scale', clusterId, namespace: member.namespace,
                name: member.eppRef || member.targetService, replicas: action === 'stop' ? 0 : 1,
            }, `${action === 'stop' ? 'Stop' : 'Start'} EPP`);
        }
    };


    const openNewGroup = () => { groupSubmission.setError(''); setGroupOpen(true); };

    const deploymentsForGroup = (groupId) => {
        const group = groups.find((item) => item.id === groupId);
        if (!group) return [];
        const target = (group.modelRef || '').trim().toLowerCase();
        const alreadyAdded = new Set(
            members.filter((member) => member.groupId === groupId).map((member) => member.executionId),
        );
        return deployments.filter((item) => {
            if (item.clusterId !== group.clusterId) return false; // providers stay in the service's cluster
            if (alreadyAdded.has(item.executionId)) return false; // already a member of this service
            const candidate = (item.model || '').trim().toLowerCase();
            if (!target || !candidate) return true; // don't over-filter when the model is unknown
            return target === candidate;
        });
    };

    const openPublish = (group) => {
        const clusterId = group?.clusterId
            || groups.find((item) => item.id === selectedGroupId)?.clusterId
            || allClusters[0]?.id
            || '';
        const groupId = group?.id || groups.find((item) => item.clusterId === clusterId)?.id || '';
        const firstDeploy = groupId ? deploymentsForGroup(groupId)[0] : undefined;
        memberSubmission.setError('');
        setMemberForm({ clusterId, groupId, executionId: firstDeploy?.executionId || '' });
        setMemberOpen(true);
    };

    const openServiceDetails = (service) => {
        const group = groups.find((item) => item.id === service.id);
        if (!group) return;
        setDetail({
            title: group.name,
            subtitle: 'Model service',
            rows: [
                { label: 'Cluster', value: group.clusterId },
                { label: 'Model ref', value: group.modelRef },
                { label: 'Served name', value: group.servedName },
                { label: 'Base model', value: group.baseModel },
                { label: 'Display name', value: group.displayName },
                { label: 'Status', value: group.status },
                { label: 'Members', value: String(service.memberCount) },
                { label: 'Description', value: group.description },
            ],
        });
    };

    const openDeploymentDetails = (deployment) => {
        const member = members.find((item) => item.id === deployment.memberId);
        const info = deployments.find((item) => item.executionId === member?.executionId);
        setDetail({
            title: deployment.name,
            subtitle: 'Service provider',
            rows: [
                { label: 'Model service', value: groups.find((item) => item.id === member?.groupId)?.name },
                { label: 'Execution', value: member?.executionId },
                { label: 'Cluster', value: member?.clusterId },
                { label: 'Target', value: deployment.target },
                { label: 'Endpoint kind', value: member?.endpointKind },
                { label: 'Status', value: member?.status },
                { label: 'Model', value: info?.model },
                { label: 'Last health', value: member?.lastHealthAt },
            ],
        });
    };

    const openEditService = (service) => {
        const group = groups.find((item) => item.id === service.id);
        if (!group) return;
        serviceEditSubmission.setError('');
        setServiceEdit({
            groupId: group.id,
            name: group.name,
            modelRef: group.modelRef,
            servedName: group.servedName || '',
            baseModel: group.baseModel || '',
            clusterId: group.clusterId || '',
            displayName: group.displayName || '',
            description: group.description || '',
            status: group.status || 'active',
        });
    };

    const toggleService = (service) => serviceToggleSubmission.run(async () => {
        const group = groups.find((item) => item.id === service.id);
        if (!group) return;
        await updateGroup(group.id, { status: group.status === 'disabled' ? 'active' : 'disabled' });
        await load({ quiet: true });
    });

    const submitServiceEdit = () => serviceEditSubmission.run(async () => {
        if (!serviceEdit?.name.trim() || !serviceEdit?.modelRef.trim()) {
            throw new Error('Name and model ref are required.');
        }
        await updateGroup(serviceEdit.groupId, {
            name: serviceEdit.name.trim(),
            modelRef: serviceEdit.modelRef.trim(),
            servedName: serviceEdit.servedName.trim() || undefined,
            baseModel: serviceEdit.baseModel.trim() || undefined,
            clusterId: serviceEdit.clusterId || undefined,
            displayName: serviceEdit.displayName.trim(),
            description: serviceEdit.description,
            status: serviceEdit.status,
        });
        setServiceEdit(null);
        await load({ quiet: true });
    });

    const eligibleDeployments = deploymentsForGroup(memberForm.groupId);
    const groupsInCluster = groups.filter((group) => group.clusterId === memberForm.clusterId);
    const allClusters = [...new Set(deployments.map((item) => item.clusterId))]
        .sort()
        .map((id) => ({
            id,
            name: deployments.find((item) => item.clusterId === id)?.clusterName || '',
        }));
    const deploymentsInCluster = eligibleDeployments;
    const selectedDeployment = eligibleDeployments.find((item) => item.executionId === memberForm.executionId) || null;
    const derivedPoolName = selectedDeployment
        ? `${selectedDeployment.namespace}/${selectedDeployment.service.endsWith('-epp')
            ? selectedDeployment.service.slice(0, -4)
            : selectedDeployment.service}`
        : '';

    const memberDeleteGroup = memberDelete ? groups.find((group) => group.id === memberDelete.groupId) : null;
    const memberDeleteProviderCount = memberDelete
        ? members.filter((member) => member.groupId === memberDelete.groupId).length
        : 0;
    const memberDeleteIsLast = memberDeleteProviderCount <= 1;
    const memberDeleteClusterName = status.clusters.find(
        (cluster) => cluster.clusterId === memberDelete?.clusterId,
    )?.clusterName || memberDelete?.clusterId;
    const serviceDeleteProviderCount = serviceDelete
        ? members.filter((member) => member.groupId === serviceDelete.id).length
        : 0;
    const serviceDeleteClusterCount = serviceDelete
        ? new Set(
            members.filter((member) => member.groupId === serviceDelete.id).map((member) => member.clusterId),
        ).size
        : 0;

    return (
        <ModulePage>
            <div className="flex w-full flex-col gap-6">
                <ModuleHeader
                    icon={Boxes}
                    title="Model services"
                    description="Create a cluster-scoped model service (the public model name), publish cluster deployments as its providers. The cluster-shared Gateway and the per-model HTTPRoute are reconciled automatically; callers use the gateway URL with an API key and only reach models they may use."
                    onToggleMobileNav={onToggleMobileNav}
                />

                {(error || deleteSubmission.error || syncSubmission.error || toggleSubmission.error || gatewaySubmission.error || notice) && (
                    <p role="alert" className={cn(
                        'whitespace-pre-wrap rounded-lg border px-3 py-2 text-xs',
                        error || deleteSubmission.error || syncSubmission.error || toggleSubmission.error || gatewaySubmission.error
                            ? 'border-rose-500/30 bg-rose-500/10 text-rose-200'
                            : 'border-slate-700/60 bg-slate-900/60 text-slate-300',
                    )}>
                        {error || deleteSubmission.error || syncSubmission.error || toggleSubmission.error || gatewaySubmission.error || notice}
                    </p>
                )}

                <section className={cn(CARD, 'flex flex-col p-4 lg:min-h-[calc(100vh-13rem)]')}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                        <SectionLabel>service flow</SectionLabel>
                        <div className="flex items-center gap-2">
                            <span className="hidden text-[11px] text-slate-500 sm:inline">
                                {status.clusters.length} cluster(s) · {status.clusters.reduce((sum, cluster) => sum + cluster.members.length, 0)} member(s)
                            </span>
                            <PermissionGate permission="model-service:gateway:manage">
                                <Button variant="secondary" size="sm" onClick={handleSync} isLoading={syncSubmission.pending}>
                                    <Repeat size={14} /> Reconcile
                                </Button>
                            </PermissionGate>
                        </div>
                    </div>
                    <div className="mt-3">
                        <DataPlaneTopology
                            clusters={status.clusters}
                            services={serviceSummaries}
                            memberGroups={memberGroups}
                            statusDot={STATUS_DOT}
                            onComponentAction={handleComponentAction}
                            onInstallGateway={installGatewayForCluster}
                            savingGateway={gatewaySubmission.pending}
                            onAddService={openNewGroup}
                            onAddMember={(service) => {
                                const group = groups.find((item) => item.id === service.id);
                                if (group) openPublish(group);
                            }}
                            onDeleteService={(service) => {
                                const group = groups.find((item) => item.id === service.id);
                                if (group) { deleteSubmission.setError(''); setServiceDelete(group); }
                            }}
                            onToggleDeployment={(deployment) => {
                                const member = members.find((item) => item.id === deployment.memberId);
                                if (member) toggleMember(member);
                            }}
                            onDeleteDeployment={(deployment) => {
                                const member = members.find((item) => item.id === deployment.memberId);
                                if (member) { deleteSubmission.setError(''); setMemberDelete(member); }
                            }}
                            onAddMemberGlobal={openPublish}
                            onViewServiceDetails={openServiceDetails}
                            onEditService={openEditService}
                            onToggleService={toggleService}
                            onViewDeploymentDetails={openDeploymentDetails}
                        />
                    </div>
                </section>

            </div>

            <Modal
                isOpen={groupOpen}
                onClose={() => setGroupOpen(false)}
                title="New service"
                variant="drawer"
                size="lg"
                footer={
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setGroupOpen(false)}>Cancel</Button>
                        <Button variant="sky" size="sm" onClick={submitGroup} isLoading={groupSubmission.pending}
                            disabled={!groupForm.name.trim() || !groupForm.clusterId}>Create</Button>
                    </>
                }
            >
                {groupSubmission.error && (
                    <p role="alert" className="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {groupSubmission.error}
                    </p>
                )}
                <div className="flex flex-col gap-3">
                    <label className="text-xs text-theme-muted">Cluster (required)</label>
                    <Select value={groupForm.clusterId}
                        onChange={(event) => setGroupForm({ ...groupForm, clusterId: event.target.value })}>
                        <option value="">— select a cluster —</option>
                        {allClusters.map((cluster) => (
                            <option key={cluster.id} value={cluster.id}>
                                {cluster.name ? `${cluster.name} (${cluster.id})` : cluster.id}
                            </option>
                        ))}
                    </Select>
                    <label className="text-xs text-theme-muted">Model name (user-facing)</label>
                    <Input value={groupForm.name} maxLength={100}
                        onChange={(event) => setGroupForm({ ...groupForm, name: event.target.value })} />
                    <p className="text-[11px] text-theme-muted">
                        This is the name callers use, and Lens uses it as the routing/model ref too — the
                        rest is filled in automatically.
                    </p>
                    <label className="text-xs text-theme-muted">Display name (optional)</label>
                    <Input value={groupForm.displayName} maxLength={200}
                        onChange={(event) => setGroupForm({ ...groupForm, displayName: event.target.value })} />
                    <label className="text-xs text-theme-muted">Description</label>
                    <Textarea rows={2} value={groupForm.description} maxLength={2000}
                        onChange={(event) => setGroupForm({ ...groupForm, description: event.target.value })} />
                </div>
            </Modal>

            <Modal
                isOpen={memberOpen}
                onClose={() => setMemberOpen(false)}
                title="Add provider"
                variant="drawer"
                size="lg"
                footer={
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setMemberOpen(false)}>Cancel</Button>
                        <Button variant="sky" size="sm" onClick={submitMember} isLoading={memberSubmission.pending}
                            disabled={!memberForm.groupId || !memberForm.clusterId || !memberForm.executionId}>Add provider</Button>
                    </>
                }
            >
                {memberSubmission.error && (
                    <p role="alert" className="mb-3 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {memberSubmission.error}
                    </p>
                )}
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    <label className="text-xs text-theme-muted md:col-span-2">
                        Cluster
                        <Select value={memberForm.clusterId} className="mt-1 w-full"
                            onChange={(event) => {
                                const clusterId = event.target.value;
                                const firstGroup = groups.find((group) => group.clusterId === clusterId);
                                const firstDeploy = firstGroup ? deploymentsForGroup(firstGroup.id)[0] : undefined;
                                setMemberForm({
                                    clusterId,
                                    groupId: firstGroup?.id || '',
                                    executionId: firstDeploy?.executionId || '',
                                });
                            }}>
                            <option value="">— select a cluster —</option>
                            {allClusters.map((cluster) => (
                                <option key={cluster.id} value={cluster.id}>
                                    {cluster.name ? `${cluster.name} (${cluster.id})` : cluster.id}
                                </option>
                            ))}
                        </Select>
                    </label>
                    <label className="text-xs text-theme-muted md:col-span-2">
                        Model service
                        <Select value={memberForm.groupId} className="mt-1 w-full" disabled={!memberForm.clusterId}
                            onChange={(event) => {
                                const groupId = event.target.value;
                                const firstDeploy = deploymentsForGroup(groupId)[0];
                                setMemberForm({ ...memberForm, groupId, executionId: firstDeploy?.executionId || '' });
                            }}>
                            <option value="">
                                {memberForm.clusterId ? '— select a model service —' : '— select a cluster first —'}
                            </option>
                            {groupsInCluster.map((group) => <option key={group.id} value={group.id}>{group.name}</option>)}
                        </Select>
                    </label>
                    <label className="text-xs text-theme-muted md:col-span-2">
                        Deployment
                        <Select value={memberForm.executionId} className="mt-1 w-full" disabled={!memberForm.groupId}
                            onChange={(event) => setMemberForm({ ...memberForm, executionId: event.target.value })}>
                            {deploymentsInCluster.map((item) => (
                                <option key={item.executionId} value={item.executionId}>
                                    {item.name || item.displayName || item.modelRef || item.executionId.slice(0, 8)}
                                    {item.displayName && item.name && item.displayName !== item.name ? ` · ${item.displayName}` : ''}
                                </option>
                            ))}
                        </Select>
                    </label>
                    <div className="rounded-lg border border-theme-border bg-theme-card p-3 text-xs text-theme-muted md:col-span-2">
                        <div className="mb-1 font-semibold text-theme-text">Derived target (from the deployment)</div>
                        {selectedDeployment ? (
                            <div className="grid grid-cols-2 gap-1 font-mono">
                                <span>namespace: {selectedDeployment.namespace}</span>
                                <span>service: {selectedDeployment.service}</span>
                                <span>pool: {derivedPoolName || '—'}</span>
                                <span>port: {selectedDeployment.port}</span>
                                <span>kind: {selectedDeployment.endpointKind}</span>
                            </div>
                        ) : !memberForm.clusterId || !memberForm.groupId ? (
                            <span>Select a cluster, then a model service.</span>
                        ) : eligibleDeployments.length === 0 ? (
                            <span>No matching deployments left to add for this model service.</span>
                        ) : (
                            <span>Select a deployment.</span>
                        )}
                    </div>
                </div>
            </Modal>

            <Modal
                isOpen={Boolean(serviceEdit)}
                onClose={() => setServiceEdit(null)}
                title={serviceEdit ? `Edit model service — ${serviceEdit.name}` : 'Edit model service'}
                size="md"
                footer={(
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setServiceEdit(null)}>Cancel</Button>
                        <Button variant="sky" size="sm" onClick={submitServiceEdit} isLoading={serviceEditSubmission.pending}>Save</Button>
                    </>
                )}
            >
                <div className="space-y-3">
                    <div>
                        <label className="block text-xs text-slate-400" htmlFor="edit-service-name">Name (public model name)</label>
                        <Input id="edit-service-name" value={serviceEdit?.name ?? ''} maxLength={100}
                            onChange={(event) => setServiceEdit((current) => (current ? { ...current, name: event.target.value } : current))} className="mt-1" />
                    </div>
                    <div>
                        <label className="block text-xs text-slate-400" htmlFor="edit-service-modelref">Model ref</label>
                        <Input id="edit-service-modelref" value={serviceEdit?.modelRef ?? ''} maxLength={200}
                            onChange={(event) => setServiceEdit((current) => (current ? { ...current, modelRef: event.target.value } : current))} className="mt-1" />
                    </div>
                    <div>
                        <label className="block text-xs text-slate-400" htmlFor="edit-service-display">Display name</label>
                        <Input id="edit-service-display" value={serviceEdit?.displayName ?? ''} maxLength={200}
                            onChange={(event) => setServiceEdit((current) => (current ? { ...current, displayName: event.target.value } : current))} className="mt-1" />
                    </div>
                    <div>
                        <label className="block text-xs text-slate-400" htmlFor="edit-service-description">Description</label>
                        <Input id="edit-service-description" value={serviceEdit?.description ?? ''}
                            onChange={(event) => setServiceEdit((current) => (current ? { ...current, description: event.target.value } : current))} className="mt-1" />
                    </div>
                    <div className="grid grid-cols-2 gap-3">
                        <div>
                            <label className="block text-xs text-slate-400" htmlFor="edit-service-cluster">Cluster</label>
                            <Input id="edit-service-cluster" value={serviceEdit?.clusterId ?? ''} maxLength={32} disabled
                                className="mt-1" />
                            <p className="mt-1 text-[11px] text-slate-500">A model service stays in its cluster.</p>
                        </div>
                        <div>
                            <label className="block text-xs text-slate-400" htmlFor="edit-service-status">Status</label>
                            <Select id="edit-service-status" value={serviceEdit?.status ?? 'active'}
                                onChange={(event) => setServiceEdit((current) => (current ? { ...current, status: event.target.value } : current))} className="mt-1">
                                <option value="active">active</option>
                                <option value="disabled">disabled</option>
                            </Select>
                        </div>
                        <div>
                            <label className="block text-xs text-slate-400" htmlFor="edit-service-served">Served name</label>
                            <Input id="edit-service-served" value={serviceEdit?.servedName ?? ''} maxLength={200}
                                onChange={(event) => setServiceEdit((current) => (current ? { ...current, servedName: event.target.value } : current))} className="mt-1" />
                        </div>
                        <div>
                            <label className="block text-xs text-slate-400" htmlFor="edit-service-base">Base model</label>
                            <Input id="edit-service-base" value={serviceEdit?.baseModel ?? ''} maxLength={200}
                                onChange={(event) => setServiceEdit((current) => (current ? { ...current, baseModel: event.target.value } : current))} className="mt-1" />
                        </div>
                    </div>
                </div>
            </Modal>


            <Modal
                isOpen={Boolean(ippConfigEdit)}
                onClose={() => setIppConfigEdit(null)}
                title={ippConfigEdit ? `Configure IPP — ${ippConfigEdit.name}` : 'Configure IPP'}
                size="lg"
                footer={(
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setIppConfigEdit(null)}>Cancel</Button>
                        <Button variant="secondary" size="sm" disabled={!ippConfigEdit?.default}
                            onClick={() => setIppConfigEdit((cur) => (cur ? { ...cur, config: cur.default } : cur))}>
                            Use chart default
                        </Button>
                        <Button variant="sky" size="sm" onClick={submitIppConfig} isLoading={ippConfigSubmission.pending} disabled={ippConfigEdit?.loading}>
                            Save &amp; apply
                        </Button>
                    </>
                )}
            >
                <p className="text-xs text-theme-muted">
                    Inference Payload Processor config (PayloadProcessorConfig body). Read live from the
                    cluster; saving upgrades IPP.
                </p>
                {ippConfigEdit?.loadError && <p role="alert" className="mt-2 text-xs text-rose-300">{ippConfigEdit.loadError}</p>}
                {ippConfigEdit?.loading ? (
                    <p className="mt-2 text-xs text-theme-muted">Loading…</p>
                ) : (
                    <Textarea className="mt-2 h-72 w-full font-mono text-xs" value={ippConfigEdit?.config ?? ''}
                        onChange={(event) => setIppConfigEdit((cur) => (cur ? { ...cur, config: event.target.value } : cur))} />
                )}
            </Modal>

            <Modal
                isOpen={Boolean(serviceDelete)}
                onClose={() => setServiceDelete(null)}
                title="Remove model service"
                size="sm"
                footer={(
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setServiceDelete(null)}>Cancel</Button>
                        <Button variant="danger" size="sm" isLoading={deleteSubmission.pending}
                            onClick={() => { removeGroup(serviceDelete); setServiceDelete(null); }}>
                            Remove
                        </Button>
                    </>
                )}
            >
                <p className="text-xs text-theme-muted">
                    Remove <span className="font-semibold text-theme-text">{serviceDelete?.name}</span> and
                    {' '}<span className="font-semibold text-theme-text">{serviceDeleteProviderCount}</span>
                    {' '}provider(s) across {serviceDeleteClusterCount} cluster(s)? Every cluster&apos;s route is
                    reconciled away and callers using it will stop working.
                </p>
                {deleteSubmission.error && (
                    <p role="alert" className="mt-2 text-xs text-rose-300">{deleteSubmission.error}</p>
                )}
            </Modal>

            <Modal
                isOpen={Boolean(memberDelete)}
                onClose={() => setMemberDelete(null)}
                title={memberDeleteIsLast ? 'Remove last provider' : 'Remove provider'}
                size="sm"
                footer={(
                    <>
                        <Button variant="secondary" size="sm" onClick={() => setMemberDelete(null)}>Cancel</Button>
                        <Button variant="danger" size="sm" isLoading={deleteSubmission.pending}
                            onClick={() => removeProvider()}>
                            Remove
                        </Button>
                    </>
                )}
            >
                <p className="text-xs text-theme-muted">
                    {memberDeleteIsLast ? (
                        <>
                            Remove the last provider (<span className="font-semibold text-theme-text">{memberDeleteClusterName}</span>)
                            {' '}of <span className="font-semibold text-theme-text">{memberDeleteGroup?.name}</span>?
                            {' '}This deletes the model service and reconciles its route away; callers using it will stop working.
                        </>
                    ) : (
                        <>
                            Remove provider <span className="font-semibold text-theme-text">{memberDelete?.name}</span> from
                            {' '}<span className="font-semibold text-theme-text">{memberDeleteClusterName}</span>?
                            {' '}It stays available from its other {memberDeleteProviderCount - 1} provider(s) and only this cluster&apos;s route is reconciled away.
                        </>
                    )}
                </p>
                {deleteSubmission.error && (
                    <p role="alert" className="mt-2 text-xs text-rose-300">{deleteSubmission.error}</p>
                )}
            </Modal>

            <Modal
                isOpen={Boolean(detail)}
                onClose={() => setDetail(null)}
                title={detail?.title}
                subtitle={detail?.subtitle}
                size="md"
            >
                <dl className="space-y-2">
                    {(detail?.rows || []).map((row) => (
                        <div key={row.label} className="flex gap-3 text-xs">
                            <dt className="w-28 shrink-0 text-slate-500">{row.label}</dt>
                            <dd className="min-w-0 flex-1 break-words text-slate-200">{row.value || '—'}</dd>
                        </div>
                    ))}
                </dl>
            </Modal>

            <Modal
                isOpen={Boolean(logsTarget)}
                onClose={() => setLogsTarget(null)}
                title={logsTarget?.title || 'Logs'}
                variant="drawer"
                size="lg"
            >
                <pre ref={logsPreRef} className="h-full overflow-auto rounded-lg border border-slate-800/80 bg-slate-950/60 p-3 font-mono text-[11px] leading-relaxed text-slate-300 whitespace-pre-wrap">
{logsLines.length ? logsLines.join('\n') : 'Waiting for log output…'}
                </pre>
            </Modal>

            <GatewayOperationLogDrawer
                isOpen={Boolean(operationLog)}
                onClose={() => setOperationLog(null)}
                title={operationLog?.title}
                status={operationLog?.status}
                lines={operationLog?.lines || []}
                message={operationLog?.message || ''}
            />
        </ModulePage>
    );
}

export default ModelServiceAdminPage;
