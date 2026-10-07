// These selectors are mounted by HA's ha-selector inside its normal forms.
// They only register Grenton elements; no stock frontend elements are modified.
const STYLE = `
  :host { display: block; color: var(--primary-text-color); }
  * { box-sizing: border-box; }
  .fields, .arguments { display: grid; gap: 16px; }
  label { display: grid; gap: 6px; min-width: 0; font-size: 14px; }
  input, select, textarea, button {
    font: inherit; color: var(--primary-text-color, #202124);
    background: var(--card-background-color, white);
    border: 1px solid var(--divider-color, #aaa); border-radius: 6px;
  }
  input, select, textarea { width: 100%; min-height: 42px; padding: 10px; }
  input[type=checkbox] { width: 24px; height: 24px; min-height: 24px; }
  textarea { min-height: 72px; resize: vertical; }
  button { padding: 8px 12px; cursor: pointer; }
  button:hover { background: var(--secondary-background-color, #f5f5f5); }
  button:disabled { opacity: .5; cursor: default; }
  :is(input, select, textarea, button):focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4); outline-offset: 2px;
  }
  .argument { border: 1px solid var(--divider-color, #ddd); border-radius: 8px; padding: 12px; }
  .heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; }
  .heading strong { font-size: 14px; }
  .buttons { display: flex; gap: 6px; }
  .inputs { display: grid; grid-template-columns: minmax(100px, 1fr) minmax(140px, 2fr); gap: 12px; }
  .hint { font-size: 13px; color: var(--secondary-text-color, #666); margin: 0; }
  .add { justify-self: start; color: var(--primary-color, #03a9f4); }
  .arguments { margin-top: 16px; }
  @media (max-width: 400px) { .inputs { grid-template-columns: 1fr; } }
`;

const copy = (value) => value === undefined ? undefined : structuredClone(value);
const argumentRows = (rows) => rows?.map((row) => {
  if (!row?.argument) return row;
  const type = row.argument.active_choice;
  return { type, value: type === "nil" ? null : row.argument[type] };
});
const TYPES = ["string", "number", "float", "boolean", "nil", "lua"];
const CALL_TYPES = ["SCRIPT", "METHOD", "ATTRIBUTE", "VARIABLE"];

class GrentonEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._disabled = false;
    // HA can set properties before the extra module has finished loading.
    // Replay those own properties through the setters when upgrading the tag.
    for (const name of ["hass", "selector", "value", "localizeValue", "disabled"]) {
      if (Object.prototype.hasOwnProperty.call(this, name)) {
        const value = this[name];
        delete this[name];
        this[name] = value;
      }
    }
  }

  set hass(value) {
    const previousLanguage = this._hass?.language;
    this._hass = value;
    if (previousLanguage !== value?.language) this._scheduleRender();
  }
  get hass() { return this._hass; }
  set selector(value) {
    if (JSON.stringify(value) === JSON.stringify(this._selector)) return;
    this._selector = value;
    this._scheduleRender();
  }
  get selector() { return this._selector; }
  set value(value) {
    value = this._prepareValue(value);
    if (JSON.stringify(value) === JSON.stringify(this._value)) return;
    this._value = copy(value);
    this._scheduleRender();
  }
  get value() { return copy(this._value); }
  _prepareValue(value) { return value; }
  set localizeValue(value) {
    if (value === this._localizeValue) return;
    this._localizeValue = value;
    this._scheduleRender();
  }
  set disabled(value) {
    if (value === this._disabled) return;
    this._disabled = value;
    this._scheduleRender();
  }
  get disabled() { return this._disabled; }

  connectedCallback() { this._scheduleRender(); }

  _scheduleRender() {
    if (this._renderPending) return;
    this._renderPending = true;
    queueMicrotask(() => {
      this._renderPending = false;
      if (this.isConnected) this._render();
    });
  }

  _text(key, fallback) {
    return this._localizeValue?.(key) || fallback;
  }

  _label(name, fallback) {
    return this._text(`scene_editor.fields.${name}.name`, fallback);
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("value-changed", {
      detail: { value: this.value }, bubbles: true, composed: true,
    }));
  }

  _container() {
    this.shadowRoot.replaceChildren();
    const style = document.createElement("style");
    style.textContent = STYLE;
    const content = document.createElement("div");
    content.className = "fields";
    this.shadowRoot.append(style, content);
    return content;
  }

  _input(name, value, onChange, type = "text", required = true) {
    const control = document.createElement(type === "lua" ? "textarea" : "input");
    control.dataset.field = name;
    if (type !== "lua") control.type = type;
    if (type === "number") control.step = "any";
    if (type === "checkbox") control.checked = value === true;
    else control.value = value ?? "";
    control.required = required && type !== "checkbox";
    control.disabled = this.disabled;
    control.addEventListener("input", () => {
      const next = type === "checkbox" ? control.checked
        : type === "number" && control.value !== "" && !control.validity.badInput
          ? control.valueAsNumber : control.value;
      onChange(next);
      this._emit();
    });
    return control;
  }

  _select(name, options, value, onChange) {
    const control = document.createElement("select");
    control.dataset.field = name;
    control.required = true;
    control.disabled = this.disabled;
    // Keep an unselected field empty until the user makes a real selection.
    if (!options.some((option) => option.value === value)) {
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "—";
      control.append(empty);
    }
    for (const item of options) {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      control.append(option);
    }
    control.value = value ?? "";
    control.addEventListener("change", () => {
      onChange(control.value);
      this._emit();
      if (!control.isConnected) {
        this.shadowRoot.querySelector(`select[data-field="${name}"]`)?.focus();
      }
    });
    return control;
  }

  _field(labelText, control) {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = labelText;
    label.append(caption, control);
    return label;
  }

  _button(text, action, disabled = false, title = text) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.title = title;
    button.setAttribute("aria-label", title);
    button.disabled = disabled || this.disabled;
    button.addEventListener("click", action);
    return button;
  }

  _renderArguments(parent, rows, onRowsChange) {
    const argumentsContainer = document.createElement("div");
    argumentsContainer.className = "arguments";
    const changeRows = (next) => {
      onRowsChange(next);
      this._emit();
      this._render();
    };
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = this._text("scene_editor.fields.arguments.description",
      "Omit trailing arguments to use the script or method defaults.");
    argumentsContainer.append(hint);
    rows.forEach((argument, index) => {
      const row = document.createElement("div");
      row.className = "argument";
      row.dataset.argument = index;
      const heading = document.createElement("div");
      heading.className = "heading";
      const title = document.createElement("strong");
      title.textContent = `${this._label("argument", "Argument")} ${index + 1}`;
      const buttons = document.createElement("div");
      buttons.className = "buttons";
      const move = (offset) => {
        const next = copy(rows);
        [next[index], next[index + offset]] = [next[index + offset], next[index]];
        changeRows(next);
      };
      buttons.append(
        this._button("↑", () => move(-1), index === 0, this._label("move_up", "Move up")),
        this._button("↓", () => move(1), index === rows.length - 1, this._label("move_down", "Move down")),
        this._button(this._label("remove", "Remove"), () => changeRows(rows.filter((_, i) => i !== index))),
      );
      heading.append(title, buttons);
      const inputs = document.createElement("div");
      inputs.className = "inputs";
      const type = this._select(`argument_${index}_type`, TYPES.map((value) => ({
        value, label: this._text(`scene_argument_types.choices.${value}`, value),
      })), argument.type, (value) => {
        argument.type = value;
        argument.value = value === "nil" ? null : value === "boolean" ? false
          : value === "number" || value === "float" ? 0 : "";
        onRowsChange(rows);
        this._render();
      });
      const input = this._input(`argument_${index}_value`,
        argument.type === "nil" ? "nil" : argument.value,
        (value) => { argument.value = value; onRowsChange(rows); },
        argument.type === "boolean" ? "checkbox"
          : argument.type === "number" || argument.type === "float" ? "number"
          : argument.type === "lua" ? "lua" : "text", argument.type !== "string");
      if (argument.type === "nil") input.disabled = true;
      inputs.append(
        this._field(this._label("argument_type", "Type"), type),
        this._field(this._label("argument_value", "Value"), input),
      );
      row.append(heading, inputs);
      argumentsContainer.append(row);
    });
    const add = this._button(this._label("add_argument", "Add argument"), () => {
      changeRows([...rows, { type: "string", value: "" }]);
      const inputs = this.shadowRoot.querySelectorAll(".argument input:not([disabled])");
      inputs[inputs.length - 1]?.focus();
    });
    add.className = "add";
    argumentsContainer.append(add);
    parent.append(argumentsContainer);
  }

  reportValidity() {
    for (const control of this.shadowRoot.querySelectorAll("input, select, textarea")) {
      if (!control.reportValidity()) return false;
    }
    return true;
  }

  focus() {
    (this.shadowRoot.querySelector(":invalid") ||
      this.shadowRoot.querySelector("select, input, textarea, button"))?.focus();
  }
}

