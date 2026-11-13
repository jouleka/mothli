/** Read one file out of a ZIP, for books that arrive zipped (most FB2 downloads do). Only what
 * that needs: the central directory, stored and deflated entries, and hard limits so a hostile
 * archive cannot expand without bound. Nothing is written anywhere; the bytes go straight to the
 * same converters an unzipped file would. */
import { BookImportError } from "./epubWriter";

export type ZipListing = { name: string; method: number; compressedSize: number; size: number; offset: number; encrypted: boolean };

const MAX_ENTRIES = 2000;
/** The largest single file this will inflate. Books are far smaller; a zip bomb is not. */
export const MAX_UNZIPPED_BYTES = 100 * 1024 * 1024;

export function listZip(bytes: Uint8Array): ZipListing[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  // The end-of-central-directory record sits in the last 64 KB (it may be followed by a comment).
  let end = -1;
  for (let at = bytes.length - 22; at >= Math.max(0, bytes.length - 22 - 0xffff); at -= 1) {
    if (view.getUint32(at, true) === 0x06054b50) {
      end = at;
      break;
    }
  }
  if (end < 0) throw new BookImportError("This zip file is damaged and can’t be opened.");
  const count = view.getUint16(end + 10, true);
  let at = view.getUint32(end + 16, true);
  if (count > MAX_ENTRIES || at === 0xffffffff) throw new BookImportError("This zip file is too large to open here. Unzip it first.");
  const decoder = new TextDecoder();
  const entries: ZipListing[] = [];
  for (let index = 0; index < count; index += 1) {
    if (at + 46 > bytes.length || view.getUint32(at, true) !== 0x02014b50)
      throw new BookImportError("This zip file is damaged and can’t be opened.");
    const nameLength = view.getUint16(at + 28, true);
    const extraLength = view.getUint16(at + 30, true);
    const commentLength = view.getUint16(at + 32, true);
    entries.push({
      name: decoder.decode(bytes.subarray(at + 46, at + 46 + nameLength)),
      method: view.getUint16(at + 10, true),
      encrypted: (view.getUint16(at + 8, true) & 1) === 1,
      compressedSize: view.getUint32(at + 20, true),
      size: view.getUint32(at + 24, true),
      offset: view.getUint32(at + 42, true),
    });
    at += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

async function inflateRaw(data: Uint8Array, limit: number): Promise<Uint8Array> {
  if (typeof DecompressionStream !== "function")
    throw new BookImportError("This browser can’t open zipped books. Unzip the file first.");
  // Written through the stream's own writer rather than Blob.stream(), which older webviews lack.
  const inflater = new DecompressionStream("deflate-raw");
  const writer = inflater.writable.getWriter();
  const fed = writer.write(data as BufferSource).then(() => writer.close());
  fed.catch(() => {}); // a bad archive surfaces through the reader below
  const reader = inflater.readable.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.length;
    if (total > limit) {
      await reader.cancel();
      throw new BookImportError("The book inside this zip is too large to open here. Unzip it first.");
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const chunk of chunks) {
    out.set(chunk, at);
    at += chunk.length;
  }
  return out;
}

export async function readZipEntry(bytes: Uint8Array, entry: ZipListing, limit = MAX_UNZIPPED_BYTES): Promise<Uint8Array> {
  if (entry.encrypted) throw new BookImportError("This zip file is password-protected. Unzip it first.");
  if (entry.size > limit || entry.size === 0xffffffff)
    throw new BookImportError("The book inside this zip is too large to open here. Unzip it first.");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (entry.offset + 30 > bytes.length || view.getUint32(entry.offset, true) !== 0x04034b50)
    throw new BookImportError("This zip file is damaged and can’t be opened.");
  const start = entry.offset + 30 + view.getUint16(entry.offset + 26, true) + view.getUint16(entry.offset + 28, true);
  const data = bytes.subarray(start, start + entry.compressedSize);
  if (data.length !== entry.compressedSize) throw new BookImportError("This zip file is damaged and can’t be opened.");
  if (entry.method === 0) return data.slice();
  if (entry.method === 8) {
    try {
      return await inflateRaw(data, limit);
    } catch (error) {
      if (error instanceof BookImportError) throw error;
      throw new BookImportError("This zip file is damaged and can’t be opened.");
    }
  }
  throw new BookImportError("This zip file uses a kind of compression Mothli can’t open. Unzip it first.");
}
