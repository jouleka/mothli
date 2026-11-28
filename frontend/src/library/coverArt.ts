// The opening two shelves ship as small, independently cacheable Gutenberg thumbnails.
// Vite imports their URLs. Each image is requested separately when its card needs it.
const openingCovers = import.meta.glob<string>("./art/*.webp", {
  eager: true, query: "?no-inline", import: "default",
});

export function classicArt(id: string) {
  return openingCovers[`./art/${id}.webp`] ?? `/api/classics/${encodeURIComponent(id)}/cover`;
}

export function ownedArt(id: string) {
  return `/api/books/${encodeURIComponent(id)}/cover`;
}
