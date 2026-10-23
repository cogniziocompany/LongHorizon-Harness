// Paging helpers for the left-sidebar task list.
//
// A busy node keeps thousands of run folders (CT110 has ~1,800), and the
// sidebar used to render every matching run as a DOM row. Paging keeps the
// rendered rows bounded to one page while the count above the list stays the
// count of every match, so nothing is hidden without a number saying so.
//
// The helpers are pure on purpose: `frontend/web/test/taskListPaging.test.tsx`
// asserts the arithmetic and the clamping rules the sidebar relies on, with
// no DOM and no network involved.

/** Rows rendered per page in the sidebar task list. */
export const TASK_PAGE_SIZE = 50;

function normalizePageSize(pageSize: number): number {
  if (!Number.isFinite(pageSize) || pageSize < 1) return TASK_PAGE_SIZE;
  return Math.floor(pageSize);
}

/** Pages needed for `total` rows. An empty list is still one (empty) page. */
export function taskPageCount(total: number, pageSize: number = TASK_PAGE_SIZE): number {
  if (!Number.isFinite(total) || total <= 0) return 1;
  return Math.max(1, Math.ceil(total / normalizePageSize(pageSize)));
}

/** Clamp a 1-based page number into [1, pageCount]. */
export function clampTaskPage(page: number, pageCount: number): number {
  const upper = Math.max(1, Math.floor(Number.isFinite(pageCount) ? pageCount : 1));
  if (!Number.isFinite(page)) return 1;
  return Math.min(upper, Math.max(1, Math.floor(page)));
}

/** The rows that belong on `page` (1-based). */
export function taskPageSlice<T>(rows: T[], page: number, pageSize: number = TASK_PAGE_SIZE): T[] {
  const size = normalizePageSize(pageSize);
  const current = clampTaskPage(page, taskPageCount(rows.length, size));
  const start = (current - 1) * size;
  return rows.slice(start, start + size);
}

/** The 1-based page that holds row `index` (0-based). */
export function taskPageOfIndex(index: number, pageSize: number = TASK_PAGE_SIZE): number {
  if (!Number.isFinite(index) || index < 0) return 1;
  return Math.floor(index / normalizePageSize(pageSize)) + 1;
}

/** The 1-based row range shown on `page` (the last page may be short). */
export function taskPageRange(
  page: number,
  total: number,
  pageSize: number = TASK_PAGE_SIZE,
): { from: number; to: number } {
  const size = normalizePageSize(pageSize);
  const rows = Math.max(0, Math.floor(Number.isFinite(total) ? total : 0));
  if (!rows) return { from: 0, to: 0 };
  const current = clampTaskPage(page, taskPageCount(rows, size));
  const from = (current - 1) * size + 1;
  return { from, to: Math.min(rows, from + size - 1) };
}

/**
 * Decide which page the task list should show.
 *
 * The rules, in order:
 *
 * 1. The open run wins. When it is inside the filtered list and either the
 *    selection changed or the filter changed, the list shows the page that
 *    holds it. That covers a run picked from outside the sidebar (deep link,
 *    the runs command, the queue drawer) and a filter change that brings the
 *    open run back into view.
 * 2. A search is a lookup: while the operator types, the list shows the hits
 *    from the top instead of jumping to the open run on every keystroke.
 * 3. A filter change that leaves the open run outside the filtered list
 *    starts at the top of the new list.
 * 4. Otherwise the page stays where the operator left it. The poll re-orders
 *    the list every three seconds and must never move the page under them.
 *    The page is clamped, so a shrinking list cannot leave an empty page.
 */
export function resolveTaskPage(args: {
  /** Index of the open run inside the filtered list, -1 when it is not in it. */
  runIndex: number;
  /** The page the operator is on. */
  currentPage: number;
  /** Pages the filtered list has. */
  pageCount: number;
  /** Rows per page. */
  pageSize: number;
  /** The filter (search or segment) changed. */
  filterChanged: boolean;
  /** The selected run changed. */
  selectionChanged: boolean;
  /**
   * The filter change is a search the operator is typing: the list shows the
   * hits from the top instead of the page of the open run.
   */
  lookup?: boolean;
}): number {
  const {
    runIndex,
    currentPage,
    pageCount,
    pageSize,
    filterChanged,
    selectionChanged,
    lookup = false,
  } = args;
  if (runIndex >= 0 && (selectionChanged || (filterChanged && !lookup))) {
    return clampTaskPage(taskPageOfIndex(runIndex, pageSize), pageCount);
  }
  if (filterChanged) return 1;
  return clampTaskPage(currentPage, pageCount);
}
