// Exercise the real popup renderer and events without a Home Assistant server.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const component = new URL("../custom_components/homeassistant_grenton/", import.meta.url);
const source = readFileSync(new URL("frontend/scene-editor.js", component), "utf8");
const DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"];

class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this.isConnected = true;
  }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
  addEventListener(event, listener) { this.listeners[event] = listener; }
  fire(event) { if (!this.disabled) this.listeners[event]?.(); }
}
const descendants = (element) => [element, ...element.children.flatMap(descendants)];

function makeEditor({ language = "en", settings = {}, sections, edit = true } = {}) {
  const elements = new Map();
  vm.runInNewContext(source, {
    HTMLElement: class {}, structuredClone,
    document: { createElement: (tag) => new Element(tag) },
    CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    customElements: { get: (name) => elements.get(name), define: (name, cls) => elements.set(name, cls) },
  });
  const editor = Object.create(elements.get("ha-selector-grenton_thermostat").prototype);
  editor._value = { cooling_enabled: false, edit_schedule: edit, sections: structuredClone(sections ?? [{
    days: DAYS, periods: [{ time: "00:00", temperature: 20 }],
  }]) };
  editor._selector = { grenton_thermostat: { cooling_available: true, schedule_available: true, minimum: 10, maximum: 30, ...settings } };
  const translations = JSON.parse(readFileSync(new URL(`translations/${language}.json`, component), "utf8")).selector;
  editor._localizeValue = (key) => key.split(".").reduce((value, part) => value?.[part], translations);
  editor._container = () => (editor.root = new Element("div"));
  editor.dispatchEvent = (event) => { editor.emitted = structuredClone(event.detail.value); };
  editor.field = (name) => descendants(editor.root).find((item) => item.dataset.field === name);
  editor.section = (index) => descendants(editor.root).find((item) => item.dataset.section === index);
  editor.button = (text, root = editor.root) => descendants(root).find((item) => item.tag === "button" && item.textContent === text);
  editor._render();
  return editor;
}

for (const language of ["en", "pl"]) {
  test(`${language}: thermostat popup localizes controls and keeps schedule opt-in`, () => {
    const editor = makeEditor({ language, edit: false });
    assert.equal(editor.section(0), undefined);
    assert.equal(editor.field("cooling_enabled").checked, false);
    assert.match(descendants(editor.root).map((item) => item.textContent ?? "").join(" "),
      language === "pl" ? /Włącz chłodzenie/ : /Enable cooling/);
    editor.field("edit_schedule").checked = true;
    editor.field("edit_schedule").fire("input");
    assert.ok(editor.section(0));
    assert.equal(editor.field("section_0_period_0_temperature").step, "0.5");
    assert.equal(editor.field("section_0_period_0_temperature").min, "10");
    assert.equal(editor.field("section_0_period_0_temperature").max, "30");
    assert.equal(editor.field("section_0_period_0_time").disabled, true);
  });
}

test("day sections support presets, individual weekdays and removal", () => {
  const editor = makeEditor();
  editor.field("section_0_days").value = "weekdays";
  editor.field("section_0_days").fire("change");
  assert.deepEqual(editor.emitted.sections[0].days, DAYS.slice(0, 5));
  editor.button("Add section").fire("click");
  assert.deepEqual(editor.emitted.sections[1].days, DAYS.slice(5));
  assert.equal(editor.field("section_1_days").value, "weekend");
  editor.field("section_1_sunday").checked = false;
  editor.field("section_1_sunday").fire("input");
  assert.deepEqual(editor.emitted.sections[1].days, ["saturday"]);
  assert.equal(editor.field("section_1_days").value, "custom");
  editor.button("Remove section", editor.section(1)).fire("click");
  assert.equal(editor.emitted.sections.length, 1);
});

test("periods use quarter-hour times, numeric half-degree temperatures and dynamic rows", () => {
  const editor = makeEditor();
  editor.button("Add period").fire("click");
  assert.equal(editor.emitted.sections[0].periods[1].time, "00:15");
  const time = editor.field("section_0_period_1_time");
  assert.equal(time.step, "900");
  assert.equal(time.max, "23:45");
  time.value = "09:00"; time.fire("input"); time.fire("change");
  const temperature = editor.field("section_0_period_1_temperature");
  temperature.value = "22.5"; temperature.valueAsNumber = 22.5; temperature.validity = { badInput: false };
  temperature.fire("input");
  assert.deepEqual(editor.emitted.sections[0].periods, [
    { time: "00:00", temperature: 20 }, { time: "09:00", temperature: 22.5 },
  ]);
  const remove = descendants(editor.section(0)).filter((item) => item.tag === "button" && item.textContent === "Remove period");
  assert.equal(remove[0].disabled, true);
  remove[1].fire("click");
  assert.equal(editor.emitted.sections[0].periods.length, 1);
});

test("seven-section and 96-period limits disable additional rows", () => {
  const periods = Array.from({ length: 96 }, (_, slot) => ({
    time: `${String(Math.floor(slot / 4)).padStart(2, "0")}:${String(slot % 4 * 15).padStart(2, "0")}`, temperature: 20,
  }));
  const editor = makeEditor({ sections: DAYS.map((day) => ({ days: [day], periods })) });
  assert.equal(editor.button("Add section").disabled, true);
  assert.equal(editor.button("Add period", editor.section(0)).disabled, true);
});

test("unavailable schedule feedback and bindings disable dependent editors", () => {
  const editor = makeEditor({ settings: { cooling_available: false, schedule_available: false } });
  assert.equal(editor.field("cooling_enabled").disabled, true);
  assert.equal(editor.field("edit_schedule").disabled, true);
  assert.equal(editor.section(0), undefined);
});

test("enabling cooling reveals an independent direction selector without selecting a manual mode", () => {
  const editor = makeEditor({ edit: false, settings: { direction: "heating" } });
  assert.equal(editor.field("control_direction"), undefined);
  editor.field("cooling_enabled").checked = true;
  editor.field("cooling_enabled").fire("input");
  assert.equal(editor.field("control_direction").value, "unchanged");
  editor.field("control_direction").value = "cooling";
  editor.field("control_direction").fire("change");
  assert.equal(editor.emitted.control_direction, "cooling");
  assert.equal(editor.emitted.edit_schedule, false);
  assert.equal(editor.emitted.mode, undefined);
});
