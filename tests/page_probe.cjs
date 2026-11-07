// Run the actual page script with a small DOM and the actual rc config bridge.
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor() { this.children = []; this.value = ''; this.textContent = ''; this.hidden = false; }
  appendChild(n) { this.children.push(n); return n; }
  removeChild(n) { this.children.splice(this.children.indexOf(n), 1); }
  get firstChild() { return this.children[0]; }
  getAttribute() { return 'light'; }
  setAttribute() {}
}
const elements = {}, listeners = {}, outbound = [], intervals = [];
const element = id => elements[id] || (elements[id] = new Element());
global.document = { getElementById: element, createElement: () => new Element(),
  documentElement: new Element(), addEventListener() {}, hidden: false };
global.window = global;
global.parent = { postMessage: m => outbound.push(m) };
global.addEventListener = (event, callback) => { (listeners[event] ||= []).push(callback); };
global.setInterval = (callback, ms) => { intervals.push({callback, ms}); };
function scripts(html) {
  for (const m of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) vm.runInThisContext(m[1]);
}
if (input.mode === 'config') {
  vm.runInThisContext(input.bridge);
  scripts(BuildCustomConfigDocument(input.html, input.initial,
    {scope:'preset', readOnly:false, hasPresetOverride:input.initialOverride}));
  if (!input.initialOverride) {
    element('url').value = input.next.server_url;
    element('save').onclick();
  } else element('restore').onclick();
  // PluginsConfigDialog.ApplyCapabilityConfig sends this after saving/removing the override.
  for (const cb of listeners.message || []) cb({data:{__orca:'config', config:input.next,
    context:{scope:'preset', readOnly:false, hasPresetOverride:!input.initialOverride}}});
  const displayed = {url:element('url').value, note:element('from-tab').textContent};
  element('save').onclick();
  process.stdout.write(JSON.stringify({displayed, outbound}));
} else if (input.mode === 'page') {
  // The tab as the person sees it after the plugin's messages and the person's clicks (H7 fixes).
  global.orca = {postMessage:m=>outbound.push(m), onMessage:callback=>global.receivePage=callback};
  scripts(input.html);
  for (const step of input.steps) {
    if (step.message) receivePage(step.message);
    else if (step.click) element(step.click).onclick();
  }
  const texts = e => [e.textContent, ...e.children.flatMap(texts)].filter(Boolean);
  const read = {};
  for (const id of input.read) read[id] = {texts: texts(element(id)), values: element(id).children.map(c => c.value)};
  process.stdout.write(JSON.stringify({read, outbound}));
} else {
  global.orca = {postMessage:m=>outbound.push(m), onMessage:callback=>global.receivePage=callback};
  scripts(input.html);
  receivePage({kind:'fleet', rows:input.rows, read_at:Date.now()/1000});
  if (input.ageSeconds) {
    // Ordinary browser clock/timers, with no more server answers after losing the connection.
    const NativeDate = Date;
    let now = NativeDate.now();
    global.Date = class extends NativeDate {
      constructor(...args) { super(...(args.length ? args : [now])); }
      static now() { return now; }
    };
    for (let elapsed = 15000; elapsed <= input.ageSeconds * 1000; elapsed += 15000) {
      now += 15000;
      for (const timer of intervals) timer.callback();
    }
  }
  process.stdout.write(JSON.stringify({cells:element('fleet').children[0].children.map(c=>c.textContent)}));
}
