/** The words of the public pages (ADR 0045 step 8): the terms, the privacy notice, the refunds
 * page and the contact page. Plain, short sentences, true to what the code does; ADR 0045's
 * "Step 8 built" lists every statement that is a legal choice for the owner or a lawyer, and every
 * one that could not be checked against the code. The operator's details come from the server's
 * settings, never from here. */
import type { ReactNode } from "react";
import { PAGE_PATHS } from "./pages";
import type { Operator } from "./site";

export const UPDATED = "27 November 2026";

export const PADDLE_LINKS = {
  buyerTerms: "https://www.paddle.com/legal/checkout-buyer-terms",
  refundPolicy: "https://www.paddle.com/legal/refund-policy",
  privacy: "https://www.paddle.com/legal/privacy",
  help: "https://paddle.net",
} as const;

/** The sentence ADR 0044 wrote for these pages, word for word. */
export const SHARED_NOTES =
  "Mothli’s notes on a book’s story (who’s who and what has happened, chapter by chapter) are made once for each exact book file and used for everyone who has that same file, whether they uploaded it or took it from the free library. Each reader is shown them only up to where they have read. Nothing of yours is shared: not your highlights, notes, conversations or reading position, and not your copy of the book. When nobody has the file any more, its notes are deleted 30 days later.";

/** The plan's daily rhythm (the owner's soft brake, 27 November 2026), in words, never numbers:
 * on the terms and wherever the plan is described. */
export const DAILY_RHYTHM =
  "Mothli keeps a gentle daily rhythm, so on a very busy day it may ask to pick up again tomorrow.";

/** What each page is given: who runs Mothli, and the AI company this server's chat uses
 * (`writer`, a name from the server's settings), or null where the chat is not open here. */
export type PageProps = { operator: Operator; writer?: string | null };

export const CHAT_NOT_OPEN =
  "Mothli’s chat isn’t open on this server yet, so no AI company receives anything from your reading here.";

function Out({ href, children }: { href: string; children: ReactNode }) {
  return <a href={href} rel="noopener noreferrer">{children}</a>;
}

