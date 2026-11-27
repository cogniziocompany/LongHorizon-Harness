// Verifies easybutt0n-17: the sidebar task list must render its status dot
// from the ONE shared status→dot mapping (frontend/core/src/statusDots.ts),
// so the dot carries the tone colour, the non-colour shape cue and the
// status word in both a tooltip and an aria-label.
//
// Network-free: no JS calls `/api/runs`. The component receives `runs` as a
// prop and renders through `renderToStaticMarkup`.

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { UiLanguageProvider } from '../src/i18n';
import { TaskListSidebar, type SegmentId } from '../src/App';
import { statusDotStyle } from '../../core/src/statusDots';
import type { RunSummary } from '../../core/src/types';

function run(id: string, status: string): RunSummary {
  return {
    id,
    status,
    task: `task-${id}`,
    updated_at: 0,
    log_dir: `/tmp/${id}`,
  };
}

function renderSidebar(runs: RunSummary[], segment: SegmentId = 'active'): string {
  return renderToStaticMarkup(
    <UiLanguageProvider>
      <TaskListSidebar
        runs={runs}
        runId=""
        search=""
        onSearchChange={() => {}}
        segment={segment}
        onSegmentChange={() => {}}
        segmentCounts={{ active: runs.length, completed: 0, other: 0 }}
        onSelectRun={() => {}}
      />
    </UiLanguageProvider>,
  );
}

test('the sidebar dot uses the shared mapping tone and shape classes', () => {
  const html = renderSidebar([run('r1', 'running'), run('r2', 'waiting_approval')]);
  const running = statusDotStyle('running');
  const waiting = statusDotStyle('waiting_approval');
  assert.ok(html.includes(running.className), 'running dot is missing the shared tone/shape classes');
  assert.ok(html.includes(waiting.className), 'waiting dot is missing the shared tone/shape classes');
});

test('the sidebar dot carries the status word in a tooltip and an aria-label', () => {
  const html = renderSidebar([run('r1', 'waiting_approval')]);
  // The status word itself must be present, so the state is announced even
  // when the dot is only a few pixels wide.
  assert.match(html, /title="[^"]*waiting approval[^"]*"/i);
  assert.match(html, /aria-label="[^"]*waiting approval[^"]*"/i);
});

test('an unknown status still renders a labelled grey dot', () => {
  const html = renderSidebar([run('r1', 'totally-unknown-status')], 'all');
  const style = statusDotStyle('totally-unknown-status');
  assert.equal(style.tone, 'pending');
  assert.ok(html.includes(style.className), 'unknown status dot is missing the fallback classes');
  assert.match(html, /aria-label="[^"]*totally-unknown-status[^"]*"/i);
});

test('the six status families render six visually distinct dots', () => {
  const statuses = ['running', 'waiting_approval', 'pending', 'failed', 'done', 'cancelled'];
  const html = renderSidebar(statuses.map((status, index) => run(`r${index}`, status)), 'all');
  const shapes = new Set<string>();
  for (const status of statuses) {
    const style = statusDotStyle(status);
    assert.ok(html.includes(style.className), `${status} dot is missing its shared classes`);
    shapes.add(style.shape);
  }
  // Colour is never the only cue: every family has its own shape.
  assert.equal(shapes.size, statuses.length);
});
