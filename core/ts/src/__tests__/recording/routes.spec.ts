import { ROUTES } from '../../recording/routes';

declare const require: (name: string) => any;
declare const __dirname: string;
const { readFileSync } = require('fs');
const { join } = require('path');

const table = JSON.parse(
  readFileSync(
    join(__dirname, '..', '..', '..', '..', '..', 'fixtures', 'api', 'routes.json'),
    'utf8'
  )
) as {
  base: string;
  routes: Array<{ name: string; method: string; path: string }>;
};

describe('route constants against fixtures/api/routes.json', () => {
  test('same names, methods and paths', () => {
    const mine = Object.entries(ROUTES)
      .map(([name, r]) => ({ name, method: r.method, path: r.path }))
      .sort((a, b) => a.name.localeCompare(b.name));
    const theirs = table.routes
      .map(({ name, method, path }) => ({ name, method, path }))
      .sort((a, b) => a.name.localeCompare(b.name));
    expect(mine).toEqual(theirs);
  });
});