function Mail({ operator }: { operator: Operator }) {
  return <a href={`mailto:${operator.contact_email}`}>{operator.contact_email}</a>;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="pp-section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export function Terms({ operator }: PageProps) {
  return (
    <>
      <p className="pp-lede">
        These are the rules for using Mothli. We’ve kept them short and plain. By using Mothli, you
        agree to them.
      </p>
      <Section title="Who runs Mothli">
        <p>
          Mothli is run by {operator.legal_name}, based in {operator.country} (“we” and “us” on this
          page). You can write to us at <Mail operator={operator} />.
        </p>
      </Section>
      <Section title="What Mothli is">
        <p>
          Mothli is a reading app. You read your books in it, and Mothli, a reading companion, talks
          with you about them. Mothli only uses the pages you have read so far, so it is built not to
          tell you what happens next.
        </p>
      </Section>
      <Section title="Your account">
        <p>
          You sign in through a separate sign-in service. We never see your password. Please keep
          your way of signing in safe, and use one account for yourself only.
        </p>
        <p>You must be at least 16 years old to use Mothli.</p>
      </Section>
      <Section title="Your books">
        <p>
          You can upload your own books as EPUB files, or add free classics from our free library.
          Only upload books you have the right to use.
        </p>
        <p>
          Your books stay yours. Your copy of a book is kept private to your account, and we never
          give your files to other readers. {SHARED_NOTES}
        </p>
        <p>If someone tells us that a file breaks their rights, we may remove it.</p>
      </Section>
      <Section title="Free reading, and the plan">
        <p>
          Reading in Mothli is free: your books, your place in them, your highlights, notes and
          bookmarks, and the free library.
        </p>
        <p>
          Talking with Mothli is the plan: the chat in your books and on your home page, who’s who,
          and the story so far. The plan is paid every month or every year. The prices are on our{" "}
          <a href="/">front page</a> and in the checkout.
        </p>
        <p>
          {DAILY_RHYTHM} The plan also has fair-use limits for each day and each month, so Mothli
          can keep running well for everyone. If you reach one, Mothli takes a rest until the next day or the next month
          and tells you so. Your books stay open the whole time.
        </p>
        <p>
          If you don’t join, you keep reading for free, and your conversations, notes and what Mothli
          remembers stay in your account for when you come back.
        </p>
      </Section>
      <Section title="Paying for the plan">
        <p>
          Paddle sells the plan to you on our behalf. When you pay, you buy from Paddle, which is the
          seller. <Out href={PADDLE_LINKS.buyerTerms}>Paddle’s buyer terms</Out> apply to your
          payment, your tax and your receipts. Paddle handles your card details, and we never see
          them.
        </p>
        <p>Your plan renews by itself at the end of each month or year until you cancel it.</p>
      </Section>
      <Section title="Cancelling">
        <p>
          You can cancel your plan any time in Your account. You keep Mothli until the end of the
          month or year you have paid for, and you won’t be charged again. Refunds are explained on
          our <a href={PAGE_PATHS.refunds}>refunds page</a>.
        </p>
      </Section>
      <Section title="Using Mothli fairly">
        <p>Please don’t:</p>
        <ul>
          <li>upload anything you don’t have the right to use, or anything illegal;</li>
          <li>try to get into anyone else’s account or anything that is theirs;</li>
          <li>overload, probe or break Mothli, or use it with bots or scripts;</li>
          <li>use Mothli to make anything illegal or harmful;</li>
          <li>sell or share your account.</li>
        </ul>
        <p>If you do, we may pause or close your account.</p>
      </Section>
      <Section title="Mothli’s answers come from AI">
        <p>
          Mothli’s replies and notes are written by AI models. They can be wrong, leave things out
          or misread the book. Please don’t rely on them for anything important. The book itself is
          always the one to trust.
        </p>
      </Section>
      <Section title="Changes">
        <p>
          Mothli will change over time: we may add, change or remove things in it. We may also update
          these terms. When we do, we’ll put the new terms on this page with the date they start.
          If you don’t agree with them, you can stop using Mothli and cancel your plan.
        </p>
      </Section>
      <Section title="Ending your account">
        <p>
          You can delete your account any time in Your account. We keep it for 30 days in case you
          change your mind, then erase it, as our <a href={PAGE_PATHS.privacy}>privacy page</a>{" "}
          explains.
        </p>
        <p>
          If you’re on the plan, asking to delete your account stops it from renewing, so you won’t
          be charged again. You keep Mothli until the end of the month or year you have paid for,
          or until your account is erased, whichever comes first. If you come back within the 30
          days, your plan still ends unless you choose “Keep my plan”.
        </p>
        <p>We may close an account that breaks these terms.</p>
      </Section>
      <Section title="Our responsibility">
        <p>
          We work hard to keep Mothli running and your things safe, but we can’t promise it will
          always be available or free of mistakes. As far as the law allows, we are not responsible
          for indirect losses, and our total responsibility to you is limited to what you paid for
          the plan in the 12 months before your claim.
        </p>
        <p>Nothing in these terms takes away rights you have by law as a consumer.</p>
      </Section>
      <Section title="The law">
        <p>
          These terms are governed by the law of {operator.country}. If you are a consumer, you also
          keep the protection of the law where you live, and you can bring a claim in your local
          courts.
        </p>
      </Section>
      <Section title="Contact">
        <p>
          Questions about these terms? Write to us at <Mail operator={operator} />.
        </p>
      </Section>
    </>
  );
}

