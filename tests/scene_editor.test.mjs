// Exercise the real row renderer with a small DOM substitute; no browser or
// frontend build dependencies are needed for these localization regressions.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const component = new URL("../custom_components/homeassistant_grenton/", import.meta.url);
const source = readFileSync(new URL("frontend/scene-editor.js", component), "utf8");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
  }
  append(...children) { this.children.push(...children); }
  setAttribute() {}
  addEventListener() {}
}

function descendants(element) {
  return [element, ...element.children.flatMap(descendants)];
}

for (const language of ["en", "pl"]) {
  test(`${language}: argument positions need no translation context and Float has a numeric control`, () => {
    const translations = JSON.parse(readFileSync(new URL(`translations/${language}.json`, component), "utf8")).selector;
    const selectors = new Map();
    vm.runInNewContext(source, {
      HTMLElement: class {},
      structuredClone,
      document: { createElement: (tag) => new Element(tag) },
      customElements: {
        get: (name) => selectors.get(name),
        define: (name, constructor) => selectors.set(name, constructor),
      },
    });
    const editor = Object.create(selectors.get("ha-selector-grenton_scene").prototype);
    editor._localizeValue = (key) => {
      const message = key.split(".").reduce((value, part) => value?.[part], translations);
      // HA's localizeValue callback provides no ICU interpolation values.
      if (message) assert.doesNotMatch(message, /\{[^}]+\}/);
      return message;
    };
    const root = new Element("div");
    editor._renderArguments(root, [
      { type: "float", value: 1.25 },
      { type: "number", value: 2 },
    ], () => {});
    const rendered = descendants(root);
    assert.deepEqual(rendered.filter((element) => element.tag === "strong").map((element) => element.textContent), ["Argument 1", "Argument 2"]);
    const typePicker = rendered.find((element) => element.dataset.field === "argument_0_type");
    assert.equal(typePicker.value, "float");
    const floatChoice = typePicker.children.find((option) => option.value === "float");
    assert.equal(floatChoice.textContent, language === "pl" ? "Liczba zmiennoprzecinkowa" : "Float");
    const floatInput = rendered.find((element) => element.dataset.field === "argument_0_value");
    assert.equal(floatInput.type, "number");
    assert.equal(floatInput.step, "any");
    assert.equal(floatInput.value, 1.25);
  });
}
