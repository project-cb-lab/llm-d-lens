import assert from 'node:assert/strict';
import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { checkEnglish } from './check-english.mjs';

// Construct Unicode test data at runtime so the test source stays English.
const sample = String.fromCodePoint(0x4e2d, 0x6587);

test('scans tracked and new text, dotfiles, filenames, supplementary Han and PR metadata', () => {
  const root = mkdtempSync(join(tmpdir(), 'english-check-'));
  try {
    execFileSync('git', ['init', '-q', root]);
    mkdirSync(join(root, '.instructions'));
    writeFileSync(join(root, 'tracked.md'), 'English documentation.\n');
    execFileSync('git', ['-C', root, 'add', 'tracked.md']);
    writeFileSync(join(root, 'tracked.md'), `English\n${sample}\n`);
    writeFileSync(join(root, '.instructions', 'new.md'), sample);
    writeFileSync(join(root, `${sample}.txt`), 'English');
    writeFileSync(join(root, 'extension.txt'), String.fromCodePoint(0x20000));
    writeFileSync(join(root, 'utf16.txt'), Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(sample, 'utf16le')]));
    writeFileSync(join(root, '.gitignore'), 'ignored.txt\nmetadata.txt\n');
    writeFileSync(join(root, 'ignored.txt'), sample);
    writeFileSync(join(root, 'metadata.txt'), sample);
    writeFileSync(join(root, 'asset.bin'), Buffer.from([0, 255, 128]));
    const findings = checkEnglish(root, ['metadata.txt']);
    assert.equal(findings.length, 6);
    assert.ok(findings.some(line => line.startsWith('tracked.md:2:')));
    assert.ok(findings.some(line => line.startsWith('.instructions/new.md:1:')));
    assert.ok(findings.some(line => line.includes('characters in filename')));
    assert.ok(findings.some(line => line.startsWith('extension.txt:1:')));
    assert.ok(findings.some(line => line.startsWith('utf16.txt:1:')));
    assert.ok(findings.some(line => line.startsWith('metadata.txt:1:')));
    assert.ok(!findings.some(line => line.startsWith('ignored.txt:')));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('accepts English text and deleted tracked files; rejects undecodable text', () => {
  const root = mkdtempSync(join(tmpdir(), 'english-check-'));
  try {
    execFileSync('git', ['init', '-q', root]);
    writeFileSync(join(root, 'deleted.md'), 'English');
    execFileSync('git', ['-C', root, 'add', 'deleted.md']);
    rmSync(join(root, 'deleted.md'));
    writeFileSync(join(root, 'README.md'), 'English with Unicode punctuation — valid.');
    assert.deepEqual(checkEnglish(root), []);
    writeFileSync(join(root, 'invalid.txt'), Buffer.from([0xff]));
    assert.throws(() => checkEnglish(root), /non-UTF-8/);
  } finally { rmSync(root, { recursive: true, force: true }); }
});
