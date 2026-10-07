(function () {
  const root = document.documentElement;
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) {} },
  };
  const session = {
    take(key) { try { const value = sessionStorage.getItem(key); sessionStorage.removeItem(key); return value; } catch (e) { return null; } },
    set(key, value) { try { sessionStorage.setItem(key, value); } catch (e) {} },
  };
  const systemDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = store.get("theme") || (systemDark ? "dark" : "light");

  function toast(text) {
    const box = document.querySelector("[data-toasts]");
    if (!box || !text) return;
    const item = document.createElement("div");
    item.className = "toast";
    item.textContent = text;
    box.appendChild(item);
    setTimeout(() => item.remove(), 3300);
  }

  function setupFilters() {
    const input = document.querySelector("[data-search-input]");
    const jobs = Array.from(document.querySelectorAll(".job"));
    const chipBox = document.querySelector("[data-market-chips]");
    const count = document.querySelector("[data-result-count]");
    const noMatch = document.querySelector("[data-no-match]");
    if (!jobs.length) return;
    let market = "";

    const markets = Array.from(new Set(jobs.map(job => job.dataset.market).filter(Boolean))).sort();
    if (chipBox && markets.length > 1) {
      ["", ...markets].forEach(name => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip" + (name === "" ? " on" : "");
        chip.textContent = name || "All markets";
        chip.addEventListener("click", () => {
          market = name;
          chipBox.querySelectorAll(".chip").forEach(other => other.classList.toggle("on", other === chip));
          apply();
        });
        chipBox.appendChild(chip);
      });
    }

    function apply() {
      const terms = (input ? input.value : "").toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      jobs.forEach(job => {
        const visible = (!market || job.dataset.market === market) && terms.every(term => job.dataset.search.includes(term));
        job.hidden = !visible;
        if (visible) shown += 1;
      });
      if (count) count.textContent = shown + (shown === 1 ? " job" : " jobs") + (shown < jobs.length ? " of " + jobs.length : "");
      if (noMatch) noMatch.hidden = shown !== 0;
    }

    if (input) input.addEventListener("input", apply);
  }

  function setupGreeting() {
    const el = document.querySelector("[data-greeting]");
    if (!el) return;
    const hour = new Date().getHours();
    const part = hour < 12 ? "Good morning" : hour < 17 ? "Good afternoon" : "Good evening";
    el.textContent = part + " · " + el.textContent;
  }

  function setupAts() {
    const form = document.querySelector("[data-ats-form]");
    if (!form) return;
    const zone = form.querySelector("[data-dropzone]");
    const input = zone.querySelector("input");
    const error = form.querySelector("[data-file-error]");
    const overlay = document.querySelector("[data-scoring]");

    function fileProblem(file) {
      if (!file) return "Choose a resume to upload.";
      if (!/\.(pdf|docx)$/i.test(file.name)) return "Upload a PDF or DOCX resume.";
      if (file.size > Number(input.dataset.max)) return "That file is larger than 5 MB.";
      return "";
    }

    function showFile() {
      const file = input.files[0];
      const problem = file ? fileProblem(file) : "";
      zone.classList.toggle("has-file", Boolean(file) && !problem);
      error.hidden = !problem;
      error.textContent = problem;
      if (file && !problem) {
        form.querySelector("[data-file-name]").textContent = file.name;
        form.querySelector("[data-file-size]").textContent = (file.size / 1024 < 1024 ? Math.round(file.size / 1024) + " KB" : (file.size / 1048576).toFixed(1) + " MB") + " · click to change";
      }
    }

    input.addEventListener("change", showFile);
    ["dragenter", "dragover"].forEach(name => zone.addEventListener(name, () => zone.classList.add("over")));
    ["dragleave", "drop"].forEach(name => zone.addEventListener(name, () => zone.classList.remove("over")));

    const rescore = form.querySelector("[data-rescore-input]");
    const label = form.querySelector("[data-submit-label]");
    const again = document.querySelector("[data-rescore]");
    if (again) again.addEventListener("click", () => {
      rescore.value = "1";
      label.textContent = "Score again";
      form.classList.add("rescoring");
      form.querySelector("[data-rescore-hint]").hidden = false;
      form.scrollIntoView({ behavior: "smooth", block: "start" });
      input.click();
    });

    function send(data) {
      return fetch(form.action, { method: "POST", body: data, headers: { Accept: "application/json" } }).then(response => {
        if (!response.ok) throw new Error(response.status);
        return response.json();
      });
    }

    async function score(data) {
      const button = form.querySelector("button[type=submit]");
      button.disabled = true;
      const pending = setTimeout(() => showProgress(form, overlay, 15), 300);
      let reply;
      try {
        reply = await send(data);
      } catch (failure) {
        clearTimeout(pending);
        HTMLFormElement.prototype.submit.call(form);
        return;
      }
      clearTimeout(pending);
      if (reply.saved && data.get("rescore") !== "1" && data.get("mode") !== "local") {
        overlay.hidden = true;
        button.disabled = false;
        const when = String(reply.created_at || "").slice(0, 16).replace("T", " ");
        const fresh = await confirmDialog({
          title: "Already scored",
          message: "This file was scored " + reply.score + "/100 on " + when + " with the same level and job description. Use that result for free, or ask Claude to score it again?",
          ok: "Score again",
          cancel: "Use saved result",
          focusCancel: true,
        });
        if (fresh) {
          data.set("rescore", "1");
          return score(data);
        }
      }
      window.location.href = reply.location;
    }

    form.addEventListener("submit", event => {
      event.preventDefault();
      const problem = fileProblem(input.files[0]);
      if (problem) {
        error.hidden = false;
        error.textContent = problem;
        return;
      }
      chooseScorer().then(mode => {
        if (!mode) return;
        form.querySelector("[data-mode-input]").value = mode;
        score(new FormData(form));
      });
    });
  }

  function showProgress(form, overlay, usual) {
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    overlay.hidden = false;
    const steps = Array.from(overlay.querySelectorAll("[data-steps] li"));
    const bar = overlay.querySelector("[data-progress]");
    const elapsed = overlay.querySelector("[data-elapsed]");
    const started = Date.now();
    (function tick() {
      const seconds = (Date.now() - started) / 1000;
      const current = steps.filter(step => Number(step.dataset.at) <= seconds).length - 1;
      steps.forEach((step, index) => {
        step.classList.toggle("done", index < current);
        step.classList.toggle("active", index === current);
      });
      bar.style.width = Math.min(95, 95 * (1 - Math.exp(-seconds / (usual / 2)))) + "%";
      elapsed.textContent = Math.floor(seconds) + "s · " + (seconds < usual * 1.4 ? "usually about " + usual + " seconds" : "taking a little longer than usual");
      if (!overlay.hidden) setTimeout(tick, 250);
    })();
    window.addEventListener("pageshow", event => {
      if (event.persisted) {
        overlay.hidden = true;
        button.disabled = false;
      }
    }, { once: true });
  }

  function chooseScorer() {
    let modal = document.querySelector("[data-scorer-modal]");
    if (!modal) {
      modal = document.createElement("div");
      modal.className = "modal-backdrop";
      modal.dataset.scorerModal = "";
      modal.hidden = true;
      modal.innerHTML = '<div class="modal scorer-modal" role="dialog" aria-modal="true" aria-labelledby="scorer-title">' +
        '<div class="modal-icon" aria-hidden="true"></div><h3 id="scorer-title">How should this resume be scored?</h3>' +
        '<div class="scorer-options">' +
        '<button type="button" class="scorer-option" data-scorer="llm"><b>Claude (LLM)</b><small>Parse checks and keywords, plus impact, seniority and clarity ratings and top fixes. Uses tokens, about 15 seconds.</small></button>' +
        '<button type="button" class="scorer-option" data-scorer="local"><b>Local ATS parser</b><small>Parse checks, word and bullet counts, pronouns, repeated verbs and keyword match only. Instant and free.</small></button>' +
        '</div><div class="modal-actions"><button type="button" class="btn" data-scorer-cancel>Cancel</button></div></div>';
      document.body.appendChild(modal);
    }
    const options = Array.from(modal.querySelectorAll("[data-scorer]"));
    let last = "llm";
    try { last = localStorage.getItem("ats-scorer") || "llm"; } catch (ignored) {}
    const opener = document.activeElement;
    modal.hidden = false;
    requestAnimationFrame(() => modal.classList.add("open"));
    (options.find(option => option.dataset.scorer === last) || options[0]).focus();

    return new Promise(resolve => {
      function close(answer) {
        modal.classList.remove("open");
        modal.hidden = true;
        document.removeEventListener("keydown", onKey, true);
        modal.removeEventListener("click", onClick);
        if (opener && opener.focus) opener.focus();
        if (answer) try { localStorage.setItem("ats-scorer", answer); } catch (ignored) {}
        resolve(answer);
      }
      function onKey(event) {
        if (event.key === "Escape") { event.preventDefault(); close(null); }
        if (event.key === "Tab") {
          event.preventDefault();
          const focusable = Array.from(modal.querySelectorAll("button"));
          const index = focusable.indexOf(document.activeElement);
          focusable[(index + (event.shiftKey ? focusable.length - 1 : 1)) % focusable.length].focus();
        }
      }
      function onClick(event) {
        const option = event.target.closest("[data-scorer]");
        if (option) close(option.dataset.scorer);
        else if (event.target === modal || event.target.closest("[data-scorer-cancel]")) close(null);
      }
      document.addEventListener("keydown", onKey, true);
      modal.addEventListener("click", onClick);
    });
  }

  function confirmDialog(options) {
    let modal = document.querySelector("[data-modal]");
    if (!modal) {
      modal = document.createElement("div");
      modal.className = "modal-backdrop";
      modal.dataset.modal = "";
      modal.hidden = true;
      modal.innerHTML = '<div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="modal-title" aria-describedby="modal-text">' +
        '<div class="modal-icon" aria-hidden="true"></div><h3 id="modal-title"></h3><p id="modal-text"></p>' +
        '<div class="modal-actions"><button type="button" class="btn" data-modal-cancel>Cancel</button><button type="button" class="btn primary" data-modal-ok></button></div></div>';
      document.body.appendChild(modal);
    }
    const ok = modal.querySelector("[data-modal-ok]");
    const cancel = modal.querySelector("[data-modal-cancel]");
    modal.querySelector("h3").textContent = options.title || "Are you sure?";
    modal.querySelector("p").textContent = options.message || "";
    modal.querySelector("p").hidden = !options.message;
    ok.textContent = options.ok || "Confirm";
    cancel.textContent = options.cancel || "Cancel";
    ok.className = "btn " + (options.danger ? "danger" : "primary");
    modal.classList.toggle("is-danger", Boolean(options.danger));
    const opener = document.activeElement;
    modal.hidden = false;
    requestAnimationFrame(() => modal.classList.add("open"));
    (options.danger || options.focusCancel ? cancel : ok).focus();

    return new Promise(resolve => {
      function close(answer) {
        modal.classList.remove("open");
        modal.hidden = true;
        document.removeEventListener("keydown", onKey, true);
        modal.removeEventListener("click", onClick);
        if (opener && opener.focus) opener.focus();
        resolve(answer);
      }
      function onKey(event) {
        if (event.key === "Escape") { event.preventDefault(); close(false); }
        if (event.key === "Tab") {
          event.preventDefault();
          (document.activeElement === ok ? cancel : ok).focus();
        }
      }
      function onClick(event) {
        if (event.target === modal || event.target.closest("[data-modal-cancel]")) close(false);
        else if (event.target.closest("[data-modal-ok]")) close(true);
      }
      document.addEventListener("keydown", onKey, true);
      modal.addEventListener("click", onClick);
    });
  }

  function setupConfirms() {
    document.addEventListener("click", event => {
      const button = event.target.closest("button[data-confirm]");
      if (!button || button.dataset.confirmed) return;
      event.preventDefault();
      confirmDialog({
        title: button.dataset.confirmTitle || button.dataset.confirm,
        message: button.dataset.confirmTitle ? button.dataset.confirm : "",
        ok: button.dataset.confirmOk,
        danger: "confirmDanger" in button.dataset,
      }).then(answer => {
        if (!answer || !button.form) return;
        button.dataset.confirmed = "1";
        button.form.requestSubmit(button);
        delete button.dataset.confirmed;
      });
    });
  }

  function setupResumeStart() {
    const form = document.querySelector("[data-rb-start]");
    if (!form) return;
    const overlay = document.querySelector("[data-progress-overlay]");
    const sourceLabel = form.querySelector("input[name=source]:checked");
    if (sourceLabel && sourceLabel.value !== "master") sourceLabel.closest("label").scrollIntoView({ block: "center" });
    form.addEventListener("submit", () => {
      const source = form.querySelector("input[name=source]:checked");
      if (source && "rewrite" in source.dataset) showProgress(form, overlay, 20);
      else form.querySelector("button[type=submit]").disabled = true;
    });
  }

  function setupResumeReview() {
    const form = document.querySelector("[data-rb-review]");
    if (!form) return;
    const boxes = Array.from(form.querySelectorAll("input[name=accept]"));
    const count = form.querySelector("[data-pick-count]");
    function update() {
      const picked = boxes.filter(box => box.checked).length;
      if (count && boxes.length) count.textContent = picked + " of " + boxes.length + " changes selected";
    }
    form.querySelectorAll("[data-pick]").forEach(button => button.addEventListener("click", () => {
      boxes.forEach(box => { box.checked = button.dataset.pick === "all"; });
      update();
    }));
    boxes.forEach(box => box.addEventListener("change", update));
    form.addEventListener("submit", () => { form.querySelector("button[type=submit]").disabled = true; });
    update();
  }

  function setupResumeEditor() {
    const root = document.querySelector("[data-rb-editor]");
    if (!root) return;
    const form = root.querySelector("[data-tex-form]");
    const textarea = root.querySelector("[data-tex]");
    const status = root.querySelector("[data-status]");
    const dirtyNote = root.querySelector("[data-dirty]");
    const errorBox = root.querySelector("[data-error]");
    const frame = root.querySelector("[data-pdf]");
    const empty = root.querySelector("[data-pdf-empty]");
    const compiling = root.querySelector("[data-compiling]");
    const pdfLink = root.querySelector("[data-download-pdf]");
    const compiledAt = root.querySelector("[data-compiled-at]");
    let editor = null;
    let dirty = false;
    let marked = null;
    let busy = null;

    if (window.CodeMirror) {
      editor = window.CodeMirror.fromTextArea(textarea, { mode: "stex", lineNumbers: true, lineWrapping: true, indentUnit: 2, tabSize: 2 });
      editor.on("change", () => setDirty(true));
    } else {
      textarea.classList.add("plain");
      textarea.addEventListener("input", () => setDirty(true));
    }
    const value = () => (editor ? editor.getValue() : textarea.value);

    function setDirty(next) {
      dirty = next;
      dirtyNote.textContent = next ? "Unsaved changes" : "";
      if (next) setStatus("idle", "Edited");
    }

    function setStatus(kind, text) {
      status.className = "rb-status " + kind;
      status.textContent = text;
    }

    function markLine(line) {
      if (!editor) return;
      if (marked !== null) editor.removeLineClass(marked, "background", "cm-error-line");
      marked = line ? line - 1 : null;
      if (marked !== null) {
        editor.addLineClass(marked, "background", "cm-error-line");
        editor.scrollTo(null, Math.max(0, editor.heightAtLine(marked, "local") - 80));
      }
    }

    function showError(text, line) {
      errorBox.hidden = !text;
      errorBox.querySelector("[data-error-text]").textContent = text || "";
      errorBox.querySelector("[data-error-line]").textContent = line ? " on line " + line : "";
      markLine(text ? line : null);
    }

    function recompile() {
      if (busy) return busy;
      setStatus("busy", "Compiling…");
      compiling.hidden = false;
      busy = fetch(form.action, {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
        body: new URLSearchParams({ tex: value() }),
      }).then(response => {
        if (!response.ok) throw new Error("The server answered " + response.status);
        return response.json();
      }).then(result => {
        setDirty(false);
        showError(result.error, result.line);
        if (result.ok) {
          setStatus("ok", "Compiled");
          frame.src = "/resume/" + root.dataset.draft + ".pdf?t=" + Date.now() + "#toolbar=0&view=FitH";
          frame.hidden = false;
          empty.hidden = true;
          pdfLink.hidden = false;
          compiledAt.textContent = "Updated " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
          toast("Compiled and saved");
        } else {
          setStatus("bad", "Compile error");
          toast("Saved, but LaTeX reported an error");
        }
        return result.ok;
      }).catch(error => {
        setStatus("bad", "Not saved");
        toast("Could not save: " + error.message);
        return false;
      }).finally(() => {
        busy = null;
        compiling.hidden = true;
      });
      return busy;
    }

    const ENGINE_LINE = /^%\s*!TEX\s+(?:TS-)?program\s*=\s*(\w+)/;
    const overleafEngine = root.querySelector("[data-overleaf-engine]");

    function engineOf(tex) {
      const match = tex.match(ENGINE_LINE);
      const engine = match && match[1].toLowerCase();
      return engine === "lualatex" ? "lualatex" : "xelatex";
    }

    function replaceAll(next) {
      if (!editor) {
        textarea.value = next;
        setDirty(true);
        return;
      }
      const scroll = editor.getScrollInfo();
      editor.replaceRange(next, { line: 0, ch: 0 }, { line: editor.lastLine() });
      editor.scrollTo(scroll.left, scroll.top);
    }

    function fixNote(note, kind, text, changes) {
      note.hidden = false;
      note.className = "fix-note " + kind;
      note.textContent = text;
      (changes || []).forEach(change => {
        const row = document.createElement("div");
        row.className = "fix-change";
        const label = document.createElement("b");
        label.textContent = change.label;
        const before = document.createElement("del");
        before.textContent = change.before;
        const after = document.createElement("ins");
        after.textContent = change.after;
        row.append(label, before, after);
        note.append(row);
      });
    }

    async function applyFix(button) {
      const note = button.closest("li").querySelector("[data-fix-note]");
      const label = button.querySelector("span");
      const sent = value();
      button.disabled = true;
      button.classList.add("working");
      label.textContent = "Applying…";
      note.hidden = true;
      try {
        const response = await fetch(button.dataset.applyFix, {
          method: "POST",
          headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
          body: new URLSearchParams({ tex: sent, fix: button.dataset.fix }),
        });
        if (!response.ok) throw new Error("The server answered " + response.status);
        const reply = await response.json();
        if (!reply.ok) throw new Error(reply.error);
        if (!reply.changes.length) {
          fixNote(note, "muted", reply.message);
          label.textContent = "Apply fix";
          button.disabled = false;
          return;
        }
        if (value() !== sent) throw new Error("You edited the file while this fix was being written. Apply it again.");
        replaceAll(reply.tex);
        const count = reply.changes.length;
        fixNote(note, "ok", "Updated " + count + (count === 1 ? " line" : " lines") + " in main.tex · ⌘Z in the editor undoes it. Check the score again to see the effect.", reply.changes);
        label.textContent = "Applied";
        button.classList.add("done");
        recompile();
      } catch (failure) {
        fixNote(note, "bad", "Could not apply this fix: " + failure.message);
        label.textContent = "Apply fix";
        button.disabled = false;
      } finally {
        button.classList.remove("working");
      }
    }

    setupDraftScore(root, recompile, () => dirty || status.classList.contains("bad") || frame.hidden, applyFix);

    root.querySelector("[data-recompile]").addEventListener("click", recompile);
    form.addEventListener("submit", event => { event.preventDefault(); recompile(); });
    document.addEventListener("keydown", event => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        recompile();
      }
    });
    root.querySelector("[data-overleaf]").addEventListener("submit", event => {
      event.currentTarget.querySelector("input[name=snip]").value = value();
      overleafEngine.value = engineOf(value());
    });
    root.querySelector("[data-download-tex]").addEventListener("click", event => {
      if (!dirty) return;
      event.preventDefault();
      const blob = new Blob([value()], { type: "application/x-tex" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "resume.tex";
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 1000);
    });
    document.addEventListener("click", event => {
      const link = event.target.closest("a[href]");
      if (!dirty || !link || link.target || link.hasAttribute("download") || link.closest("[data-rb-editor] .rb-actions")) return;
      event.preventDefault();
      confirmDialog({ title: "Leave without saving?", message: "Your edits to main.tex have not been compiled or saved yet.", ok: "Discard edits", danger: true })
        .then(answer => { if (answer) { dirty = false; window.location.href = link.href; } });
    });
    if (editor && !errorBox.hidden) {
      const line = Number((errorBox.querySelector("[data-error-line]").textContent.match(/\d+/) || [0])[0]);
      markLine(line || null);
    }
  }

  function setupDraftScore(root, recompile, needsCompile, applyFix) {
    const panel = root.querySelector("[data-rb-ats]");
    const toggle = root.querySelector("[data-ats-toggle]");
    const form = panel.querySelector("[data-rb-ats-form]");
    const run = panel.querySelector("[data-ats-run]");
    const busy = panel.querySelector("[data-ats-busy]");
    const busyText = panel.querySelector("[data-ats-busy-text]");
    const error = panel.querySelector("[data-ats-error]");
    const result = panel.querySelector("[data-ats-result]");

    function open(next) {
      panel.hidden = !next;
      toggle.setAttribute("aria-expanded", String(next));
      toggle.classList.toggle("on", next);
      if (next) panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    toggle.addEventListener("click", () => open(panel.hidden));
    result.addEventListener("click", event => {
      const button = event.target.closest("[data-apply-fix]");
      if (button && !button.disabled) applyFix(button);
    });
    panel.querySelector("[data-ats-close]").addEventListener("click", () => open(false));

    function fail(text) {
      error.hidden = false;
      error.textContent = text;
    }

    form.addEventListener("submit", async event => {
      event.preventDefault();
      const mode = await chooseScorer();
      if (!mode) return;
      form.querySelector("[data-mode-input]").value = mode;
      error.hidden = true;
      run.disabled = true;
      busy.hidden = false;
      result.classList.add("stale");
      let timer = null;
      try {
        if (needsCompile()) {
          busyText.textContent = "Saving and compiling your edits…";
          if (!(await recompile())) {
            fail("Fix the LaTeX error and compile before scoring.");
            return;
          }
        }
        const started = Date.now();
        (function tick() {
          busyText.textContent = "Scoring your draft… " + Math.floor((Date.now() - started) / 1000) + "s" + (mode === "local" ? "" : " · usually about 15 seconds");
          timer = setTimeout(tick, 500);
        })();
        const response = await fetch(form.action, { method: "POST", body: new URLSearchParams(new FormData(form)), headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("The server answered " + response.status);
        const reply = await response.json();
        if (!reply.ok) {
          fail(reply.error);
          return;
        }
        result.innerHTML = reply.html;
        result.classList.remove("stale");
        countUp(result);
      } catch (failure) {
        fail("Could not score this draft: " + failure.message);
      } finally {
        clearTimeout(timer);
        run.disabled = false;
        busy.hidden = true;
      }
    });
  }

  function countUp(scope) {
    (scope || document).querySelectorAll("[data-count]").forEach(el => {
      const target = Number(el.dataset.count);
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const started = performance.now();
      (function frame(now) {
        const t = Math.min(1, (now - started) / 1400);
        el.textContent = Math.round(target * (1 - Math.pow(1 - t, 3)));
        if (t < 1) requestAnimationFrame(frame);
      })(started);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    setupFilters();
    setupGreeting();
    countUp();
    document.querySelectorAll(".bar-col").forEach((bar, index) => bar.style.setProperty("--i", index));
    document.querySelectorAll(".job").forEach((job, index) => { job.style.animationDelay = Math.min(index, 12) * 25 + "ms"; });
    toast(session.take("toast"));

    document.querySelectorAll("form[data-toast]").forEach(form => {
      form.addEventListener("submit", () => session.set("toast", form.dataset.toast));
    });

    document.querySelectorAll("button[data-suggest]").forEach(button => {
      button.addEventListener("click", event => {
        event.preventDefault();
        const box = document.getElementById(button.dataset.suggest);
        box.value = box.placeholder;
        box.focus();
      });
    });

    setupAts();
    setupConfirms();
    setupResumeStart();
    setupResumeReview();
    setupResumeEditor();

    const toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) toggle.addEventListener("click", () => {
      const next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      store.set("theme", next);
    });

    document.addEventListener("click", event => {
      const button = event.target.closest("[data-copy]");
      if (!button) return;
      const source = document.getElementById(button.dataset.copy);
      navigator.clipboard.writeText(source.innerText).then(() => {
        button.classList.add("done");
        toast("Copied to clipboard");
        setTimeout(() => button.classList.remove("done"), 1500);
      });
    });

    document.addEventListener("keydown", event => {
      const input = document.querySelector("[data-search-input]");
      if (event.key === "/" && input && document.activeElement !== input) {
        event.preventDefault();
        input.focus();
      } else if (event.key === "Escape" && input && document.activeElement === input) {
        input.value = "";
        input.dispatchEvent(new Event("input"));
        input.blur();
      }
    });
  });
})();
