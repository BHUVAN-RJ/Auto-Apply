/* Everything that happens on one page, buffered for the collector to drain.
 *
 * It listens and it patches two functions. It never clicks, never types,
 * never navigates, never closes anything.
 *
 * The two patches are the point of the whole file. `window.open` and
 * `window.close` are wrapped so that a tab appearing or disappearing has a
 * *reason* attached - the call's own stack, and the last thing the person
 * touched before it. Three things can open a blank tab on a Workday page
 * (the page itself, Jobright's extension, our own code) and from the outside
 * they look identical; from in here they do not.
 *
 * Secret fields are decided by the collector, which hands their refs down in
 * `window.__trkSecret`. A field this does not know about is treated as a
 * secret until told otherwise, so the first keystroke in an unseen box is
 * never the one that leaks.
 */
(() => {
  if (window.__trk) return;

  const CAP = 4000;            // rows held if the collector stops draining
  const TEXT = 160;            // longest value kept per row
  const buffer = [];
  const state = {
    secrets: new Set(),        // refs the collector has called secret
    known: new Set(),          // refs it has classified at all
    before: new Map(),         // ref -> value when the field was entered
    filled: new Map(),         // ref -> value the page/an extension put there
    lastTouch: null,           // the last thing the person pointed at
    errors: new Map(),         // ref/key -> the error text showing now
    counter: 0,
  };
  window.__trk = state;

  const now = () => Math.round(performance.timeOrigin + performance.now());

  const push = (kind, row) => {
    if (buffer.length > CAP) buffer.shift();
    buffer.push(Object.assign({ kind: kind, at: now(), url: location.href }, row));
  };

  // -- naming a control ---------------------------------------------------

  const ref = (el) => {
    if (!el || el.nodeType !== 1) return "";
    let r = el.getAttribute("data-trk-ref");
    if (!r) { r = "t" + (++state.counter); el.setAttribute("data-trk-ref", r); }
    return r;
  };

  const text = (el) => ((el && (el.innerText || el.textContent)) || "")
      .replace(/\s+/g, " ").trim();

  // The label a screen reader would read, in the order it would try.
  const label = (el) => {
    if (!el || el.nodeType !== 1) return "";
    const by = el.getAttribute("aria-labelledby");
    if (by) {
      const parts = by.split(/\s+/).map((id) => {
        const n = document.getElementById(id);
        return n ? text(n) : "";
      }).filter(Boolean);
      if (parts.length) return parts.join(" ").slice(0, 120);
    }
    if (el.getAttribute("aria-label")) return el.getAttribute("aria-label").slice(0, 120);
    if (el.id) {
      const tag = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (tag) return text(tag).slice(0, 120);
    }
    const wrap = el.closest ? el.closest("label") : null;
    if (wrap) return text(wrap).slice(0, 120);
    const block = el.closest ? el.closest("[data-automation-id^='formField-'],.field,fieldset,div") : null;
    if (block) {
      const own = block.querySelector("label,legend,h1,h2,h3,h4,span");
      if (own) return text(own).slice(0, 120);
    }
    return (el.getAttribute("placeholder") || "").slice(0, 120);
  };

  const where = (el) => {
    const steps = [];
    for (let n = el; n && n.nodeType === 1 && steps.length < 8; n = n.parentElement) {
      const auto = n.getAttribute("data-automation-id");
      if (auto) { steps.unshift("[" + auto + "]"); break; }
      if (n.id && !/^\d/.test(n.id)) { steps.unshift("#" + n.id); break; }
      steps.unshift(n.tagName.toLowerCase());
    }
    return steps.join(">").slice(0, 200);
  };

  const describe = (el) => el && el.nodeType === 1 ? {
    ref: ref(el),
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute("type") || "").toLowerCase(),
    auto: el.getAttribute("data-automation-id") || "",
    id: el.id || "",
    name: el.getAttribute("name") || "",
    role: el.getAttribute("role") || "",
    autocomplete: el.getAttribute("autocomplete") || "",
    label: label(el),
    says: text(el).slice(0, 120),
    path: where(el),
  } : null;

  // Until the collector says otherwise a field is a secret. The collector
  // reads the description, decides with `policy.secret_field`, and sends the
  // refs back; that is one round trip, and the first keystroke in a box
  // nobody has classified is not worth a credential.
  const secret = (el) => {
    const r = el.getAttribute("data-trk-ref");
    if (r && state.secrets.has(r)) return true;
    if (r && state.known.has(r)) return false;
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "password") return true;
    return !r || !state.known.has(r);
  };

  const value = (el) => {
    if (secret(el)) return null;
    const v = el.value != null ? String(el.value) : "";
    return v.length > TEXT ? v.slice(0, TEXT) + "…(" + v.length + ")" : v;
  };

  // -- the person, and the scripts ---------------------------------------
  //
  // `isTrusted` is the whole discrimination between a hand and a script, and
  // it is the one field no extension can forge.

  document.addEventListener("pointerdown", (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    state.lastTouch = { el: describe(el), at: now(), trusted: e.isTrusted };
  }, true);

  document.addEventListener("click", (e) => {
    const d = describe(e.target);
    push("click", { el: d, trusted: e.isTrusted, button: e.button,
                    detail: e.detail, modifiers: [e.metaKey && "meta", e.ctrlKey && "ctrl",
                                                  e.shiftKey && "shift", e.altKey && "alt"].filter(Boolean) });
  }, true);

  document.addEventListener("keydown", (e) => {
    const el = e.target;
    const inSecret = el && el.nodeType === 1 && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")
        ? secret(el) : false;
    push("key", {
      el: describe(el),
      // A secret's keys are counted, never named: that something was typed
      // is the shape of the flow, and which keys is the credential.
      key: inSecret ? "<secret>" : e.key,
      code: inSecret ? "" : e.code,
      repeat: e.repeat,
      modifiers: [e.metaKey && "meta", e.ctrlKey && "ctrl", e.shiftKey && "shift",
                  e.altKey && "alt"].filter(Boolean),
      trusted: e.isTrusted,
    });
  }, true);

  document.addEventListener("focusin", (e) => {
    const el = e.target;
    if (!el || !("value" in el)) return;
    state.before.set(ref(el), secret(el) ? null : String(el.value || ""));
  }, true);

  // The row that answers "what did I correct". `was` is the value the field
  // held when it was entered, `now` the value when it was left; `filled` is
  // what a script had put there, so a correction of an autofill is told from
  // a correction of one's own typing.
  document.addEventListener("focusout", (e) => {
    const el = e.target;
    if (!el || !("value" in el)) return;
    const r = ref(el);
    const was = state.before.get(r);
    const is = secret(el) ? null : String(el.value || "");
    if (was === undefined || was === is) return;
    push("edit", { el: describe(el), was: was, now: is,
                   filled: state.filled.get(r) ?? null,
                   corrected: state.filled.has(r) && state.filled.get(r) !== is,
                   trusted: e.isTrusted });
  }, true);

  document.addEventListener("input", (e) => {
    const el = e.target;
    if (!el || !("value" in el)) return;
    const r = ref(el);
    if (!e.isTrusted) state.filled.set(r, secret(el) ? null : String(el.value || ""));
    push("input", { el: describe(el), value: value(el), trusted: e.isTrusted });
  }, true);

  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    const row = { el: describe(el), trusted: e.isTrusted };
    if (el.tagName === "SELECT") {
      row.chose = el.selectedOptions ? [...el.selectedOptions].map((o) => text(o)).join(", ") : "";
    } else if (el.type === "checkbox" || el.type === "radio") {
      row.on = !!el.checked;
    } else if (el.type === "file") {
      // The names are the person's: a resume is named after them. The count
      // and the sizes are the fact worth keeping.
      row.files = el.files ? [...el.files].map((f) => ({ bytes: f.size, type: f.type })) : [];
    } else {
      row.value = value(el);
    }
    push("change", row);
  }, true);

  document.addEventListener("submit", (e) => {
    push("submit", { el: describe(e.target), trusted: e.isTrusted });
  }, true);

  document.addEventListener("copy", () => push("clipboard", { what: "copy" }), true);
  document.addEventListener("paste", (e) => {
    let n = 0;
    try { n = (e.clipboardData.getData("text") || "").length; } catch (err) { n = -1; }
    push("clipboard", { what: "paste", chars: n, el: describe(e.target) });
  }, true);

  let scrolled = 0;
  window.addEventListener("scroll", () => {
    const t = now();
    if (t - scrolled < 1000) return;      // a scroll is a gesture, not a stream
    scrolled = t;
    push("scroll", { y: Math.round(window.scrollY) });
  }, true);

  // -- why a tab opened, and who closed it --------------------------------

  const stack = () => {
    try { return (new Error().stack || "").split("\n").slice(2, 7).join(" | ").slice(0, 500); }
    catch (e) { return ""; }
  };

  const realOpen = window.open;
  window.open = function (...args) {
    push("window.open", {
      wanted: String(args[0] == null ? "" : args[0]).slice(0, 300),
      name: String(args[1] == null ? "" : args[1]).slice(0, 80),
      features: String(args[2] == null ? "" : args[2]).slice(0, 200),
      // The two things that name the culprit: who called it, and what the
      // person had just touched. A call with no recent touch is a script's.
      from: stack(),
      after: state.lastTouch,
      sinceTouch: state.lastTouch ? now() - state.lastTouch.at : null,
    });
    return realOpen.apply(this, args);
  };

  const realClose = window.close;
  window.close = function (...args) {
    push("window.close", { from: stack(), after: state.lastTouch,
                           sinceTouch: state.lastTouch ? now() - state.lastTouch.at : null });
    return realClose.apply(this, args);
  };

  window.addEventListener("beforeunload", () => push("leaving", {}), true);

  // -- errors, raised and cleared ----------------------------------------
  //
  // An error is a thing that appears and then goes away again, so both ends
  // are recorded: "the form complained" and "it stopped complaining" are two
  // facts and the gap between them is how long it took to fix.

  const errorText = (el) => text(el).slice(0, 200);

  const sweepErrors = () => {
    const found = new Map();
    const nodes = document.querySelectorAll(
        "[aria-invalid='true'],[role='alert'],[data-automation-id*='error'],"
        + "[class*='error' i]:not(html):not(body),[class*='invalid' i]");
    for (const el of nodes) {
      if (!el.offsetParent && !el.getClientRects().length) continue;
      const key = el.getAttribute("data-trk-ref") || where(el);
      const msg = errorText(el) || (el.getAttribute("aria-invalid") === "true"
          ? "(field marked invalid)" : "");
      if (!msg) continue;
      found.set(key, msg);
    }
    for (const [key, msg] of found) {
      if (state.errors.get(key) !== msg) {
        state.errors.set(key, msg);
        push("error", { what: "raised", where: key, says: msg });
      }
    }
    for (const key of [...state.errors.keys()]) {
      if (!found.has(key)) {
        const was = state.errors.get(key);
        state.errors.delete(key);
        push("error", { what: "cleared", where: key, says: was });
      }
    }
  };

  const observer = new MutationObserver(() => {
    clearTimeout(state.sweep);
    state.sweep = setTimeout(sweepErrors, 300);
  });
  try {
    observer.observe(document.documentElement,
        { subtree: true, childList: true, attributes: true, characterData: true,
          // `style` belongs here: a form that already has its complaint in
          // the DOM and shows it by flipping `display` changes no class and
          // no role, so the sweep never ran and the error was never seen.
          attributeFilter: ["aria-invalid", "class", "role", "style", "hidden"] });
  } catch (e) { /* a document that is already gone */ }
  setTimeout(sweepErrors, 500);
  // A backstop, because an error can also arrive as plain text in a node
  // that was already there and nothing above is guaranteed to notice.
  setInterval(sweepErrors, 2000);

  // The page's own complaints. A console error is often the real reason a
  // step did nothing.
  window.addEventListener("error", (e) => push("page_error", {
    says: String(e.message || "").slice(0, 300),
    at_file: String(e.filename || "").slice(0, 200) + ":" + (e.lineno || 0),
  }), true);
  window.addEventListener("unhandledrejection", (e) => push("page_error", {
    says: ("unhandled rejection: " + String((e.reason && e.reason.message) || e.reason || ""))
        .slice(0, 300),
  }), true);

  // -- the drain ---------------------------------------------------------

  window.__trkDrain = () => { const out = buffer.splice(0, buffer.length); return JSON.stringify(out); };
  window.__trkClassify = (secretRefs, knownRefs) => {
    for (const r of secretRefs || []) state.secrets.add(r);
    for (const r of knownRefs || []) state.known.add(r);
    return state.known.size;
  };
  window.__trkFields = () => JSON.stringify(
      [...document.querySelectorAll("input,select,textarea")]
          .filter((el) => el.type !== "hidden")
          .map((el) => describe(el)));

  push("watching", { title: document.title.slice(0, 160) });
})();
