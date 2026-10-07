import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs';
import { resolve, relative, sep } from 'node:path';
import ts from 'typescript';

export const SOURCE_ROOTS = ['src/', 'server/', 'llm_d_bench/', 'tools/', 'scripts/'];
export const digest = text => createHash('sha256').update(text).digest('hex');
export const git = (root, args) => execFileSync('git', args, { cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });

export function isSource(path) {
  return SOURCE_ROOTS.some(prefix => path.startsWith(prefix))
    && /\.(?:[cm]?js|jsx|ts|tsx|py)$/.test(path)
    && !/(?:^|\/)(?:node_modules|__pycache__|dist|build|vendor|fixtures|tests|test)(?:\/|$)/.test(path)
    && !/(?:^|\/)test_[^/]+\.py$|(?:\.test|\.spec|\.smoke)\.[^/]+$|\.d\.ts$/.test(path)
    && !path.endsWith('/mcp/tools.ts');
}

export function safeFile(root, path) {
  if (typeof path !== 'string' || !path || path.includes('\\') || path.split('/').includes('..')) return false;
  const full = resolve(root, path);
  if (!full.startsWith(resolve(root) + sep) || !existsSync(full) || lstatSync(full).isSymbolicLink()) return false;
  return lstatSync(full).isFile() && !relative(realpathSync(root), realpathSync(full)).startsWith('..');
}

