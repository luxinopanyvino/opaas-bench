# onpremIA Labs — website

The website of [onpremIA Labs](https://www.onpremialabs.com), the lab behind OPAAS.
It presents the lab, its values (governance, sovereignty, security, identity,
explainability) and its products: **OPAAS** (live, open source), **Evolve** and
**NexoDual** (in development).

This directory is not part of the `opaas` package: the sdist excludes it and the
wheel only contains `src/opaas`.

## Layout

```
onpremialabs/
├── index.html          the whole page, in Spanish; complete without JavaScript
└── assets/
    ├── css/site.css    design tokens (light and dark), layout, motion
    ├── js/i18n.js      English translations of every data-i18n key, and the
    │                   strings the scripts generate, in both languages
    ├── js/site.js      language and theme switches, hero mesh, values tabs,
    │                   product filter, OPAAS terminal, Evolve and NexoDual
    └── img/            favicon and the OPAAS meerkat (copy of docs/img/mascot.svg)
```

## Principles

- **No third-party resources.** No web fonts, CDNs, analytics or cookies: the
  site renders with the system font stack and loads only its own files. It is
  how the sovereignty value is practised, and the page says so — keep it true.
- **Spanish is the source text.** `index.html` holds it; `site.js` reads it from
  the page and swaps in `i18n.js` for English. A new text needs a `data-i18n`
  key (or `data-i18n-html` / `data-i18n-aria`) and its English entry.
- **No claim the products cannot back.** Evolve and NexoDual are marked
  *in development*; the OPAAS terminal is labelled an illustrative demo.
- **Motion is optional.** Everything animated stops under
  `prefers-reduced-motion` and when it is off screen.

## Running it locally

Any static server will do:

```bash
python -m http.server 8770 --directory onpremialabs
```

Then open <http://localhost:8770>. Opening `index.html` straight from disk
works too in most browsers.

## Publishing

The directory is a self-contained static site: copy it to any web server or
static host (GitHub Pages, Cloudflare Pages, Netlify, an nginx on your own
host) and point `www.onpremialabs.com` at it. The OPAAS links point at
`github.com/luxinopanyvino/opaas-bench`; while that repository is private,
visitors without access will get a 404 there.
