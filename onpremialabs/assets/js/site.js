/* onpremIA Labs — site behaviour.
   Plain JavaScript, no framework and no third-party code. The page is complete
   without it (Spanish, every section visible); this file adds the language
   switch, the theme, and the motion. Anything animated stops for visitors who
   ask for reduced motion, and for parts of the page that are off screen. */

(() => {
  "use strict";

  const root = document.documentElement;
  root.classList.add("js");

  const I18N = window.OPL_I18N || { en: {}, dynamic: { es: {}, en: {} } };
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const $ = (sel, ctx = document) => ctx.querySelector(sel);
  const $$ = (sel, ctx = document) => Array.from(ctx.querySelectorAll(sel));

  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* private mode */ } },
  };

  /** Runs `start` while `el` is on screen and `stop` when it leaves. */
  function whileVisible(el, start, stop) {
    if (!el) return;
    if (!("IntersectionObserver" in window)) { start(); return; }
    new IntersectionObserver((entries) => {
      entries.forEach((e) => (e.isIntersecting ? start() : stop()));
    }, { threshold: 0.15 }).observe(el);
  }

  /* ------------------------------------------------------------ language */

  let lang = "es";
  const listeners = [];

  // The Spanish text is read from the page once, so it is written in one place only.
  $$("[data-i18n]").forEach((el) => { el.dataset.es = el.textContent; });
  $$("[data-i18n-html]").forEach((el) => { el.dataset.esHtml = el.innerHTML; });
  $$("[data-i18n-aria]").forEach((el) => { el.dataset.esAria = el.getAttribute("aria-label") || ""; });

  function applyLanguage(next) {
    lang = next === "en" ? "en" : "es";
    const en = lang === "en" ? I18N.en : null;
    $$("[data-i18n]").forEach((el) => {
      el.textContent = (en && en[el.dataset.i18n]) || el.dataset.es;
    });
    $$("[data-i18n-html]").forEach((el) => {
      el.innerHTML = (en && en[el.dataset.i18nHtml]) || el.dataset.esHtml;
    });
    $$("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", (en && en[el.dataset.i18nAria]) || el.dataset.esAria);
    });
    root.lang = lang;
    $$("#lang-toggle [data-lang]").forEach((s) => s.classList.toggle("is-on", s.dataset.lang === lang));
    store.set("opl-lang", lang);
    listeners.forEach((fn) => fn(lang));
  }

  const dyn = () => I18N.dynamic[lang] || I18N.dynamic.es;
  const onLanguage = (fn) => listeners.push(fn);

  $("#lang-toggle").addEventListener("click", () => applyLanguage(lang === "es" ? "en" : "es"));

  /* ------------------------------------------------------------ theme */

  const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");
  const effectiveTheme = () => root.dataset.theme || (darkQuery.matches ? "dark" : "light");
  const themeButton = $("#theme-toggle");

  function labelTheme() {
    const d = dyn();
    themeButton.setAttribute("aria-label", effectiveTheme() === "dark" ? d.themeLight : d.themeDark);
  }

  themeButton.addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    store.set("opl-theme", next);
    labelTheme();
    mesh.recolour();
  });
  darkQuery.addEventListener?.("change", () => { labelTheme(); mesh.recolour(); });
  onLanguage(labelTheme);

  /* ------------------------------------------------------------ navigation */

  const nav = $(".nav");
  const burger = $("#nav-burger");
  const links = $("#nav-links");

  const onScroll = () => nav.classList.toggle("is-scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  burger.addEventListener("click", () => {
    const open = burger.getAttribute("aria-expanded") !== "true";
    burger.setAttribute("aria-expanded", String(open));
    links.classList.toggle("is-open", open);
  });
  links.addEventListener("click", (e) => {
    if (e.target.closest("a")) {
      burger.setAttribute("aria-expanded", "false");
      links.classList.remove("is-open");
    }
  });

  if ("IntersectionObserver" in window) {
    const navLinks = $$("a", links);
    const spy = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        navLinks.forEach((a) => a.classList.toggle("is-current", a.hash === `#${entry.target.id}`));
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    navLinks.forEach((a) => { const s = $(a.hash); if (s) spy.observe(s); });
  }

  /* ------------------------------------------------------------ reveal */

  if ("IntersectionObserver" in window && !reduced) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add("is-in"); io.unobserve(entry.target); }
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -40px 0px" });
    $$(".reveal").forEach((el, i) => {
      el.style.transitionDelay = `${(i % 4) * 70}ms`;
      io.observe(el);
    });
  } else {
    $$(".reveal").forEach((el) => el.classList.add("is-in"));
  }

  /* ------------------------------------------------------------ hero: clock and rotator */

  const clock = $("#board-clock");
  const tick = () => {
    clock.textContent = new Date().toLocaleTimeString(lang === "en" ? "en-GB" : "es-ES", { hour12: false });
  };
  tick();
  setInterval(tick, 1000);

  const rotator = $("#rotator");
  let rotIndex = 0;
  let rotTimer = null;

  function typeWord(word, done) {
    let i = 0;
    rotator.textContent = "";
    const step = () => {
      rotator.textContent = word.slice(0, ++i);
      if (i < word.length) rotTimer = setTimeout(step, 70);
      else rotTimer = setTimeout(done, 1900);
    };
    step();
  }
  function eraseWord(done) {
    const step = () => {
      rotator.textContent = rotator.textContent.slice(0, -1);
      if (rotator.textContent) rotTimer = setTimeout(step, 35);
      else done();
    };
    step();
  }
  function rotate() {
    const words = dyn().rotator;
    typeWord(words[rotIndex % words.length], () => eraseWord(() => { rotIndex++; rotate(); }));
  }
  function startRotator() {
    clearTimeout(rotTimer);
    if (reduced) { rotator.textContent = dyn().rotator.join(" · "); return; }
    rotate();
  }
  onLanguage(startRotator);

  /* ------------------------------------------------------------ hero: agent mesh */

  const mesh = (() => {
    const canvas = $("#mesh");
    const ctx = canvas.getContext("2d");
    let w = 0, h = 0, dpr = 1, nodes = [], packets = [], raf = 0, running = false;
    let colNode = "29,115,80", colWarm = "163,93,42";

    function recolour() {
      const css = getComputedStyle(root);
      colNode = css.getPropertyValue("--mesh-node").trim() || colNode;
      colWarm = css.getPropertyValue("--mesh-warm").trim() || colWarm;
      if (!running) draw();
    }

    function resize() {
      const rect = canvas.getBoundingClientRect();
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width; h = rect.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.round(Math.min(90, Math.max(28, (w * h) / 16000)));
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.28,
        vy: (Math.random() - 0.5) * 0.28,
        r: Math.random() * 1.6 + 1.1,
        agent: Math.random() < 0.14,
        phase: Math.random() * Math.PI * 2,
      }));
      packets = [];
      if (!running) draw();
    }

    const linkDist = () => Math.min(170, Math.max(110, w / 9));

    function step() {
      for (const n of nodes) {
        n.x += n.vx; n.y += n.vy; n.phase += 0.03;
        if (n.x < -20) n.x = w + 20; else if (n.x > w + 20) n.x = -20;
        if (n.y < -20) n.y = h + 20; else if (n.y > h + 20) n.y = -20;
      }
      // An agent occasionally sends a message to a neighbour: a packet travelling a link.
      if (packets.length < 6 && Math.random() < 0.05) {
        const agents = nodes.filter((n) => n.agent);
        const from = agents[Math.floor(Math.random() * agents.length)];
        if (from) {
          const d = linkDist();
          const near = nodes.filter((n) => n !== from && Math.hypot(n.x - from.x, n.y - from.y) < d);
          const to = near[Math.floor(Math.random() * near.length)];
          if (to) packets.push({ from, to, t: 0 });
        }
      }
      packets = packets.filter((p) => (p.t += 0.018) < 1);
    }

    function draw() {
      ctx.clearRect(0, 0, w, h);
      const d = linkDist();
      for (let i = 0; i < nodes.length; i++) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j++) {
          const b = nodes[j];
          const dist = Math.hypot(a.x - b.x, a.y - b.y);
          if (dist < d) {
            ctx.strokeStyle = `rgba(${colNode},${(1 - dist / d) * 0.28})`;
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          }
        }
      }
      for (const n of nodes) {
        if (n.agent) {
          const glow = 5 + Math.sin(n.phase) * 2.5;
          ctx.fillStyle = `rgba(${colWarm},0.16)`;
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r + glow, 0, Math.PI * 2); ctx.fill();
          ctx.fillStyle = `rgba(${colWarm},0.95)`;
        } else {
          ctx.fillStyle = `rgba(${colNode},0.7)`;
        }
        ctx.beginPath(); ctx.arc(n.x, n.y, n.agent ? n.r + 1.2 : n.r, 0, Math.PI * 2); ctx.fill();
      }
      for (const p of packets) {
        const x = p.from.x + (p.to.x - p.from.x) * p.t;
        const y = p.from.y + (p.to.y - p.from.y) * p.t;
        ctx.fillStyle = `rgba(${colWarm},${Math.sin(p.t * Math.PI)})`;
        ctx.beginPath(); ctx.arc(x, y, 2.4, 0, Math.PI * 2); ctx.fill();
      }
    }

    function loop() { step(); draw(); raf = requestAnimationFrame(loop); }
    function start() { if (running || reduced) return; running = true; loop(); }
    function stop() { running = false; cancelAnimationFrame(raf); }

    let resizeTimer;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 150); });
    document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));

    recolour();
    resize();
    whileVisible(canvas, start, stop);
    return { recolour };
  })();

  /* ------------------------------------------------------------ values */

  const valuesBox = $(".values");
  const tabs = $$(".vtab");
  const CYCLE_MS = 7000;
  let valueIndex = 0, valueTimer = null, valuesAuto = !reduced, valuesOnScreen = false;
  valuesBox.style.setProperty("--cycle", `${CYCLE_MS}ms`);

  function selectValue(index, focus) {
    valueIndex = (index + tabs.length) % tabs.length;
    tabs.forEach((tab, i) => {
      const on = i === valueIndex;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $(`#${tab.getAttribute("aria-controls")}`).hidden = !on;
    });
    if (focus) tabs[valueIndex].focus();
    scheduleValues();
  }
  function scheduleValues() {
    clearTimeout(valueTimer);
    if (valuesAuto && valuesOnScreen) valueTimer = setTimeout(() => selectValue(valueIndex + 1), CYCLE_MS);
  }
  function stopAuto() {
    // Once a visitor picks a value themselves, the page stops changing it under them.
    valuesAuto = false;
    clearTimeout(valueTimer);
    valuesBox.classList.add("is-paused");
  }

  tabs.forEach((tab, i) => tab.addEventListener("click", () => { stopAuto(); selectValue(i); }));
  $(".values__tabs").addEventListener("keydown", (e) => {
    const keys = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 };
    if (e.key in keys) { e.preventDefault(); stopAuto(); selectValue(valueIndex + keys[e.key], true); }
    if (e.key === "Home") { e.preventDefault(); stopAuto(); selectValue(0, true); }
    if (e.key === "End") { e.preventDefault(); stopAuto(); selectValue(tabs.length - 1, true); }
  });
  if (reduced) valuesBox.classList.add("is-paused");
  whileVisible(valuesBox, () => {
    valuesOnScreen = true;
    if (valuesAuto) selectValue(valueIndex); // restarts the progress bar from zero
  }, () => { valuesOnScreen = false; clearTimeout(valueTimer); });

  /* ------------------------------------------------------------ product filter */

  const chips = $$(".chip[data-filter]");
  const products = $$("#products .product");
  chips.forEach((chip) => chip.addEventListener("click", () => {
    const filter = chip.dataset.filter;
    chips.forEach((c) => {
      c.classList.toggle("is-active", c === chip);
      c.setAttribute("aria-pressed", String(c === chip));
    });
    let shown = 0;
    products.forEach((p) => {
      const show = filter === "all" || p.dataset.status === filter;
      p.classList.toggle("is-hidden", !show);
      if (show) { shown++; p.classList.add("is-in"); }
    });
    $("#products-empty").hidden = shown > 0;
  }));

  /* ------------------------------------------------------------ OPAAS terminal */

  const SCRIPT = [
    ["cmd", "opaas discover scan --well-known -o inventory.json"],
    ["ok", "✓ engine  ollama  127.0.0.1:11434  qwen2.5:7b-instruct"],
    ["ok", "✓ agents identified: 2  (A2A card · sentinel records)"],
    ["cmd", "opaas run --model qwen2.5:7b-instruct --backend ollama --suite all"],
    ["out", "  A performance · B capability · C consistency"],
    ["out", "  D safety · E compliance   (per category, no total score)"],
    ["w", "  ! not measured → declared in limitations, never zero"],
    ["out", "  report.json  sha256:9c41…e07a"],
    ["cmd", "opaas sign report.json --key keys/root.pem"],
    ["ok", "✓ credential.json  W3C VC · Ed25519 · eddsa-jcs-2022"],
    ["cmd", "opaas verify credential.json"],
    ["ok", "✓ signature valid · verified offline"],
  ];
  const term = $("#terminal code");
  let termTimer = null, termPlayed = false;

  const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
  const lineHtml = ([kind, text]) =>
    kind === "cmd" ? `<span class="p">$</span> ${esc(text)}` : `<span class="${kind === "out" ? "o" : kind}">${esc(text)}</span>`;

  function playTerminal() {
    clearTimeout(termTimer);
    termPlayed = true;
    if (reduced) { term.innerHTML = SCRIPT.map(lineHtml).join("\n"); return; }
    const done = [];
    let line = 0;
    const render = (partial) =>
      (term.innerHTML = [...done, partial].filter((x) => x !== undefined).join("\n") + '<span class="cursor"></span>');

    function nextLine() {
      if (line >= SCRIPT.length) { render(); return; }
      const [kind, text] = SCRIPT[line];
      if (kind !== "cmd") {
        done.push(lineHtml(SCRIPT[line++]));
        render();
        termTimer = setTimeout(nextLine, 260);
        return;
      }
      let i = 0;
      const typeChar = () => {
        render(`<span class="p">$</span> ${esc(text.slice(0, ++i))}`);
        if (i < text.length) termTimer = setTimeout(typeChar, 24 + Math.random() * 30);
        else { done.push(lineHtml(SCRIPT[line++])); termTimer = setTimeout(nextLine, 420); }
      };
      termTimer = setTimeout(typeChar, 380);
    }
    nextLine();
  }
  $("#term-replay").addEventListener("click", playTerminal);
  whileVisible($(".terminal"), () => { if (!termPlayed) playTerminal(); }, () => {});

  /* ------------------------------------------------------------ Evolve */

  const stages = $$("#stages .stage");
  const modalities = $$(".modalities span");
  let evolveTimer = null, stageIndex = 0;

  function evolveTick() {
    stageIndex = (stageIndex + 1) % stages.length;
    stages.forEach((s, i) => s.classList.toggle("is-on", i === stageIndex));
    // A few modalities light up together: the flow combines them.
    const pick = new Set();
    while (pick.size < 3) pick.add(Math.floor(Math.random() * modalities.length));
    modalities.forEach((m, i) => m.classList.toggle("is-on", pick.has(i)));
  }
  whileVisible($(".evolve"), () => {
    if (reduced || evolveTimer) return;
    evolveTimer = setInterval(evolveTick, 1800);
  }, () => { clearInterval(evolveTimer); evolveTimer = null; });

  /* ------------------------------------------------------------ NexoDual */

  const actors = $$(".actor");
  const actorBox = $("#actor-detail");
  let currentActor = "hub";

  function showActor(name) {
    currentActor = name;
    const [title, text] = dyn().actors[name];
    actors.forEach((a) => a.setAttribute("aria-pressed", String(a.dataset.actor === name)));
    $$(".nexo__lines line").forEach((l) => l.classList.toggle("is-on", name === "hub" || l.dataset.link === name));
    $("#actor-title").textContent = title;
    $("#actor-text").textContent = text;
    actorBox.classList.remove("is-swapping");
    void actorBox.offsetWidth; // restart the animation
    actorBox.classList.add("is-swapping");
  }
  actors.forEach((a) => a.addEventListener("click", () => showActor(a.dataset.actor)));
  onLanguage(() => showActor(currentActor));

  /* ------------------------------------------------------------ start */

  $("#year").textContent = String(new Date().getFullYear());
  const saved = store.get("opl-lang");
  const preferred = saved || ((navigator.language || "es").toLowerCase().startsWith("en") ? "en" : "es");
  applyLanguage(preferred);
})();
