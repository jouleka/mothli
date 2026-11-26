/** The front page's book for showing what Mothli does: Pride and Prejudice (public domain), at
 * four places a reader might have read up to, with what Mothli would say at each. Every word is
 * written out: the welcome holds no figure (SignIn.test.tsx), and Mothli never says more than the
 * pages before the bookmark hold. */

export type StopId = "one" | "three" | "sixteen" | "thirtysix";
export type AskId = "darcy" | "wickham" | "ending";

export type Stop = {
  id: StopId;
  /** The chapter as the book writes it. */
  numeral: string;
  /** The chapter as a person says it. */
  spoken: string;
  passage: string;
  said: string;
};

export const STOPS: Stop[] = [
  {
    id: "one",
    numeral: "I",
    spoken: "chapter one",
    passage: "It is a truth universally acknowledged, that a single man in possession of a good fortune must be in want of a wife.",
    said: "The opening line",
  },
  {
    id: "three",
    numeral: "III",
    spoken: "chapter three",
    passage: "She is tolerable: but not handsome enough to tempt me.",
    said: "Mr. Darcy, at the Meryton ball",
  },
  {
    id: "sixteen",
    numeral: "XVI",
    spoken: "chapter sixteen",
    passage: "His behaviour to myself has been scandalous.",
    said: "Mr. Wickham, about Mr. Darcy",
  },
  {
    id: "thirtysix",
    numeral: "XXXVI",
    spoken: "chapter thirty-six",
    passage: "Till this moment, I never knew myself.",
    said: "Elizabeth, after reading a letter",
  },
];

export const ASKS: { id: AskId; text: string }[] = [
  { id: "darcy", text: "Who’s Mr. Darcy?" },
  { id: "wickham", text: "Can I trust Wickham?" },
  { id: "ending", text: "How does it end?" },
];

export const ANSWERS: Record<StopId, Record<AskId, string>> = {
  one: {
    darcy: "Nobody yet! So far it’s Mrs. Bennet buzzing about a rich young man called Bingley taking Netherfield. If a Darcy turns up, we’ll meet him together.",
    wickham: "Who? There’s no Wickham on any page you’ve read, so I haven’t met him either. Ask me again when he walks in.",
    ending: "Nice try! You’re on chapter one. I only know the book as far as you’ve read it, and right now that’s Mrs. Bennet with some very big news about a neighbour.",
  },
  three: {
    darcy: "Bingley’s friend from the ball. Ten thousand a year, and he just called Lizzy “tolerable” where she could hear him. The nerve! The whole room has decided he’s proud. Have you?",
    wickham: "Still no Wickham on any page you’ve read. I’m as in the dark as you are, which is exactly how I like it.",
    ending: "Ha! Three chapters in and already peeking at the last page? I couldn’t tell you if I wanted to. I’m exactly where you are.",
  },
  sixteen: {
    darcy: "The proud one from the ball. And now Wickham says Darcy cheated him out of the living his father promised him. Lizzy believes every word. Do you?",
    wickham: "Charming, isn’t he? If his story about Darcy is true, it’s awful. I only know what you’ve read, so you’re the judge. Though my, he tells a near stranger a lot, doesn’t he?",
    ending: "Not a chance. I genuinely don’t know yet. Turn the page and we’ll find out together.",
  },
  thirtysix: {
    darcy: "Well. He proposed, she turned him down (magnificently), and then his letter told Wickham’s story very differently. Lizzy is rereading everything she thought she knew. Me too.",
    wickham: "Darcy’s letter says Wickham tried to run off with Georgiana, Darcy’s fifteen-year-old sister, for her fortune. Lizzy’s face, honestly. “Till this moment, I never knew myself.” Oof.",
    ending: "Still no! I’m right here on chapter thirty-six with you, and I’m every bit as curious. Keep going.",
  },
};

/** Who's who, as far as the bookmark: each person from the chapter they're met in, and what
 * Mothli would say of them by then. */
export const CAST: { name: string; from: StopId; lines: Partial<Record<StopId, string>> }[] = [
  { name: "Elizabeth Bennet", from: "one", lines: { one: "Lizzy. Her father’s favourite, the quickest in the house." } },
  { name: "Mrs. Bennet", from: "one", lines: { one: "Five daughters to marry off, and nerves to match." } },
  { name: "Mr. Bennet", from: "one", lines: { one: "Dry as toast. Teases his wife for sport." } },
  { name: "Mr. Bingley", from: "one", lines: { one: "Rich, single, and just took Netherfield." } },
  {
    name: "Mr. Darcy",
    from: "three",
    lines: {
      three: "Bingley’s friend. Proud, says everyone.",
      sixteen: "Proud, and maybe cruel, says Wickham.",
      thirtysix: "Wrote a letter that changed everything.",
    },
  },
  { name: "Mr. Collins", from: "sixteen", lines: { sixteen: "A cousin. Will tell you all about Lady Catherine." } },
  {
    name: "Mr. Wickham",
    from: "sixteen",
    lines: {
      sixteen: "An officer, all charm, with a story about Darcy.",
      thirtysix: "His story, told very differently.",
    },
  },
];

const ORDER: StopId[] = STOPS.map((stop) => stop.id);

/** What Mothli says of someone at this bookmark, or null before they're met. */
export function castLine(entry: (typeof CAST)[number], at: StopId): string | null {
  const here = ORDER.indexOf(at);
  if (ORDER.indexOf(entry.from) > here) return null;
  for (let index = here; index >= 0; index -= 1) {
    const line = entry.lines[ORDER[index]];
    if (line) return line;
  }
  return null;
}

/** Chapter one's opening, with three lines a reader might mark, and what Mothli says of each. */
export const OPENING: ({ text: string } | { mark: string; note: string })[] = [
  { text: "It is " },
  {
    mark: "a truth universally acknowledged",
    note: "Universally! Austen is grinning here. “Everyone knows” really means “every mother with a daughter knows.”",
  },
  { text: ", that a single man in possession of a good fortune must be in want of a wife. However little known the feelings or views of such a man may be on his first entering a neighbourhood, this truth is so well fixed in the minds of the surrounding families, that he is considered as " },
  {
    mark: "the rightful property of some one or other of their daughters",
    note: "Property! The poor man hasn’t even unpacked and the whole county has divided him up.",
  },
  { text: ". “Do not you want to know who has taken it?” cried his wife, impatiently. “You want to tell me, and " },
  {
    mark: "I have no objection to hearing it",
    note: "Mr. Bennet, king of the dry reply. He knows exactly what he’s doing to her, and he’s loving it.",
  },
  { text: ".”" },
];