export function discover(root) {
  return [...new Set(git(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard']).split('\0'))]
    .filter(path => isSource(path) && safeFile(root, path)).sort();
}

export function sources(root, revision) {
  if (!revision) return discover(root).map(path => ({ path, text: readFileSync(resolve(root, path), 'utf8') }));
  const commit = git(root, ['rev-parse', '--verify', `${revision}^{commit}`]).trim();
  return git(root, ['ls-tree', '-r', '-z', commit]).split('\0').filter(Boolean).flatMap(record => {
    const [header, path] = record.split('\t');
    if (!header.startsWith('100') || !isSource(path)) return [];
    return [{ path, text: git(root, ['show', `${commit}:${path}`]) }];
  });
}

export function declarations(path, text) {
  if (path.endsWith('.py')) {
    return [...text.matchAll(/^(\s*)(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)\s*[:(]/gm)]
      .map(match => ({ symbol: match[2], line: text.slice(0, match.index + match[1].length).split('\n').length, callable: true }));
  }
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
  const found = [];
  const visit = node => {
    if ((ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isVariableDeclaration(node)
        || ts.isInterfaceDeclaration(node) || ts.isTypeAliasDeclaration(node) || ts.isMethodDeclaration(node)) && node.name && ts.isIdentifier(node.name)) {
      found.push({ symbol: node.name.text, line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1,
        endLine: source.getLineAndCharacterOfPosition(node.end).line + 1,
        callable: ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isMethodDeclaration(node)
          || (ts.isVariableDeclaration(node) && node.initializer && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))),
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return found;
}

// Bounded, overlapping chunks cover module-level code and long function tails.
export function chunks(path, text) {
  const lines = text.split('\n');
  const symbols = declarations(path, text).filter(item => item.callable);
  const starts = new Set([0, ...symbols.map(item => item.line - 1)]);
  for (let i = 0; i < lines.length; i += 30) starts.add(i);
  return [...starts].sort((a, b) => a - b).flatMap(start => {
    const body = lines.slice(start, start + 40).join('\n');
    if (!body.trim()) return [];
    const symbol = symbols.findLast(item => item.line <= start + 1 && (!item.endLine || item.endLine >= start + 1))?.symbol || '(module)';
    return [{ path, line: start + 1, endLine: Math.min(start + 40, lines.length), symbol, text: body,
      hash: digest(`${path}\n${symbol}\n${body}`) }];
  });
}

export function validateCatalog(root, catalog) {
  if (catalog?.version !== 1 || !Array.isArray(catalog.entries) || !catalog.entries.length) return ['Catalog must have version 1 and nonempty entries'];
  const errors = [];
  const ids = new Set();
  const sourcePaths = new Set(discover(root));
  for (const entry of catalog.entries) {
    if (!entry || typeof entry !== 'object') { errors.push('Invalid catalog entry'); continue; }
    const label = entry.id || '(missing id)';
    if (!/^[a-z][a-z0-9-]*$/.test(entry.id || '') || ids.has(entry.id)) errors.push(`${label}: missing/invalid/duplicate id`);
    ids.add(entry.id);
    for (const key of ['purpose', 'boundaries']) if (typeof entry[key] !== 'string' || !entry[key].trim()) errors.push(`${label}: missing ${key}`);
    if (!Array.isArray(entry.symbols) || !entry.symbols.length || entry.symbols.some(x => typeof x !== 'string')) errors.push(`${label}: symbols required`);
    if (!sourcePaths.has(entry.path)) errors.push(`${label}: invalid source path ${entry.path}`);
    else {
      const text = readFileSync(resolve(root, entry.path), 'utf8');
      // AST validation prevents examples inside Python docstrings counting as declarations.
      const symbols = entry.path.endsWith('.py')
        ? JSON.parse(execFileSync('python3', ['-c', 'import ast,json,sys; tree=ast.parse(sys.stdin.read()); print(json.dumps([n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef))]))'], { input: text, encoding: 'utf8' }))
        : declarations(entry.path, text).map(x => x.symbol);
      const declared = new Set(symbols);
      for (const symbol of entry.symbols || []) if (!declared.has(symbol)) errors.push(`${label}: declaration ${symbol} missing in ${entry.path}`);
    }
    for (const key of ['examples', 'tests']) {
      if (!Array.isArray(entry[key])) { errors.push(`${label}: ${key} must be an array`); continue; }
      if (key === 'examples' && !entry[key].length) errors.push(`${label}: a real example is required`);
      for (const path of entry[key]) if (!safeFile(root, path)) errors.push(`${label}: missing/unsafe ${key} reference ${path}`);
    }
    if (!entry.tests?.length && !entry.testNote?.trim()) errors.push(`${label}: document missing test coverage with testNote`);
  }
  return errors;
}

export function renderMap(catalog) {
  const lines = ['# Reusable Capability Index', '', '<!-- Generated by npm run reuse:map. Edit .reuse/catalog.json, not this file. -->', '',
    'This is a starting point for discovery, not a complete function inventory. Source code, callers, and tests are authoritative. See the [reuse development guide](refactoring/reuse-first-agent-design.md) for usage.', ''];
  const link = path => `[${path}](../${path})`;
  for (const entry of catalog.entries) lines.push(`## ${entry.id}`, '', entry.purpose, '',
    `- Entry point: ${link(entry.path)}`, `- Symbols: ${entry.symbols.map(x => `\`${x}\``).join(', ')}`,
    `- Boundaries: ${entry.boundaries}`, `- Examples: ${entry.examples.map(link).join(', ')}`,
    `- Tests: ${entry.tests.length ? entry.tests.map(link).join(', ') : entry.testNote}`, '');
  return lines.join('\n');
}

const keywords = new Set('function return if else for while const let var class new throw try catch finally async await import from export default def in with as except raise None True False null true false switch case break continue yield and or not is pass'.split(' '));

function tokens(text, python) {
  // A lexical heuristic, not a parser or proof of behavioral equivalence.
  const pattern = python
    ? /#[^\n]*|"""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]/g
    : /\/\/[^\n]*|\/\*[\s\S]*?\*\/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|[^\s]/g;
  const result = [];
  let line = 1, offset = 0;
  for (const match of text.matchAll(pattern)) {
    line += text.slice(offset, match.index).split('\n').length - 1;
    offset = match.index;
    const raw = match[0];
    if (python ? raw.startsWith('#') || raw.startsWith('"""') || raw.startsWith("'''") : raw.startsWith('//') || raw.startsWith('/*')) continue;
    const normalized = /^["'`\d]/.test(raw) ? 'LITERAL' : /^[A-Za-z_$]/.test(raw) && !keywords.has(raw) ? 'ID' : raw;
    result.push({ value: normalized, line });
  }
  return result;
}

export function duplicates(files, changed = new Set(files.map(x => x.path)), { minTokens = 70 } = {}) {
  const windows = new Map();
  const pairs = new Map();
  for (const file of [...files].sort((a, b) => a.path.localeCompare(b.path))) {
    const parsed = tokens(file.text, file.path.endsWith('.py'));
    for (let i = 0; i <= parsed.length - minTokens; i++) {
      const key = digest(`${file.path.endsWith('.py') ? 'py' : 'js'}:${parsed.slice(i, i + minTokens).map(x => x.value).join(' ')}`);
      const location = { path: file.path, line: parsed[i].line, endLine: parsed[i + minTokens - 1].line };
      const previous = windows.get(key) || new Map();
      for (const other of previous.values()) {
        if (other.path === file.path || (!changed.has(other.path) && !changed.has(file.path))) continue;
        const pair = `${other.path}\0${file.path}`;
        if (!pairs.has(pair)) pairs.set(pair, { id: digest(pair), left: other, right: location, minTokens,
          message: 'Normalized token overlap; inspect behavior and callers before deciding reuse.' });
      }
      if (!previous.has(file.path)) previous.set(file.path, location);
      windows.set(key, previous);
    }
  }
  return [...pairs.values()];
}
