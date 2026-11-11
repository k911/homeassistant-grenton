import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const component = new URL("../custom_components/homeassistant_grenton/", import.meta.url);
const source = readFileSync(new URL("frontend/scene-editor.js", component), "utf8");

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.isConnected = true; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { (this.attributes ??= {})[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  fire(name) { if (!this.disabled) this.listeners[name]?.(); }
}

function timer({ language = "en", elapsed = 0, direction = "up", segment = null, segmentStarted = 0 } = {}) {
  const elements = new Map(), intervals = new Map();
  let now = 0, intervalId = 0;
  const calls = [], events = [];
  vm.runInNewContext(source, {
    HTMLElement: class {}, structuredClone,
    performance: { now: () => now },
    setInterval: (callback) => { intervals.set(++intervalId, callback); return intervalId; },
    clearInterval: (id) => intervals.delete(id),
    queueMicrotask: (callback) => callback(), encodeURIComponent,
    CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    document: { createElement: (tag) => new Element(tag) },
    customElements: { get: (name) => elements.get(name), define: (name, cls) => elements.set(name, cls) },
  });
  const editor = Object.create(elements.get("ha-selector-grenton_shutter_measurement").prototype);
  const translations = JSON.parse(readFileSync(new URL(`translations/${language}.json`, component), "utf8")).selector;
  editor._selector = { grenton_shutter_measurement: {
    flow_id: "flow123", elapsed_ms: elapsed, direction, segment, segment_started_ms: segmentStarted,
  } };
  editor._hass = { language, callApi: (...args) => { calls.push(args); return Promise.resolve({ type: "abort" }); } };
  editor._localizeValue = (key) => key.split(".").reduce((node, part) => node?.[part], translations);
  editor.isConnected = true;
  editor._container = () => (editor.root = new Element("div"));
  editor.dispatchEvent = (event) => events.push(event);
  editor._render();
  return { editor, intervals, calls, events,
    tick(time) { now = time; for (const callback of intervals.values()) callback(); },
    counter: () => editor.root.children.find((node) => node.tag === "output"),
    counters: () => editor.root.children.filter((node) => node.tag === "output"),
    cancel: () => editor.root.children.find((node) => node.tag === "button"),
  };
}

for (const language of ["en", "pl"]) {
  test(`${language}: ticks update only counter text, preserving the dialog and buttons`, () => {
    const view = timer({ language, elapsed: 875 });
    const root = view.editor.root, counter = view.counter(), cancel = view.cancel();
    assert.equal(counter.textContent, "↑ 0 s");
    assert.equal(cancel.textContent, language === "pl" ? "Anuluj" : "Cancel");
    view.tick(1000);
    assert.equal(counter.textContent, "↑ 1 s");
    view.tick(26321);
    assert.equal(counter.textContent, "↑ 27 s");
    assert.equal(view.editor.root, root);
    assert.equal(view.counter(), counter);
    assert.equal(view.cancel(), cancel);
    assert.equal(view.calls.length, 0);
    assert.equal(view.events.length, 0);
  });
}

test("the displayed counter uses elapsed time across background pauses and HA updates", () => {
  const view = timer({ direction: "down", elapsed: 2875 });
  assert.equal(view.counter().textContent, "↓ 2 s");
  view.tick(60000);
  assert.equal(view.counter().textContent, "↓ 62 s");
  const root = view.editor.root;
  view.editor.hass = { ...view.editor.hass, states: {} };
  assert.equal(view.editor.root, root);
  view.editor._render();
  assert.equal(view.counter().textContent, "↓ 62 s");
  assert.equal(view.intervals.size, 1);
});

test("disconnecting clears the interval and reconnecting keeps the elapsed time", () => {
  const view = timer();
  view.tick(5000);
  view.editor.isConnected = false;
  view.editor.disconnectedCallback();
  assert.equal(view.intervals.size, 0);
  view.tick(9000);
  view.editor.isConnected = true;
  view.editor.connectedCallback();
  assert.equal(view.counter().textContent, "↑ 9 s");
  assert.equal(view.intervals.size, 1);
});

test("Cancel submits one native flow transition with no frontend timing result", async () => {
  const view = timer();
  view.tick(27321);
  view.cancel().fire("click");
  view.cancel().fire("click");
  assert.equal(view.calls.length, 1);
  assert.equal(view.calls[0][0], "POST");
  assert.equal(view.calls[0][1], "config/config_entries/options/flow/flow123");
  assert.deepEqual(JSON.parse(JSON.stringify(view.calls[0][2])), { measurement: "cancel" });
  assert.equal(view.events[0].type, "flow-update");
  assert.equal(view.events[0].bubbles, true);
  assert.equal(view.events[0].composed, true);
  assert.deepEqual(await view.events[0].detail.stepPromise, { type: "abort" });
});

test("a disabled timer cannot submit Cancel", () => {
  const view = timer();
  view.editor._disabled = true;
  view.editor._render();
  view.cancel().fire("click");
  assert.equal(view.calls.length, 0);
});

for (const language of ["en", "pl"]) {
  test(`${language}: the second stage shows total and slat time without rebuilding either counter`, () => {
    const view = timer({ language, direction: "down", elapsed: 24321, segment: "tilt", segmentStarted: 24321 });
    const root = view.editor.root, [total, slats] = view.counters(), cancel = view.cancel();
    assert.equal(total.textContent, "↓ 24 s");
    assert.equal(slats.textContent, "↻ 0 s");
    view.tick(3468);
    assert.equal(total.textContent, "↓ 27 s");
    assert.equal(slats.textContent, "↻ 3 s");
    assert.equal(slats.attributes["aria-label"], language === "pl" ? "Ruch lameli" : "Slat movement");
    assert.equal(view.editor.root, root);
    assert.equal(view.counters()[0], total);
    assert.equal(view.counters()[1], slats);
    assert.equal(view.cancel(), cancel);
    assert.equal(view.events.length, 0);
    assert.equal(view.calls.length, 0);
  });
}

test("shutters without tilt show only the total counter", () => {
  const view = timer();
  assert.equal(view.counters().length, 1);
  view.tick(24000);
  assert.equal(view.counter().textContent, "↑ 24 s");
});

for (const language of ["en", "pl"]) {
  test(`${language}: opening counts tilt first and then travel with stable counters`, () => {
    const openingTilt = timer({ language, direction: "up", segment: "tilt" });
    openingTilt.tick(3001);
    assert.equal(openingTilt.counters()[1].textContent, "↻ 3 s");
    const openingTravel = timer({ language, direction: "up", elapsed: 3001,
      segment: "travel", segmentStarted: 3001 });
    const root = openingTravel.editor.root, [total, travel] = openingTravel.counters();
    openingTravel.tick(24321);
    assert.equal(total.textContent, "↑ 27 s");
    assert.equal(travel.textContent, "↑ 24 s");
    assert.equal(travel.attributes["aria-label"], language === "pl" ? "Przejazd rolety" : "Shutter travel");
    assert.equal(openingTravel.editor.root, root);
    assert.equal(openingTravel.counters()[1], travel);
    assert.equal(openingTravel.calls.length, 0);
    assert.equal(openingTravel.events.length, 0);
  });
}
