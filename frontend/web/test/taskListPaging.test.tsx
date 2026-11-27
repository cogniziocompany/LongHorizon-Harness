// Verifies the paging of the left-sidebar task list.
//
// A node keeps thousands of runs (CT110 has ~1,800), so the sidebar renders
// one page of rows at a time instead of every match. The count above the list
// must keep reporting every match, and the pager must say which slice is on
// screen and how many pages there are.
//
// The arithmetic is pure and lives in `src/taskListPaging.ts`; the component
// assertions render <TaskListSidebar /> through `renderToStaticMarkup`.
//
// Network-free: no JS calls `/api/runs`. The component receives `runs` as a
// prop.

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { UiLanguageProvider } from '../src/i18n';
import { TaskListSidebar, TaskPager, statusSegment, type SegmentId } from '../src/App';
import {
  TASK_PAGE_SIZE,
  clampTaskPage,
  resolveTaskPage,
  taskPageCount,
  taskPageOfIndex,
  taskPageRange,
  taskPageSlice,
} from '../src/taskListPaging';
import type { RunSummary } from '../../core/src/types';

function run(id: string, status: RunSummary['status'], task = `task-${id}`): RunSummary {
  return {
    id,
    status,
    task,
    updated_at: 0,
    log_dir: `/tmp/${id}`,
  };
}

function render(props: {
  runs: RunSummary[];
  segment?: SegmentId;
  search?: string;
  runId?: string | null;
  language?: 'zh' | 'en';
}) {
  const segment = props.segment ?? 'all';
  const counts: Record<SegmentId, number> = { active: 0, completed: 0, other: 0, all: props.runs.length };
  for (const r of props.runs) counts[statusSegment(r.status)] += 1;
  const html = renderToStaticMarkup(
    <UiLanguageProvider initialLanguage={props.language ?? 'en'}>
      <TaskListSidebar
        runs={props.runs}
        runId={props.runId ?? null}
        search={props.search ?? ''}
        onSearchChange={() => undefined}
        segment={segment}
        onSegmentChange={() => undefined}
        segmentCounts={counts}
        onSelectRun={() => undefined}
      />
    </UiLanguageProvider>,
  );
  // React may separate adjacent text children with comment markers; the
  // assertions below look at the text a user reads.
  return html.replace(/<!-- -->/g, '');
}

