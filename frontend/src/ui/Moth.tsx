import "./Moth.css";

export type MothMood = "idle" | "curious" | "thinking" | "happy" | "sleepy";

/** Mothli, drawn as a bookplate stamp: a line moth whose wings read as an open book.
 * It takes the colour of the text around it (Mothli's blue pencil by default).
 * Decorative by default; pass `label` when the moth is the only thing conveying a state.
 * `glow` is accepted for older call sites; the stamp never glows. */
export function Moth({
  mood = "idle",
  size = 48,
  label,
  className = "",
}: {
  mood?: MothMood;
  size?: number;
  glow?: boolean;
  label?: string;
  className?: string;
}) {
  const a11y = label
    ? { role: "img" as const, "aria-label": label }
    : { "aria-hidden": true as const };
  return (
    <svg
      className={`moth moth--${mood} ${className}`}
      viewBox="0 0 76 60"
      width={size}
      height={Math.round((size * 60) / 76)}
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      focusable="false"
      {...a11y}
    >
      <g className="moth-wing moth-wing--left">
        <path d="M38 24C30 8 12 2 4 8c-3 12 8 26 34 26" />
        <path d="M38 34c-10 0-20 6-18 14 4 8 14 4 18-6" />
        <circle cx="18" cy="18" r="3.5" />
      </g>
      <g className="moth-wing moth-wing--right">
        <path d="M38 24C46 8 64 2 72 8c3 12-8 26-34 26" />
        <path d="M38 34c10 0 20 6 18 14-4 8-14 4-18-6" />
        <circle cx="58" cy="18" r="3.5" />
      </g>
      <path d="M38 18v32" />
      <path d="M38 18c-2-6-5-10-9-12M38 18c2-6 5-10 9-12" />
    </svg>
  );
}
