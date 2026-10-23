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
