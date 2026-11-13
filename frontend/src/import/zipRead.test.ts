import { deflateRawSync } from "node:zlib";
import { describe, expect, test } from "vitest";
import { prepareBookUpload } from "./bookFiles";
import { listZip, readZipEntry } from "./zipRead";
import { crc32, zipStore } from "./zipStore";

const enc = new TextEncoder();
const fb2 =
  "<FictionBook><description><title-info><book-title>Zipped</book-title></title-info></description><body><section><title><p>One</p></title><p>" +
  "Tomas walked along the harbour. ".repeat(40) +
  "</p></section></body></FictionBook>";

/** A zip whose entries are really deflated, as downloaded book archives are. */
function deflatedZip(files: { name: string; data: Uint8Array; flags?: number }[]) {
  const parts: Uint8Array[] = [];
  const central: Uint8Array[] = [];
  let offset = 0;
  for (const file of files) {
    const name = enc.encode(file.name);
    const packed = new Uint8Array(deflateRawSync(file.data));
    const local = new Uint8Array(30 + name.length);
    const lv = new DataView(local.buffer);
    lv.setUint32(0, 0x04034b50, true);
    lv.setUint16(6, file.flags ?? 0, true);
    lv.setUint16(8, 8, true);
    lv.setUint32(14, crc32(file.data), true);
    lv.setUint32(18, packed.length, true);
    lv.setUint32(22, file.data.length, true);
    lv.setUint16(26, name.length, true);
    local.set(name, 30);
    const dir = new Uint8Array(46 + name.length);
    const dv = new DataView(dir.buffer);
    dv.setUint32(0, 0x02014b50, true);
    dv.setUint16(8, file.flags ?? 0, true);
    dv.setUint16(10, 8, true);
    dv.setUint32(16, crc32(file.data), true);
    dv.setUint32(20, packed.length, true);
    dv.setUint32(24, file.data.length, true);
    dv.setUint16(28, name.length, true);
    dv.setUint32(42, offset, true);
    dir.set(name, 46);
    parts.push(local, packed);
    central.push(dir);
    offset += local.length + packed.length;
  }
  const centralSize = central.reduce((sum, part) => sum + part.length, 0);
  const end = new Uint8Array(22);
  const ev = new DataView(end.buffer);
  ev.setUint32(0, 0x06054b50, true);
  ev.setUint16(8, files.length, true);
  ev.setUint16(10, files.length, true);
  ev.setUint32(12, centralSize, true);
  ev.setUint32(16, offset, true);
  const all = [...parts, ...central, end];
  const out = new Uint8Array(all.reduce((sum, part) => sum + part.length, 0));
  let at = 0;
  for (const part of all) {
    out.set(part, at);
    at += part.length;
  }
  return out;
}

describe("reading a zip", () => {
  test("lists and reads stored entries", async () => {
    const zip = zipStore([{ name: "a.txt", data: enc.encode("hello") }, { name: "dir/b.fb2", data: enc.encode(fb2) }]);
    const entries = listZip(zip);
    expect(entries.map((entry) => entry.name)).toEqual(["a.txt", "dir/b.fb2"]);
    expect(new TextDecoder().decode(await readZipEntry(zip, entries[0]))).toBe("hello");
  });

  test("inflates compressed entries", async () => {
    const zip = deflatedZip([{ name: "book.fb2", data: enc.encode(fb2) }]);
    const [entry] = listZip(zip);
    expect(entry.method).toBe(8);
    expect(entry.compressedSize).toBeLessThan(entry.size);
    expect(new TextDecoder().decode(await readZipEntry(zip, entry))).toBe(fb2);
  });

  test("refuses to expand past the size limit", async () => {
    const zip = deflatedZip([{ name: "bomb.fb2", data: new Uint8Array(200_000) }]);
    const [entry] = listZip(zip);
    await expect(readZipEntry(zip, entry, 50_000)).rejects.toThrow(/too large/);
    // a lying size field does not get past the limit either
    await expect(readZipEntry(zip, { ...entry, size: 10 }, 50_000)).rejects.toThrow(/too large/);
  });

  test("explains password-protected and damaged archives", async () => {
    const locked = deflatedZip([{ name: "book.fb2", data: enc.encode(fb2), flags: 1 }]);
    await expect(readZipEntry(locked, listZip(locked)[0])).rejects.toThrow(/password-protected/);
    expect(() => listZip(enc.encode("this is not a zip file at all, just some text"))).toThrow(/damaged/);
    const zip = deflatedZip([{ name: "book.fb2", data: enc.encode(fb2) }]);
    const [entry] = listZip(zip);
    await expect(readZipEntry(zip, { ...entry, method: 99 })).rejects.toThrow(/kind of compression/);
  });
});

describe("zipped books", () => {
  test("a zipped FB2 is unzipped and converted like any other", async () => {
    const zip = deflatedZip([
      { name: "__MACOSX/._book.fb2", data: enc.encode("junk") },
      { name: "readme.txt", data: enc.encode("hi") },
      { name: "folder/book.fb2", data: enc.encode(fb2) },
    ]);
    const upload = await prepareBookUpload(new File([zip as BlobPart], "book.fb2.zip"));
    expect(upload.name).toBe("book.epub");
    expect(upload.type).toBe("application/epub+zip");
  });

  test("an EPUB inside a zip is passed through untouched", async () => {
    const epubBytes = enc.encode("PK pretend epub bytes");
    const zip = zipStore([{ name: "novel.epub", data: epubBytes }]);
    const upload = await prepareBookUpload(new File([zip as BlobPart], "novel.zip"));
    expect(upload.name).toBe("novel.epub");
    expect(upload.size).toBe(epubBytes.length);
  });

  test("a zip with no book in it is explained", async () => {
    const zip = zipStore([{ name: "photo.jpg", data: new Uint8Array(4) }]);
    await expect(prepareBookUpload(new File([zip as BlobPart], "stuff.zip"))).rejects.toThrow(/doesn’t contain a book/);
  });
});
