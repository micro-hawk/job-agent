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

    form.addEventListener("submit", event => {
      const problem = fileProblem(input.files[0]);
      if (problem) {
        event.preventDefault();
        error.hidden = false;
        error.textContent = problem;
        return;
      }
      form.querySelector("button[type=submit]").disabled = true;
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
        bar.style.width = Math.min(95, 95 * (1 - Math.exp(-seconds / 7))) + "%";
        elapsed.textContent = seconds < 20 ? Math.floor(seconds) + "s · usually about 15 seconds" : Math.floor(seconds) + "s · taking a little longer than usual";
        setTimeout(tick, 250);
      })();
    });

    window.addEventListener("pageshow", event => {
      if (event.persisted) {
        overlay.hidden = true;
        form.querySelector("button[type=submit]").disabled = false;
      }
    });
  }

  function countUp() {
    document.querySelectorAll("[data-count]").forEach(el => {
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

    document.querySelectorAll("button[data-confirm]").forEach(button => {
      button.addEventListener("click", event => { if (!confirm(button.dataset.confirm)) event.preventDefault(); });
    });

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
