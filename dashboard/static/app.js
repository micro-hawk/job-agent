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

  document.addEventListener("DOMContentLoaded", () => {
    setupFilters();
    setupGreeting();
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

    document.querySelectorAll("form[data-busy]").forEach(form => {
      form.addEventListener("submit", () => {
        const button = form.querySelector("button[type=submit]");
        button.disabled = true;
        button.textContent = button.dataset.busyText;
      });
    });

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