function rowCount(html: string): number {
  return (html.match(/data-run-id="/g) ?? []).length;
}

test('taskPageCount counts the pages a list needs', () => {
  assert.equal(TASK_PAGE_SIZE, 50);
  // An empty list is still one (empty) page: the pager never shows 0 pages.
  assert.equal(taskPageCount(0), 1);
  assert.equal(taskPageCount(-5), 1);
  assert.equal(taskPageCount(50), 1);
  assert.equal(taskPageCount(51), 2);
  assert.equal(taskPageCount(120), 3);
  // 1,771 runs is what the CT110 node showed: 36 pages of 50.
  assert.equal(taskPageCount(1771), 36);
  // A page size below 1 falls back to the default instead of dividing by zero.
  assert.equal(taskPageCount(10, 0), 1);
  assert.equal(taskPageCount(120, 40), 3);
});

test('clampTaskPage keeps the page inside the available range', () => {
  assert.equal(clampTaskPage(0, 5), 1);
  assert.equal(clampTaskPage(3, 5), 3);
  assert.equal(clampTaskPage(99, 5), 5);
  assert.equal(clampTaskPage(2, 1), 1);
  assert.equal(clampTaskPage(Number.NaN, 5), 1);
});

test('taskPageSlice returns only the rows of the requested page', () => {
  const rows = Array.from({ length: 120 }, (_, index) => index);
  assert.equal(taskPageSlice(rows, 1).length, 50);
  assert.equal(taskPageSlice(rows, 1)[0], 0);
  assert.equal(taskPageSlice(rows, 1)[49], 49);
  assert.equal(taskPageSlice(rows, 2)[0], 50);
  // The last page is short.
  assert.equal(taskPageSlice(rows, 3).length, 20);
  assert.equal(taskPageSlice(rows, 3)[19], 119);
  // A page past the end clamps to the last page.
  assert.deepEqual(taskPageSlice(rows, 99), taskPageSlice(rows, 3));
  assert.deepEqual(taskPageSlice([], 1), []);
});

test('taskPageOfIndex maps a row back to its page', () => {
  assert.equal(taskPageOfIndex(0), 1);
  assert.equal(taskPageOfIndex(49), 1);
  assert.equal(taskPageOfIndex(50), 2);
  assert.equal(taskPageOfIndex(119), 3);
  assert.equal(taskPageOfIndex(-1), 1);
});

test('taskPageRange reports the visible slice', () => {
  assert.deepEqual(taskPageRange(1, 1771), { from: 1, to: 50 });
  assert.deepEqual(taskPageRange(36, 1771), { from: 1751, to: 1771 });
  assert.deepEqual(taskPageRange(3, 120), { from: 101, to: 120 });
  assert.deepEqual(taskPageRange(1, 0), { from: 0, to: 0 });
});

test('the sidebar renders one page of rows, not every match', () => {
  const runs = Array.from({ length: 120 }, (_, index) => run(`run-${index}`, 'completed'));
  const html = render({ runs });

  // Only the first page of rows is in the DOM.
  assert.equal(rowCount(html), TASK_PAGE_SIZE);
  assert.ok(html.includes('data-run-id="run-0"'), 'the first row must be rendered');
  assert.ok(html.includes('data-run-id="run-49"'), 'the last row of page 1 must be rendered');
  assert.ok(!html.includes('data-run-id="run-50"'), 'row 51 belongs to page 2');
  assert.ok(!html.includes('data-run-id="run-119"'), 'row 120 belongs to page 3');

  // The count above the list still reports every match, not the page size.
  assert.match(html, /class="session-label">Tasks <span>120<\/span>/);

  // The pager says which slice is on screen and how many pages there are.
  assert.ok(html.includes('data-task-pager'), 'the pager must be rendered');
  assert.ok(html.includes('1–50 / 120'), 'the pager must report the visible range');
  assert.ok(html.includes('Page 1 / 3'), 'the pager must report the page number');

  // On the first page there is nowhere to go back to.
  assert.match(html, /data-page-action="first" disabled/);
  assert.match(html, /data-page-action="prev" disabled/);
  assert.doesNotMatch(html, /data-page-action="next" disabled/);
  assert.doesNotMatch(html, /data-page-action="last" disabled/);
});

test('the pager is hidden when everything fits on one page', () => {
  const runs = Array.from({ length: 3 }, (_, index) => run(`run-${index}`, 'running'));
  const html = render({ runs });
  assert.equal(rowCount(html), 3);
  assert.ok(!html.includes('data-task-pager'), 'a single page needs no pager');
});

test('paging applies to the filtered set, not to the whole list', () => {
  const runs = [
    ...Array.from({ length: 60 }, (_, index) => run(`done-${index}`, 'completed')),
    ...Array.from({ length: 5 }, (_, index) => run(`live-${index}`, 'running')),
  ];
  // The active segment holds 5 rows: one page, no pager.
  const active = render({ runs, segment: 'active' });
  assert.equal(rowCount(active), 5);
  assert.ok(!active.includes('data-task-pager'));

  // The completed segment holds 60 rows: two pages.
  const completed = render({ runs, segment: 'completed' });
  assert.equal(rowCount(completed), 50);
  assert.ok(completed.includes('1–50 / 60'), 'the pager counts the filtered rows');
  assert.ok(completed.includes('Page 1 / 2'));
  assert.ok(!completed.includes('data-run-id="live-0"'), 'filtered-out rows stay hidden');
});

test('the search narrows the rows before the page is cut', () => {
  const runs = Array.from({ length: 120 }, (_, index) => run(`run-${index}`, 'completed', `task-${index}`));
  const html = render({ runs, search: 'task-1' });
  // "task-1" matches task-1, task-10..task-19 and task-100..task-119:
  // 31 rows in total, which still fit on one page.
  assert.equal(rowCount(html), 31);
  assert.ok(!html.includes('data-task-pager'), 'the filtered rows fit on one page');
});

test('resolveTaskPage follows the open run and never moves the page under the poll', () => {
  const base = { currentPage: 1, pageCount: 36, pageSize: 50 };
  // The open run wins: it is inside the filtered list and the selection
  // changed, so the list shows the page that holds it.
  assert.equal(resolveTaskPage({ ...base, runIndex: 54, filterChanged: false, selectionChanged: true }), 2);
  // A filter change that brings the open run back into view re-arms the jump:
  // the operator must not be left with an invisible selection.
  assert.equal(resolveTaskPage({ ...base, runIndex: 54, filterChanged: true, selectionChanged: false }), 2);
  // A filter change without the open run in the list starts at the top.
  assert.equal(resolveTaskPage({ ...base, currentPage: 7, runIndex: -1, filterChanged: true, selectionChanged: false }), 1);
  // The poll changes neither the filter nor the selection: the page stays
  // where the operator left it.
  assert.equal(resolveTaskPage({ ...base, currentPage: 7, runIndex: 54, filterChanged: false, selectionChanged: false }), 7);
  // A shrinking list clamps the page instead of leaving an empty page.
  assert.equal(resolveTaskPage({ ...base, currentPage: 7, pageCount: 2, runIndex: -1, filterChanged: false, selectionChanged: false }), 2);
  // The jump never lands outside the list.
  assert.equal(resolveTaskPage({ ...base, runIndex: 1750, filterChanged: true, selectionChanged: false }), 36);
  // A search is a lookup: while the operator types, the list shows the hits
  // from the top instead of jumping to the open run on every keystroke.
  assert.equal(resolveTaskPage({ ...base, runIndex: 54, filterChanged: true, selectionChanged: false, lookup: true }), 1);
  // Clearing the search is a change of view again, so the open run wins.
  assert.equal(resolveTaskPage({ ...base, runIndex: 54, filterChanged: true, selectionChanged: false, lookup: false }), 2);
});

test('the pager disables the forward buttons on the last page', () => {
  const html = renderToStaticMarkup(
    <UiLanguageProvider initialLanguage="en">
      <TaskPager page={36} pageCount={36} from={1751} to={1771} total={1771} onPage={() => undefined} />
    </UiLanguageProvider>,
  );
  assert.match(html, /data-page-action="next" disabled/);
  assert.match(html, /data-page-action="last" disabled/);
  assert.doesNotMatch(html, /data-page-action="first" disabled/);
  assert.doesNotMatch(html, /data-page-action="prev" disabled/);
  assert.ok(html.includes('1751–1771 / 1771'), 'the pager reports the short last page');
  assert.ok(html.includes('Page 36 / 36'));
  // The current page is announced, not just printed.
  assert.match(html, /data-page-indicator="true" aria-current="page"/);
});