export function Privacy({ operator, writer = null }: PageProps) {
  return (
    <>
      <p className="pp-lede">
        This page says what Mothli keeps about you, why, who else handles it, and how to take it
        with you or have it erased.
      </p>
      <Section title="Who is responsible">
        <p>
          {operator.legal_name}, based in {operator.country}, runs Mothli and is responsible for
          your data. Write to us at <Mail operator={operator} /> about anything on this page.
        </p>
      </Section>
      <Section title="What we keep, and why">
        <ul>
          <li>
            <strong>Your account.</strong> From the sign-in service: your name, your email address,
            whether the service has checked that address, and the id it gives you. We use them to
            sign you in and to know which account is yours.
          </li>
          <li>
            <strong>Keeping your sign-in safe.</strong> When you signed in, and a scrambled
            fingerprint of your browser and of the network you connect from, which can’t be turned
            back into either. We also keep a short record of what happened in your account and when
            (signing in and out, adding or removing a book, taking a copy of your data, asking to
            delete it), with nothing you wrote in it.
          </li>
          <li>
            <strong>Your books.</strong> The files you upload, stored encrypted, and the free
            classics you add. Where you are in each book, and how much of it you have read.
          </li>
          <li>
            <strong>What you make while reading.</strong> Your highlights, notes and bookmarks, and
            how you like the page to look.
          </li>
          <li>
            <strong>Your conversations with Mothli</strong>, in your books and on your home page.
          </li>
          <li>
            <strong>What Mothli remembers about you</strong>: small things you have told it in
            conversation. You can see them, and forget any of them, in Your account.
          </li>
          <li>
            <strong>Mothli’s notes on the chapters you have finished</strong>, made for you.
          </li>
          <li>
            <strong>Your plan.</strong> Any gift of Mothli, and your plan’s status and dates as
            Paddle reports them, with Paddle’s ids for your plan and for you as its customer. We
            never see or keep your card details.
          </li>
          <li>
            <strong>What Mothli’s replies cost us</strong>, so we can keep spending within limits.
            It is never shown to anyone.
          </li>
        </ul>
        <p>
          We keep all of this to give you the app you signed up for, and for no other reason. We
          don’t sell it, and we don’t use it for advertising.
        </p>
      </Section>
      <Section title="Who else handles it">
        <ul>
          <li>
            <strong>The sign-in service</strong> signs you in, and tells us your name and email
            address.
          </li>
          {writer ? (
            <li>
              <strong>{writer}</strong>, whose AI model writes Mothli’s replies and notes, and{" "}
              <strong>TypeSafe</strong>, which checks every sentence Mothli writes against what you
              have read before you see it. To answer you, both receive passages from pages you have
              read, your question, recent messages of your conversation, Mothli’s notes on the book
              up to where you are, and, on your home page, what Mothli remembers about you. To make
              notes on a book, they receive the text of its chapters as readers reach them. They
              never receive your name, your email address or your book files.
            </li>
          ) : (
            <li>{CHAT_NOT_OPEN}</li>
          )}
          <li>
            <strong>Paddle</strong>, when you join the plan. You buy the plan from Paddle, and
            Paddle collects what it needs for the payment (such as your email address, your country
            and your card) under{" "}
            <Out href={PADDLE_LINKS.privacy}>Paddle’s privacy policy</Out>. We give Paddle your
            account’s id inside each checkout, so it can tell us whose plan it is, and your email
            address to fill in its checkout form. Paddle tells us your plan’s status and dates.
            Paddle’s checkout opens from Paddle’s own site, and may use cookies of its own there.
          </li>
          <li>
            <strong>Where Mothli runs.</strong> Our server, which we rent, holds the database and
            your book files. <strong>Cloudflare</strong> carries every connection to Mothli (so it
            sees the address you connect from) and stores our nightly backups. The backups are
            encrypted before they leave our server, with a key only we hold.
          </li>
        </ul>
        <p>
          Some of these companies may handle your data outside your country, including in the
          United States.
        </p>
      </Section>
      <Section title="What other readers can see">
        <p>
          Nothing of yours. Other readers never see your books, your place, your questions or your
          conversations. {SHARED_NOTES}
        </p>
      </Section>
      <Section title="How long we keep it">
        <p>
          As long as you have your account. When you delete it, you’re signed out everywhere at
          once, and we keep everything for 30 days in case you change your mind (signing in again
          lets you keep it). After that we erase your books and everything that is yours. What’s
          left is your account’s bare number, with no name and no email, and the few lines of that
          record about the account itself, such as when you asked for it to be erased and when it
          was.
        </p>
        <p>
          Each nightly backup is kept for two weeks. So your data stays in backups made before it
          was erased for up to two weeks after that, and then it’s gone from them too.
        </p>
        <p>
          If you’re on the plan, deleting your account stops it from renewing, and after the 30 days
          we erase your plan’s details with everything else. Paddle is the seller of the plan, so it
          keeps its own records of your purchases and payments, even after we erase your account. We
          don’t ask Paddle to delete them.
        </p>
      </Section>
      <Section title="Your copy, and your choices">
        <ul>
          <li>
            <strong>Take a copy of your data</strong> in Your account: one file with your name,
            email address and when you joined; the books on your shelf and where you are in each;
            your highlights, notes and bookmarks; your conversations with Mothli; Mothli’s notes on
            the chapters you finished, and who you have met and the story so far, up to where you
            are; how you like the page to look; what Mothli remembers about you; and your plan (any
            gift, and your plan’s status and dates). The books themselves
            aren’t in it: open a book, press Notebook, and choose “Download the book”.
          </li>
          <li>
            <strong>Forget things</strong>: clear a conversation, or forget anything Mothli
            remembers, whenever you like.
          </li>
          <li>
            <strong>Delete your account</strong> in Your account, as above.
          </li>
        </ul>
        <p>
          You may also have rights under the law where you live, such as asking us what we hold
          about you, or asking us to correct it, erase it or stop using it. Write to{" "}
          <Mail operator={operator} /> and we’ll help.
        </p>
      </Section>
      <Section title="Cookies, and what stays on your device">
        <p>
          Mothli sets three cookies, all needed for signing in: one keeps you signed in, one
          protects your requests from other websites, and one is used only while you sign in. There
          are no advertising, analytics or tracking cookies, and no tracking scripts.
        </p>
        <p>
          Your browser also keeps a few things on your device for Mothli, such as light or dark,
          how you like your books to look, and copies of the books you save for reading offline.
          Signing out clears the offline books.
        </p>
      </Section>
      <Section title="Children">
        <p>Mothli is not for anyone under 16.</p>
      </Section>
      <Section title="Changes">
        <p>If we change this page, we’ll put the new version here with the date it starts.</p>
      </Section>
    </>
  );
}

