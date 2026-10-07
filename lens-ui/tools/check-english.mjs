#!/usr/bin/env node
import { execFileSync } from 'node:child_process';
import { lstatSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const han = /\p{Script=Han}/u;

// Git owns the inventory, including dotfiles and untracked, non-ignored additions.
export function checkEnglish(root, textFiles = []) {
  const paths = execFileSync('git', ['ls-files', '-z', '--cached', '--others', '--exclude-standard'], {
    cwd: root, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
  }).split('\0').filter(Boolean);
  const findings = [];
  for (const path of new Set(paths)) {
    if (han.test(path)) findings.push(`${path}: Chinese characters in filename`);
    const full = resolve(root, path);
    let stat;
    try { stat = lstatSync(full); }
    catch (error) { if (error.code === 'ENOENT') continue; throw error; }
    if (stat.isFile()) scan(full, path, findings);
  }
  for (const path of textFiles) scan(resolve(root, path), path, findings);
  return findings;
}

function scan(full, label, findings) {
  const bytes = readFileSync(full);
  let content;
  if (bytes[0] === 0xff && bytes[1] === 0xfe) content = new TextDecoder('utf-16le', { fatal: true }).decode(bytes);
  else if (bytes[0] === 0xfe && bytes[1] === 0xff) content = new TextDecoder('utf-16be', { fatal: true }).decode(bytes);
  else {
    if (bytes.includes(0)) return; // Binary assets are not repository text.
    try { content = new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
    catch { throw new Error(`${label}: non-UTF-8 text; convert to UTF-8 before checking`); }
  }
  content.split(/\r?\n/).forEach((line, index) => {
    if (han.test(line)) findings.push(`${label}:${index + 1}: ${line.trim()}`);
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  try {
    const args = process.argv.slice(2);
    const textFiles = [];
    for (let i = 0; i < args.length; i += 2) {
      if (args[i] !== '--text-file' || !args[i + 1]) throw new Error('Usage: npm run check:english -- [--text-file PATH ...]');
      textFiles.push(args[i + 1]);
    }
    const root = execFileSync('git', ['rev-parse', '--show-toplevel'], { encoding: 'utf8' }).trim();
    const findings = checkEnglish(root, textFiles);
    if (findings.length) {
      console.error(findings.join('\n'));
      console.error(`English check failed: ${findings.length} finding(s). Translate the text into English and rerun; do not delete content or encode it to bypass this check.`);
      process.exitCode = 1;
    } else console.log('English check passed: no Chinese characters in repository text or supplied PR text.');
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
