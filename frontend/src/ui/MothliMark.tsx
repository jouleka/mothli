import "./MothliMark.css";

/** One original book-moth mark shared by the app, toolbar, and conversation. */
export function MothliMark({ className = "" }: { className?: string }) {
  return <img className={`mothli-mark ${className}`} src="/mothli-icon.svg" alt="" aria-hidden="true" width={36} height={36} draggable={false} />;
}
