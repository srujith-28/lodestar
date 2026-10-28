import { useCallback, useEffect, useState } from "react";
import { apiFetch as fetch } from "./api";

type AgentTask = {
  id: number;
  is_sample: boolean;
  task_input: string;
  status: "pending" | "running" | "completed" | "failed" | "awaiting_approval";
  created_at: string;
  updated_at: string;
};

type ActivityItem = {
  id: number;
  task_id: number;
  event_type: string;
  tool_name: string | null;
  status: string;
  approval_required: boolean;
  created_at: string;
};

type Overview = {
  memory_count: number;
  task_count: number;
  pending_approval_count: number;
  recent_tasks: AgentTask[];
  recent_activity: ActivityItem[];
};

type HealthStatus = "loading" | "online" | "offline";
type ProviderSetup = {
  provider: string;
  status: "missing_configuration" | "configured_unverified";
  api_key_configured: boolean;
  model_id_configured: boolean;
  model_id: string | null;
} | null;
type ProviderCallState = "idle" | "in_progress" | "succeeded" | "failed";
type HomeViewProps = {
  apiBaseUrl: string;
  healthStatus: HealthStatus;
  healthMessage: string;
  providerSetup: ProviderSetup;
  providerStatusLoading: boolean;
  providerCallState: ProviderCallState;
  onRefreshHealth: () => void;
  onRefreshProvider: () => void;
  onNavigate: (page: "chat" | "memory" | "agent" | "skills" | "history", taskId?: number) => void;
};

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Time unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

async function responseError(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null);
  return typeof payload?.detail === "string" ? payload.detail : `Dashboard request failed (${response.status}).`;
}

