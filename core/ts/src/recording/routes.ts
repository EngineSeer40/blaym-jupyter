/**
 * The HTTP routes of the Blaym server extension, as the browser calls them.
 * The table is checked against `fixtures/api/routes.json` (generated from the
 * Python route table) by `src/__tests__/recording/routes.spec.ts`: the two
 * cannot drift apart without a failing test.
 */
export type RouteMethod = 'GET' | 'POST' | 'PUT';

export interface Route {
  readonly method: RouteMethod;
  /** Relative to `ROUTE_BASE`; `{name}` marks a path parameter. */
  readonly path: string;
}

export const ROUTE_BASE = 'blaym-jupyter/api';

export const ROUTES = {
  ingest: { method: 'POST', path: '/ingest' },
  put_blob: { method: 'PUT', path: '/blobs/{explorationId}' },
  prepare_outputs: { method: 'POST', path: '/outputs/prepare' },
  resolve_cell_revision: { method: 'POST', path: '/cells/revision' },
  register: { method: 'POST', path: '/notebooks/register' },
  status: { method: 'GET', path: '/notebooks/{notebookId}/status' },
  close: { method: 'POST', path: '/notebooks/close' },
  rename: { method: 'POST', path: '/notebooks/rename' },
  save: { method: 'POST', path: '/notebooks/save' },
  info: { method: 'GET', path: '/info' },
  state: { method: 'GET', path: '/state' },
  outline: { method: 'GET', path: '/outline' },
  events_since: { method: 'GET', path: '/events-since' },
  timeline: { method: 'GET', path: '/timeline' },
  events_between: { method: 'GET', path: '/events-between' },
  search: { method: 'GET', path: '/search' },
  get_blob: { method: 'GET', path: '/blobs/{explorationId}/{contentHash}' },
  stream: { method: 'GET', path: '/stream' },
  viewer_asset: { method: 'GET', path: '/viewer/{name}' }
} as const satisfies Record<string, Route>;

export type RouteName = keyof typeof ROUTES;

/** The path of a route with its parameters filled in (and escaped). */
export function routePath(
  name: RouteName,
  params: Record<string, string> = {}
): string {
  return ROUTES[name].path.replace(/\{(\w+)\}/g, (_m, key: string) => {
    const value = params[key];
    if (value === undefined) {
      throw new Error(`route ${name} needs ${key}`);
    }
    return encodeURIComponent(value);
  });
}
