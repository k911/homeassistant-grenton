// Exercise the popup against HA-shaped flow responses, including lifecycle
// transitions where HA keeps a custom panel mounted after navigation.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../custom_components/homeassistant_grenton/frontend/scene-editor.js", import.meta.url), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));
const descendants = (element) => [element, ...element.children.flatMap(descendants)];

class Element {
  constructor(tag = "element") {
    this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {};
    this.isConnected = true;
  }
  attachShadow() { return this.shadowRoot = new Element("shadow"); }
  append(...children) { this.children.push(...children); }
  replaceChildren() {
    for (const child of this.children) for (const node of descendants(child)) node.isConnected = false;
    this.children = [];
  }
  setAttribute() {}
  addEventListener(name, listener) { this.listeners[name] = listener; }
  dispatchEvent(event) { this.listeners[event.type]?.(event); }
  fire(name, detail) { this.dispatchEvent({ type: name, detail, stopPropagation() {}, preventDefault() {} }); }
  querySelectorAll(selector) {
    return descendants(this).filter((node) => selector.split(",").map((tag) => tag.trim()).includes(node.tag));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0]; }
  reportValidity() { return this.disabled || !this.required || (this.value !== undefined && this.value !== ""); }
  showModal() { this.open = true; }
}

const selection = { type: "form", flow_id: "flow1", step_id: "entity_list", last_step: false,
  data_schema: [{ name: "entity", required: true, selector: { entity: { include_entities: ["switch.office_0", "switch.office_1"] } } }] };
const inventory = { device_id: "office-device", name: "Office", widget_type: "ON_OFF_DOUBLE",
  entities: [
    { entity_id: "switch.office_0", name: "Channel 1", configurable: true },
    { entity_id: "switch.office_1", name: "Channel 2", configurable: true },
    { entity_id: "sensor.office_status", name: "Status", configurable: false },
  ], flow: selection };

function popup(metadata = inventory, callApi = async () => {}, preUpgrade = false) {
  const definitions = new Map();
  const listeners = new Map();
  const window = {
    location: { pathname: "/grenton-configure/entry/office" },
    history: { replaceState(_state, _title, path) { window.location.pathname = path; } },
    addEventListener(name, callback) { listeners.set(name, callback); },
    removeEventListener(name) { listeners.delete(name); },
    dispatchEvent(event) { listeners.get(event.type)?.(event); },
  };
  vm.runInNewContext(source, {
    HTMLElement: class extends Element {
      constructor() {
        super();
        if (preUpgrade) {
          Object.defineProperty(this, "hass", { value: hass, configurable: true, writable: true });
          Object.defineProperty(this, "route", { value: { path: "/entry/office" }, configurable: true, writable: true });
        }
      }
    }, structuredClone, queueMicrotask, window, Event,
    CustomEvent: class { constructor(type, options) { this.type = type; Object.assign(this, options); } },
    document: { createElement(tag) { return definitions.has(tag) ? new (definitions.get(tag))() : new Element(tag); } },
    customElements: { get: (tag) => definitions.get(tag), define: (tag, ctor) => definitions.set(tag, ctor) },
  });
  const calls = [];
  const hass = {
    language: "en", states: { "switch.office_1": { attributes: { friendly_name: "Desk light" } } },
    localize: () => "", loadBackendTranslation: async () => {},
    callWS: async (command) => { calls.push(command); return typeof metadata === "function" ? metadata() : structuredClone(metadata); },
    callApi: async (...args) => { calls.push(args); return callApi(...args); },
  };
  const panel = new (definitions.get("grenton-device-configuration"))();
  panel.hass = hass; panel.route = { path: "/entry/office" }; panel.connectedCallback();
  panel.field = (name) => descendants(panel.shadowRoot).find((node) => node.dataset.field === name);
  panel.text = () => descendants(panel.shadowRoot).map((node) => node.textContent || "").join("\n");
  return { panel, hass, calls, window };
}

test("popup shows device inventory, validates selection, and uses existing typed action editor before saving", async () => {
  const draft = { call_type: "METHOD", clu_id: "clu1", object_name: "DOUT", index: "2",
    arguments: [{ type: "number", value: 800 }, { type: "number", value: 0 }] };
  const action = { type: "form", flow_id: "flow1", step_id: "configure_scene_action", last_step: true,
    data_schema: [{ name: "action", description: { suggested_value: draft },
      selector: { grenton_scene: { clus: [{ value: "clu1", label: "CLU" }] } } }] };
  const { panel, calls } = popup(inventory, async (method, _path, data) => {
    if (method !== "POST") return;
    return data.entity ? structuredClone(action) : { type: "create_entry", title: "", data: {} };
  });
  await flush();
  assert.match(panel.text(), /Desk light/);
  assert.match(panel.text(), /sensor.office_status · Read only/);
  assert.deepEqual(panel.field("entity").children.map((option) => option.value), ["", "switch.office_0", "switch.office_1"]);
  await panel._submit();
  assert.equal(calls.length, 1, "missing required selection must not submit");
  panel.field("entity").value = "switch.office_1"; panel.field("entity").fire("change");
  await panel._submit(); await flush();
  const editor = panel._controls[0];
  assert.equal(editor.selector.grenton_scene.clus[0].value, "clu1");
  assert.deepEqual(editor.value, draft);
  const controls = descendants(editor.shadowRoot);
  assert.equal(controls.find((node) => node.dataset.field === "argument_0_value").value, 800);
  assert.equal(controls.find((node) => node.dataset.field === "argument_1_value").value, 0);
  editor.fire("value-changed", { value: { ...draft, arguments: [{ type: "number", value: 900 }] } });
  await panel._submit();
  assert.equal(calls[2][2].action.arguments[0].value, 900);
  assert.match(panel.text(), /Configuration saved/);
  await panel._close();
  assert.equal(calls.filter((call) => call[0] === "DELETE").length, 0, "completed flows need no cancellation");
});

