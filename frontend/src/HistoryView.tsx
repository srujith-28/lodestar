import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiFetch as fetch } from "./api";

type JsonObject = Record<string, unknown>;
type TaskStatus = "pending" | "running" | "completed" | "failed" | "awaiting_approval";
type TaskFilterStatus = TaskStatus | "all";
type Task = {
  id: number;
  is_sample: boolean;
  skill_id: number | null;
  task_input: string;
  status: TaskStatus;
  plan_steps: Array<{
    step: number;
    description: string;
    tool_name: string;
    permission_level: string;
    requires_approval: boolean;
    status: string;
    input: JsonObject;
  }>;
  result_summary: string | null;
  created_at: string;
  updated_at: string;
};
type TraceEvent = {
  id: number;
  task_id: number;
  step_index: number | null;
  event_type: string;
  tool_name: string | null;
  status: string;
  approval_required: boolean;
  input_data: JsonObject | null;
  result_data: unknown;
  message: string | null;
  created_at: string;
};
type HistoryViewProps = { apiBaseUrl: string; initialTaskId?: number | null; onNavigate: (page: "agent") => void };

const PAGE_SIZE = 50;

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Time unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

async function responseError(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null);
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item: { msg?: unknown }) => typeof item?.msg === "string" ? item.msg : null)
      .filter((message: string | null): message is string => message !== null);
    if (messages.length) return messages.join(" ");
  }
  return `History request failed (${response.status}).`;
}

