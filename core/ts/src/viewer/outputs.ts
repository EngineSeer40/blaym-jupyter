/**
 * Rich output for the ghost book. Text that fits inline shows from `preview` at
 * once. Anything else shows its preview first and, when it scrolls into view,
 * the full content is fetched once (cached by hash for the life of the page)
 * and drawn by type: images from `blob:` URLs (SVG through `<img>`, which runs
 * no script), a sanitised table for HTML, a labelled placeholder for the rest.
 * Recorded code is never run and no history text is `innerHTML`'d.
 */
import type { OutputItem } from 'blaym-notebook-types';
import { renderOutputText } from './ghost';
import type { OutputRenderer } from './ghost';
import { renderIcon } from './icons';
import { sanitizeTable } from './sanitize';
import { placeholderKind } from './frames';
import type { SizeMemory } from './frames';

const IMAGES = ['image/png', 'image/jpeg', 'image/gif'];
const SVG = 'image/svg+xml';
const PLAIN = 'text/plain';
const HTML = 'text/html';

type Bundle = Record<string, unknown>;

/** What a fetched blob holds: the MIME bundle, or null when it could not be read. */
async function load(
  fetchBlob: (hash: string) => Promise<ArrayBuffer>,
  hash: string
): Promise<Bundle | null> {
  try {
    const json: unknown = JSON.parse(
      new TextDecoder().decode(await fetchBlob(hash))
    );
    const data =
      json && typeof json === 'object'
        ? (json as { data?: unknown }).data
        : undefined;
    return data && typeof data === 'object' ? (data as Bundle) : null;
  } catch {
    return null;
  }
}

/** A text value: a string, or nbformat's list of lines. */
function textOf(v: unknown): string | undefined {
  if (typeof v === 'string') {
    return v;
  }
  return Array.isArray(v) && v.every(x => typeof x === 'string')
    ? v.join('')
    : undefined;
}

function bytesOf(base64: string): Uint8Array {
  const bin = atob(base64.replace(/\s/g, ''));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) {
    out[i] = bin.charCodeAt(i);
  }
  return out;
}

function div(className: string, text?: string): HTMLDivElement {
  const e = document.createElement('div');
  e.className = className;
  if (text !== undefined) {
    e.textContent = text;
  }
  return e;
}

function placeholder(
  mime: string,
  title: string | undefined,
  kind: string,
  word?: string
): HTMLElement {
  const box = div('bv-placeholder bv-out-placeholder');
  box.append(renderIcon(kind, null, null, 14));
  const m = document.createElement('span');
  m.className = 'bv-out-mime';
  m.textContent = word ?? mime;
  if (word) {
    box.title = mime;
  }
  box.append(m);
  if (title) {
    const t = document.createElement('span');
    t.textContent = title;
    box.append(t);
  }
  return box;
}

export interface OutputRendererOptions {
  fetchBlob(hash: string): Promise<ArrayBuffer>;
  /** Remembers the height each output was drawn at, so a placeholder can keep it. */
  sizes?: SizeMemory;
}

/**
 * The quick frame's output: no fetch, a box with the kind's icon and the
 * `searchText` title (else the MIME type), at the size last drawn for the hash.
 */
export function createQuickOutputRenderer(sizes: SizeMemory): OutputRenderer {
  return (item, mount) => {
    const kind = placeholderKind(item);
    mount.setAttribute('data-output-type', item.outputType);
    const mimes = item.mimeTypes;
    const word =
      kind === 'chart'
        ? mimes.some(m => m.startsWith('image/'))
          ? 'Image'
          : 'Chart'
        : kind === 'table'
          ? 'Table'
          : mimes.length > 0 && mimes.every(m => m.startsWith('text/'))
            ? 'Text'
            : 'Output';
    const box = placeholder(
      mimes[0] ?? item.outputType,
      item.searchText || undefined,
      kind === 'chart' ? 'chart' : 'other',
      `${word}${item.searchText ? ' ·' : ''}`
    );
    box.classList.add('bv-out-quick');
    box.style.setProperty(
      'min-height',
      `${sizes.heightOf(item.contentHash, kind)}px`
    );
    mount.append(box);
  };
}

