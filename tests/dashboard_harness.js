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
const wanted = ['buildMarkers', 'analyzeSpikes', 'qualityStats', 'events', 'probesSummary', 'speedScale', 'needleAngle', 'speedMedian', 'speedUsage', 'speedStats', 'speedAdvice', 'latencyFigures'].map(fn).join('\n');
const consts = ['pT', 'fmtTS', 'fmtDur', 'pct', 'f1', 'fmtAvail', 'isMeasure', 'isSpeedMeasure', 'refreshLabel', 'fmtEvery', 'everyLabel', 'fmtAgo', 'escHtml', 'fmtMbps', 'speedOk', 'speedBarColor'].map(line).join('\n');
const gaps = (script.match(/^const STALE_MS = .*$/m) || [''])[0];

// Runs the page's own setProbesOpen against a minimal fake DOM and storage. scenario = {store, throws, steps: [[open, save], ...]}.
function simulateProbes(scn) {
  const els = {};
  const el = (extra) => ({ hidden: null, attrs: {}, cls: new Set(['collapsed']), setAttribute(k, v) { this.attrs[k] = v; },
    classList: { toggle: (c, on) => { on ? els.card.cls.add(c) : els.card.cls.delete(c); } }, ...extra });
  els['#probesPanel'] = el(); els['#probesSummary'] = el(); els['#probesToggle'] = el(); els['#probesCard'] = els.card = el();
  const store = Object.assign({}, scn.store || {});
  const localStorage = {
    getItem: k => { if (scn.throws) throw new Error('blocked'); return k in store ? store[k] : null; },
    setItem: (k, v) => { if (scn.throws) throw new Error('blocked'); store[k] = String(v); },
  };
  const src = line('PROBES_KEY') + '\n' + fn('setProbesOpen') + '\nreturn { setProbesOpen, isOpen: () => probesOpen };';
  const page = new Function('$', 'localStorage', src)(sel => els[sel], localStorage);
  const states = [{ initialOpen: page.isOpen() }];
  for (const [open, save] of scn.steps || []) {
    page.setProbesOpen(open, save);
    states.push({ open: page.isOpen(), panelHidden: els['#probesPanel'].hidden, summaryHidden: els['#probesSummary'].hidden,
      expanded: els['#probesToggle'].attrs['aria-expanded'], collapsed: els.card.cls.has('collapsed'), store: Object.assign({}, store) });
  }
  return states;
}

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
    calls: (input.calls || []).map(([name, args]) => ({ refreshLabel, fmtEvery, everyLabel, fmtAgo, escHtml, probesSummary, speedScale, needleAngle, fmtMbps, speedMedian, speedBarColor, speedUsage, speedOk, speedStats, speedAdvice, latencyFigures, isMeasure, isSpeedMeasure })[name](...args)),
    spikes: analyzeSpikes(input.rows, buildMarkers(input.rows)).map(e => ({ cause: e.cause, group: e.group, lost: e.lost })),
  };`;
const out = new Function('input', body)(input);
out.probesSim = (input.probesSim || []).map(simulateProbes);
process.stdout.write(JSON.stringify(out));
