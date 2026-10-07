import ts from 'typescript';
import { execFileSync } from 'node:child_process';
import { digest, declarations } from './repository.mjs';

function publicDeclarations(path, text) {
  if (path.endsWith('.py')) {
    return JSON.parse(execFileSync('python3', ['-c', `import ast,json,sys
s=sys.stdin.read(); t=ast.parse(s)
print(json.dumps([{'symbol':n.name,'body':ast.get_source_segment(s,n)} for n in t.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and not n.name.startswith('_')]))`], { input: text, encoding: 'utf8' }));
  }
  const source = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true);
  const rows = [];
  const localDeclaration = name => source.statements.find(row => row.name?.text === name || (ts.isVariableStatement(row) && row.declarationList.declarations.some(d => d.name.getText(source) === name)));
  const add = (symbol, node) => rows.push({ symbol, body: node.getText(source) });
  for (const node of source.statements) {
    if (ts.isExportDeclaration(node)) {
      if (node.exportClause && ts.isNamedExports(node.exportClause)) {
        for (const item of node.exportClause.elements) {
          const local = item.propertyName?.text || item.name.text;
          const declaration = !node.moduleSpecifier && localDeclaration(local);
          add(item.name.text, declaration || node);
        }
      } else add(node.exportClause?.name?.text || '*', node);
    } else if (ts.isExportAssignment(node)) {
      const declaration = ts.isIdentifier(node.expression) && localDeclaration(node.expression.text);
      rows.push({ symbol: 'default', body: node.getText(source) + (declaration?.getText(source) || '') });
    }
    else if (node.modifiers?.some(mod => mod.kind === ts.SyntaxKind.ExportKeyword)) {
      if (ts.isVariableStatement(node)) for (const d of node.declarationList.declarations) add(d.name.getText(source), d);
      else add(node.name?.text || 'default', node);
    }
  }
  return rows;
}

export function registrationCandidates(files, oldFiles, catalog, reviews = []) {
  const old = new Map(oldFiles.map(file => [file.path, file.text]));
  return files.flatMap(file => {
    if (old.get(file.path) === file.text) return [];
    const previous = new Map(publicDeclarations(file.path, old.get(file.path) || '').map(row => [row.symbol, digest(row.body)]));
    const rows = publicDeclarations(file.path, file.text);
    // Catalog can deliberately include non-exported domain capabilities.
    for (const entry of catalog.entries.filter(entry => entry.path === file.path)) {
      for (const symbol of entry.symbols) if (!rows.some(row => row.symbol === symbol) && declarations(file.path, file.text).some(row => row.symbol === symbol)) rows.push({ symbol, body: file.text });
    }
    return rows.filter(row => previous.get(row.symbol) !== digest(row.body)).map(row => {
      const key = `${file.path}#${row.symbol}`;
      const hash = digest(file.text); // an exclusion expires on any enclosing contract change
      const ids = catalog.entries.filter(entry => entry.path === file.path && entry.symbols.includes(row.symbol)).map(entry => entry.id);
      const review = reviews.find(item => item.key === key && item.hash === hash && typeof item.reason === 'string' && item.reason.trim());
      return { key, path: file.path, symbol: row.symbol, hash, capabilityIds: ids,
        disposition: ids.length ? 'registered' : review ? 'excluded' : 'unreviewed', reason: review?.reason || null };
    });
  });
}