class GrentonSceneEditor extends GrentonEditor {
  _prepareValue(value) {
    return value ? { ...value, arguments: argumentRows(value.arguments) } : value;
  }
  _render() {
    const parent = this._container();
    const draft = this._value ?? {};
    const type = draft.call_type ?? "";
    const settings = this.selector?.grenton_scene ?? {};
    const state = settings.mode === "state";
    const callTypes = state ? ["ATTRIBUTE", "VARIABLE"] : CALL_TYPES;
    const control = (name, label, value = draft[name]) => this._field(
      this._label(name, label),
      this._input(name, value, (next) => { this._value = { ...this._value, [name]: next }; }),
    );
    parent.append(this._field(this._label(state ? "state_call_type" : "call_type",
      state ? "State source type" : "Action call type"), this._select(
      "call_type", callTypes.map((value) => ({
        value, label: this._text(`scene_call_types.options.${value}`, value),
      })), type, (value) => {
        this._value = { ...this._value, call_type: value };
        this._render();
      },
    )));
    parent.append(this._field(this._label("clu_id", "CLU"), this._select(
      "clu_id", settings.clus ?? [], draft.clu_id,
      (value) => { this._value = { ...this._value, clu_id: value }; },
    )));
    if (!callTypes.includes(type)) return;
    const renderValue = () => {
      if (!state && settings.editable_value) {
        parent.append(this._field(this._label("set_value", "Value to set"),
          this._input("value", draft.value,
            (value) => { this._value = { ...this._value, value }; }, "text", false)));
      }
    };
    if (type === "VARIABLE") {
      parent.append(control("variable_name", "Variable name", draft.variable_name));
      renderValue();
      return;
    }
    parent.append(this._field(this._label(type === "SCRIPT" ? "script_name" : "object_name",
      type === "SCRIPT" ? "Script name" : "Object name"),
      this._input("object_name", draft.object_name,
        (value) => { this._value = { ...this._value, object_name: value }; })));
    if (type !== "SCRIPT") {
      parent.append(this._field(this._label(type === "METHOD" ? "method_index" : "attribute_index",
        type === "METHOD" ? "Method index" : "Attribute index"),
        this._input("index", draft.index, (value) => { this._value = { ...this._value, index: value }; })));
    }
    if (type === "SCRIPT" || type === "METHOD") {
      this._renderArguments(parent, draft.arguments ?? [],
        (rows) => { this._value = { ...this._value, arguments: rows }; });
    }
    if (type === "ATTRIBUTE") renderValue();
  }
}

class GrentonArgumentsEditor extends GrentonEditor {
  _prepareValue(value) { return argumentRows(value); }
  _render() {
    const parent = this._container();
    const label = document.createElement("strong");
    label.textContent = this.label || this._label("arguments", "Arguments");
    parent.append(label);
    this._renderArguments(parent, this._value ?? [],
      (rows) => { this._value = rows; });
  }
}

if (!customElements.get("ha-selector-grenton_scene")) {
  customElements.define("ha-selector-grenton_scene", GrentonSceneEditor);
}
if (!customElements.get("ha-selector-grenton_arguments")) {
  customElements.define("ha-selector-grenton_arguments", GrentonArgumentsEditor);
}
