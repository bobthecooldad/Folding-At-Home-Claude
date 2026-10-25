/*
 * Folding@home dashboard card for Home Assistant.
 *
 *   type: custom:foldingathome-card
 *   title: Folding@home        # optional
 *   projects_height: 400       # optional, px height of the scrolling projects box
 *   show_projects: true        # optional, show the bottom Impact / Projects box
 *   projects_view: impact      # optional, opens on "impact" (topic stats) or "details"
 *   research_hours: 24         # optional, Impact time window in hours (0 = all time)
 *
 *   type: custom:foldingathome-research-card   # just the Impact statistics
 *   hours: 24                  # optional, 0 = all time (finish times are kept 31 days)
 *   title: ...                 # optional, defaults to your donor name
 *   show_running: true         # optional, outlined blocks for WUs running now
 *
 * The layout (which entities belong to which machine, GPU, ...) comes from the
 * integration's `foldingathome/dashboard` websocket command, so new machines
 * and GPUs appear by themselves. Live values come from normal HA states.
 */

const CARD_VERSION = "1.3.0";

const COLORS = {
  green: "var(--success-color, #43a047)",
  orange: "var(--warning-color, #ffa600)",
  red: "var(--error-color, #db4437)",
  grey: "var(--disabled-text-color, #9e9e9e)",
};

const MACHINE_STATUS = {
  folding: ["green", "Folding"],
  finishing: ["orange", "Finishing"],
  paused: ["red", "Stopped"],
  waiting: ["red", "Waiting"],
  idle: ["red", "Idle"],
  offline: ["grey", "Offline"],
};

const SOLAR_STATUS = {
  disabled: "Off",
  no_sensor: "No surplus sensor",
  waiting_for_surplus: "Waiting for surplus",
  queued: "Queued (higher priority first)",
  starting: "Starting…",
  running_on_solar: "Running on solar",
  stopping: "Stopping…",
};

const STOP_ACTIONS = { pause: "Pause", finish: "Finish WU", smart: "Smart" };

const GROUP_SWITCH_LABELS = {
  on_idle: "Only when idle",
  on_battery: "On battery",
  keep_awake: "Keep awake",
  beta: "Beta",
};