export function createOutputRenderer(
  options: OutputRendererOptions
): OutputRenderer {
  const cache = new Map<string, Promise<Bundle | null>>();
  const urls = new Map<HTMLImageElement, string>();
  let watcher: MutationObserver | undefined;
  let viewer: IntersectionObserver | undefined;
  const waiting = new Map<Element, () => void>();

  // A blob URL lives as long as its `<img>` is in the page.
  function track(img: HTMLImageElement, url: string): void {
    urls.set(img, url);
    watcher ??= new MutationObserver(() => {
      for (const [el, u] of urls) {
        if (!el.isConnected) {
          URL.revokeObjectURL(u);
          urls.delete(el);
        }
      }
    });
    watcher.observe(document.body, { childList: true, subtree: true });
  }

  function image(blob: Blob, label: string): HTMLImageElement {
    const img = document.createElement('img');
    img.className = 'bv-out-img';
    img.alt = label;
    const url = URL.createObjectURL(blob);
    track(img, url);
    img.src = url;
    return img;
  }

  /** The element for a loaded bundle, or null to keep the preview. */
  function draw(item: OutputItem, data: Bundle): HTMLElement | null {
    const title = item.searchText || undefined;
    for (const mime of IMAGES) {
      const v = textOf(data[mime]);
      if (v !== undefined) {
        try {
          return image(new Blob([bytesOf(v)], { type: mime }), title ?? mime);
        } catch {
          return placeholder(mime, title, 'chart');
        }
      }
    }
    const svg = textOf(data[SVG]);
    if (svg !== undefined) {
      return image(new Blob([svg], { type: SVG }), title ?? 'chart');
    }
    const plain = textOf(data[PLAIN]);
    const html = textOf(data[HTML]);
    if (html !== undefined) {
      const table = sanitizeTable(html);
      if (table) {
        table.className = 'bv-out-table';
        return table;
      }
    }
    const other = Object.keys(data).find(k => k !== PLAIN && k !== HTML);
    if (other !== undefined && !(html !== undefined && plain !== undefined)) {
      return placeholder(
        other,
        title,
        /plotly|chart/.test(other) ? 'chart' : 'other'
      );
    }
    if (plain !== undefined) {
      const pre = document.createElement('pre');
      pre.className = 'bv-out-text';
      pre.textContent = plain;
      return pre;
    }
    const mime =
      other ?? (html !== undefined ? HTML : (item.mimeTypes[0] ?? 'unknown'));
    return placeholder(mime, title, 'other');
  }

  async function fill(item: OutputItem, mount: HTMLElement): Promise<void> {
    let pending = cache.get(item.contentHash);
    if (!pending) {
      pending = load(options.fetchBlob, item.contentHash);
      cache.set(item.contentHash, pending);
    }
    const data = await pending;
    if (data === null) {
      // A cut or missing blob: the preview stays, and the user is told.
      cache.delete(item.contentHash);
      if (!item.truncated) {
        mount.append(div('bv-truncated', 'full content unavailable'));
      }
      return;
    }
    const shown = draw(item, data);
    if (shown) {
      mount.replaceChildren(shown);
      if (shown instanceof HTMLImageElement && !shown.complete) {
        shown.addEventListener('load', () => measure(item, mount));
      } else {
        measure(item, mount);
      }
    }
  }

  function measure(item: OutputItem, mount: HTMLElement): void {
    options.sizes?.remember(item.contentHash, mount.offsetHeight);
  }

  function whenVisible(mount: HTMLElement, run: () => void): void {
    if (typeof IntersectionObserver === 'undefined') {
      run();
      return;
    }
    viewer ??= new IntersectionObserver(entries => {
      for (const e of entries) {
        const go = e.isIntersecting ? waiting.get(e.target) : undefined;
        if (go) {
          waiting.delete(e.target);
          viewer?.unobserve(e.target);
          go();
        }
      }
    });
    waiting.set(mount, run);
    viewer.observe(mount);
  }

  return (item, mount) => {
    renderOutputText(item, mount);
    const inline =
      item.outputType === 'stream' ||
      item.outputType === 'error' ||
      (item.preview !== '' && item.mimeTypes.every(m => m === PLAIN));
    if (inline) {
      window.requestAnimationFrame(() => measure(item, mount));
    } else {
      // Until the content has loaded the box keeps the height last drawn for it.
      const known = options.sizes?.known(item.contentHash);
      if (known) {
        mount.style.setProperty('min-height', `${known}px`);
      }
      whenVisible(mount, () => {
        void fill(item, mount);
      });
    }
  };
}
