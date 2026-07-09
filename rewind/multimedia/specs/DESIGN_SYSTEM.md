# DESIGN_SYSTEM.md — Rewind (locked directives)

> Product name **Rewind** · tagline "rewind the moment." These are binding UI
> directives. Every implemented screen must read from these tokens — do not
> introduce new colors, fonts, or radii. Reference implementation: turn `5a`
> in `Rewind Flow.dc.html`.

## 1. Color — one accent token
The entire UI derives its brand color from a single token. Switching the accent
must be a one-variable change.

```yaml
accent:            "#E0916B"   # Soft Clay — the ONE brand color
accent_hover:      "#D9835A"
accent_pressed:    "#C9744B"
accent_tint_bg:    "rgba(224,145,107,0.12)"   # selected media, active chip
accent_tint_border:"rgba(224,145,107,0.40)"
on_accent:         "#151517"   # text/icon ON a clay surface (near-black, not white)

neutrals:
  ink:      "#0C0D0F"   # app background
  surface:  "#141518"   # cards / phone body
  surface2: "#17191D"   # nested panels, inputs
  line:     "#26282D"   # borders / dividers
  text:     "#F4F2EE"   # primary text
  muted:    "#8A877F"   # secondary text
  faint:    "#615E57"   # labels, disabled
```
Rule: primary actions use `accent` bg + `on_accent` text (never white text on clay).
Reserve `accent` for the ONE primary action / active state per view — everything
else is neutral. No gradients.

## 2. Type — two families only
```yaml
display: "Space Grotesk"   # wordmark, headings, numerals, status bar
body:    "Outfit"          # everything else: UI, labels, paragraphs
mono:    "JetBrains Mono"  # ONLY in the developer/transparency view & code
```
Do not add a third UI font. Headings 700/800, body 400–700. Uppercase labels use
Outfit 700, letter-spacing .1em.

## 3. Iconography — Lucide only, no emoji
- Use **Lucide** line icons everywhere. **No emoji in product UI** (emoji are
  allowed only in contributor-authored content, e.g. a note they typed).
- Logo mark = Lucide `rewind` (filled with `accent`), locked up left of the
  "Rewind" wordmark in Space Grotesk.
- Canonical icon map:
  `image` photo · `video` clip · `mic` voice · `pen-line` note · `file-text` doc ·
  `lock` no-account · `plus` add · `signal`/`wifi`/`battery-full` status bar ·
  `chevron-right` phase nav.

## 4. Shape & spacing — less rounded
```yaml
radius:
  card: 12
  tile: 10
  pill_input: 8
  phone_frame: 26
spacing_base: 4            # 4/8/12/16/20/24
hit_target_min: 44         # px — every tappable control
```
Corners are crisp, not pill-round. Borders are 1px `line`; elevation via subtle
shadow only on floating surfaces, not inline cards.

## 5. Component directives
- **Primary button:** `accent` bg, `on_accent` text, radius 8–12, weight 700, min-height 44.
- **Secondary button:** `surface2` bg, `line` border, `text`.
- **Input/select:** `surface2` bg, `line` border, focus ring `accent_tint_border`.
- **Chip / tab:** neutral by default; active = `accent_tint_bg` bg + `accent` text + `accent_tint_border`.
- **Media-type badge:** icon in `accent`; keep a consistent icon per type (§3).

## 6. Accessibility
- Body text ≥ 12.5px mobile; never below 24px on any 1920×1080 slide/marketing frame.
- Maintain ≥ 4.5:1 contrast for text; `on_accent` (#151517) on Soft Clay passes.
- All interactive targets ≥ 44px; visible focus state required.

## 7. How implementers apply this
Expose the tokens once (CSS custom properties / a single theme object) and read
everywhere. Brand color changes = edit `--accent` (+ its 4 derived values) only.
No component may hardcode `#E0916B`.