function esc(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function slotColor(status) {
  if (status == null || status === "unavailable" || status === "unknown") return "grey";
  const s = String(status).toLowerCase();
  if (s.startsWith("finish")) return "orange";
  if (["running", "downloading", "uploading", "assigning", "core"].includes(s)) return "green";
  if (s === "unsupported") return "grey";
  return "red";
}

function fmtNumber(value) {
  const n = Number(value);
  if (!isFinite(n)) return "–";
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function fmtEta(seconds) {
  const s = Number(seconds);
  if (!isFinite(s) || s <= 0) return "";
  const h = Math.floor(s / 3600);
  const m = Math.round((s % 3600) / 60);
  if (h >= 24) return `${Math.floor(h / 24)}d ${h % 24}h`;
  return h ? `${h}h ${m}m` : `${m}m`;
}

function fmtDate(value) {
  if (!value) return "";
  const d = new Date(String(value).replace(" ", "T"));
  return isNaN(d) ? "" : d.toLocaleDateString();
}

const WINDOWS = [
  [1, "Last hour"], [6, "Last 6 hours"], [12, "Last 12 hours"], [24, "Last 24 hours"],
  [48, "Last 2 days"], [168, "Last 7 days"], [720, "Last 30 days"], [0, "All time"],
];

function windowText(hours) {
  const h = Number(hours) || 0;
  if (!h) return "All time";
  const known = WINDOWS.find(([v]) => v === h);
  if (known) return "Completed in the " + known[1].toLowerCase();
  return h % 24 === 0 ? `Completed in the last ${h / 24} days` : `Completed in the last ${h} hours`;
}

const VIEW_KEY = "foldingathome-card-view";
function loadView() {
  try { return window.localStorage.getItem(VIEW_KEY); } catch (e) { return null; }
}
function saveView(view) {
  try { window.localStorage.setItem(VIEW_KEY, view); } catch (e) { /* private mode */ }
}

/* Topic rows: label + count, and one block per work unit. Filled blocks are
 * finished WUs in the window, outlined blocks are WUs running right now. */
function renderTopics(research, { showRunning = true, action = "more", maxTopics = 0 } = {}) {
  let topics = (research.topics || []).filter((t) => t.wus || (showRunning && t.running));
  if (maxTopics > 0) topics = topics.slice(0, maxTopics);
  if (!topics.length) {
    return `<div class="muted small">No work units ${Number(research.hours) ? "finished in this time window yet" : "recorded yet"}.</div>`;
  }
  const most = Math.max(...topics.map((t) => t.wus + (showRunning ? t.running : 0)), 1);
  // Up to 30 blocks per row; bigger numbers group several WUs per block
  const per = Math.max(1, Math.ceil(most / 30));
  const rows = topics.map((t) => {
    const done = Math.ceil(t.wus / per);
    const running = showRunning ? Math.ceil(t.running / per) : 0;
    const width = ((t.wus + (showRunning ? t.running : 0)) / most) * 100;
    const blocks = '<span class="blk"></span>'.repeat(done) + '<span class="blk run"></span>'.repeat(running);
    const target = action === "more" ? (t.entity || "") : t.topic;
    const title = `${t.projects.length} project${t.projects.length === 1 ? "" : "s"}${t.machines.length ? " · on " + t.machines.join(", ") : ""}`;
    return `<div class="topic" data-action="${action === "more" ? (t.entity ? "more" : "") : "topic"}" data-entity="${esc(target)}" data-topic="${esc(t.topic)}" title="${esc(title)}">
        <div class="row"><span class="tname">${esc(t.label)}</span>
          ${showRunning && t.running ? `<span class="running">${'<span class="dot" style="background:' + COLORS.green + '"></span>'}${esc(t.running)} running</span>` : ""}
          <span class="grow"></span><b>${esc(fmtNumber(t.wus))}</b></div>
        <div class="blocks" style="width:${Math.max(width, 2)}%">${blocks}</div>
      </div>`;
  }).join("");
  const note = per > 1 ? `<div class="muted small">Each block = ${per} work units.</div>` : "";
  return rows + note;
}

const RESEARCH_STYLE = `
  .topic { padding:6px 0; cursor:pointer; }
  .topic .tname { color:var(--primary-text-color); }
  .topic b { color:var(--primary-text-color); }
  .blocks { display:flex; gap:6px; margin-top:6px; min-width:12px; }
  .blk { flex:1; height:10px; border-radius:5px; background:var(--primary-color); min-width:3px; }
  .blk.run { background:transparent; box-shadow:inset 0 0 0 2px ${COLORS.green}; }
  .running { display:inline-flex; align-items:center; gap:4px; font-size:.8em; color:var(--primary-text-color); }
  .running .dot { width:8px; height:8px; border-radius:50%; display:inline-block; }
`;


class FoldingAtHomeCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._layout = null;
    this._error = null;
    this._signature = "";
    this._expanded = new Set();
    this._research = null;
    this._view = null;
    this._topicFilter = null;
    this._onClick = this._onClick.bind(this);
    this._onChange = this._onChange.bind(this);
  }

  static getStubConfig() {
    return { title: "Folding@home", projects_view: "impact", research_hours: 24 };
  }

  static getConfigElement() {
    return document.createElement("foldingathome-card-editor");
  }

  setConfig(config) {
    const old = this._config;
    this._config = {
      title: "Folding@home", projects_height: 400, show_projects: true,
      projects_view: "impact", research_hours: 24, ...config,
    };
    if (old && (old.projects_view !== this._config.projects_view)) this._view = null;
    if (old && Number(old.research_hours) !== Number(this._config.research_hours)) this._loadResearch();
    this._signature = "";
    if (this._hass) this._render();
  }

  get _currentView() {
    return this._view || loadView() || (this._config.projects_view === "details" ? "details" : "impact");
  }

  getCardSize() {
    return 14;
  }

  getGridOptions() {
    return { columns: "full", min_columns: 6 };
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._layout && !this._loading) {
      this._loadLayout();
      this._loadResearch();
    }
    this._render();
  }

  connectedCallback() {
    this.shadowRoot.addEventListener("click", this._onClick);
    this.shadowRoot.addEventListener("change", this._onChange);
    // New machines / GPUs and fresh project info show up without a reload
    this._timer = setInterval(() => {
      this._loadLayout();
      this._loadResearch();
    }, 60000);
  }

  disconnectedCallback() {
    this.shadowRoot.removeEventListener("click", this._onClick);
    this.shadowRoot.removeEventListener("change", this._onChange);
    clearInterval(this._timer);
  }

  async _loadLayout() {
    if (!this._hass) return;
    this._loading = true;
    try {
      const result = await this._hass.callWS({ type: "foldingathome/dashboard" });
      this._layout = result.accounts || [];
      this._error = null;
    } catch (err) {
      this._error = (err && (err.message || err.code)) || String(err);
    }
    this._loading = false;
    this._signature = "";
    this._render();
  }

  async _loadResearch() {
    if (!this._hass || !this._config || !this._config.show_projects) return;
    try {
      const hours = Number(this._config.research_hours) || 0;
      const result = await this._hass.callWS({ type: "foldingathome/research", hours });
      this._research = result.accounts || [];
    } catch (err) {
      this._research = null; // older integration: Impact view just shows a note
    }
    this._signature = "";
    this._render();
  }

  /* ------------------------------------------------------------ helpers */
  _st(entityId) {
    return entityId && this._hass ? this._hass.states[entityId] : undefined;
  }

  _val(entityId) {
    const s = this._st(entityId);
    return s ? s.state : undefined;
  }

  _attr(entityId, name) {
    const s = this._st(entityId);
    return s ? s.attributes[name] : undefined;
  }

  _allEntityIds() {
    const ids = [];
    for (const acc of this._layout || []) {
      ids.push(...Object.values(acc.account || {}));
      for (const m of acc.machines) {
        ids.push(...Object.values(m.entities));
        for (const c of m.cpus) ids.push(...Object.values(c.sensors), c.cpus);
        for (const g of m.gpus) ids.push(...Object.values(g.sensors), g.switch);
        for (const grp of Object.values(m.group_switches || {})) ids.push(...Object.values(grp));
      }
    }
    return ids.filter(Boolean);
  }

  _computeSignature() {
    const parts = this._allEntityIds().map((id) => {
      const s = this._st(id);
      if (!s) return id + "=?";
      const a = s.attributes;
      return `${id}=${s.state}|${a.surplus_w ?? ""}|${a.transition_in_s ?? ""}|${a.max ?? ""}`;
    });
    return JSON.stringify([this._error, this._layout ? this._layout.length : -1, [...this._expanded], parts,
      (this._layout || []).map((a) => a.projects.length), this._currentView, this._topicFilter,
      JSON.stringify(this._research)]);
  }

  /* -------------------------------------------------------------- events */
  _onClick(ev) {
    const el = ev.composedPath().find((n) => n.dataset && n.dataset.action);
    if (!el || !this._hass) return;
    const { action, entity } = el.dataset;
    if (action === "press") {
      this._hass.callService("button", "press", { entity_id: entity });
    } else if (action === "toggle") {
      ev.preventDefault();
      this._hass.callService("switch", "toggle", { entity_id: entity });
    } else if (action === "more") {
      this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: entity }, bubbles: true, composed: true }));
    } else if (action === "expand") {
      const key = el.dataset.key;
      if (this._expanded.has(key)) this._expanded.delete(key);
      else this._expanded.add(key);
      this._render();
    } else if (action === "reload") {
      this._loadLayout();
    } else if (action === "view") {
      this._view = el.dataset.view;
      saveView(this._view);
      if (this._view === "impact") this._topicFilter = null;
      this._render();
    } else if (action === "topic") {
      this._topicFilter = el.dataset.topic;
      this._view = "details";
      this._render();
    } else if (action === "clearfilter") {
      this._topicFilter = null;
      this._render();
    }
  }

  _onChange(ev) {
    const el = ev.target;
    if (!el.dataset || !this._hass) return;
    const { action, entity } = el.dataset;
    if (action === "number") {
      const value = Number(el.value);
      if (isFinite(value)) this._hass.callService("number", "set_value", { entity_id: entity, value });
    } else if (action === "select") {
      this._hass.callService("select", "select_option", { entity_id: entity, option: el.value });
    }
  }

  /* ------------------------------------------------------------- render */
  _render() {
    if (!this._config || !this._hass) return;
    // Don't redraw under the user's fingers while they type a number
    const active = this.shadowRoot.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "SELECT")) return;
    const sig = this._computeSignature();
    if (sig === this._signature) return;
    this._signature = sig;

    let body;
    if (this._error) {
      body = `<div class="msg">Could not load Folding@home data: ${esc(this._error)}
        <button class="btn" data-action="reload">Retry</button></div>`;
    } else if (!this._layout) {
      body = `<div class="msg">Loading…</div>`;
    } else if (!this._layout.length) {
      body = `<div class="msg">No Folding@home account is set up yet.</div>`;
    } else {
      body = this._layout.map((acc) => this._renderAccount(acc)).join("");
    }
    this.shadowRoot.innerHTML = `<style>${STYLE}${RESEARCH_STYLE}</style><ha-card>${body}</ha-card>`;
  }

  _dot(color, title = "") {
    return `<span class="dot" style="background:${COLORS[color] || COLORS.grey}" title="${esc(title)}"></span>`;
  }

  _toggle(entityId, label) {
    if (!entityId) return "";
    const st = this._st(entityId);
    const on = st && st.state === "on";
    const disabled = !st || st.state === "unavailable";
    return `<label class="toggle ${disabled ? "disabled" : ""}" data-action="${disabled ? "" : "toggle"}" data-entity="${esc(entityId)}" title="${esc(label)}">
      <span class="track ${on ? "on" : ""}"><span class="knob"></span></span>${label ? `<span class="tlabel">${esc(label)}</span>` : ""}</label>`;
  }

  _number(entityId, label, unit = "") {
    if (!entityId) return "";
    const st = this._st(entityId);
    if (!st) return "";
    const a = st.attributes;
    return `<label class="num"><span>${esc(label)}</span>
      <input type="number" data-action="number" data-entity="${esc(entityId)}" value="${esc(Number(st.state))}"
        min="${esc(a.min)}" max="${esc(a.max)}" step="${esc(a.step || 1)}" ${st.state === "unavailable" ? "disabled" : ""}>
      ${unit ? `<span class="unit">${esc(unit)}</span>` : ""}</label>`;
  }

  _bar(percent, color) {
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    return `<div class="bar"><div style="width:${p}%;background:${COLORS[color] || COLORS.grey}"></div></div>`;
  }

  _renderAccount(acc) {
    const a = acc.account || {};
    const nodeState = this._val(a.node);
    const nodeColor = nodeState === "connected" ? "green" : nodeState === "login_expired" ? "orange" : "red";
    const header = `<div class="header">
        <div class="title">${esc(this._config.title)}</div>
        <div class="summary">
          <span class="chip" data-action="more" data-entity="${esc(a.node || "")}">${this._dot(nodeColor, "Node")}${esc(nodeState === "connected" ? "Connected" : nodeState === "login_expired" ? "Login expired" : "Disconnected")}</span>
          <span class="chip" data-action="more" data-entity="${esc(a.machines_online || "")}">${esc(this._val(a.machines_online) ?? "–")}/${acc.machines.length} online</span>
          <span class="chip" data-action="more" data-entity="${esc(a.total_ppd || "")}"><b>${fmtNumber(this._val(a.total_ppd))}</b>&nbsp;PPD</span>
          ${a.score ? `<span class="chip" data-action="more" data-entity="${esc(a.score)}">Score ${fmtNumber(this._val(a.score))}</span>` : ""}
        </div>
        <div class="allbtns">
          ${a.button_fold_all ? `<button class="btn green" data-action="press" data-entity="${esc(a.button_fold_all)}">Fold all</button>` : ""}
          ${a.button_finish_all ? `<button class="btn orange" data-action="press" data-entity="${esc(a.button_finish_all)}">Finish all</button>` : ""}
          ${a.button_pause_all ? `<button class="btn red" data-action="press" data-entity="${esc(a.button_pause_all)}">Pause all</button>` : ""}
        </div>
      </div>`;
    const machines = `<div class="machines">${acc.machines.map((m) => this._renderMachine(m, acc)).join("")}</div>`;
    const projects = this._config.show_projects ? this._renderProjects(acc) : "";
    return header + machines + projects;
  }

  _renderMachine(m, acc) {
    const e = m.entities;
    const status = this._val(e.status) || "offline";
    const [color, label] = MACHINE_STATUS[status] || ["grey", status];
    const mode = this._val(e.mode);
    const offline = status === "offline";
    const solarCount = acc.machines.filter((x) => this._val(x.entities.solar_control) === "on").length;

    // ---- Computer
    const computer = `<section class="box computer">
        <div class="row head" data-action="more" data-entity="${esc(e.status || "")}">
          ${this._dot(color, label)}<span class="name">${esc(m.name)}</span>
          <span class="badge" style="color:${COLORS[color]}">${esc(label)}</span>
        </div>
        <div class="row sub">
          <span><b>${fmtNumber(this._val(e.ppd))}</b> PPD</span>
          <span>${esc(this._val(e.running_units) ?? 0)} WU running</span>
          <span class="muted">${esc(this._val(e.version) && this._val(e.version) !== "unavailable" ? "v" + this._val(e.version) : "")}</span>
        </div>
      </section>`;

    // ---- Master controls
    const btn = (key, text, cls, active) =>
      e[key] ? `<button class="btn ${cls} ${active ? "active" : ""}" ${offline ? "disabled" : ""} data-action="press" data-entity="${esc(e[key])}">${text}</button>` : "";
    const master = `<section class="box master">
        <div class="boxtitle">Master controls</div>
        <div class="btnrow">
          ${btn("button_fold", "Fold", "green", mode === "fold" && status === "folding")}
          ${btn("button_finish", "Finish", "orange", mode === "finish")}
          ${btn("button_pause", "Pause", "red", mode === "pause")}
        </div>
      </section>`;

    // ---- CPU
    const cpu = m.cpus.map((c) => {
      const s = c.sensors;
      const st = this._val(s.status);
      const col = slotColor(st);
      const progress = this._val(s.progress);
      const proj = this._val(s.project);
      const eta = fmtEta(this._val(s.eta) && Number(this._val(s.eta)) * (this._attr(s.eta, "unit_of_measurement") === "h" ? 3600 : this._attr(s.eta, "unit_of_measurement") === "min" ? 60 : 1));
      const groupSw = m.group_switches[c.group || "default"] || {};
      return `<section class="box cpu">
          <div class="row head" data-action="more" data-entity="${esc(s.progress || "")}">
            ${this._dot(col, st)}<span class="name">${esc(c.name)}</span><span class="badge" style="color:${COLORS[col]}">${esc(st ?? "–")}</span>
          </div>
          ${this._bar(progress, col)}
          <div class="row sub"><span>${esc(progress != null && progress !== "unavailable" ? Number(progress).toFixed(1) + "%" : "")}</span>
            <span>${proj && proj !== "unknown" && proj !== "unavailable" ? "Project " + esc(proj) : ""}</span><span>${esc(eta ? "ETA " + eta : "")}</span></div>
          <div class="opts">
            ${this._number(c.cpus, "CPUs", this._attr(c.cpus, "max") ? "of " + this._attr(c.cpus, "max") : "")}
            ${Object.entries(GROUP_SWITCH_LABELS).map(([k, l]) => this._toggle(groupSw[k], l)).join("")}
          </div>
        </section>`;
    }).join("");

    // ---- GPUs
    const gpuRows = m.gpus.length
      ? m.gpus.map((g) => {
          const s = g.sensors;
          const st = this._val(s.status);
          const col = slotColor(st);
          const progress = this._val(s.progress);
          const proj = this._val(s.project);
          return `<div class="gpu">
              <div class="row head">
                ${this._dot(col, st)}<span class="name" data-action="more" data-entity="${esc(s.progress || "")}">${esc(g.name)}</span>
                ${this._toggle(g.switch, "")}
              </div>
              ${this._bar(progress, col)}
              <div class="row sub"><span style="color:${COLORS[col]}">${esc(st ?? "–")}</span>
                <span>${progress && progress !== "unavailable" && Number(progress) > 0 ? Number(progress).toFixed(1) + "%" : ""}</span>
                <span>${proj && proj !== "unknown" && proj !== "unavailable" ? "Project " + esc(proj) : ""}</span>
                <span>${Number(this._val(s.ppd)) > 0 ? fmtNumber(this._val(s.ppd)) + " PPD" : ""}</span></div>
            </div>`;
        }).join("")
      : `<div class="muted">No GPUs detected</div>`;
    const gpu = `<section class="box gpus"><div class="boxtitle">GPUs</div>${gpuRows}</section>`;

    // ---- Solar
    const solarOn = this._val(e.solar_control) === "on";
    const solarState = this._val(e.solar_status);
    const surplus = this._attr(e.solar_status, "surplus_w");
    const transition = this._attr(e.solar_status, "transition_in_s");
    const stopSel = e.solar_stop_action ? this._st(e.solar_stop_action) : null;
    const solar = `<section class="box solar">
        <div class="row head">
          <span class="boxtitle grow">Solar</span>${this._toggle(e.solar_control, solarOn ? "On" : "Off")}
        </div>
        <div class="row sub" data-action="more" data-entity="${esc(e.solar_status || "")}">
          <span>${esc(SOLAR_STATUS[solarState] || solarState || "–")}${transition ? ` (${esc(Math.round(transition / 60))} min)` : ""}</span>
          <span>${surplus != null ? "Surplus " + fmtNumber(surplus) + " W" : ""}</span>
        </div>
        ${solarOn || solarCount ? `<div class="opts grid2">
          ${this._number(e.solar_priority, "Priority", solarCount > 1 ? "1 = first" : "")}
          ${stopSel ? `<label class="num"><span>When sun fades</span><select data-action="select" data-entity="${esc(e.solar_stop_action)}">
            ${(stopSel.attributes.options || []).map((o) => `<option value="${esc(o)}" ${o === stopSel.state ? "selected" : ""}>${esc(STOP_ACTIONS[o] || o)}</option>`).join("")}
          </select></label>` : ""}
          ${this._number(e.solar_start_w, "Start at", "W")}
          ${this._number(e.solar_stop_w, "Stop below", "W")}
          ${this._number(e.solar_start_delay, "Start delay", "min")}
          ${this._number(e.solar_stop_delay, "Stop delay", "min")}
        </div>` : `<div class="muted small">Turn on to fold on spare solar power. Choose the surplus sensor under the integration's Configure → Solar control.</div>`}
      </section>`;

    return `<div class="col">${computer}${master}<div class="cpus">${cpu}</div>${gpu}${solar}</div>`;
  }

  _renderProjects(acc) {
    const view = this._currentView;
    const tabs = `<div class="tabs" role="tablist">
        <button class="tab ${view === "impact" ? "on" : ""}" data-action="view" data-view="impact" role="tab">Impact</button>
        <button class="tab ${view === "details" ? "on" : ""}" data-action="view" data-view="details" role="tab">Projects</button>
      </div>`;
    const height = Number(this._config.projects_height) || 400;
    let title;
    let inner;
    if (view === "impact") {
      const research = (this._research || []).find((r) => r.entry_id === acc.entry_id);
      title = research
        ? `How you're helping <span class="muted small">· ${esc(windowText(research.hours))} · ${esc(fmtNumber(research.total))} work unit${research.total === 1 ? "" : "s"}</span>`
        : "How you're helping";
      inner = research
        ? renderTopics(research, { action: "topic" }) + `<div class="muted small hint">Click a topic to see its projects.</div>`
        : `<div class="muted small">Loading…</div>`;
    } else {
      let items = acc.projects || [];
      const filter = this._topicFilter;
      if (filter) items = items.filter((p) => p.topic === filter);
      const label = filter && items.length ? items[0].topic_label : filter;
      title = `Projects <span class="muted">(${items.length})</span>${filter ? ` <span class="filter" data-action="clearfilter" title="Show all">${esc(label)} ✕</span>` : ""}`;
      inner = items.length
        ? items.map((p) => this._renderProject(acc, p)).join("")
        : `<div class="muted">No projects yet. They appear as work units are assigned and completed.</div>`;
    }
    return `<div class="projwrap">
        <div class="projhead"><div class="boxtitle">${title}</div><span class="grow"></span>${tabs}</div>
        <div class="projects" style="max-height:${height}px">${inner}</div>
      </div>`;
  }

  _renderProject(acc, p) {
    const key = `${acc.entry_id}:${p.project}`;
    const open = this._expanded.has(key);
    const desc = p.description || "No description available yet.";
    const topic = p.topic_label || p.cause;
    return `<div class="proj ${p.current_on && p.current_on.length ? "current" : ""}">
        <div class="row head">
          <a class="pnum" href="https://stats.foldingathome.org/project/${esc(p.project)}" target="_blank" rel="noopener noreferrer">Project ${esc(p.project)}</a>
          ${topic ? `<span class="cause" title="${esc(p.cause ? "F@h cause: " + p.cause : "")}">${esc(topic)}</span>` : ""}
          ${(p.current_on || []).map((n) => `<span class="running">${this._dot("green")}${esc(n)}</span>`).join("")}
          <span class="grow"></span>
          <span class="muted small">${p.wus ? esc(p.wus) + " WU" + (p.wus === 1 ? "" : "s") : ""}${p.last_seen ? " · " + esc(fmtDate(p.last_seen)) : ""}</span>
        </div>
        <div class="muted small">${esc([p.institution, p.manager].filter(Boolean).join(" · "))}${p.machines && p.machines.length ? ` · on ${esc(p.machines.join(", "))}` : ""}</div>
        <div class="desc ${open ? "open" : ""}" data-action="expand" data-key="${esc(key)}" title="Click to ${open ? "collapse" : "expand"}">${esc(desc)}</div>
      </div>`;
  }
}

