import yaml from 'js-yaml';

// Fast feedback before generating the reference; the server still checks edits and facts.
export function validateImportManifest(content) {
    let documents;
    try {
        documents = yaml.loadAll(content).filter(document => document != null);
    } catch (error) {
        const location = error.mark ? ` at line ${error.mark.line + 1}, column ${error.mark.column + 1}` : '';
        throw new Error(`Invalid YAML${location}: ${error.reason || error.message}`);
    }
    if (!documents.length) throw new Error('The selected YAML file contains no resources.');
    documents.forEach((document, index) => {
        if (!document || typeof document !== 'object' || Array.isArray(document)) {
            throw new Error(`Document ${index + 1}: expected a Kubernetes resource mapping.`);
        }
        for (const [field, value] of Object.entries({ apiVersion: document.apiVersion, kind: document.kind, 'metadata.name': document.metadata?.name })) {
            if (typeof value !== 'string' || !value.trim()) throw new Error(`Document ${index + 1}: ${field} is required and must be a string.`);
        }
    });
}
