/**
 * @jest-environment jsdom
 */
import { sanitizeTable } from '../../viewer/sanitize';

describe('sanitizeTable', () => {
  it('keeps a table and drops everything else', () => {
    const table = sanitizeTable(
      '<!-- note --><style>td{color:red}</style>' +
        '<table class="x" style="color:red" onclick="a()"><caption>Sales</caption>' +
        '<thead><tr><th colspan="2" onerror="a()">region</th></tr></thead>' +
        '<tbody><tr><td rowspan="3" style="x" colspan="evil">N<script>alert(1)</script>' +
        '<a href="javascript:a()">orth</a><img src=x onerror=a()><!-- c --></td><td>12</td></tr></tbody>' +
        '</table>'
    );
    expect(table).not.toBeNull();
    const html = (table as HTMLTableElement).outerHTML;
    expect(html).toBe(
      '<table><caption>Sales</caption><thead><tr><th colspan="2">region</th></tr></thead>' +
        '<tbody><tr><td rowspan="3">North</td><td>12</td></tr></tbody></table>'
    );
    expect(
      sanitizeTable('<p onclick="a()">no table</p><script>x</script>')
    ).toBeNull();
  });
});