const STYLE = `
  :host { display:block; }
  ha-card { display:block; padding:16px; box-sizing:border-box; }
  .msg { padding:8px 0; color:var(--secondary-text-color); }
  .header { display:flex; flex-wrap:wrap; align-items:center; gap:8px 16px; margin-bottom:12px; }
  .title { font-size:1.4em; font-weight:500; color:var(--primary-text-color); }
  .summary { display:flex; flex-wrap:wrap; gap:6px; flex:1; }
  .allbtns { display:flex; gap:6px; }
  .chip { display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:16px;
    background:var(--secondary-background-color); color:var(--primary-text-color); font-size:.9em; cursor:pointer; }
  .machines { display:grid; grid-template-columns:repeat(auto-fit, minmax(270px, 1fr)); column-gap:12px; row-gap:8px; }
  /* every computer spans 5 shared rows, so the boxes line up across columns */
  .col { display:grid; grid-template-rows:subgrid; grid-row:span 5; row-gap:8px; min-width:0; }
  .cpus { display:flex; flex-direction:column; gap:8px; }
  .cpus > .box { flex:1; }
  @supports not (grid-template-rows: subgrid) { .col { display:flex; flex-direction:column; gap:8px; } }
  .box { border:1px solid var(--divider-color); border-radius:12px; padding:10px 12px;
    background:var(--card-background-color); }
  .computer { border-width:2px; }
  .boxtitle { font-weight:500; color:var(--primary-text-color); margin-bottom:6px; }
  .row { display:flex; align-items:center; gap:8px; min-width:0; }
  .row.head { cursor:pointer; }
  .row.sub { flex-wrap:wrap; gap:4px 12px; font-size:.9em; color:var(--secondary-text-color); margin-top:4px; }
  .name { font-weight:500; color:var(--primary-text-color); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; flex:1; min-width:0; }
  .badge { font-size:.85em; font-weight:500; }
  .dot { width:12px; height:12px; border-radius:50%; flex:none; display:inline-block; box-shadow:0 0 0 2px rgba(0,0,0,.08); }
  .muted { color:var(--secondary-text-color); }
  .small { font-size:.85em; }
  .grow { flex:1; }
  .btnrow { display:flex; gap:6px; }
  .btn { flex:1; border:none; border-radius:8px; padding:8px 10px; font:inherit; font-weight:500; cursor:pointer;
    color:#fff; background:var(--primary-color); opacity:.85; }
  .btn:hover { opacity:1; }
  .btn.green { background:${COLORS.green}; } .btn.orange { background:${COLORS.orange}; } .btn.red { background:${COLORS.red}; }
  .btn.active { opacity:1; box-shadow:0 0 0 3px var(--primary-text-color) inset; }
  .btn:disabled { opacity:.35; cursor:default; }
  .allbtns .btn { flex:none; }
  .bar { height:6px; border-radius:3px; background:var(--divider-color); overflow:hidden; margin-top:6px; }
  .bar > div { height:100%; border-radius:3px; transition:width .5s; }
  .opts { display:flex; flex-wrap:wrap; gap:6px 12px; margin-top:8px; align-items:center; }
  .opts.grid2 { display:grid; grid-template-columns:1fr 1fr; }
  .num { display:flex; align-items:center; gap:6px; font-size:.9em; color:var(--secondary-text-color); min-width:0; }
  .num input, .num select { width:72px; padding:4px 6px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--secondary-background-color); color:var(--primary-text-color); font:inherit; }
  .num select { width:auto; max-width:110px; }
  .unit { font-size:.85em; }
  .toggle { display:inline-flex; align-items:center; gap:6px; cursor:pointer; font-size:.85em; color:var(--secondary-text-color); user-select:none; }
  .toggle.disabled { opacity:.4; cursor:default; }
  .track { width:34px; height:18px; border-radius:9px; background:var(--disabled-text-color, #9e9e9e); position:relative; transition:background .2s; flex:none; }
  .track.on { background:var(--primary-color); }
  .knob { position:absolute; top:2px; left:2px; width:14px; height:14px; border-radius:50%; background:#fff; transition:left .2s; }
  .track.on .knob { left:18px; }
  .gpu + .gpu { border-top:1px dashed var(--divider-color); margin-top:8px; padding-top:8px; }
  .projwrap { margin-top:16px; }
  .projects { overflow-y:auto; border:1px solid var(--divider-color); border-radius:12px; padding:4px 12px; }
  .proj { padding:10px 0; border-bottom:1px solid var(--divider-color); }
  .proj:last-child { border-bottom:none; }
  .proj.current { border-left:3px solid ${COLORS.green}; padding-left:8px; margin-left:-12px; }
  .pnum { font-weight:500; color:var(--primary-color); text-decoration:none; }
  .projhead { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-bottom:6px; }
  .projhead .boxtitle { margin-bottom:0; }
  .tabs { display:inline-flex; border:1px solid var(--divider-color); border-radius:16px; overflow:hidden; }
  .tab { border:none; background:transparent; color:var(--secondary-text-color); font:inherit; font-size:.9em; padding:4px 12px; cursor:pointer; }
  .tab.on { background:var(--primary-color); color:var(--text-primary-color, #fff); }
  .filter { font-size:.8em; padding:2px 8px; border-radius:10px; background:var(--primary-color); color:var(--text-primary-color, #fff); cursor:pointer; margin-left:4px; }
  .hint { margin:6px 0 4px; }
  .projects .topic { padding:8px 0; }
  .cause { font-size:.8em; padding:2px 8px; border-radius:10px; background:var(--secondary-background-color); color:var(--primary-text-color); }
  .running { display:inline-flex; align-items:center; gap:4px; font-size:.8em; color:var(--primary-text-color); }
  .running .dot { width:8px; height:8px; }
  .desc { margin-top:4px; font-size:.9em; color:var(--primary-text-color); cursor:pointer;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .desc.open { display:block; }
  @media (max-width: 600px) { ha-card { padding:12px; } .opts.grid2 { grid-template-columns:1fr; } }
`;

