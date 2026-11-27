/**
 * The only HTML the viewer shows from history: a table's structure and text.
 * The input is parsed with `DOMParser` (which runs nothing), and a new table is
 * built from an allow-list. Parsed nodes are never reused, and every attribute
 * is dropped except numeric `colspan` and `rowspan`.
 */
const TABLE_TAGS = new Set([
  'table',
  'thead',
  'tbody',
  'tfoot',
  'tr',
  'th',
  'td',
  'caption'
]);
/** Containers whose children are rows or cells, so loose text between them is dropped. */
const STRUCTURE = new Set(['table', 'thead', 'tbody', 'tfoot', 'tr']);
/** Elements dropped with everything inside them. */
const DROP = new Set([
  'script',
  'style',
  'template',
  'noscript',
  'iframe',
  'object',
  'embed',
  'svg',
  'math'
]);
const SPAN_MAX = 1000;

function copyChildren(from: Node, to: HTMLElement, inStructure: boolean): void {
  for (const node of Array.from(from.childNodes)) {
    if (node.nodeType === Node.TEXT_NODE) {
      if (!inStructure && node.textContent) {
        to.append(document.createTextNode(node.textContent));
      }
    } else if (node.nodeType === Node.ELEMENT_NODE) {
      const src = node as Element;
      const tag = src.tagName.toLowerCase();
      if (TABLE_TAGS.has(tag)) {
        const made = document.createElement(tag);
        for (const name of ['colspan', 'rowspan']) {
          const value = src.getAttribute(name);
          if (value !== null && /^\d{1,4}$/.test(value.trim())) {
            const n = Number(value);
            if (n >= 1 && n <= SPAN_MAX) {
              made.setAttribute(name, String(n));
            }
          }
        }
        copyChildren(src, made, STRUCTURE.has(tag));
        to.append(made);
      } else if (!DROP.has(tag) && !inStructure) {
        // Inline markup inside a cell (a link, bold): its text stays, the markup goes.
        copyChildren(src, to, false);
      }
    }
    // Comments and everything else are dropped.
  }
}

/**
 * Even an inert parse reports a CSP violation for each `onerror=`-style
 * attribute, and the page's CSP forbids them. Their names are changed inside
 * tags first; every attribute is dropped below anyway.
 */
function defuse(html: string): string {
  return html.replace(/<[a-z][^>]*>/gi, tag =>
    tag.replace(/([\s/"'])on(?=[a-z]+\s*=)/gi, '$1x-on')
  );
}

/** The first table of `html` rebuilt as plain table markup, or null when there is none. */
export function sanitizeTable(html: string): HTMLTableElement | null {
  const doc = new DOMParser().parseFromString(defuse(html), 'text/html');
  const source = doc.querySelector('table');
  if (!source) {
    return null;
  }
  const out = document.createElement('table');
  copyChildren(source, out, true);
  return out;
}
