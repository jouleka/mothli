/** Which book files the library accepts, and what to say about the ones it doesn't. Kept tiny and
 * free of converter code so the library can answer straight away; the converters load on demand. */

export type BookFileKind = "epub" | "fb2" | "kindle" | "pdf" | "zipped" | "kfx" | "unknown";

export const BOOK_FORMATS = "EPUB, FB2, MOBI or AZW3";
export const BOOK_ACCEPT =
  ".epub,.fb2,.mobi,.azw,.azw3,.prc,application/epub+zip,application/x-fictionbook+xml,application/x-mobipocket-ebook,application/vnd.amazon.ebook";

export function bookFileKind(file: { name: string; type: string }): BookFileKind {
  const name = file.name.toLowerCase();
  if (name.endsWith(".epub") || file.type === "application/epub+zip") return "epub";
  if (name.endsWith(".fb2") || file.type === "application/x-fictionbook+xml") return "fb2";
  if (/\.(mobi|azw3?|prc)$/.test(name) || file.type === "application/x-mobipocket-ebook" || file.type === "application/vnd.amazon.ebook") return "kindle";
  if (name.endsWith(".pdf") || file.type === "application/pdf") return "pdf";
  if (/\.(fb2\.zip|fbz|zip)$/.test(name)) return "zipped";
  if (/\.(kfx|azw8|kpf)$/.test(name)) return "kfx";
  return "unknown";
}

/** A plain explanation for a file the library can't take. Null when the file is fine to try. */
export function unsupportedBookMessage(kind: BookFileKind): string | null {
  if (kind === "pdf") return `PDFs aren’t supported yet. Add an ${BOOK_FORMATS} book.`;
  if (kind === "zipped") return "Unzip this file first, then add the book inside it.";
  if (kind === "kfx") return `This Kindle format can’t be read outside Kindle. Add an ${BOOK_FORMATS} book.`;
  if (kind === "unknown") return `Mothli can’t open this kind of file. Add an ${BOOK_FORMATS} book.`;
  return null;
}

/** Turn a picked file into the EPUB the server expects. EPUBs pass straight through. */
export async function prepareBookUpload(file: File): Promise<File> {
  const kind = bookFileKind(file);
  if (kind === "fb2") return (await import("./fb2ToEpub")).fb2ToEpub(file);
  if (kind === "kindle") return (await import("./mobiToEpub")).mobiToEpub(file);
  return file;
}