/* ------------------------------------------------- research (impact) card */
class FoldingAtHomeResearchCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._error = null;
    this._sig = "";
    this._onClick = (ev) => {
      const el = ev.composedPath().find((n) => n.dataset && n.dataset.action === "more");
      if (el && el.dataset.entity) {
        this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: el.dataset.entity }, bubbles: true, composed: true }));
      }
    };
  }

  static getStubConfig() {
    return { hours: 24 };
  }

  static getConfigElement() {
    return document.createElement("foldingathome-research-card-editor");
  }

  setConfig(config) {
    const old = this._config;
    this._config = { hours: 24, show_running: true, max_topics: 0, ...config };
    if (old && Number(old.hours) !== Number(this._config.hours)) this._load();
    this._sig = "";
    this._render();
  }

  getCardSize() {
    return 2 + Math.min(8, ((this._data && this._data[0] && this._data[0].topics.length) || 3));
  }

  getGridOptions() {
    return { columns: 12, min_columns: 4 };
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._load();
  }

  connectedCallback() {
    this.shadowRoot.addEventListener("click", this._onClick);
    this._timer = setInterval(() => this._load(), 60000);
    if (this._hass) this._load();
  }

  disconnectedCallback() {
    this.shadowRoot.removeEventListener("click", this._onClick);
    clearInterval(this._timer);
  }

  async _load() {
    if (!this._hass || !this._config) return;
    try {
      const result = await this._hass.callWS({ type: "foldingathome/research", hours: Number(this._config.hours) || 0 });
      this._data = result.accounts || [];
      this._error = null;
    } catch (err) {
      this._error = (err && (err.message || err.code)) || String(err);
    }
    this._render();
  }

  _render() {
    if (!this._config) return;
    const sig = JSON.stringify([this._config, this._data, this._error]);
    if (sig === this._sig) return;
    this._sig = sig;
    let body;
    if (this._error) body = `<div class="muted">Could not load Folding@home data: ${esc(this._error)}</div>`;
    else if (!this._data) body = `<div class="muted">Loading…</div>`;
    else if (!this._data.length) body = `<div class="muted">No Folding@home account is set up yet.</div>`;
    else {
      body = this._data.map((r) => `
        <div class="title">${esc(this._config.title || r.donor || "Folding@home")}</div>
        <div class="muted sub">Folding@home · ${esc(windowText(r.hours))} · ${esc(fmtNumber(r.total))} work unit${r.total === 1 ? "" : "s"}</div>
        ${renderTopics(r, { showRunning: this._config.show_running !== false, maxTopics: Number(this._config.max_topics) || 0 })}`).join('<hr>');
    }
    this.shadowRoot.innerHTML = `<style>
        :host { display:block; }
        ha-card { display:block; padding:16px; box-sizing:border-box; }
        .title { font-size:1.2em; font-weight:500; color:var(--primary-text-color); }
        .sub { margin:6px 0 8px; font-size:.9em; }
        .muted { color:var(--secondary-text-color); }
        .small { font-size:.85em; }
        .row { display:flex; align-items:center; gap:8px; min-width:0; }
        .grow { flex:1; }
        hr { border:none; border-top:1px solid var(--divider-color); margin:12px 0; }
        ${RESEARCH_STYLE}
      </style><ha-card>${body}</ha-card>`;
  }
}

