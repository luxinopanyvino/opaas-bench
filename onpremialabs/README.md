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
    ├── css/site.css    design tokens (those of www.onpremialabs.com), layout, motion
    ├── js/i18n.js      English translations of every data-i18n key, and the
    │                   strings the scripts generate, in both languages
    ├── js/site.js      language switch, menu panel, hero mesh, lit values,
    │                   product filter, OPAAS terminal, Evolve and NexoDual
    └── img/            favicon and the OPAAS meerkat (copy of docs/img/mascot.svg)
```

## Design

The site follows the design of the live www.onpremialabs.com, so both read as
one brand: the deep navy background with blue and green glows, glass cards
with 24px corners, the Outfit typeface, the emerald accent, the "Panel" with
pill buttons, numbered cards, big green metrics, and partner logos in grey
until hovered. The colour tokens at the top of `site.css` are the ones that
site defines (`--bg-main`, `--bg-card`, `--accent-green`, …); keep them in
step if the live site changes.

## Principles

- **Spanish is the source text.** `index.html` holds it; `site.js` reads it from
  the page and swaps in `i18n.js` for English. A new text needs a `data-i18n`
  key (or `data-i18n-aria`) and its English entry.
- **No claim the products cannot back.** Evolve and NexoDual are marked
  *in development*; the OPAAS terminal is labelled an illustrative demo, and
  the evaluation blocks list only what OPAAS measures.
- **No cookies, no analytics.** `localStorage` keeps only the chosen language.
- **Motion is optional.** Everything animated stops under
  `prefers-reduced-motion` and when it is off screen.

### External resources

Two kinds, both deliberate:

- **Outfit, from Google Fonts**, as the live site loads it. To serve the site
  with no third-party request, download the Outfit `woff2` files (SIL Open
  Font License) into `assets/fonts/`, declare them with `@font-face` at the top
  of `site.css`, and remove the three Google Fonts `<link>`s from `index.html`.
- **Partner logos and the Media Kit** are referenced from
  `https://www.onpremialabs.com/`, where they already live. Served from that
  domain they are same-origin; if a logo cannot load, the card shows the
  partner's name instead.

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
