import "./MothliMark.css";
import { Moth, type MothMood } from "./Moth";

/** Mothli's small mark for toolbars and headers: the live moth, without its lamp-glow. */
export function MothliMark({ className = "", mood = "idle", size = 36 }: { className?: string; mood?: MothMood; size?: number }) {
  return <Moth className={`mothli-mark ${className}`} mood={mood} size={size} glow={false} />;
}