/* ---------------------------------------------------------- card editors */
const WINDOW_OPTIONS = WINDOWS.map(([value, label]) => ({ value: String(value), label }));

class FahEditorBase extends HTMLElement {
  setConfig(config) {
    this._config = config;
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  _update() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.computeLabel = (schema) => schema.label || schema.name;
      this._form.addEventListener("value-changed", (ev) => {
        ev.stopPropagation();
        const config = { ...ev.detail.value };
        for (const key of this.constructor.numberKeys) {
          if (config[key] !== undefined && config[key] !== "") config[key] = Number(config[key]);
        }
        this._config = config;
        this.dispatchEvent(new CustomEvent("config-changed", { detail: { config }, bubbles: true, composed: true }));
      });
      this.appendChild(this._form);
    }
    const data = { ...this._config };
    for (const key of this.constructor.numberKeys) {
      if (data[key] !== undefined && this.constructor.selectKeys.includes(key)) data[key] = String(data[key]);
    }
    this._form.hass = this._hass;
    this._form.schema = this.constructor.schema;
    this._form.data = data;
  }
}

class FahCardEditor extends FahEditorBase {
  static numberKeys = ["research_hours", "projects_height"];
  static selectKeys = ["research_hours"];
  static schema = [
    { name: "title", label: "Title", selector: { text: {} } },
    { name: "show_projects", label: "Show the Impact / Projects box", default: true, selector: { boolean: {} } },
    {
      name: "projects_view", label: "Open the box on", default: "impact",
      selector: { select: { mode: "list", options: [
        { value: "impact", label: "Impact: work units per research topic" },
        { value: "details", label: "Projects: every project with its summary" },
      ] } },
    },
    { name: "research_hours", label: "Impact time window", default: "24", selector: { select: { mode: "dropdown", options: WINDOW_OPTIONS } } },
    { name: "projects_height", label: "Box height (px)", default: 400, selector: { number: { min: 120, max: 1200, step: 20, mode: "box" } } },
  ];
}

