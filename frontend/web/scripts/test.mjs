// Wires frontend/web/test/*.test.tsx into `npm test`, mirroring the core
// package's runner (frontend/core/scripts/test.mjs): esbuild bundles the
// node:test + react-dom/server suites into a temp directory, `node --test`
// executes the explicit file list, and the directory is removed afterwards.
//
// The suites are network-free: every model-list/API call goes through an
// injected seam or a replaced-and-restored globalThis.fetch.
import { createRequire } from 'node:module';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawn } from 'node:child_process';

const localRequire = createRequire(import.meta.url);
let build;
try {
  ({ build } = localRequire('esbuild'));
} catch {
  const coreRequire = createRequire(new URL('../../core/package.json', import.meta.url));
  ({ build } = coreRequire('esbuild'));
}

const testDirectory = resolve('test');
const entryPoints = (await readdir(testDirectory))
  .filter((name) => name.endsWith('.test.tsx'))
  .sort()
  .map((name) => join(testDirectory, name));
if (entryPoints.length === 0) {
  console.error('no .test.tsx suites found under frontend/web/test');
  process.exit(1);
}

const directory = await mkdtemp(join(tmpdir(), 'lh-harness-web-test-'));
try {
  await build({
    entryPoints,
    bundle: true,
    platform: 'node',
    format: 'cjs',
    target: 'node20',
    jsx: 'automatic',
    outdir: directory,
    outExtension: { '.js': '.cjs' },
  });
  const bundles = new Set(
    (await readdir(directory)).filter((name) => name.endsWith('.cjs')).sort(),
  );
  const missing = entryPoints
    .map((name) => name.slice(testDirectory.length + 1).replace(/\.test\.tsx$/, '.test.cjs'))
    .filter((name) => !bundles.has(name));
  if (missing.length > 0) throw new Error(`esbuild did not emit: ${missing.join(', ')}`);
  await new Promise((resolveRun, rejectRun) => {
    const child = spawn(
      process.execPath,
      ['--test', ...entryPoints.map((name) =>
        join(directory, name.slice(testDirectory.length + 1).replace(/\.test\.tsx$/, '.test.cjs')))],
      { stdio: 'inherit' },
    );
    child.on('error', rejectRun);
    child.on('exit', (code) => code === 0 ? resolveRun() : rejectRun(new Error(`node --test exited with ${code}`)));
  });
} finally {
  await rm(directory, { recursive: true, force: true });
}
