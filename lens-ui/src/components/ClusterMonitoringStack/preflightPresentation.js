export function checkPassed(check) {
    return check?.passed === true || ['passed', 'ready', 'ok', 'success'].includes(check?.status);
}

export function checkTone(check) {
    if (checkPassed(check)) return 'success';
    return check?.blocking === false ? 'warning' : 'danger';
}

export function checkLabel(check) {
    if (checkPassed(check)) return 'Passed';
    return check?.blocking === false ? 'Missing' : 'Failed';
}
