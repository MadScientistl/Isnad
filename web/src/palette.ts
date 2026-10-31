/** Deterministic per-type colours and shapes (no ontology-specific names): the same type set always gets the same look. */
export const PALETTE = ["#56B4E9", "#2fd0a0", "#E69F00", "#CC79A7", "#8fd0ff", "#ff8a5c", "#c9a227", "#9ca3af", "#b57bd9", "#4ec3d6", "#ff6b61", "#9bcf53"];
export const SHAPES = ["circle", "square", "diamond", "tri"] as const;
export type Shape = (typeof SHAPES)[number];
export const CY_SHAPES = ["ellipse", "round-rectangle", "diamond", "triangle", "hexagon", "star", "rectangle", "pentagon"];

const hash = (s: string) => { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; };

export interface TypeStyle { color: string; shape: Shape; cyShape: string }
/**
 * Assigns each name a colour: first choice is hash(name), with linear probing so names in the set stay distinct
 * (until the palette is exhausted). Names are processed in sorted order, so the result depends only on the set.
 */
export function assignStyles(names: string[]): Record<string, TypeStyle> {
  const out: Record<string, TypeStyle> = {};
  const used = new Set<number>();
  for (const n of [...names].sort()) {
    let i = hash(n) % PALETTE.length;
    if (used.size < PALETTE.length) while (used.has(i)) i = (i + 1) % PALETTE.length;
    used.add(i);
    out[n] = { color: PALETTE[i], shape: SHAPES[hash(n + "#s") % SHAPES.length], cyShape: CY_SHAPES[hash(n + "#c") % CY_SHAPES.length] };
  }
  return out;
}
export const hexToRgba = (hex: string, a = 255): [number, number, number, number] => [parseInt(hex.slice(1, 3), 16), parseInt(hex.slice(3, 5), 16), parseInt(hex.slice(5, 7), 16), a];
/** Generic "needs attention" status words (any ontology). */
export const ALERT_STATUS = new Set(["closed", "delayed", "flagged", "failed", "error", "blocked", "overdue", "rejected", "offline", "broken"]);