export function HistoryView({ apiBaseUrl, initialTaskId, onNavigate }: HistoryViewProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [searchText, setSearchText] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskFilterStatus>("all");
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);
  const [approvalBusy, setApprovalBusy] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [confirmRerun, setConfirmRerun] = useState(false);

  const loadTasks = useCallback(async (options: {
    signal?: AbortSignal;
    query?: string;
    status?: TaskFilterStatus;
  } = {}) => {
    setLoading(true);
    setError(null);
    try {
      const query = options.query ?? searchQuery;
      const status = options.status ?? statusFilter;
      const response = await fetch(`${apiBaseUrl}/api/agent/tasks/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          q: query.trim() || null,
          status: status === "all" ? null : status,
          limit: PAGE_SIZE + 1,
          offset: 0,
        }),
        signal: options.signal,
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = (await response.json()) as Task[];
      const visibleTasks = payload.slice(0, PAGE_SIZE);
      setTasks(visibleTasks);
      setHasMore(payload.length > PAGE_SIZE);
      setSelectedTask((current) => current ? visibleTasks.find((task) => task.id === current.id) ?? null : null);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setTasks([]);
      setHasMore(false);
      setSelectedTask(null);
      setEvents([]);
      setError(cause instanceof Error ? cause.message : "Could not load task history.");
    } finally {
      if (!options.signal?.aborted) setLoading(false);
    }
  }, [apiBaseUrl, searchQuery, statusFilter]);

  useEffect(() => {
    const controller = new AbortController();
    void loadTasks({ signal: controller.signal });
    return () => controller.abort();
  }, [loadTasks]);

  useEffect(() => {
    if (initialTaskId) void loadTrace(initialTaskId);
  }, [initialTaskId]);

  async function loadTrace(taskId: number) {
    setTraceLoading(true);
    setTraceError(null);
    setConfirmRerun(false);
    setEvents([]);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/tasks/${taskId}/trace`);
      if (!response.ok) throw new Error(await responseError(response));
      const payload = (await response.json()) as { task: Task; events: TraceEvent[] };
      setSelectedTask(payload.task);
      setEvents(payload.events);
    } catch (cause) {
      setTraceError(cause instanceof Error ? cause.message : "Could not load this execution trace.");
    } finally {
      setTraceLoading(false);
    }
  }

  async function loadMore() {
    if (loadingMore || !hasMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/tasks/search`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          q: searchQuery.trim() || null,
          status: statusFilter === "all" ? null : statusFilter,
          limit: PAGE_SIZE + 1,
          offset: tasks.length,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const payload = (await response.json()) as Task[];
      setTasks((current) => [...current, ...payload.slice(0, PAGE_SIZE)]);
      setHasMore(payload.length > PAGE_SIZE);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load more task history.");
    } finally {
      setLoadingMore(false);
    }
  }

  function applyFilters(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSearchQuery(searchText.trim());
  }

  function clearFilters() {
    setSearchText("");
    setSearchQuery("");
    setStatusFilter("all");
    setSelectedTask(null);
    setEvents([]);
    setTraceError(null);
  }

  const mayRepeatWrites = Boolean(selectedTask?.plan_steps.some(
    (step) => step.permission_level === "write" || step.requires_approval,
  ));

  function requestRerun() {
    if (!selectedTask || !["completed", "failed"].includes(selectedTask.status) || rerunBusy) return;
    if (mayRepeatWrites) {
      setConfirmRerun(true);
      return;
    }
    void runAgain(false);
  }

  async function runAgain(confirmWrite: boolean) {
    if (!selectedTask || rerunBusy) return;
    setRerunBusy(true);
    setTraceError(null);
    setError(null);
    setSuccess(null);
    setConfirmRerun(false);
    const sourceTaskId = selectedTask.id;
    try {
      let response: Response;
      try {
        response = await fetch(`${apiBaseUrl}/api/agent/tasks/${sourceTaskId}/rerun`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm_write: confirmWrite }),
        });
      } catch {
        throw new Error("The connection ended before Lodestar confirmed the new run. Refresh History before retrying; the task may already have been created.");
      }
      if (response.status >= 500) {
        const detail = await responseError(response);
        throw new Error(`The server did not confirm the new run. Refresh History before retrying; the task may already exist. ${detail}`);
      }
      if (!response.ok) throw new Error(await responseError(response));
      const created = (await response.json()) as Task;
      setSearchText("");
      setSearchQuery("");
      setStatusFilter("all");
      await loadTasks({ query: "", status: "all" });
      await loadTrace(created.id);
      setSuccess(`Created task #${created.id} as a new run of task #${sourceTaskId}. The prior trace remains unchanged.`);
    } catch (cause) {
      setTraceError(cause instanceof Error ? cause.message : "Could not run this task again.");
    } finally {
      setRerunBusy(false);
    }
  }

  async function decideApproval(approved: boolean) {
    if (!selectedTask || selectedTask.status !== "awaiting_approval" || approvalBusy) return;
    setApprovalBusy(true);
    setTraceError(null);
    setSuccess(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/tasks/${selectedTask.id}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      await loadTrace(selectedTask.id);
      setSuccess(approved ? "Approval recorded. The next registered step ran or is waiting for its own approval." : "Approval denied. The pending write did not run.");
      await loadTasks();
    } catch (cause) {
      setTraceError(cause instanceof Error ? cause.message : "Could not record this approval decision.");
    } finally {
      setApprovalBusy(false);
    }
  }

  return (
    <section className="history-section" id="history" aria-labelledby="history-title">
      <div className="page-section-heading">
        <div>
          <p className="eyebrow"><span className="eyebrow-line" /> PERSISTED LOCAL RECORDS</p>
          <h1 id="history-title">Task history</h1>
        </div>
        <div className="heading-side-note">
          <p>Inspect real task outcomes, approval decisions, and registered tool traces. Chat conversations are not saved.</p>
          <button className="button-secondary" type="button" onClick={() => void loadTasks()} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh history"}
          </button>
        </div>
      </div>

      {error && <div className="inline-alert error" role="alert">{error}</div>}
      {success && <div className="inline-alert success" role="status">{success}</div>}
      {traceError && <div className="inline-alert error" role="alert">{traceError}</div>}

      <form className="history-filters surface-card" onSubmit={applyFilters} role="search" aria-label="Search task history">
        <label className="sr-only" htmlFor="history-search">Search task descriptions and results</label>
        <input
          id="history-search"
          type="search"
          value={searchText}
          onChange={(event) => setSearchText(event.target.value)}
          maxLength={120}
          placeholder="Search tasks and results"
        />
        <label className="sr-only" htmlFor="history-status">Filter tasks by status</label>
        <select id="history-status" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as TaskFilterStatus)}>
          <option value="all">All statuses</option>
          <option value="pending">Pending</option>
          <option value="running">Running</option>
          <option value="awaiting_approval">Awaiting approval</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
        </select>
        <button className="button-secondary" type="submit" disabled={loading}>Search</button>
        {(searchQuery || statusFilter !== "all") && <button className="text-button" type="button" onClick={clearFilters}>Clear</button>}
      </form>

      {loading ? (
        <div className="surface-card history-state" role="status">Loading persisted tasks…</div>
      ) : tasks.length === 0 ? (
        <div className="surface-card history-state empty-state">
          {searchQuery || statusFilter !== "all" ? (
            <>
              <strong>No tasks match these filters</strong>
              <span>Try another phrase or status, or clear the filters.</span>
              <button className="button-secondary" type="button" onClick={clearFilters}>Clear filters</button>
            </>
          ) : (
            <>
              <strong>No task history yet</strong>
              <span>Run an explicitly selected local tool or approved skill to create the first persisted trace.</span>
              <button className="button-primary" type="button" onClick={() => onNavigate("agent")}>Open Agent</button>
            </>
          )}
        </div>
      ) : (
        <div className="history-layout">
          <section className="surface-card history-list-card" aria-label="Saved agent tasks">
            <div className="surface-heading">
              <div><span className="eyebrow-label">LOCAL SQLITE</span><h2>Task results</h2><span className="history-result-count">Showing {tasks.length}{hasMore ? "+" : ""} matching tasks</span></div>
            </div>
            <div className="history-task-list">
              {tasks.map((task) => (
                <button
                  className={`history-task-row ${selectedTask?.id === task.id ? "selected" : ""}`}
                  type="button"
                  key={task.id}
                  onClick={() => void loadTrace(task.id)}
                  aria-current={selectedTask?.id === task.id ? "true" : undefined}
                >
                  <span className="history-task-copy"><strong>{task.task_input}</strong><span className="history-task-meta">{task.is_sample && <span className="sample-data-badge">Sample data</span>}<time dateTime={task.created_at}>{formatTime(task.created_at)}</time></span></span>
                  <span className={`status-badge ${task.status}`}>{task.status.replaceAll("_", " ")}</span>
                </button>
              ))}
            </div>
            {hasMore && <button className="load-more-button" type="button" onClick={() => void loadMore()} disabled={loadingMore}>{loadingMore ? "Loading…" : "Load older tasks"}</button>}
          </section>

          <section className="surface-card history-detail-card" aria-label="Selected task details" aria-live="polite">
            {!selectedTask && !traceLoading ? (
              <div className="history-state empty-state"><strong>Select a task</strong><span>Its saved plan and execution trace will appear here.</span></div>
            ) : traceLoading ? (
              <div className="history-state" role="status">Loading the selected task trace…</div>
            ) : selectedTask ? (
              <>
                <div className="history-detail-heading">
                  <div><span className="eyebrow-label">TASK #{selectedTask.id}{selectedTask.skill_id ? ` · SKILL #${selectedTask.skill_id}` : ""}</span><h2>{selectedTask.task_input}</h2></div>
                  <span className={`status-badge ${selectedTask.status}`}>{selectedTask.status.replaceAll("_", " ")}</span>
                </div>
                <p className="history-updated">Created {formatTime(selectedTask.created_at)} · Updated {formatTime(selectedTask.updated_at)}</p>
                {selectedTask.result_summary && <p className="history-result">{selectedTask.result_summary}</p>}

                {selectedTask.status === "running" && (
                  <div className="history-runtime-note" role="status">
                    This is the last persisted status. Task execution is synchronous and does not resume after a server restart.
                  </div>
                )}

                {["completed", "failed"].includes(selectedTask.status) && (
                  <div className="history-rerun-row">
                    <button className="button-secondary" type="button" onClick={requestRerun} disabled={rerunBusy}>
                      {rerunBusy ? "Creating new run…" : "Run again as a new task"}
                    </button>
                    <span>This creates a separate task from the saved selection using current local data; it does not resume the previous execution.</span>
                  </div>
                )}

                {confirmRerun && mayRepeatWrites && (
                  <div className="approval-box history-approval" role="group" aria-label="Confirm repeating write actions">
                    <div>
                      <strong>This plan may repeat write actions.</strong>
                      <p>A new task will repeat the saved steps and could duplicate earlier writes. Each write will still pause for its own approval.</p>
                    </div>
                    <div className="approval-actions">
                      <button className="button-secondary" type="button" onClick={() => setConfirmRerun(false)} disabled={rerunBusy}>Cancel</button>
                      <button className="button-primary" type="button" onClick={() => void runAgain(true)} disabled={rerunBusy}>{rerunBusy ? "Creating…" : "Confirm new run"}</button>
                    </div>
                  </div>
                )}

                {selectedTask.status === "awaiting_approval" && (
                  <div className="approval-box history-approval">
                    <div><strong>Review before approving</strong><p>Only the pending registered write will run after approval. Later write steps require their own approval.</p></div>
                    <div className="approval-actions">
                      <button className="button-secondary" type="button" onClick={() => void decideApproval(false)} disabled={approvalBusy}>{approvalBusy ? "Recording…" : "Deny"}</button>
                      <button className="button-primary" type="button" onClick={() => void decideApproval(true)} disabled={approvalBusy}>{approvalBusy ? "Recording…" : "Approve pending step"}</button>
                    </div>
                  </div>
                )}

                <div className="history-plan">
                  <h3>Plan steps</h3>
                  {selectedTask.plan_steps.map((step) => (
                    <article className="history-plan-step" key={step.step}>
                      <div className="history-plan-title"><span className="plan-index">{String(step.step).padStart(2, "0")}</span><div><strong>{step.description}</strong><span>{step.tool_name} · {step.permission_level}{step.requires_approval ? " · approval required" : ""}</span></div><span className={`step-status ${step.status}`}>{step.status.replaceAll("_", " ")}</span></div>
                      <details><summary>Inspect planned input</summary><pre>{pretty(step.input)}</pre></details>
                    </article>
                  ))}
                </div>

                {selectedTask.plan_steps.some((step) => step.tool_name === "memory_search") && (
                  <p className="memory-context-note">This task used only the actual matches returned by local keyword search below. No other memories were attached.</p>
                )}
                <div className="surface-heading trace-section-heading"><div><span className="eyebrow-label">ACTUAL EXECUTION</span><h3>Trace events</h3></div><button className="text-button" type="button" onClick={() => void loadTrace(selectedTask.id)} disabled={traceLoading}>Refresh trace</button></div>
                {events.length === 0 ? (
                  <div className="surface-state">No trace events were returned for this task.</div>
                ) : (
                  <div className="history-trace-list">
                    {events.map((event) => (
                      <article className="history-trace-event" key={event.id}>
                        <div className="history-trace-header"><strong>{event.event_type.replaceAll("_", " ")}</strong><span className={`step-status ${event.status}`}>{event.status.replaceAll("_", " ")}</span><time dateTime={event.created_at}>{formatTime(event.created_at)}</time></div>
                        <div className="history-trace-meta">{event.tool_name && <code>{event.tool_name}</code>}{event.approval_required && <span className="permission-pill write">approval required</span>}</div>
                        {event.message && <p>{event.message}</p>}
                        {event.input_data && <details><summary>Actual tool input</summary><pre>{pretty(event.input_data)}</pre></details>}
                        {event.result_data !== null && event.result_data !== undefined && <details open={event.event_type === "tool_completed"}><summary>{event.tool_name === "memory_search" ? "Actual keyword matches · local memories" : "Actual tool result"}</summary><pre>{pretty(event.result_data)}</pre></details>}
                      </article>
                    ))}
                  </div>
                )}
              </>
            ) : null}
          </section>
        </div>
      )}
      <p className="privacy-footnote">Task descriptions and tool traces remain in the configured local database. No LLM call is made by the local task runner.</p>
    </section>
  );
}
