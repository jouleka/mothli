/** Mothli, flying. Drawn only once the app runs, outside the page (a portal into the body), so
 * the page itself is the same with or without it. It lands beside whichever part of the page is
 * being read (the `[data-perch]` marks), follows the pointer around the opening for a while,
 * and with the lights off circles the lamp wherever the pointer goes. Tap it and it says hello.
 * With reduced motion it simply sits at each mark. */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Moth } from "../../ui/Moth";
import { shownFor } from "./arrival";

const SIZE = 54;
const FOLLOW_MS = 2600;
const HELLOS = [
  "Hey, that tickles!",
  "Shh, I’m reading.",
  "No spoilers. Not even for you.",
  "Moths and books. Name a better pair.",
  "Is that a lamp? Hold still.",
];

export function Flyer({ lamp }: { lamp: boolean }) {
  const [host, setHost] = useState<HTMLElement | null>(null);
  useEffect(() => setHost(document.body), []);
  return host ? createPortal(<FlyingMoth lamp={lamp} />, host) : null;
}

function FlyingMoth({ lamp }: { lamp: boolean }) {
  const body = useRef<HTMLDivElement>(null);
  const bubble = useRef<HTMLDivElement>(null);
  const lampOn = useRef(lamp);
  lampOn.current = lamp;
  const poke = useRef<() => void>(() => undefined);
  const [hello, setHello] = useState<string | null>(null);
  const said = useRef(0);

  useEffect(() => {
    const node = body.current!;
    // Browsers without it (and tests) are treated as moving freely.
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)") ?? { matches: false };
    const root = document.documentElement;
    const perches = () => [...document.querySelectorAll<HTMLElement>("[data-perch]")];
    const perchAt = () => {
      const marks = perches();
      const line = window.innerHeight * 0.42;
      let best: DOMRect | null = null;
      let score = Infinity;
      for (const mark of marks) {
        const box = mark.getBoundingClientRect();
        if (box.width === 0 && box.height === 0 && mark.offsetParent === null) continue;
        const gap = Math.abs(box.top - line);
        if (gap < score) {
          score = gap;
          best = box;
        }
      }
      return best ? { x: best.left + best.width / 2, y: best.top + best.height / 2 } : null;
    };

    // Coming in from the top corner, unless the page has been up a while already.
    const first = perchAt();
    const late = shownFor() > 1800 || still.matches;
    let x = late && first ? first.x : window.innerWidth + 80;
    let y = late && first ? first.y : -80;
    let vx = late ? 0 : -9;
    let vy = late ? 0 : 5;
    const begin = performance.now() + (late ? 0 : Math.max(0, 900 - shownFor()));
    let pointer: { x: number; y: number; at: number } | null = null;
    let lastLamp = { x: window.innerWidth / 2, y: window.innerHeight * 0.35 };
    let state = "";
    let frame = 0;

    const setState = (next: string) => {
      if (next === state) return;
      node.classList.remove(`is-${state}`);
      node.classList.add(`is-${next}`);
      state = next;
    };

    const onPointer = (event: PointerEvent) => {
      pointer = { x: event.clientX, y: event.clientY, at: performance.now() };
      lastLamp = { x: event.clientX, y: event.clientY };
      root.style.setProperty("--lp-lamp-x", `${event.clientX}px`);
      root.style.setProperty("--lp-lamp-y", `${event.clientY}px`);
    };
    window.addEventListener("pointermove", onPointer, { passive: true });
    window.addEventListener("pointerdown", onPointer, { passive: true });

    poke.current = () => {
      vy -= 14;
      vx += (Math.random() - 0.5) * 16;
    };

    const inOpening = (py: number) => {
      const opening = document.querySelector(".lp-hero");
      if (!opening) return false;
      const box = opening.getBoundingClientRect();
      return py >= box.top && py <= box.bottom;
    };

    const tick = (now: number) => {
      frame = requestAnimationFrame(tick);
      if (now < begin) {
        node.style.transform = `translate3d(${x - SIZE / 2}px, ${y - SIZE / 2}px, 0)`;
        return;
      }
      const t = now / 1000;
      const followLamp = lampOn.current;
      const recent = pointer && now - pointer.at < FOLLOW_MS && pointer.y > 0;
      let target: { x: number; y: number } | null = null;
      let orbit = false;
      if (followLamp) {
        target = pointer ?? lastLamp;
        orbit = true;
      } else if (recent && pointer && inOpening(pointer.y) && !still.matches) {
        target = pointer;
        orbit = true;
      } else {
        target = perchAt();
      }
      if (!target) return;
      let tx = target.x;
      let ty = target.y;
      if (orbit) {
        tx += Math.cos(t * 1.7) * 46 + 18;
        ty += Math.sin(t * 2.3) * 30 - 34;
      }

      if (still.matches) {
        x = tx;
        y = ty;
        vx = 0;
        vy = 0;
      } else {
        // A spring with a little wander: moths never fly straight.
        const ax = (tx - x) * 0.011 + Math.sin(t * 5.3) * 0.18;
        const ay = (ty - y) * 0.011 + Math.cos(t * 4.1) * 0.22;
        vx = (vx + ax) * 0.9;
        vy = (vy + ay) * 0.9;
        const speed = Math.hypot(vx, vy);
        if (speed > 24) {
          vx = (vx / speed) * 24;
          vy = (vy / speed) * 24;
        }
        x += vx;
        y += vy;
      }
      const speed = Math.hypot(vx, vy);
      const resting = !orbit && Math.hypot(tx - x, ty - y) < 6 && speed < 0.6;
      setState(resting ? "resting" : orbit ? "hovering" : "flying");
      const tilt = Math.max(-26, Math.min(26, vx * 2.4));
      node.style.transform = `translate3d(${x - SIZE / 2}px, ${y - SIZE / 2}px, 0) rotate(${tilt}deg)`;
      if (bubble.current) {
        const right = x + 30 + bubble.current.offsetWidth < window.innerWidth - 12;
        bubble.current.style.transform = `translate3d(${right ? x + 30 : x - 30 - bubble.current.offsetWidth}px, ${y - 64}px, 0)`;
      }
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      window.removeEventListener("pointermove", onPointer);
      window.removeEventListener("pointerdown", onPointer);
    };
  }, []);

  useEffect(() => {
    if (!hello) return;
    const timer = window.setTimeout(() => setHello(null), 2600);
    return () => window.clearTimeout(timer);
  }, [hello]);

  return (
    <>
      <div
        ref={body}
        className="lp-flyer"
        aria-hidden="true"
        onClick={() => {
          poke.current();
          setHello(HELLOS[said.current % HELLOS.length]);
          said.current += 1;
        }}
      >
        <Moth size={SIZE} />
      </div>
      <div ref={bubble} className={`lp-flyer-says m-hand${hello ? " is-saying" : ""}`} aria-hidden="true">
        {hello}
      </div>
    </>
  );
}
