import { type FormEvent, useEffect, useRef, useState } from "react";
import type {
  HighlightMark,
  ReaderMark,
  ReaderMarkAnchor,
  SelectionAction,
} from "../api";
import { Icon } from "../ui/Icon";
import { Button } from "../ui/Button";
import { useReaderPopover } from "../ui/useReaderPopover";
import type { SavedCompanionReply } from "./companionNotes";
import "./notebook.css";

export type ReaderSelection = { anchor: ReaderMarkAnchor; text: string };

export function ReaderMarks({
  marks,
  selection,
  currentAnchor,
  exportUrl,
  status,
  onHighlight,
  onAssist,
  onAnnotate,
  onBookmark,
  onNavigate,
  onDelete,
  onClearSelection,
  onTalkAboutSelection,
  savedReplies = [],
  onNavigateReply,
  onDeleteReply,
  openRequest = 0,
  onSourceJump,
}: {
  marks: ReaderMark[];
  selection: ReaderSelection | null;
  currentAnchor: ReaderMarkAnchor | null;
  exportUrl: string;
  status: string;
  onHighlight: (color: HighlightMark["color"]) => void;
  onAssist: (action: SelectionAction) => void;
  onAnnotate: (body: string) => void;
  onBookmark: (label: string) => void;
  onNavigate: (mark: ReaderMark) => void;
  onDelete: (mark: ReaderMark) => void;
  onClearSelection: () => void;
  onTalkAboutSelection?: () => void;
  savedReplies?: SavedCompanionReply[];
  onNavigateReply?: (reply: SavedCompanionReply) => void;
  onDeleteReply?: (id: string) => void;
  openRequest?: number;
  onSourceJump?: () => void;
}) {
  const { open, closing, id, triggerRef, panelRef, toggle, close } =
    useReaderPopover();
  const [note, setNote] = useState("");
  const [label, setLabel] = useState("");
  const [selectionTool, setSelectionTool] = useState<"highlight" | "note" | "understand" | null>(null);
  const [filter, setFilter] = useState<"all" | "pip" | ReaderMark["kind"]>("all");
  const [query, setQuery] = useState("");
  const noteRef = useRef<HTMLTextAreaElement>(null);
  const busy = /^(saving|deleting)\b/.test(status);
  const visibleMarks = marks.filter((mark) =>
    (filter === "all" || mark.kind === filter) &&
    `${mark.selected_text ?? ""} ${mark.body ?? ""} ${mark.label ?? ""}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  ).sort((a, b) => a.anchor.atom - b.anchor.atom);

  const visibleReplies = savedReplies.filter(reply => `${reply.question} ${reply.body}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));

  useEffect(() => {
    if (openRequest > 0 && !open) toggle();
    // Only an explicit source-return request reopens the retained notebook view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest]);

  // A failed save keeps its passage and draft. Only a new passage or a cleared
  // selection starts a new draft; opening another tool does not discard it.
  useEffect(() => {
    setSelectionTool(null);
    setNote("");
  }, [selection?.anchor.cfi, selection?.text]);
  useEffect(() => {
    if (selectionTool === "note") noteRef.current?.focus();
  }, [selectionTool]);
  useEffect(() => {
    if (status === "bookmark saved" || status === "bookmark queued for sync")
      setLabel("");
  }, [status]);
  const saveNote = (event: FormEvent) => {
    event.preventDefault();
    if (!note.trim()) return;
    onAnnotate(note.trim());
  };
  const saveBookmark = (event: FormEvent) => {
    event.preventDefault();
    if (!currentAnchor) return;
    onBookmark(label.trim());
  };

  return (
    <div className="reader-marks">
      <button
        ref={triggerRef}
        type="button"
        className="plain reader-tool marks-trigger"
        aria-label="Notebook"
        aria-expanded={open && !closing}
        aria-controls={id}
        onClick={toggle}
      >
        <Icon name="bookmark" />
        <span>Notebook</span>
      </button>
      {selection && (
        <section className="selection-actions" aria-label="Selection actions">
          <div className="selection-context">
            <q>{selection.text}</q>
            <Button
              variant="ghost"
              className="selection-dismiss icon-button"
              aria-label="Dismiss selected passage"
              disabled={busy}
              onClick={onClearSelection}
            ><Icon name="close" size={16} /></Button>
          </div>
          <div className="selection-tool-picker" role="group" aria-label="Work with selected text">
            <Button
              variant="ghost"
              aria-expanded={selectionTool === "highlight"}
              aria-controls={`${id}-highlight`}
              disabled={busy}
              onClick={() => setSelectionTool(selectionTool === "highlight" ? null : "highlight")}
            ><span className="selection-highlight-icon" aria-hidden="true">H</span>Highlight</Button>
            <Button
              variant="ghost"
              icon="bookmark"
              aria-expanded={selectionTool === "note"}
              aria-controls={`${id}-note-editor`}
              disabled={busy}
              onClick={() => setSelectionTool(selectionTool === "note" ? null : "note")}
            >Note</Button>
            <Button variant="ghost" icon="sparkle" disabled={busy}
              onClick={() => onTalkAboutSelection ? onTalkAboutSelection() : onAssist("explain")}
            >Talk to Pip</Button>
            <Button variant="ghost" className="icon-button" aria-label="More passage tools"
              aria-expanded={selectionTool === "understand"} aria-controls={`${id}-understand`}
              disabled={busy} onClick={() => setSelectionTool(selectionTool === "understand" ? null : "understand")}
            ><Icon name="more" /></Button>
          </div>
          {selectionTool === "understand" && <div id={`${id}-understand`} className="selection-ai-actions" aria-label="Passage tools">
            <Button disabled={busy} onClick={() => onAssist("define")}>Define</Button>
            <Button disabled={busy} onClick={() => onAssist("translate")}>Translate</Button>
          </div>}
          {selectionTool === "highlight" && <div
            id={`${id}-highlight`}
            className="selection-highlight-actions"
            aria-label="Highlight color"
          >

            {(["yellow", "green", "blue", "pink"] as const).map((color) => (
              <button
                key={color}
                type="button"
                disabled={busy}
                onClick={() => onHighlight(color)}
              >
                <span
                  className={`highlight-swatch ${color}`}
                  aria-hidden="true"
                />
                <span className="sr-only">{color} highlight</span>
              </button>
            ))}
          </div>}
          {selectionTool === "note" && <form id={`${id}-note-editor`} className="selection-note-editor" onSubmit={saveNote}>
            <label htmlFor={`${id}-note`}>Add a note</label>
            <textarea
              ref={noteRef}
              id={`${id}-note`}
              value={note}
              maxLength={10000}
              rows={3}
              placeholder="A thought to keep…"
              onChange={(event) => setNote(event.currentTarget.value)}
            />
            <Button
              type="submit"
              variant="primary"
              disabled={!note.trim() || busy}
            >
              save note
            </Button>
          </form>}
          {!open && (
            <p className="marks-status" role="status" aria-live="polite">
              {status}
            </p>
          )}
        </section>
      )}
      {open && (
        <section
          id={id}
          className="reader-popover marks-panel notebook-simple"
          data-closing={closing || undefined}
          aria-hidden={closing || undefined}
          aria-label="Highlights, notes, and bookmarks"
          tabIndex={-1}
          ref={panelRef}
        >
          <header className="reader-panel-heading">
            <div className="notebook-title"><Icon name="book" /><h2>Notebook</h2></div>
            <Button
              variant="ghost"
              className="icon-button"
              aria-label="Close notebook"
              onClick={() => close()}
            >
              <Icon name="close" />
            </Button>
          </header>
          <label className="notebook-search focus-field">
            <Icon name="search" size={17} />
            <input type="search" aria-label="Search your notebook" placeholder="Find something…" value={query} onChange={(event) => setQuery(event.target.value)} />
          </label>
          <div className="notebook-filters" role="group" aria-label="Filter notebook">
            {([
              ["all", "All"], ["highlight", "Highlights"],
              ["annotation", "Notes"], ["bookmark", "Bookmarks"], ["pip", "Pip"],
            ] as const).map(([value, title]) => (
              <button
                key={value}
                type="button"
                aria-pressed={filter === value}
                onClick={() => setFilter(value)}
              >{title}</button>
            ))}
          </div>
          <form className="bookmark-form" onSubmit={saveBookmark}>
            <label className="sr-only" htmlFor={`${id}-label`}>Bookmark label</label>
            <div>
              <input
                id={`${id}-label`}
                value={label}
                maxLength={500}
                placeholder="Bookmark label (optional)"
                onChange={(event) => setLabel(event.currentTarget.value)}
              />
              <Button
                type="submit"
                variant="primary"
                disabled={!currentAnchor || busy}
              >
                <Icon name="bookmark" size={16} /> Keep page
              </Button>
            </div>
          </form>
          <p className="marks-status" role="status" aria-live="polite">
            {status}
          </p>
          {filter !== "pip" && <section className="notebook-section" aria-label="Your notes and passages">
            {filter === "all" && (visibleMarks.length > 0 || visibleReplies.length > 0) && <h3>Your pages</h3>}
            {visibleMarks.length ? <ol className="marks-list">
              {visibleMarks.map(mark => <li key={`${mark.kind}-${mark.id}`} data-mark-kind={mark.kind} data-mark-color={mark.kind === "highlight" ? mark.color : undefined}>
                <button type="button" className="plain mark-location" onClick={() => { onSourceJump?.(); onNavigate(mark); close(); }}>
                  <span className="notebook-mark-meta"><Icon name={mark.kind === "bookmark" ? "bookmark" : mark.kind === "annotation" ? "list" : "book"} size={14} />
                    <span>{mark.kind === "annotation" ? "Note" : mark.kind === "highlight" ? "Highlight" : "Bookmark"} · {mark.anchor.atom}</span>
                    {mark.pending && <span title="Waiting to sync"><Icon name="cloud" size={14} /><span className="sr-only">Queued for sync</span></span>}
                  </span>
                  <span>{mark.kind === "highlight" ? mark.selected_text : mark.kind === "annotation" ? mark.body : mark.label || "Bookmarked page"}</span>
                </button>
                <Button variant="ghost" className="mark-delete icon-button" aria-label={`Delete ${mark.kind}`} disabled={busy} onClick={() => onDelete(mark)}><Icon name="close" size={15} /></Button>
              </li>)}
            </ol> : (filter !== "all" || !visibleReplies.length) && <div className="notebook-empty"><Icon name="book" size={30} /><p>{query.trim() ? "Nothing found." : "A little space for your favorite pages."}</p></div>}
          </section>}
          {(filter === "all" || filter === "pip") && <section className="notebook-section notebook-pip-keeps" aria-label="Saved from Pip">
            {(visibleReplies.length > 0 || filter === "pip") && <h3><Icon name="sparkle" size={15} /> From Pip</h3>}
            {visibleReplies.length ? <ol className="marks-list">
              {visibleReplies.map(reply => <li key={reply.id}>
                <div className="notebook-saved-reply"><span className="notebook-reply-question">{reply.question}</span><p>{reply.body}</p>
                  {onNavigateReply && reply.citations.length > 0 && <Button variant="ghost" icon="book" onClick={() => { onSourceJump?.(); onNavigateReply(reply); close(); }}>Passage</Button>}
                </div>
                {onDeleteReply && <Button variant="ghost" className="mark-delete icon-button" aria-label="Delete saved reply" onClick={() => onDeleteReply(reply.id)}><Icon name="close" size={15} /></Button>}
              </li>)}
            </ol> : filter === "pip" && <div className="notebook-empty"><Icon name="sparkle" size={30} /><p>{query.trim() ? "Nothing found." : "Keep a favorite reply here."}</p></div>}
          </section>}
          <a
            className="plain marks-export"
            href={exportUrl}
            download="litlet-reader-marks.json"
          >
            <Icon name="download" size={16} /> Export pages
          </a>
        </section>
      )}
      {!open && !selection && status && (
        <p className="reader-feedback" role="status" aria-live="polite">
          {status}
        </p>
      )}
    </div>
  );
}
