/** Which book files the library accepts, and what to say about the ones it doesn't. Kept tiny and
 * free of converter code so the library can answer straight away; the converters load on demand. */

export type BookFileKind = "epub" | "fb2" | "kindle" | "pdf" | "zipped" | "kfx" | "unknown";

export const BOOK_FORMATS = "EPUB, FB2, MOBI or AZW3";
export const BOOK_ACCEPT =
  ".epub,.fb2,.fb2.zip,.fbz,.zip,.mobi,.azw,.azw3,.prc,application/epub+zip,application/zip,application/x-fictionbook+xml,application/x-mobipocket-ebook,application/vnd.amazon.ebook";

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
  if (kind === "kfx") return `This Kindle format can’t be read outside Kindle. Add an ${BOOK_FORMATS} book.`;
  if (kind === "unknown") return `Mothli can’t open this kind of file. Add an ${BOOK_FORMATS} book.`;
  return null;
}

/** The book inside a zip: an FB2 first (that is what zipped books almost always are), then others. */
async function unzipBook(file: File): Promise<File> {
  const [{ listZip, readZipEntry }, { BookImportError, readBytes }] = await Promise.all([import("./zipRead"), import("./epubWriter")]);
  const bytes = await readBytes(file);
  const entries = listZip(bytes).filter((entry) => !entry.name.endsWith("/") && !/(^|\/)(__MACOSX\/|\.)/.test(entry.name));
  const rank = (name: string) => ["fb2", "epub", "kindle"].indexOf(bookFileKind({ name, type: "" }));
  const book = entries.filter((entry) => rank(entry.name) >= 0).sort((a, b) => rank(a.name) - rank(b.name))[0];
  if (!book) throw new BookImportError(`This zip doesn’t contain a book Mothli can open. Add an ${BOOK_FORMATS} book.`);
  const inner = await readZipEntry(bytes, book);
  return new File([inner as BlobPart], book.name.split("/").pop() || "book", { type: "" });
}

/** Turn a picked file into the EPUB the server expects. EPUBs pass straight through. */
export async function prepareBookUpload(file: File): Promise<File> {
  const kind = bookFileKind(file);
  if (kind === "zipped") return prepareBookUpload(await unzipBook(file));
  if (kind === "fb2") return (await import("./fb2ToEpub")).fb2ToEpub(file);
  if (kind === "kindle") return (await import("./mobiToEpub")).mobiToEpub(file);
  return file;
}
