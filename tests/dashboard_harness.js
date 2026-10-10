// Runs the dashboard page's real logic functions (copied out of src/dashboard.html) on data given as JSON.
// usage: node dashboard_harness.js <dashboard.html>   with {"rows":[],"notes":[],"merrs":[],"scans":[],"xmin":0,"xmax":0} on stdin
const fs = require('fs');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.slice(html.indexOf('<script>') + 8, html.lastIndexOf('</script>'));

function fn(name) {                       // a `function name(...) { ... }` declaration, matched by braces
  const i = script.indexOf('function ' + name + '(');
  if (i < 0) return '';
  let depth = 0, j = script.indexOf('{', i);
  for (let k = j; k < script.length; k++) {
    if (script[k] === '{') depth++;
    else if (script[k] === '}' && --depth === 0) return script.slice(i, k + 1);
  }
  throw new Error('unbalanced ' + name);
}
function line(name) {                     // a one-line `const name = ...;` declaration
  const m = script.match(new RegExp('^const ' + name + ' = .*$', 'm'));
  return m ? m[0] : '';
}
const wanted = ['buildMarkers', 'analyzeSpikes', 'qualityStats', 'events'].map(fn).join('\n');
const consts = ['pT', 'fmtTS', 'fmtDur', 'pct', 'f1', 'fmtAvail', 'isMeasure', 'refreshLabel', 'fmtEvery', 'everyLabel', 'fmtAgo', 'escHtml'].map(line).join('\n');
const gaps = (script.match(/^const STALE_MS = .*$/m) || [''])[0];

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const body = `
  let notes = input.notes, merrs = input.merrs || [], scans = input.scans, zoom = null;
  ${gaps}
  ${consts}
  ${wanted}
  return {
    stats: qualityStats(input.rows, input.xmin, input.xmax),
    markers: buildMarkers(input.rows),
    events: events(input.rows),
    calls: (input.calls || []).map(([name, args]) => ({ refreshLabel, fmtEvery, everyLabel, fmtAgo, escHtml })[name](...args)),
    spikes: analyzeSpikes(input.rows, buildMarkers(input.rows)).map(e => ({ cause: e.cause, group: e.group, lost: e.lost })),
  };`;
process.stdout.write(JSON.stringify(new Function('input', body)(input)));
