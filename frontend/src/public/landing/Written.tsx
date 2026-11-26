/** Mothli's words in blue pencil. First drawn whole (the page written out without JavaScript
 * draws the same); once `writing`, each word arrives after the one before, as if being written,
 * while a screen reader gets the whole sentence at once. */
export function Written({ text, writing, className = "" }: { text: string; writing: boolean; className?: string }) {
  if (!writing) return <p className={`m-hand lp-written ${className}`}>{text}</p>;
  const words = text.split(" ");
  return (
    <p className={`m-hand lp-written lp-written--writing ${className}`}>
      <span className="lp-sr">{text}</span>
      <span aria-hidden="true">
        {words.map((word, index) => (
          <span key={index} className="lp-word" style={{ animationDelay: `${index * 55}ms` }}>
            {word}
            {index < words.length - 1 ? " " : ""}
          </span>
        ))}
      </span>
    </p>
  );
}
