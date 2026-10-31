# Isnad brand kit

Isnad (إسناد) is the Arabic term for a chain of transmission: each claim is backed by the link before it. The platform's core promise is the same: every fact, inference, proposal and action carries provenance and an audit trail. The visual language is the Isnad brand kit: a deep charcoal canvas, white type and one lime accent.

## Name
- Written **Isnad** in text, **ISNAD** in the wordmark, **إسناد** in Arabic. Never "IsNad" or "isnad" in prose. Code identifiers, env vars and DB roles use `isnad` / `ISNAD_`.
- Tagline: *Every decision, traceable.*

## Logo (`logo-mark.svg`, `logo-wordmark.svg`)
An X of two crossed rounded bars with node ends: claims linked into a chain. Lime on charcoal; the wordmark is ISNAD in Sora, wide tracking, white. Clear space: the width of one node on all sides. Minimum mark size 16 px. Do not recolor, rotate, outline or add effects. (Drawn from the brand-kit sheet; the name stays Isnad.)

## Color (dark-first)
| Role | Name | Hex | Use |
|---|---|---|---|
| Accent | Lime Green | `#d0f94a` | Primary buttons, active nav, focus, links, interactive highlights |
| Canvas | Deep Charcoal | `#02090b` | App background |
| Text | White | `#ffffff` | Text and high-contrast surfaces |
| Lime scale | 200 / 300 / 400 | `#b7f53a` / `#8bdc2e` / `#5edb10` | Hover, charts |
| Surfaces | Green 900 and derived | `#0b1f1f`, `#071013`, `#0c1a1c`, `#12262a` | Panels, cards, inputs |
| Muted text | Gray 200 | `#a7b3b1` | Secondary text |
| Lines | Gray 700 / derived | `#2a3736`, `#1a2a2c` | Borders |
| Error | Red | `#ff6b61` | Errors, destructive |

Rules: text on lime is black, never white. Data semantics (ok / bad / warn / info) stay colorblind-safe (Okabe-Ito, lifted for dark backgrounds). Lime glow gradients are subtle and only behind page headers.

## Typography
Sora for headings, display, wordmark and big numbers; Inter for body, UI and labels; Tajawal for Arabic (RTL). All free, loaded from Google Fonts.

## Components
- Buttons are pills (`999px`). Primary: lime fill, black text. Secondary: dark surface with a 1 px border. Destructive text: red.
- Cards: `#071013`, 1 px border, 14 px radius, no heavy shadow.
- Nav: left rail with line icons; the active item is lime text on a faint lime wash with a 2 px lime edge.
- Agent chat: docked panel on the right, available on every page (Ctrl/Cmd+K); user bubbles lime, agent replies on a dark surface.
- Motion: subtle, 150-250 ms ease; respect `prefers-reduced-motion`.

## Voice
Plain, confident, short. Say what was done and by whom. Prefer "traceable", "evidence", "approved by", over hype.

## Files
`tokens.css` (copied to `web/src/brand.css`; `web/src/theme.css` applies it), `logo-mark.svg`, `logo-wordmark.svg`.
