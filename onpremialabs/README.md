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
├── index.html          the whole page, in English; complete without JavaScript
├── contact.php         the contact form's endpoint: emails the message (PHP)
├── contact.config.example.php   its settings; copy to contact.config.php on the server
└── assets/
    ├── css/site.css    design tokens (those of www.onpremialabs.com), layout, motion
    ├── js/i18n.js      Spanish translations of every data-i18n key, and the
    │                   strings the scripts generate, in both languages
    ├── js/site.js      language switch, menu panel, hero mesh, lit values,
    │                   product filter, OPAAS terminal, Evolve, NexoDual, contact form
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

- **English is the source text and the default.** `index.html` holds it;
  `site.js` reads it from the page and swaps in `i18n.js` when a visitor picks
  Spanish (the choice is remembered). A new text needs a `data-i18n` key (or
  `data-i18n-aria`) and its Spanish entry.
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
- **Partner logos** are referenced from `https://www.onpremialabs.com/`, where
  they already live. Served from that domain they are same-origin; if a logo
  cannot load, the card shows the partner's name instead.

## Contact form

"Contact onpremIA Labs" (and the "Become a collaborator or sponsor" card,
with the topic preset) opens a form in a dialog: name, email, company
(optional), topic, message and consent. `site.js` posts it as JSON to
**`contact.php`**, next to `index.html` on the same hosting, which emails it to
**info@onpremialabs.com** with PHP's `mail()`. The visitor's details never pass
through a third party. On success the dialog shows a thank-you message.

On the server (Hostinger, PHP 8.1+):

1. Upload `contact.php` next to `index.html`.
2. Optionally, copy `contact.config.example.php` to **`contact.config.php`** on
   the server to change the destination mailbox, the sending mailbox, the
   subject prefix, the rate limit or the allowed origins. Without it,
   `contact.php` uses the same values as the example (info@onpremialabs.com),
   so losing the file when `public_html` is replaced does not break the form.
   It is gitignored.
3. `envelope_from` must be a real mailbox of the domain in Hostinger's Emails
   panel, so SPF and DMARC pass.
4. Send one test message from the live site and check the inbox, and the spam
   folder the first time.

What `contact.php` does:

- **Sends From the lab's mailbox, with the visitor in Reply-To.** Pressing
  Reply answers the visitor. Sending "From" the visitor's own address fails SPF
  and DMARC and lands in spam.
- **Validates** the email, the message length and every field's size; strips
  control characters from anything that reaches a header, so a crafted name
  cannot inject headers; encodes names and subject as UTF-8.
- **Limits abuse:** at most `rate_limit_max` messages per IP in
  `rate_limit_window_sec` (the IP is stored only as a hash), refuses posts whose
  `Origin` is not in `allowed_origins`, and silently drops submissions that fill
  the hidden honeypot field.
- **Answers JSON:** `{"success": true}` or `{"success": false, "error": "..."}`
  with a 4xx/5xx status. The page shows a specific message for 429 (too many
  messages); for any other failure it offers a `mailto:` link with the subject
  and message filled in, so nothing the visitor wrote is lost.
- **Without JavaScript**, the button is a plain `mailto:info@onpremialabs.com`.

A local `python -m http.server` does not run PHP: sending from a local preview
fails by design and shows the failure message.

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