test("close cancels unfinished flow and navigates to this device; returning starts a new flow", async () => {
  const { panel, calls, window } = popup();
  await flush();
  await panel._close();
  assert.equal(window.location.pathname, "/config/devices/device/office-device");
  assert.deepEqual(calls[1], ["DELETE", "config/config_entries/options/flow/flow1"]);
  assert.equal(panel.shadowRoot.children.length, 0);
  window.location.pathname = "/grenton-configure/entry/office";
  window.dispatchEvent(new Event("popstate")); await flush();
  assert.equal(calls.filter((call) => call.type === "grenton/device_configuration").length, 2);
  assert.ok(descendants(panel.shadowRoot).find((node) => node.tag === "dialog" && node.open));
});

test("properties supplied before the module loads still open exactly one popup flow", async () => {
  const { panel, calls } = popup(inventory, async () => {}, true);
  await flush();
  assert.match(panel.text(), /Office · ON_OFF_DOUBLE/);
  assert.ok(panel.field("entity"));
  assert.equal(calls.filter((call) => call.type === "grenton/device_configuration").length, 1);
});

test("late startup response is cancelled after leaving the panel and read-only devices have no save button", async () => {
  let finish;
  const { panel, calls, window } = popup(() => new Promise((resolve) => { finish = resolve; }));
  window.location.pathname = "/config/devices/dashboard";
  window.dispatchEvent(new Event("location-changed"));
  finish(structuredClone(inventory)); await flush();
  assert.equal(panel.shadowRoot.children.length, 0);
  assert.deepEqual(calls[1], ["DELETE", "config/config_entries/options/flow/flow1"]);
  const readOnly = popup({ ...inventory, flow: null, entities: [
    ...inventory.entities,
    { entity_id: "switch.clu_use_cloud", name: "Use cloud", configurable: false, configuration_control: true },
  ] }); await flush();
  assert.match(readOnly.panel.text(), /These entities have no additional configuration/);
  assert.match(readOnly.panel.text(), /switch.clu_use_cloud · Configuration control/);
  assert.deepEqual(descendants(readOnly.panel.shadowRoot).filter((node) => node.tag === "button").map((node) => node.textContent), ["Close"]);
});

test("CLU popup accepts variable name and optional label as text and saves the selected Grenton type", async () => {
  const menu = { type: "form", flow_id: "flow1", step_id: "clu_variables", last_step: false,
    data_schema: [{ name: "operation", required: true, selector: { select: { options: ["add"], translation_key: "clu_variable_operations" } } }] };
  const definition = { type: "form", flow_id: "flow1", step_id: "configure_clu_variable", last_step: false,
    data_schema: [
      { name: "variable_name", required: true, default: "", selector: { text: {} } },
      { name: "label", required: false, default: "", selector: { text: {} } },
      { name: "grenton_type", required: true, default: "STRING", selector: { select: { options: ["BOOLEAN", "STRING", "INTEGER", "FLOAT"], translation_key: "grenton_variable_types" } } },
    ] };
  const { panel, calls } = popup({ ...inventory, widget_type: "CLU", flow: menu }, async (_method, _path, data) => {
    return data.operation ? structuredClone(definition) : { type: "create_entry", title: "", data: {} };
  });
  await flush();
  panel.field("operation").value = "add"; panel.field("operation").fire("change");
  await panel._submit(); await flush();
  assert.equal(panel.field("variable_name").tag, "input");
  assert.equal(panel.field("variable_name").required, true);
  assert.equal(panel.field("label").tag, "input");
  assert.equal(panel.field("label").required, false);
  await panel._submit();
  assert.equal(calls.length, 2, "empty required variable name prevents saving");
  panel.field("variable_name").value = "OfficeEnabled"; panel.field("variable_name").fire("input");
  panel.field("grenton_type").value = "BOOLEAN"; panel.field("grenton_type").fire("change");
  await panel._submit(); await flush();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[2][2])), { variable_name: "OfficeEnabled", label: "", grenton_type: "BOOLEAN" });
  assert.match(panel.text(), /Configuration saved/);
});