export function Refunds({ operator }: PageProps) {
  return (
    <>
      <p className="pp-lede">
        Paddle handles payments for Mothli and sells the plan on our behalf. So refunds are handled
        by Paddle, under Paddle’s own terms. Here’s how it works.
      </p>
      <Section title="Cancel any time">
        <p>
          You can cancel your plan in Your account whenever you like. You keep Mothli until the end
          of the month or year you have paid for, and you won’t be charged again. Reading in Mothli
          stays free either way.
        </p>
      </Section>
      <Section title="Asking for a refund">
        <p>
          Refunds are decided by Paddle, under{" "}
          <Out href={PADDLE_LINKS.refundPolicy}>Paddle’s refund policy</Out> and{" "}
          <Out href={PADDLE_LINKS.buyerTerms}>Paddle’s buyer terms</Out>. To ask for one, use the
          link in the receipt Paddle emailed you, or go to{" "}
          <Out href={PADDLE_LINKS.help}>paddle.net</Out>.
        </p>
      </Section>
      <Section title="Your rights under the law">
        <p>
          Where the law gives you a right to cancel a purchase and be refunded, such as the right of
          withdrawal for buyers in the European Union and the United Kingdom, Paddle’s terms explain
          how it applies to you and how to use it.
        </p>
      </Section>
      <Section title="Something not working?">
        <p>
          If Mothli isn’t working as it should, please tell us at <Mail operator={operator} />. We’d
          love the chance to fix it.
        </p>
      </Section>
    </>
  );
}

export function Contact({ operator }: PageProps) {
  return (
    <>
      <p className="pp-lede">
        Write to us at <Mail operator={operator} />. We read every message, and we’ll answer as soon
        as we can.
      </p>
      <Section title="Who runs Mothli">
        <p>
          {operator.legal_name}, based in {operator.country}.
        </p>
      </Section>
      <Section title="Payments and receipts">
        <p>
          Paddle sells the plan on our behalf. For a receipt, a payment or a refund, Paddle can also
          help you directly at <Out href={PADDLE_LINKS.help}>paddle.net</Out>.
        </p>
      </Section>
    </>
  );
}
