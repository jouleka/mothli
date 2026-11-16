import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError, type AiRuntime } from "../api";
import { Button } from "./Button";
import { Icon } from "./Icon";

type RuntimeState = {
  data: AiRuntime | null;
  failed: boolean;
  checking: boolean;
  retry: () => void;
};
const RuntimeContext = createContext<RuntimeState | null>(null);

/** One owner-neutral local runtime request shared by the shelf, companion and Ask dialog. */
export function AiRuntimeProvider({
  children,
  enabled = true,
}: {
  children: ReactNode;
  enabled?: boolean;
}) {
  const [data, setData] = useState<AiRuntime | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [checking, setChecking] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    let active = true;
    const manual = attempt > 0;
    const timeout = window.setTimeout(() => controller.abort(), manual ? 90000 : 5000);
    if (!manual) setFailed(false);
    setChecking(manual);
    const request = manual ? api.checkAiRuntime(controller.signal) : api.aiRuntime(controller.signal);
    void request
      .then(
        (value) => {
          if (active) { setData(value); setFailed(false); }
        },
        (error: unknown) => {
          // Older/local versions and hosted mode may not expose the local runtime endpoint.
          if (active && !(error instanceof ApiError && error.status === 404))
            setFailed(true);
        },
      )
      .finally(() => {
        window.clearTimeout(timeout);
        if (active) setChecking(false);
      });
    return () => {
      active = false;
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [attempt, enabled]);
  return (
    <RuntimeContext.Provider
      value={
        enabled
          ? { data, failed, checking, retry: () => setAttempt((value) => value + 1) }
          : null
      }
    >
      {children}
    </RuntimeContext.Provider>
  );
}

/** The AI runtime for whoever runs the app. ``compact`` is the shelf's footer, which every reader
 * sees: there the developer's details (models, effort, local testing, test embeddings, "Check
 * connection") stay folded behind one short line the reader can open, in every state but one.
 * The exception is a test model standing in for AI (demo mode): every reader is told that plainly,
 * since its answers are not real. */
function FoldedStatus({ runtime }: { runtime: RuntimeState }) {
  const { data } = runtime;
  const degraded = data?.completion_status === "degraded";
  const title = runtime.failed || !data
    ? "AI status unavailable"
    : degraded
      ? "AI connection unavailable"
      : data.completion_status === "ready"
        ? "AI connected"
        : "AI configured";
  return (
    <details className="ai-status ai-status-compact" data-mode={degraded || runtime.failed ? "unavailable" : "live"}>
      <summary><Icon name="sparkle" size={14} /> {title} <Icon name="chevronDown" size={13} /></summary>
      {data && !runtime.failed && (
        <>
          <p>
            {data.models.synthesis}
            {degraded
              ? " · Check your provider configuration."
              : data.completion_status === "unchecked"
                ? " · Not yet checked by a successful request."
                : " · Answers use evidence from your completed pages."}
          </p>
          {data.local_testing && <p>Local testing · Using your ChatGPT account.</p>}
          {data.reasoning && <p>Low effort for extraction and selection · Medium for answers and review.</p>}
          {data.embedding_mode === "demo" && (
            <p>Search is using test embeddings. Configure a real embedding provider before judging retrieval quality.</p>
          )}
        </>
      )}
      <Button variant="ghost" onClick={runtime.retry} disabled={runtime.checking}>{runtime.checking ? "Checking…" : "Check connection"}</Button>
    </details>
  );
}

export function AiStatus({ compact = false }: { compact?: boolean }) {
  const runtime = useContext(RuntimeContext);
  if (!runtime || (!runtime.data && !runtime.failed)) return null;
  if (compact && (runtime.failed || runtime.data?.mode !== "demo")) return <FoldedStatus runtime={runtime} />;
  if (runtime.failed)
    return (
      <div className="ai-status" role="status">
        <strong>AI status unavailable</strong>
        <Button variant="ghost" onClick={runtime.retry} disabled={runtime.checking}>
          {runtime.checking ? "Checking…" : "Check connection"}
        </Button>
      </div>
    );
  const { data } = runtime;
  if (!data) return null;
  const demo = data.mode === "demo";
  const degraded = data.completion_status === "degraded";
  return (
    <div
      className="ai-status"
      data-mode={demo ? "demo" : degraded ? "unavailable" : "live"}
      role="status"
    >
      <strong>
        <Icon name="sparkle" size={15} />
        {demo
          ? "Preview mode · AI not connected"
          : degraded
            ? "AI connection unavailable"
            : data.completion_status === "ready"
              ? "AI connected"
              : "AI configured"}
      </strong>
      {demo ? (
        <>
          <p>
            Questions and recaps use an offline test model. This cannot provide
            real AI answers.
          </p>
          <details>
            <summary>Connect your AI provider</summary>
            <p>
              Add your provider key to the local .env configuration and restart
              the app. Your key stays on the server.
            </p>
          </details>
        </>
      ) : (
        <>
          <p>
            {data.models.synthesis}
            {degraded
              ? " · Check your provider configuration."
              : data.completion_status === "unchecked"
                ? " · Not yet checked by a successful request."
                : " · Answers still depend on the evidence in your completed pages."}
          </p>
          {data.local_testing && <p>Local testing · Using your ChatGPT account.</p>}
          {data.reasoning && <p>Low effort for extraction and selection · Medium for answers and review.</p>}
          {data.embedding_mode === "demo" && (
            <p>
              Search is using test embeddings. Configure a real embedding
              provider before judging retrieval quality.
            </p>
          )}
          <Button variant="ghost" onClick={runtime.retry} disabled={runtime.checking}>
            {runtime.checking ? "Checking…" : "Check connection"}
          </Button>
        </>
      )}
    </div>
  );
}
