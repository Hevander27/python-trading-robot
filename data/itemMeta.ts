/*
 * Pure derivation helpers for the drawer's Docs/Notes rows. StoredDocument /
 * StoredNote persist only `content` (HTML) + timestamps, so the row's snippet,
 * word count, and relative time are derived here rather than stored. Kept pure
 * and side-effect free so they are trivially unit-testable and reusable across
 * both panes.
 */

const TAG_RE = /<[^>]*>/g;
const WHITESPACE_RE = /\s+/g;

/** Strip HTML tags and collapse whitespace to the plain text of `html`. */
export function htmlToText(html: string): string {
  return html.replace(TAG_RE, " ").replace(WHITESPACE_RE, " ").trim();
}

/**
 * A one-line snippet for a row: plain text of `html`, truncated to `max` chars
 * on a whole-word boundary with an ellipsis. Empty content yields "".
 */
export function htmlToSnippet(html: string, max = 60): string {
  const text = htmlToText(html);
  if (text.length <= max) return text;
  const slice = text.slice(0, max);
  const lastSpace = slice.lastIndexOf(" ");
  const head = lastSpace > 0 ? slice.slice(0, lastSpace) : slice;
  return `${head}…`;
}

/** Word count of `html` (whitespace-delimited tokens of its plain text). */
export function wordCount(html: string): number {
  const text = htmlToText(html);
  return text ? text.split(" ").length : 0;
}

/**
 * Derive an item title from its content — the leading `maxWords` words of the
 * plain text, capped at `maxChars` on a whole-word boundary. Returns "" when the
 * content has no text (so the caller keeps the placeholder title). Used to
 * auto-name a still-"Untitled" doc/note from its first few words on save.
 */
export function deriveTitleFromContent(html: string, maxWords = 6, maxChars = 48): string {
  const text = htmlToText(html);
  if (!text) return "";
  const head = text.split(" ").slice(0, maxWords).join(" ");
  if (head.length <= maxChars) return head;
  const slice = head.slice(0, maxChars);
  const lastSpace = slice.lastIndexOf(" ");
  return lastSpace > 0 ? slice.slice(0, lastSpace) : slice;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const WEEK = 7 * DAY;

/**
 * Compact relative time from an ISO timestamp to `now` (e.g. "now", "5m ago",
 * "2h ago", "3d ago", "2w ago"). Future timestamps clamp to "now". An unparseable
 * input yields "" so a malformed record never crashes a row.
 */
export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const diff = now - then;
  if (diff < MINUTE) return "now";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)}h ago`;
  if (diff < WEEK) return `${Math.floor(diff / DAY)}d ago`;
  return `${Math.floor(diff / WEEK)}w ago`;
}
