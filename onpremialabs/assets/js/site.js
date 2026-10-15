/* onpremIA Labs — site behaviour.
   Plain JavaScript, no framework. The page is complete without it (English,
   every section visible, the link panel as its menu); this file adds the language
   switch, the Menu dropdown, and the motion. Anything animated stops for visitors who
   ask for reduced motion, and for parts of the page that are off screen. */

(() => {
  "use strict";

  const root = document.documentElement;
  root.classList.add("js");

  const I18N = window.OPL_I18N || { es: {}, dynamic: { es: {}, en: {} } };
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

  /* ------------------------------------------------------------ menu contents */

  // The dropdown carries a copy of the panel's links, made before the language
  // code reads the page so the copies are translated with everything else.
  const menuNav = $("#menu-drop nav");
  $$("#panel-nav .panel__links > a").forEach((a) => menuNav.append(a.cloneNode(true)));

  /* ------------------------------------------------------------ language */

  let lang = "en";
  const listeners = [];

  // The English text is read from the page once, so it is written in one place only.
  $$("[data-i18n]").forEach((el) => { el.dataset.en = el.textContent; });
  $$("[data-i18n-aria]").forEach((el) => { el.dataset.enAria = el.getAttribute("aria-label") || ""; });
  $$("[data-i18n-placeholder]").forEach((el) => { el.dataset.enPh = el.getAttribute("placeholder") || ""; });

  function applyLanguage(next) {
    lang = next === "es" ? "es" : "en";
    const es = lang === "es" ? I18N.es : null;
    $$("[data-i18n]").forEach((el) => {
      el.textContent = (es && es[el.dataset.i18n]) || el.dataset.en;
    });
    $$("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", (es && es[el.dataset.i18nAria]) || el.dataset.enAria);
    });
    $$("[data-i18n-placeholder]").forEach((el) => {
      el.setAttribute("placeholder", (es && es[el.dataset.i18nPlaceholder]) || el.dataset.enPh);
    });
    root.lang = lang;
    $$("#lang-toggle [data-lang]").forEach((s) => s.classList.toggle("is-on", s.dataset.lang === lang));
    store.set("opl-lang", lang);
    listeners.forEach((fn) => fn(lang));
  }

  const dyn = () => I18N.dynamic[lang] || I18N.dynamic.en;
  const onLanguage = (fn) => listeners.push(fn);
  $("#lang-toggle").addEventListener("click", () => applyLanguage(lang === "en" ? "es" : "en"));

  /* ------------------------------------------------------------ top bar and menu */

  const topbar = $(".topbar");
  const menuBtn = $("#menu-btn");
  const menuDrop = $("#menu-drop");

  const onScroll = () => topbar.classList.toggle("is-scrolled", window.scrollY > 8);
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // The Menu button opens a floating bar of links under the sticky top bar. It
  // stays open while the visitor jumps between sections and scrolls, and closes
  // only with the Menu button again or Escape. Without JavaScript the button
  // stays hidden and the panel is the menu.
  menuBtn.hidden = false;
  const menuOpen = () => menuBtn.getAttribute("aria-expanded") === "true";

  // A section reached from a link must land below the bar and the open menu,
  // not under them, so the scroll offset follows their combined height.
  function fitScrollOffset() {
    root.style.scrollPaddingTop = `${topbar.offsetHeight + (menuOpen() ? menuDrop.offsetHeight : 0) + 8}px`;
  }

  function setMenu(open, { focus = false } = {}) {
    menuBtn.setAttribute("aria-expanded", String(open));
    menuDrop.hidden = !open;
    fitScrollOffset();
    if (open && focus) $("a", menuDrop).focus();
  }
  menuBtn.addEventListener("click", () => setMenu(!menuOpen(), { focus: true }));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menuOpen()) { setMenu(false); menuBtn.focus(); }
  });
  window.addEventListener("resize", fitScrollOffset);

  if ("IntersectionObserver" in window) {
    const pills = $$("a.pill", $("#panel-nav")).concat($$("a.pill", menuDrop));
    const spy = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        pills.forEach((a) => a.classList.toggle("is-current", a.hash === `#${entry.target.id}`));
      });
    }, { rootMargin: "-45% 0px -50% 0px" });
    pills.forEach((a) => { const s = $(a.hash); if (s) spy.observe(s); });
  }

  /* ------------------------------------------------------------ reveal */

  if ("IntersectionObserver" in window && !reduced) {
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) { entry.target.classList.add("is-in"); io.unobserve(entry.target); }
      });
    }, { threshold: 0.1, rootMargin: "0px 0px -40px 0px" });
    $$(".reveal").forEach((el, i) => {
      el.style.transitionDelay = `${(i % 5) * 60}ms`;
      io.observe(el);
    });
  } else {
    $$(".reveal").forEach((el) => el.classList.add("is-in"));
  }

  /* ------------------------------------------------------------ hero rotator */

  const rotator = $("#rotator");
  let rotIndex = 0;
  let rotTimer = null;

  function typeWord(word, done) {
    let i = 0;
    rotator.textContent = "";
    const step = () => {
      rotator.textContent = word.slice(0, ++i);
      rotTimer = i < word.length ? setTimeout(step, 70) : setTimeout(done, 1900);
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
  onLanguage(() => {
    clearTimeout(rotTimer);
    if (reduced) rotator.textContent = dyn().rotator.join(" · ");
    else rotate();
  });

  /* ------------------------------------------------------------ hero mesh */

  (() => {
    const canvas = $("#mesh");
    const ctx = canvas.getContext("2d");
    const GREEN = "16,185,129", BLUE = "59,130,246", SKY = "56,189,248";
    let w = 0, h = 0, nodes = [], packets = [], raf = 0, running = false;

    function resize() {
      const rect = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = rect.width; h = rect.height;
      canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const count = Math.round(Math.min(80, Math.max(26, (w * h) / 14000)));
      nodes = Array.from({ length: count }, () => ({
        x: Math.random() * w, y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3,
        r: Math.random() * 1.5 + 1, agent: Math.random() < 0.15, phase: Math.random() * 6.28,
      }));
      packets = [];
      if (!running) draw();
    }

    const linkDist = () => Math.min(160, Math.max(100, w / 8));

    function step() {
      for (const n of nodes) {
        n.x += n.vx; n.y += n.vy; n.phase += 0.03;
        if (n.x < -20) n.x = w + 20; else if (n.x > w + 20) n.x = -20;
        if (n.y < -20) n.y = h + 20; else if (n.y > h + 20) n.y = -20;
      }
      // Now and then an agent sends a message to a neighbour: a packet along a link.
      if (packets.length < 6 && Math.random() < 0.05) {
        const agents = nodes.filter((n) => n.agent);
        const from = agents[Math.floor(Math.random() * agents.length)];
        if (from) {
          const near = nodes.filter((n) => n !== from && Math.hypot(n.x - from.x, n.y - from.y) < linkDist());
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
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          const dist = Math.hypot(a.x - b.x, a.y - b.y);
          if (dist < d) {
            ctx.strokeStyle = `rgba(${BLUE},${(1 - dist / d) * 0.3})`;
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
          }
        }
      }
      for (const n of nodes) {
        if (n.agent) {
          ctx.fillStyle = `rgba(${GREEN},0.15)`;
          ctx.beginPath(); ctx.arc(n.x, n.y, n.r + 5 + Math.sin(n.phase) * 2.5, 0, 6.283); ctx.fill();
          ctx.fillStyle = `rgba(${GREEN},0.95)`;
        } else {
          ctx.fillStyle = `rgba(${SKY},0.55)`;
        }
        ctx.beginPath(); ctx.arc(n.x, n.y, n.agent ? n.r + 1.2 : n.r, 0, 6.283); ctx.fill();
      }
      for (const p of packets) {
        ctx.fillStyle = `rgba(${GREEN},${Math.sin(p.t * Math.PI)})`;
        ctx.beginPath();
        ctx.arc(p.from.x + (p.to.x - p.from.x) * p.t, p.from.y + (p.to.y - p.from.y) * p.t, 2.4, 0, 6.283);
        ctx.fill();
      }
    }

    function loop() { step(); draw(); raf = requestAnimationFrame(loop); }
    function start() { if (running || reduced) return; running = true; loop(); }
    function stop() { running = false; cancelAnimationFrame(raf); }

    let resizeTimer;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 150); });
    document.addEventListener("visibilitychange", () => (document.hidden ? stop() : start()));
    resize();
    whileVisible(canvas, start, stop);
  })();

  /* ------------------------------------------------------------ values: one lit at a time */

  const values = $$(".value");
  let valueIndex = -1, valueTimer = null, hovering = false;
  function lightNext() {
    if (hovering) return;
    valueIndex = (valueIndex + 1) % values.length;
    values.forEach((v, i) => v.classList.toggle("is-lit", i === valueIndex));
  }
  $(".values").addEventListener("mouseenter", () => { hovering = true; values.forEach((v) => v.classList.remove("is-lit")); });
  $(".values").addEventListener("mouseleave", () => { hovering = false; });
  whileVisible($(".values"), () => {
    if (reduced || valueTimer) return;
    lightNext();
    valueTimer = setInterval(lightNext, 2600);
  }, () => { clearInterval(valueTimer); valueTimer = null; });

  /* ------------------------------------------------------------ product filter */

  const chips = $$("[data-filter]");
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

  // Real command names; the output is illustrative and the page says so.
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
    // A few modalities light up together: one flow combines them.
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
    void actorBox.offsetWidth; // restarts the animation
    actorBox.classList.add("is-swapping");
  }
  actors.forEach((a) => a.addEventListener("click", () => showActor(a.dataset.actor)));
  onLanguage(() => showActor(currentActor));

  /* ------------------------------------------------------------ partner logos */

  // The logos live on www.onpremialabs.com; if one cannot load, its name stays.
  $$(".partner__logo img").forEach((img) => {
    const fallback = () => { img.replaceWith(Object.assign(document.createElement("b"), { textContent: img.alt })); };
    if (img.complete && img.naturalWidth === 0) fallback();
    else img.addEventListener("error", fallback);
  });

  /* ------------------------------------------------------------ contact form */

  // Messages go to contact.php, next to this page on the lab's own hosting,
  // which emails them to CONTACT_EMAIL: the visitor's details never pass
  // through a third party. It takes a JSON POST and answers {"success": true}
  // or {"success": false, "error": "..."}. See onpremialabs/README.md.
  const CONTACT_EMAIL = "info@onpremialabs.com";
  const CONTACT_ENDPOINT = "contact.php";

  const dialog = $("#contact-dialog");
  const form = $("#contact-form");
  const thanks = $("#contact-thanks");
  const status = $("#contact-status");
  const submit = $("#contact-submit");
  const canDialog = typeof dialog.showModal === "function";
  let opener = null;

  function resetContact() {
    form.hidden = false;
    thanks.hidden = true;
    status.hidden = true;
    form.reset();
    $$("[aria-invalid]", form).forEach((el) => el.removeAttribute("aria-invalid"));
    $$(".field__error", form).forEach((el) => { el.textContent = ""; });
  }

  function openContact(topic) {
    if (!canDialog) { window.location.href = `mailto:${CONTACT_EMAIL}`; return; }
    if (!thanks.hidden) resetContact();
    if (topic) form.elements.topic.value = topic;
    dialog.showModal();
    document.body.style.overflow = "hidden";
    form.elements.name.focus();
  }
  function closeContact() { dialog.close(); }

  dialog.addEventListener("close", () => {
    document.body.style.overflow = "";
    if (!thanks.hidden) resetContact(); // the next visitor of this page starts afresh
    if (opener) opener.focus();
  });
  // A click on the backdrop (outside the card) closes it, as Escape does.
  dialog.addEventListener("click", (e) => { if (e.target === dialog) closeContact(); });
  $$("[data-close-contact]").forEach((b) => b.addEventListener("click", closeContact));
  $$("[data-open-contact]").forEach((a) => a.addEventListener("click", (e) => {
    e.preventDefault();
    opener = a;
    openContact(a.dataset.topic);
  }));

  function validate() {
    const msg = dyn().form;
    const checks = {
      name: (v) => (v.trim().length >= 2 ? "" : msg.name),
      email: (v) => (/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(v.trim()) ? "" : msg.email),
      topic: (v) => (v ? "" : msg.topic),
      message: (v) => (v.trim().length >= 10 ? "" : msg.message),
    };
    let first = null;
    for (const [field, check] of Object.entries(checks)) {
      const el = form.elements[field];
      const error = check(el.value);
      if (error) el.setAttribute("aria-invalid", "true");
      else el.removeAttribute("aria-invalid");
      $(`[data-error-for="${field}"]`, form).textContent = error;
      if (error && !first) first = el;
    }
    const consent = form.elements.consent;
    if (consent.checked) consent.removeAttribute("aria-invalid");
    else consent.setAttribute("aria-invalid", "true");
    $('[data-error-for="consent"]', form).textContent = consent.checked ? "" : msg.consent;
    if (!consent.checked && !first) first = consent;
    if (first) first.focus();
    return !first;
  }

  // Errors clear as soon as the visitor fixes the field.
  form.addEventListener("input", (e) => {
    if (e.target.getAttribute("aria-invalid") === "true") {
      e.target.removeAttribute("aria-invalid");
      const box = $(`[data-error-for="${e.target.name}"]`, form);
      if (box) box.textContent = "";
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    status.hidden = true;
    if (!validate()) return;
    if (form.elements._honey.value) { showThanks(); return; } // a bot: pretend, send nothing

    const data = Object.fromEntries(new FormData(form));
    const payload = {
      name: data.name.trim(),
      email: data.email.trim(),
      organization: (data.organization || "").trim(),
      topic: data.topic,
      message: data.message.trim(),
      language: lang,
      _honey: data._honey || "",
    };

    submit.disabled = true;
    submit.classList.add("is-sending");
    try {
      const res = await fetch(CONTACT_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || body.success !== true) throw Object.assign(new Error(body.error || res.status), { status: res.status });
      showThanks();
    } catch (err) {
      // Never lose the message: offer the visitor's own mail client with it filled in.
      const subject = `[onpremialabs.com] ${payload.topic} — ${payload.name}`;
      const mailto = `mailto:${CONTACT_EMAIL}?subject=${encodeURIComponent(subject)}` +
        `&body=${encodeURIComponent(payload.message)}`;
      const text = err.status === 429 ? dyn().form.tooMany : dyn().form.failed;
      status.innerHTML = `${esc(text)} <a href="${mailto}">${CONTACT_EMAIL}</a>.`;
      status.hidden = false;
    } finally {
      submit.disabled = false;
      submit.classList.remove("is-sending");
    }
  });

  function showThanks() {
    form.hidden = true;
    thanks.hidden = false;
    thanks.focus();
  }

  /* ------------------------------------------------------------ start */

  $("#year").textContent = String(new Date().getFullYear());
  // English unless the visitor chose Spanish with the switch before.
  applyLanguage(store.get("opl-lang") === "es" ? "es" : "en");
})();
