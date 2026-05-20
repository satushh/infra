const chat = document.getElementById("chat");
const form = document.getElementById("form");
const q = document.getElementById("q");
const send = document.getElementById("send");
const providerSel = document.getElementById("provider");
const modelSel = document.getElementById("model");
const pickerStatus = document.getElementById("picker-status");

const history = [];
let providersData = null;

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
}

function addMsg(role, content) {
  const m = el("div", `msg ${role}`);
  m.appendChild(el("div", "role", role));
  const body = el("div", "body");
  body.innerHTML = content;
  m.appendChild(body);
  chat.appendChild(m);
  chat.scrollTop = chat.scrollHeight;
  return body;
}

function renderAnswer(text) {
  if (/^(HEAD|FINALITY|PEERS|EXECUTION|RESOURCE|ANOMALIES|NEXT)\s/m.test(text)) {
    return `<pre class="briefing">${escapeHTML(text)}</pre>`;
  }
  return escapeHTML(text).replace(/\n/g, "<br/>");
}

async function loadProviders() {
  pickerStatus.textContent = "loading…";
  pickerStatus.classList.remove("err");
  try {
    const r = await fetch("/providers");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    providersData = await r.json();

    providerSel.innerHTML = "";
    for (const [name, info] of Object.entries(providersData.providers)) {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = info.available ? name : `${name} (unavailable)`;
      opt.disabled = !info.available;
      providerSel.appendChild(opt);
    }
    const pref = providersData.default_provider;
    if (providersData.providers[pref]?.available) providerSel.value = pref;
    else {
      const firstAvail = Object.entries(providersData.providers).find(([_, i]) => i.available);
      if (firstAvail) providerSel.value = firstAvail[0];
    }
    populateModels();
    pickerStatus.textContent = "";
  } catch (e) {
    pickerStatus.textContent = `provider load failed: ${e.message}`;
    pickerStatus.classList.add("err");
  }
}

function populateModels() {
  const p = providerSel.value;
  const info = providersData?.providers?.[p];
  modelSel.innerHTML = "";
  if (!info || !info.available) {
    modelSel.disabled = true;
    const opt = document.createElement("option");
    opt.textContent = info?.error || "unavailable";
    modelSel.appendChild(opt);
    return;
  }
  modelSel.disabled = false;
  for (const m of info.models || []) {
    const opt = document.createElement("option");
    opt.value = m;
    opt.textContent = m;
    modelSel.appendChild(opt);
  }
  if (info.default_model) modelSel.value = info.default_model;
}

providerSel.addEventListener("change", populateModels);

q.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// Sticky switch: updates the dropdowns so the new pick persists for
// subsequent turns. If the suggested model isn't in the dropdown's
// option list (e.g. a tool-capable model filtered out of the curated
// list), inject it so .value sticks.
function setProviderModel(provider, model) {
  providerSel.value = provider;
  populateModels();
  if (model) {
    const exists = [...modelSel.options].some(o => o.value === model);
    if (!exists) {
      const opt = document.createElement("option");
      opt.value = model;
      opt.textContent = model;
      modelSel.appendChild(opt);
    }
    modelSel.value = model;
  }
}

function renderRecoverablePanel(ev, question) {
  const panel = el("div", "recoverable");
  const head = el("div", "rec-head",
    `⚠ <strong>${escapeHTML(ev.provider)} · ${escapeHTML(ev.model)}</strong> ` +
    `emitted a malformed tool call (${escapeHTML(ev.error_type || "error")}).`);
  panel.appendChild(head);

  const suggestions = ev.suggestions || [];
  if (suggestions.length === 0) {
    panel.appendChild(el("div", "rec-empty", "No alternative providers available."));
    return panel;
  }

  const row = el("div", "rec-row");
  row.appendChild(el("span", "rec-label", "Retry with:"));
  for (const s of suggestions) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rec-btn";
    btn.textContent = `${s.provider} · ${s.model}`;
    btn.addEventListener("click", () => {
      setProviderModel(s.provider, s.model);
      panel.remove();
      runQuestion(question);
    });
    row.appendChild(btn);
  }
  const dismiss = document.createElement("button");
  dismiss.type = "button";
  dismiss.className = "rec-btn rec-dismiss";
  dismiss.textContent = "Dismiss";
  dismiss.addEventListener("click", () => panel.remove());
  row.appendChild(dismiss);
  panel.appendChild(row);
  return panel;
}

async function runQuestion(question) {
  send.disabled = true;
  const provider = providerSel.value;
  const model = modelSel.value;

  addMsg("user", escapeHTML(question));
  const assistantBody = addMsg("assistant", "");
  const meta = el("div", "meta", `${escapeHTML(provider)} · ${escapeHTML(model)}`);
  assistantBody.appendChild(meta);
  const status = el("div", "status-line", `<span class="spinner"></span>thinking…`);
  assistantBody.appendChild(status);
  const toolBox = el("div", "tools");
  assistantBody.appendChild(toolBox);

  try {
    const resp = await fetch("/ask/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({q: question, history, provider, model}),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let finalText = null;
    const pendingTools = new Map();
    let errorText = null;
    let recoverable = null;

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});
      let idx;
      while ((idx = buf.indexOf("\n\n")) !== -1) {
        const chunk = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 2);
        if (!chunk.startsWith("data:")) continue;
        const event = JSON.parse(chunk.slice(5).trim());

        if (event.type === "tool_start") {
          const t = el("div", "tool");
          t.innerHTML = `<span class="spinner"></span><span class="name">${escapeHTML(event.tool)}</span> <span class="args">${escapeHTML(JSON.stringify(event.args))}</span>`;
          toolBox.appendChild(t);
          pendingTools.set(event.tool + ":" + JSON.stringify(event.args), t);
        } else if (event.type === "tool_end") {
          const matches = [...pendingTools.entries()].filter(([k]) => k.startsWith(event.tool + ":"));
          const last = matches[matches.length - 1];
          if (last) {
            const [k, t] = last;
            pendingTools.delete(k);
            t.querySelector(".spinner")?.remove();
            const r = el("div", "result", `→ ${escapeHTML(event.result_preview || "")}`);
            t.appendChild(r);
          }
        } else if (event.type === "final") {
          finalText = event.answer || "(no answer)";
        } else if (event.type === "error") {
          errorText = event.message;
        } else if (event.type === "recoverable_error") {
          recoverable = event;
        } else if (event.type === "retry") {
          status.innerHTML = `<span class="spinner"></span>retry ${event.attempt}/${event.max} — ${escapeHTML(event.reason)}…`;
        } else if (event.type === "meta") {
          meta.textContent = `${event.provider} · ${event.model}`;
        }
      }
    }

    status.remove();
    if (recoverable) {
      assistantBody.appendChild(renderRecoverablePanel(recoverable, question));
    } else if (errorText) {
      const errBox = el("div", "error", `error: ${escapeHTML(errorText)}`);
      assistantBody.appendChild(errBox);
    } else {
      const ans = el("div", "answer", renderAnswer(finalText || "(no response)"));
      assistantBody.appendChild(ans);
      history.push({role: "user", content: question});
      history.push({role: "assistant", content: finalText || ""});
    }
  } catch (err) {
    status.remove();
    const errBox = el("div", "error", `error: ${escapeHTML(err.message)}`);
    assistantBody.appendChild(errBox);
  } finally {
    send.disabled = false;
    q.focus();
  }
}

form.addEventListener("submit", async e => {
  e.preventDefault();
  const question = q.value.trim();
  if (!question) return;
  q.value = "";
  await runQuestion(question);
});

loadProviders();
