// Presentation helpers for the External providers module.

export { errorMessage } from '../../utils/errorMessage';

export function providerTitle(provider) {
    return provider?.name || provider?.id || 'AI provider';
}
