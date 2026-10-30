# Isnad brand kit

Isnad (إسناد) is the Arabic term for a chain of transmission: each claim is backed by the link before it. The platform's core promise is the same: every fact, inference, proposal and action carries provenance and an audit trail. The visual language follows HUMAIN's (humain.com): a white, spacious canvas, black type, one fresh green family, and a lime accent for calls to action.

Source: colors, type scale and font stack were read from humain.com's live CSS variables (2026-10-30). HUMAIN publishes no downloadable guidelines, so anything not on the site (logo, usage rules) is our own and should be reviewed before public use. Do not reuse HUMAIN's logo or name.

## Name
- Written **Isnad** in text, **ISNAD** in the wordmark, **إسناد** in Arabic. Never "IsNad" or "isnad" in prose. Code identifiers, env vars and DB roles use `isnad` / `ISNAD_`.
- Tagline: *Every decision, traceable.*

## Logo (`logo-mark.svg`, `logo-wordmark.svg`)
Two interlocking pill links, a chain, in the primary gradient. Wordmark is uppercase Optiker next to the mark. Clear space: height of one link on all sides. Minimum mark size 16 px. On dark backgrounds keep the gradient and set the wordmark white. Do not recolor the links, rotate, outline or add effects.

## Color
| Role | Name | Hex | Use |
|---|---|---|---|
| Primary | Air | `#00d49c` | Primary buttons, active states, success |
| Primary hover | Teal | `#00ad92` | Hover, pressed |
| Secondary | Aqua | `#00879f` | Links, secondary actions, charts |
| Secondary deep | Deep | `#006687` | Text-safe accent on white (contrast 6.1:1) |
| Tertiary | Oasis | `#d0f94a` | CTA pills, highlights, gradient top |
| Tint | Oasis soft | `#edffb1` | Highlight backgrounds |
| Info tint | Glacier | `#c5ecff` | Info backgrounds |
| Error | Red | `#b3261e` | Errors, destructive |
| Ink | Black | `#000000` | Text, nav |
| Surface | White / Grey 100 | `#fff` / `#f5f5f5` | Canvas / panels |
| Lines | Grey 200-300 | `#eee` / `#e0e0e0` | Borders |
| Muted text | Grey 700 | `#666666` | Secondary text |

Rules: black text on white or Oasis; never white text on Air or Oasis (fails contrast), use black. Air/Aqua are for fills and large UI, use Deep for small colored text. Gradients are vertical only: Oasis → Air → Aqua. Semantic colors for data (ok / bad / warn / info) stay colorblind-safe (Okabe-Ito) and are unchanged from the app.

## Typography
| Role | Face | Style |
|---|---|---|
| Display, H1, H3, small labels | **Optiker** | 400, uppercase, tracking -0.02em, tight line height (1.0) |
| H2, H4-H6, body, UI | **ABC Repro** | 400 body, 500 headings, tracking -0.02em on headings, line height 1.2 |
| Arabic | **Tajawal** (400, 500; free, Google Fonts) | RTL, same sizes |
Optiker and ABC Repro are commercial fonts: the app falls back to Inter, then the system font until licensed files are added under `web/public/fonts/`. Scale (fluid): display 64-125 px, H1 32-60, H3 24-40, H4 24-36, H5 20-28, H6 16-20, body 14-16.

## Components
- Buttons are pills (`999px`). Primary CTA: Oasis fill, black text. Standard primary: Air fill, black text. Secondary: white with 1 px grey border. Destructive text: Red.
- Cards: white, 1 px `#e0e0e0` border, 12 px radius, no heavy shadow.
- Nav: black bar, white text, active item a white pill with black text.
- Motion: subtle, 150-250 ms ease; respect `prefers-reduced-motion`.

## Voice
Plain, confident, short. Say what was done and by whom. Prefer "traceable", "evidence", "approved by", over hype.

## Files
`tokens.css` (CSS variables, imported by `web/src/style.css`), `logo-mark.svg`, `logo-wordmark.svg`.