export function HomeView({
  apiBaseUrl,
  healthStatus,
  healthMessage,
  providerSetup,
  providerStatusLoading,
  providerCallState,
  onRefreshHealth,
  onRefreshProvider,
  onNavigate,
}: HomeViewProps) {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadOverview = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/overview`, { signal });
      if (!response.ok) throw new Error(await responseError(response));
      setOverview((await response.json()) as Overview);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setError(cause instanceof Error ? cause.message : "Could not load the local overview.");
      setOverview(null);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    const controller = new AbortController();
    void loadOverview(controller.signal);
    return () => controller.abort();
  }, [loadOverview]);

  const providerNote = providerCallState === "succeeded"
    ? "A real chat reply arrived during this session. Model eligibility still needs confirmation."
    : providerCallState === "failed"
      ? providerSetup && (!providerSetup.api_key_configured || !providerSetup.model_id_configured)
        ? "Provider not configured. The request was rejected before a live model call."
        : providerSetup
          ? "The configured provider request failed. No successful inference is recorded."
          : "The chat request failed, but provider status could not be confirmed."
      : providerCallState === "in_progress"
        ? "Waiting for the configured provider to return a reply."
        : !providerSetup
          ? "Provider status is unavailable; no live inference has been verified."
          : !providerSetup.api_key_configured
            ? "Provider not configured. Set NEBIUS_API_KEY in the server environment after access is granted; no live inference has been verified."
            : !providerSetup.model_id_configured
              ? "Provider not configured. Set a verified NEBIUS_MODEL_ID on the server before making a live request."
              : "Configuration is present, but no live check runs until you send a chat message.";
  const providerConfigurationMissing = Boolean(
    providerSetup && (!providerSetup.api_key_configured || !providerSetup.model_id_configured),
  );
  const providerBadgeLabel = providerCallState === "succeeded"
    ? "Live reply"
    : providerCallState === "failed"
      ? providerConfigurationMissing ? "Provider not configured" : "Request failed"
      : providerStatusLoading
        ? "Checking…"
        : providerConfigurationMissing
          ? "Provider not configured"
          : providerSetup?.status === "configured_unverified"
            ? "Unverified"
            : providerSetup ? "Not connected" : "Status unavailable";

  return (
    <section className="home-view" id="home" aria-labelledby="home-title">
      <div className="home-intro">
        <div>
          <p className="eyebrow"><span className="eyebrow-line" /> YOUR PERSONAL AI WORKSPACE</p>
          <h1 id="home-title">Remember what matters.<br /><span>Pick up where you left off.</span></h1>
          <p className="home-promise">Get things done—with control.</p>
        </div>
        <div className="home-actions" aria-label="Quick actions">
          <button className="button-primary" type="button" onClick={() => onNavigate("chat")}>Open chat</button>
          <button className="button-secondary" type="button" onClick={() => onNavigate("memory")}>Save a memory</button>
        </div>
      </div>

      <section className={`health-banner ${healthStatus}`} aria-label="Local API and database status" role="status" aria-live="polite">
        <span className={`health-indicator ${healthStatus}`} aria-hidden="true" />
        <div className="health-banner-copy">
          <strong>{healthStatus === "online" ? "Local workspace ready" : healthStatus === "offline" ? "Local service unavailable" : "Checking local service"}</strong>
          <span>{healthMessage}</span>
        </div>
        <button className="text-button" type="button" onClick={onRefreshHealth} disabled={healthStatus === "loading"}>
          {healthStatus === "loading" ? "Checking…" : "Refresh"}
        </button>
      </section>

      <div className="overview-grid" aria-label="Workspace totals" aria-busy={loading}>
        <article className="metric-card">
          <span className="metric-label">SAVED MEMORIES</span>
          <strong>{loading ? "—" : overview?.memory_count ?? "—"}</strong>
          <span className="metric-caption">Stored in your local database</span>
        </article>
        <article className="metric-card">
          <span className="metric-label">AGENT TASKS</span>
          <strong>{loading ? "—" : overview?.task_count ?? "—"}</strong>
          <span className="metric-caption">Persisted task runs</span>
        </article>
        <article className={`metric-card ${overview?.pending_approval_count ? "metric-attention" : ""}`}>
          <span className="metric-label">NEED YOUR APPROVAL</span>
          <strong>{loading ? "—" : overview?.pending_approval_count ?? "—"}</strong>
          <span className="metric-caption">Write actions waiting for a decision</span>
        </article>
      </div>

      {error && (
        <div className="inline-alert error" role="alert">
          <span>{error}</span>
          <button className="text-button" type="button" onClick={() => void loadOverview()} disabled={loading}>Try again</button>
        </div>
      )}

      <div className="dashboard-grid">
        <section className="surface-card recent-tasks-card" aria-labelledby="recent-tasks-title">
          <div className="surface-heading">
            <div><span className="eyebrow-label">LOCAL AGENT</span><h2 id="recent-tasks-title">Recent tasks</h2></div>
            <button className="text-button" type="button" onClick={() => onNavigate("history")}>View history</button>
          </div>
          {loading ? (
            <div className="surface-state" role="status">Loading saved task activity…</div>
          ) : error ? (
            <div className="surface-state">Task activity is unavailable.</div>
          ) : !overview?.recent_tasks.length ? (
            <div className="surface-state empty-state">
              <strong>No tasks yet</strong>
              <span>Run a selected local tool to create a persisted task trace.</span>
              <button className="text-button" type="button" onClick={() => onNavigate("agent")}>Open Agent</button>
            </div>
          ) : (
            <ul className="activity-list">
              {overview.recent_tasks.map((task) => (
                <li className="task-list-row" key={task.id}>
                  <button className="task-row-button" type="button" onClick={() => onNavigate("history", task.id)} aria-label={`Open task ${task.id} in history`}>
                    <span className="task-row-copy"><strong>{task.task_input}</strong><span className="task-row-meta">{task.is_sample && <span className="sample-data-badge">Sample data</span>}<time dateTime={task.created_at}>{formatTime(task.created_at)}</time></span></span>
                    <span className={`status-badge ${task.status}`}>{task.status.replaceAll("_", " ")}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="surface-card activity-card" aria-labelledby="activity-title">
          <div className="surface-heading">
            <div><span className="eyebrow-label">PERSISTED TRACE EVENTS</span><h2 id="activity-title">Recent activity</h2></div>
            <span className="local-only-label">LOCAL</span>
          </div>
          {loading ? (
            <div className="surface-state" role="status">Loading execution activity…</div>
          ) : error ? (
            <div className="surface-state">Execution activity is unavailable.</div>
          ) : !overview?.recent_activity.length ? (
            <div className="surface-state empty-state"><strong>Nothing recorded yet</strong><span>Actual tool steps will appear here after a task runs.</span></div>
          ) : (
            <ul className="activity-list">
              {overview.recent_activity.slice(0, 6).map((event) => (
                <li className="activity-row" key={event.id}>
                  <span className={`activity-marker ${event.status}`} aria-hidden="true" />
                  <div><strong>{event.tool_name ?? event.event_type.replaceAll("_", " ")}</strong><span>Task #{event.task_id} · {event.status.replaceAll("_", " ")}</span></div>
                  <time dateTime={event.created_at}>{formatTime(event.created_at)}</time>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="surface-card provider-card" aria-labelledby="provider-title">
          <div className="surface-heading">
            <div><span className="eyebrow-label">MODEL CONNECTIVITY</span><h2 id="provider-title">Nebius Token Factory</h2></div>
            <span className={`status-badge ${providerCallState === "succeeded" ? "completed" : providerCallState === "failed" && !providerConfigurationMissing ? "failed" : "pending"}`}>
              {providerBadgeLabel}
            </span>
          </div>
          <p className="provider-note">{providerNote}</p>
          <div className="provider-card-footer">
            <span className="provider-model-reference">
              {providerSetup?.model_id
                ? <>Model <code>{providerSetup.model_id}</code> · configuration only</>
                : providerSetup
                  ? "Model ID not configured · configuration only"
                  : "Model status unavailable"}
            </span>
            <button className="text-button" type="button" onClick={onRefreshProvider} disabled={providerStatusLoading}>
              {providerStatusLoading ? "Checking…" : "Refresh status"}
            </button>
          </div>
        </section>
      </div>

      <section className="quick-links" aria-label="Workspace destinations">
        <button type="button" onClick={() => onNavigate("memory")}><span>01</span><strong>Memory</strong><small>Review what you chose to save</small></button>
        <button type="button" onClick={() => onNavigate("agent")}><span>02</span><strong>Agent</strong><small>Run an explicitly selected local tool</small></button>
        <button type="button" onClick={() => onNavigate("skills")}><span>03</span><strong>Skills</strong><small>Review reusable approved workflows</small></button>
      </section>

      <p className="dashboard-footnote">Home shows persisted local records. Configuration is not a live model check; no model call is made on this page.</p>
    </section>
  );
}
