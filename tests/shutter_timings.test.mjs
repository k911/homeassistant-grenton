import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const component = new URL("../custom_components/homeassistant_grenton/", import.meta.url);
const source = readFileSync(new URL("frontend/scene-editor.js", component), "utf8");

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this.isConnected = true; this.validity = { badInput: false };
  }
  get valueAsNumber() { return Number(this.value); }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { (this.attributes ??= {})[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  fire(name) { if (!this.disabled && !this.readOnly) this.listeners[name]?.(); }
}
const descendants = (node) => [node, ...node.children.flatMap(descendants)];

function editor({ language = "en", keepTotal = true, direction = "up", configured = {} } = {}) {
  const elements = new Map();
  vm.runInNewContext(source, {
    HTMLElement: class {}, structuredClone,
    CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    document: { createElement: (tag) => new Element(tag) },
    customElements: { get: (name) => elements.get(name), define: (name, cls) => elements.set(name, cls) },
  });
  const view = Object.create(elements.get("ha-selector-grenton_shutter_timings").prototype);
  const translations = JSON.parse(readFileSync(new URL(`translations/${language}.json`, component), "utf8")).selector;
  view._selector = { grenton_shutter_timings: {
    direction, measured_ms: 27789, measured_travel_ms: 24321, measured_lamel_ms: 3468,
    configured_up_ms: 22000, configured_down_ms: 21500, configured_lamel_ms: 4500, ...configured,
  } };
  view._value = { total_ms: 27000, travel_ms: 24000, lamel_move_timeout: 3000, keep_total: keepTotal };
  view._hass = { language };
  view._localizeValue = (key) => key.split(".").reduce((node, part) => node?.[part], translations);
  view._container = () => (view.root = new Element("div"));
  view.dispatchEvent = (event) => { view.emitted = structuredClone(event.detail.value); };
  view.field = (key) => descendants(view.root).find((node) => node.dataset.field === key);
  view.measurements = () => descendants(view.root).filter((node) => node.dataset.measured);
  view.configured = () => descendants(view.root).filter((node) => node.dataset.configured);
  view.edit = (key, value) => { view.field(key).value = String(value); view.field(key).fire("input"); };
  view.sync = (checked) => { view.field("keep_total").checked = checked; view.field("keep_total").fire("input"); };
  view._render();
  return view;
}

for (const language of ["en", "pl"]) {
  test(`${language}: original total, slat and travel measurements stay read-only when fields change`, () => {
    const view = editor({ language });
    const nodes = view.measurements(), texts = nodes.map((node) => node.textContent);
    assert.equal(nodes.length, 3);
    assert.match(texts[0], /27789 ms/);
    assert.match(texts[1], /3468 ms/);
    assert.match(texts[2], /24321 ms/);
    assert.ok(nodes.every((node) => node.tag === "dd"));
    const root = view.root, slat = view.field("lamel_move_timeout"), travel = view.field("travel_ms");
    view.edit("lamel_move_timeout", 4000);
    assert.equal(view.emitted.total_ms, 27000);
    assert.equal(view.emitted.travel_ms, 23000);
    assert.equal(view.emitted.lamel_move_timeout, 4000);
    view.edit("travel_ms", 25000);
    assert.equal(view.emitted.lamel_move_timeout, 2000);
    view.edit("total_ms", 28000);
    assert.equal(view.emitted.travel_ms, 26000);
    assert.equal(view.root, root);
    assert.equal(view.field("lamel_move_timeout"), slat);
    assert.equal(view.field("travel_ms"), travel);
    assert.deepEqual(view.measurements().map((node) => node.textContent), texts);
    assert.equal(view.field("keep_total").checked, true);
  });
}

test("unchecked synchronization allows independent times and calculates the total", () => {
  const view = editor();
  view.sync(false);
  assert.equal(view.field("total_ms").readOnly, true);
  view.edit("lamel_move_timeout", 4000);
  assert.equal(view.emitted.travel_ms, 24000);
  assert.equal(view.emitted.total_ms, 28000);
  view.edit("travel_ms", 25000);
  assert.equal(view.emitted.lamel_move_timeout, 4000);
  assert.equal(view.emitted.total_ms, 29000);
  assert.equal(view.emitted.keep_total, false);
  view.sync(true);
  assert.equal(view.field("total_ms").readOnly, false);
  view.edit("lamel_move_timeout", 5000);
  assert.equal(view.emitted.travel_ms, 24000);
  assert.equal(view.emitted.total_ms, 29000);
});

test("an unchecked saved preference remains unchecked after rendering", () => {
  const view = editor({ keepTotal: false });
  assert.equal(view.field("keep_total").checked, false);
  assert.equal(view.field("total_ms").readOnly, true);
  view.edit("lamel_move_timeout", 3500);
  assert.equal(view.emitted.travel_ms, 24000);
  assert.equal(view.emitted.total_ms, 27500);
});

test("invalid drafts stay editable without silently clamping values", () => {
  const view = editor();
  const input = view.field("lamel_move_timeout");
  view.edit("lamel_move_timeout", 28000);
  assert.equal(view.emitted.travel_ms, -1000);
  view.edit("lamel_move_timeout", "");
  assert.equal(view.emitted.lamel_move_timeout, "");
  view.edit("lamel_move_timeout", 3000);
  assert.equal(view.emitted.travel_ms, 24000);
  assert.equal(view.field("lamel_move_timeout"), input);
  assert.equal(input.min, "1");
  assert.equal(input.max, "600000");
  assert.equal(input.step, "1");
});

for (const language of ["en", "pl"]) {
  for (const direction of ["up", "down"]) {
    test(`${language}/${direction}: current configuration and measured travel direction remain clear while editing`, () => {
      const view = editor({ language, direction });
      const configured = view.configured(), before = configured.map((node) => node.textContent);
      assert.equal(configured.length, 3);
      assert.match(before[0], /22000 ms/);
      assert.match(before[1], /21500 ms/);
      assert.match(before[2], /4500 ms/);
      assert.ok(configured.every((node) => node.tag === "dd"));
      const measuredRow = descendants(view.root).find((node) =>
        node.children.some((child) => child.dataset.measured === "measured_travel_ms"));
      assert.equal(measuredRow.children[0].textContent, language === "pl"
        ? `Przejazd rolety w ${direction === "up" ? "górę" : "dół"}` : `Shutter travel ${direction}`);
      const travelField = descendants(view.root).find((node) =>
        node.tag === "label" && node.children.some((child) => child.dataset.field === "travel_ms"));
      assert.equal(travelField.children[0].textContent, language === "pl"
        ? `Czas przejazdu rolety w ${direction === "up" ? "górę" : "dół"} (ms)`
        : `${direction === "up" ? "Opening" : "Closing"} travel time (ms)`);
      const syncIndex = view.root.children.findIndex((node) => node.className === "shutter-sync");
      const explanation = view.root.children[syncIndex + 1];
      assert.equal(explanation.className, "hint");
      assert.match(explanation.textContent, language === "pl" ? /Odznacz/ : /Uncheck/);
      assert.equal(view.root.children[syncIndex + 2].tag, "label");
      view.edit("lamel_move_timeout", 4000);
      assert.deepEqual(view.configured().map((node) => node.textContent), before);
    });
  }
}

test("unknown configured times are shown as unknown and zero remains a real value", () => {
  const view = editor({ configured: { configured_up_ms: null, configured_down_ms: 0, configured_lamel_ms: null } });
  assert.deepEqual(view.configured().map((node) => node.textContent), ["—", "0 s (0 ms)", "—"]);
});

for (const language of ["en", "pl"]) {
  for (const direction of ["up", "down"]) {
    test(`${language}/${direction}: opposite-direction adaptation is unchecked and requires an explicit choice`, () => {
      const view = editor({ language, direction });
      const checkbox = view.field("adapt_opposite");
      assert.equal(checkbox.checked, false);
      const label = descendants(view.root).find((node) => node.tag === "label" && node.children.includes(checkbox));
      assert.equal(label.children[1].textContent, language === "pl"
        ? `Dostosuj czas ruchu rolety w ${direction === "up" ? "dół" : "górę"}`
        : `Adjust ${direction === "up" ? "closing" : "opening"} travel time`);
      checkbox.checked = true;
      checkbox.fire("input");
      assert.equal(view.emitted.adapt_opposite, true);
      assert.equal(view.emitted.travel_ms, 24000);
      assert.equal(view.emitted.lamel_move_timeout, 3000);
      checkbox.checked = false;
      checkbox.fire("input");
      assert.equal(view.emitted.adapt_opposite, false);
    });
  }
}
