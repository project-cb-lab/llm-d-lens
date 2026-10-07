## What does this PR do?

<!-- Describe the changes and their purpose -->

## Why is this change needed?

<!-- Explain the motivation: bug fix, feature request, performance improvement, etc. -->

## How was this tested?

<!-- Describe how you verified the changes work correctly -->
- [ ] Unit tests added/updated
- [ ] Integration/e2e tests added/updated
- [ ] Manual testing performed

## Reuse review

<!-- For behavior changes: cite paths/symbols and actual analysis, not just "searched".
     Documentation-only changes may say N/A with a reason. Never invent approval. -->

- Search scope and nearest existing candidates:
- Reused, extended or extracted capabilities:
- Why any new/separate implementation is necessary:
- Shared callers affected and regression validation:
- Capability catalog/map updates (or why unnecessary):
- Duplicate findings analyzed (`npm run reuse:check -- --base <base-ref>`):
- Human decisions, scope and conversation/review references (if required):

- [ ] No dependent operation was performed while its human decision was pending
- [ ] No unresolved requests remain in `.reuse/decisions.json`

## Checklist

- [ ] Code follows project [contributing guidelines](../CONTRIBUTING.md)
- [ ] Tests pass locally (`make test`)
- [ ] Linters pass (`make lint`)
- [ ] Documentation updated (if applicable)
- [ ] `npm run check:english` passes for the full repository and the exact PR title/body; all findings translated and reviewed

## Related Issues

<!-- Link to related issues: Fixes #123, Related to #456 -->