class FahResearchCardEditor extends FahEditorBase {
  static numberKeys = ["hours", "max_topics"];
  static selectKeys = ["hours"];
  static schema = [
    { name: "title", label: "Title (empty = your donor name)", selector: { text: {} } },
    { name: "hours", label: "Time window", default: "24", selector: { select: { mode: "dropdown", options: WINDOW_OPTIONS } } },
    { name: "show_running", label: "Show work units running now", default: true, selector: { boolean: {} } },
    { name: "max_topics", label: "Max topics (0 = all)", default: 0, selector: { number: { min: 0, max: 30, mode: "box" } } },
  ];
}

const ELEMENTS = {
  "foldingathome-card": FoldingAtHomeCard,
  "foldingathome-research-card": FoldingAtHomeResearchCard,
  "foldingathome-card-editor": FahCardEditor,
  "foldingathome-research-card-editor": FahResearchCardEditor,
};
for (const [tag, cls] of Object.entries(ELEMENTS)) {
  if (!customElements.get(tag)) customElements.define(tag, cls);
}
window.customCards = window.customCards || [];
for (const card of [
  {
    type: "foldingathome-card",
    name: "Folding@home",
    description: "All your Folding@home computers: controls, CPU, GPUs, solar, and what your work is helping (Impact or Projects).",
  },
  {
    type: "foldingathome-research-card",
    name: "Folding@home research",
    description: "Work units per research topic (Cancer, Alzheimer's, …) over a time window you choose.",
  },
]) {
  if (!window.customCards.some((c) => c.type === card.type)) {
    window.customCards.push({ ...card, preview: false, documentationURL: "https://github.com/bobthecooldad/Folding-At-Home-Claude" });
  }
}
console.info(`%c FOLDINGATHOME-CARD %c ${CARD_VERSION} `, "background:#1D4277;color:#fff", "background:#2EE6C5;color:#000");
