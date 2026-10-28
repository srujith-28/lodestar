import { useCallback, useEffect, useState, type FormEvent } from "react";
import { apiFetch as fetch } from "./api";

type JsonObject = Record<string, unknown>;
type SkillStatus = "draft" | "pending_approval" | "approved" | "disabled";
type PlannerProviderStatus = "missing_configuration" | "configured_unverified" | "unavailable";

type RegisteredTool = {
  name: string;
  description: string;
  permission_level: "read" | "write";
  requires_approval: boolean;
  input_schema: Record<string, unknown>;
};

type SkillStep = {
  tool_name: string;
  description: string;
  tool_input: JsonObject;
};

type SkillItem = {
  id: number;
  name: string;
  description: string;
  steps: SkillStep[];
  status: SkillStatus;
  created_at: string;
  updated_at: string;
};

type TaskItem = {
  id: number;
  is_sample: boolean;
  skill_id: number | null;
  task_input: string;
  status: "pending" | "running" | "completed" | "failed" | "awaiting_approval";
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

type AgentViewsProps = { apiBaseUrl: string };

const DEFAULT_STEP: SkillStep = {
  tool_name: "current_datetime",
  description: "Read the current UTC date and time",
  tool_input: {},
};

function defaultToolInput(toolName: string): JsonObject {
  if (toolName === "memory_search") return { query: "", limit: 10 };
  if (toolName === "save_memory") return { content: "", category: "general", source: "user" };
  return {};
}

async function responseError(response: Response): Promise<string> {
  const payload = await response.json().catch(() => null);
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item: { msg?: unknown }) => (typeof item?.msg === "string" ? item.msg : null))
      .filter((message: string | null): message is string => message !== null);
    if (messages.length) return messages.join(" ");
  }
  return `Request failed (${response.status}).`;
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function formatTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Timestamp unavailable" : date.toLocaleString();
}

function ToolInputFields({
  toolName,
  value,
  prefix,
  onChange,
}: {
  toolName: string;
  value: JsonObject;
  prefix: string;
  onChange: (next: JsonObject) => void;
}) {
  const update = (key: string, next: unknown) => onChange({ ...value, [key]: next });

  if (toolName === "memory_search") {
    return (
      <div className="agent-tool-fields">
        <label htmlFor={`${prefix}-query`}>Search terms</label>
        <input
          id={`${prefix}-query`}
          value={typeof value.query === "string" ? value.query : ""}
          onChange={(event) => update("query", event.target.value)}
          maxLength={100}
          required
          placeholder="For example: coastal walks"
        />
        <label htmlFor={`${prefix}-limit`}>Maximum results</label>
        <input
          id={`${prefix}-limit`}
          type="number"
          min={1}
          max={50}
          value={typeof value.limit === "number" ? value.limit : 10}
          onChange={(event) => update("limit", Number(event.target.value) || 1)}
        />
      </div>
    );
  }

  if (toolName === "save_memory") {
    return (
      <div className="agent-tool-fields">
        <label htmlFor={`${prefix}-content`}>Memory content</label>
        <textarea
          id={`${prefix}-content`}
          value={typeof value.content === "string" ? value.content : ""}
          onChange={(event) => update("content", event.target.value)}
          maxLength={5_000}
          rows={3}
          required
          placeholder="Do not enter passwords, API keys, or credentials."
        />
        <div className="agent-tool-field-row">
          <div>
            <label htmlFor={`${prefix}-category`}>Category</label>
            <input
              id={`${prefix}-category`}
              value={typeof value.category === "string" ? value.category : "general"}
              onChange={(event) => update("category", event.target.value)}
              maxLength={60}
              required
            />
          </div>
          <div>
            <label htmlFor={`${prefix}-source`}>Source</label>
            <input
              id={`${prefix}-source`}
              value={typeof value.source === "string" ? value.source : "user"}
              onChange={(event) => update("source", event.target.value)}
              maxLength={80}
              required
            />
          </div>
        </div>
      </div>
    );
  }

  return <p className="agent-inline-note">This read-only tool does not need additional input.</p>;
}

export function AgentView({ apiBaseUrl }: AgentViewsProps) {
  const [tools, setTools] = useState<RegisteredTool[]>([]);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [selectedTarget, setSelectedTarget] = useState("tool:memory_search");
  const [toolInput, setToolInput] = useState<JsonObject>({ query: "", limit: 10 });
  const [taskInput, setTaskInput] = useState("");
  const [task, setTask] = useState<TaskItem | null>(null);
  const [events, setEvents] = useState<TraceEvent[]>([]);
  const [traceLoading, setTraceLoading] = useState(false);
  const [loadingOptions, setLoadingOptions] = useState(true);
  const [plannerProviderStatus, setPlannerProviderStatus] = useState<PlannerProviderStatus>("unavailable");
  const [plannerModelId, setPlannerModelId] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [actingOnApproval, setActingOnApproval] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadOptions = useCallback(async () => {
    setLoadingOptions(true);
    setError(null);
    try {
      const [toolResponse, skillResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/api/agent/tools`),
        fetch(`${apiBaseUrl}/api/agent/skills`),
      ]);
      if (!toolResponse.ok) throw new Error(await responseError(toolResponse));
      if (!skillResponse.ok) throw new Error(await responseError(skillResponse));
      setTools((await toolResponse.json()) as RegisteredTool[]);
      setSkills((await skillResponse.json()) as SkillItem[]);
      try {
        const providerResponse = await fetch(`${apiBaseUrl}/api/provider/status`);
        if (!providerResponse.ok) throw new Error("Provider status unavailable.");
        const provider = (await providerResponse.json()) as {
          status: PlannerProviderStatus;
          model_id: string | null;
        };
        setPlannerProviderStatus(provider.status);
        setPlannerModelId(provider.model_id);
      } catch {
        setPlannerProviderStatus("unavailable");
        setPlannerModelId(null);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load registered tools and skills.");
    } finally {
      setLoadingOptions(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void loadOptions();
  }, [loadOptions]);

  async function loadTrace(taskId: number) {
    setTraceLoading(true);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/tasks/${taskId}/trace`);
      if (!response.ok) throw new Error(await responseError(response));
      const payload = (await response.json()) as { task: TaskItem; events: TraceEvent[] };
      setTask(payload.task);
      setEvents(payload.events);
    } finally {
      setTraceLoading(false);
    }
  }

  async function refreshTrace(taskId: number) {
    setError(null);
    try {
      await loadTrace(taskId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not refresh the execution trace.");
    }
  }

  const selectedSkillId = selectedTarget.startsWith("skill:")
    ? Number(selectedTarget.slice("skill:".length))
    : null;
  const selectedToolName = selectedTarget.startsWith("tool:")
    ? selectedTarget.slice("tool:".length)
    : null;
  const selectedTool = tools.find((tool) => tool.name === selectedToolName);
  const selectedModelPlanner = selectedTarget === "planner:nebius";

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    setTask(null);
    setEvents([]);
    try {
      const body = selectedModelPlanner
        ? { task_input: taskInput }
        : selectedSkillId
          ? { task_input: taskInput, skill_id: selectedSkillId }
          : { task_input: taskInput, tool_name: selectedToolName, tool_input: toolInput };
      const taskEndpoint = selectedModelPlanner ? "/api/agent/tasks/plan" : "/api/agent/tasks";
      let response: Response;
      try {
        response = await fetch(`${apiBaseUrl}${taskEndpoint}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch {
        throw new Error("The connection ended before Lodestar confirmed the task result. Check History before retrying; the task may already exist.");
      }
      if (response.status >= 500) {
        const detail = await responseError(response);
        throw new Error(`The server did not confirm the task result. Check History before retrying; the task may already exist. ${detail}`);
      }
      if (!response.ok) throw new Error(await responseError(response));
      const created = (await response.json()) as TaskItem;
      setTask(created);
      try {
        await loadTrace(created.id);
      } catch {
        setError(`Task #${created.id} was accepted with status ${created.status}, but its trace could not be loaded. Refresh the trace before taking another action.`);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not submit this task.");
    } finally {
      setSubmitting(false);
    }
  }

  async function decideApproval(approved: boolean) {
    if (!task || actingOnApproval) return;
    setActingOnApproval(true);
    setError(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/tasks/${task.id}/approval`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      await loadTrace(task.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not record the approval decision.");
    } finally {
      setActingOnApproval(false);
    }
  }

  return (
    <section className="agent-section" id="agent" aria-labelledby="agent-title">
      <div className="agent-heading">
        <div>
          <div className="eyebrow subtle"><span className="eyebrow-line" /> AGENT TASK RUNNER</div>
          <h2 id="agent-title">Give a task<br /><span>a clear next step.</span></h2>
        </div>
        <div className="agent-heading-copy">
          <p className="section-description">
            Choose a local deterministic tool or approved skill, or explicitly request a bounded proposal from the configured Nebius model. Every proposed action is checked locally before execution.
          </p>
          <a className="agent-text-button" href="#skills">Review reusable skills</a>
          <button className="agent-text-button" type="button" onClick={() => void loadOptions()} disabled={loadingOptions}>
            {loadingOptions ? "Refreshing…" : "Refresh tools and skills"}
          </button>
        </div>
      </div>

      <div className="agent-panel">
        <div className="demo-guide">
          <span className="card-kicker">30-SECOND MEMORY RECALL DEMO</span>
          <p><a href="#memories">Save a note</a>, then search one of its keywords below. Lodestar runs the selected local tool and stores the actual result in this task’s trace.</p>
        </div>
        <form className="agent-task-form" onSubmit={submitTask}>
          <label htmlFor="agent-task-input">Task description</label>
          <textarea
            id="agent-task-input"
            value={taskInput}
            onChange={(event) => setTaskInput(event.target.value)}
            required
            maxLength={4_000}
            rows={2}
            placeholder="Describe the task for the selected planning mode."
          />
          <p className="agent-input-hint">
            {selectedModelPlanner
              ? "This description is recorded and sent to Nebius as a planning request; saved memories are not included."
              : "This description is recorded with the task. The selected registered tool or approved skill determines the action."}
          </p>
          <label htmlFor="agent-target">Planning and execution mode</label>
          <select
            id="agent-target"
            value={selectedTarget}
            onChange={(event) => {
              const target = event.target.value;
              setSelectedTarget(target);
              if (target.startsWith("tool:")) setToolInput(defaultToolInput(target.slice(5)));
            }}
            disabled={loadingOptions}
          >
            <optgroup label="Registered local tools">
              {tools.map((tool) => (
                <option key={tool.name} value={`tool:${tool.name}`}>
                  {tool.name}{tool.requires_approval ? " · approval required" : " · read-only"}
                </option>
              ))}
            </optgroup>
            <optgroup label="Approved skills">
              {skills.filter((skill) => skill.status === "approved").map((skill) => (
                <option key={skill.id} value={`skill:${skill.id}`}>{skill.name}</option>
              ))}
            </optgroup>
            <optgroup label="Model planning">
              <option value="planner:nebius">Plan with the configured Nebius model</option>
            </optgroup>
          </select>

          {selectedModelPlanner && (
            <div className="agent-selected-tool">
              <span className={`planner-status-pill ${plannerProviderStatus}`}>
                {plannerProviderStatus === "configured_unverified"
                  ? "Configured · unverified"
                  : plannerProviderStatus === "missing_configuration"
                    ? "Provider not configured"
                    : "Provider status unavailable"}
              </span>
              <span>
                The task description is sent to Nebius for a proposal; saved memories are not sent. If planning is unavailable or invalid, the task fails without running tools. Write actions still require separate approval.
              </span>
              <span className="provider-model-reference">
                {plannerProviderStatus === "unavailable"
                  ? "Model status unavailable"
                  : plannerModelId
                    ? <>Model <code>{plannerModelId}</code> · configuration does not confirm live inference</>
                    : "Model ID not configured"}
              </span>
            </div>
          )}

          {selectedTool && (
            <div className="agent-selected-tool">
              <span className={`permission-pill ${selectedTool.permission_level}`}>
                {selectedTool.requires_approval ? "Approval required" : "Read only"}
              </span>
              <span>{selectedTool.description}</span>
            </div>
          )}
          {selectedToolName && (
            <ToolInputFields
              toolName={selectedToolName}
              value={toolInput}
              prefix="agent-tool"
              onChange={setToolInput}
            />
          )}
          {selectedSkillId && (
            <p className="agent-inline-note">This saved skill will run only because it is approved. Any write tool inside it still requires approval for this task.</p>
          )}
          <div className="agent-form-footer">
            <span>{selectedModelPlanner ? "Only locally validated registered tools can run; no memory is added to the model request." : "Only the selected tools run. Memory search happens only when selected directly or in a skill."}</span>
            <button className="agent-primary-button" type="submit" disabled={submitting || loadingOptions || !taskInput.trim()}>
              {submitting ? (selectedModelPlanner ? "Planning…" : "Running…") : selectedModelPlanner ? "Plan with Nebius" : "Run local task"}
            </button>
          </div>
        </form>

        {error && <div className="agent-notice error" role="alert">{error}</div>}
        {submitting && <div className="agent-notice" role="status">Sending to the local runner and waiting for backend confirmation. Completion is shown only after Lodestar returns the saved status.</div>}

        {task && (
          <div className="agent-run" aria-live="polite">
            <div className="agent-run-header">
              <div>
                <span className="card-kicker">TASK #{task.id}</span>
                {task.is_sample && <span className="sample-data-badge">Sample data</span>}
                <h3>{task.task_input}</h3>
              </div>
              <span className={`agent-status ${task.status}`}>{task.status.replaceAll("_", " ")}</span>
            </div>
            {task.result_summary && <p className="agent-result-summary">{task.result_summary}</p>}
            <div className="agent-plan-list">
              {task.plan_steps.map((step) => (
                <article className="agent-plan-step" key={step.step}>
                  <div className="agent-plan-step-heading">
                    <span className="plan-index">{String(step.step).padStart(2, "0")}</span>
                    <div>
                      <strong>{step.description}</strong>
                      <span>{step.tool_name} · {step.permission_level}{step.requires_approval ? " · approval required" : ""}</span>
                    </div>
                    <span className={`step-status ${step.status}`}>{step.status.replaceAll("_", " ")}</span>
                  </div>
                  <details>
                    <summary>Inspect planned input</summary>
                    <pre>{pretty(step.input)}</pre>
                  </details>
                </article>
              ))}
            </div>

            {task.status === "awaiting_approval" && (
              <div className="approval-box">
                <div>
                  <strong>This task needs your approval.</strong>
                  <p>Review the planned tool and its input above before allowing it to run.</p>
                </div>
                <div className="approval-actions">
                  <button className="agent-secondary-button" type="button" onClick={() => void decideApproval(false)} disabled={actingOnApproval}>
                    Deny
                  </button>
                  <button className="agent-primary-button" type="button" onClick={() => void decideApproval(true)} disabled={actingOnApproval}>
                    {actingOnApproval ? "Recording…" : "Approve tool call"}
                  </button>
                </div>
              </div>
            )}

            <div className="trace-heading">
              <div>
                <span className="card-kicker">EXECUTION TRACE</span>
                <h3>What actually happened</h3>
              </div>
              <button className="agent-text-button" type="button" onClick={() => void refreshTrace(task.id)} disabled={traceLoading}>{traceLoading ? "Refreshing…" : "Refresh trace"}</button>
            </div>
            {events.length === 0 ? (
              <div className="agent-empty" role="status">{traceLoading ? "Loading task events…" : "No execution events are available. Refresh the trace to try again."}</div>
            ) : (
              <div className="trace-events">
                {events.map((event) => (
                  <article className="trace-event" key={event.id}>
                    <div className="trace-event-header">
                      <span className="trace-event-name">{event.event_type.replaceAll("_", " ")}</span>
                      <span className={`step-status ${event.status}`}>{event.status.replaceAll("_", " ")}</span>
                      <time dateTime={event.created_at}>{formatTime(event.created_at)}</time>
                    </div>
                    <div className="trace-event-meta">
                      {event.tool_name && <span>{event.tool_name}</span>}
                      {event.approval_required && <span className="permission-pill write">approval required</span>}
                    </div>
                    {event.message && <p>{event.message}</p>}
                    {event.input_data && <details><summary>Actual tool input</summary><pre>{pretty(event.input_data)}</pre></details>}
                    {event.result_data !== null && event.result_data !== undefined && (
                      <details open={event.event_type === "tool_completed"}>
                        <summary>
                          {event.event_type === "plan_created"
                            ? "Validated model-proposed plan"
                            : event.tool_name === "memory_search"
                              ? "Actual keyword matches · local memories"
                              : event.event_type === "plan_validation"
                                ? "Validation outcome"
                                : "Actual tool result"}
                        </summary>
                        <pre>{pretty(event.result_data)}</pre>
                      </details>
                    )}
                  </article>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      <p className="agent-privacy-note">Execution is limited to registered local tools · Every write pauses for approval · Model planning is optional and clearly traced</p>
    </section>
  );
}

export function SkillsView({ apiBaseUrl }: AgentViewsProps) {
  const [tools, setTools] = useState<RegisteredTool[]>([]);
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [actingOnSkillId, setActingOnSkillId] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [steps, setSteps] = useState<SkillStep[]>([{ ...DEFAULT_STEP }]);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const loadSkills = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [toolResponse, skillResponse] = await Promise.all([
        fetch(`${apiBaseUrl}/api/agent/tools`),
        fetch(`${apiBaseUrl}/api/agent/skills`),
      ]);
      if (!toolResponse.ok) throw new Error(await responseError(toolResponse));
      if (!skillResponse.ok) throw new Error(await responseError(skillResponse));
      setTools((await toolResponse.json()) as RegisteredTool[]);
      setSkills((await skillResponse.json()) as SkillItem[]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load skills.");
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void loadSkills();
  }, [loadSkills]);

  function updateStep(index: number, changes: Partial<SkillStep>) {
    setSteps((current) => current.map((step, stepIndex) => (
      stepIndex === index ? { ...step, ...changes } : step
    )));
  }

  async function createSkill(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/skills`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, description, steps }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const created = (await response.json()) as SkillItem;
      setSuccess(`“${created.name}” saved as a draft. Submit it when you are ready to review and approve it.`);
      setName("");
      setDescription("");
      setSteps([{ ...DEFAULT_STEP }]);
      await loadSkills();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not create this skill.");
    } finally {
      setSaving(false);
    }
  }

  async function updateSkill(skillId: number, action: "submit" | "approve" | "disable") {
    if (actingOnSkillId !== null) return;
    if (action === "disable" && !window.confirm("Disable this skill? It will no longer be available to run.")) return;
    setActingOnSkillId(skillId);
    setError(null);
    setSuccess(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/agent/skills/${skillId}/${action}`, { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response));
      const updated = (await response.json()) as SkillItem;
      setSuccess(`“${updated.name}” is now ${updated.status.replaceAll("_", " ")}.`);
      await loadSkills();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not update this skill.");
    } finally {
      setActingOnSkillId(null);
    }
  }

  return (
    <section className="skills-section" id="skills" aria-labelledby="skills-title">
      <div className="skills-heading">
        <div>
          <div className="eyebrow subtle"><span className="eyebrow-line" /> REUSABLE WORKFLOWS</div>
          <h2 id="skills-title">Make a good process<br /><span>easy to repeat.</span></h2>
        </div>
        <p className="section-description">
          Skills are saved sequences of registered local tools. Each skill starts as a draft and needs your explicit approval before it can run.
        </p>
      </div>

      <div className="skills-layout">
        <form className="skill-editor" onSubmit={createSkill}>
          <div className="skill-editor-heading">
            <span className="card-kicker">NEW SKILL · DRAFT</span>
            <h3>Describe a repeatable workflow</h3>
            <p>Only tools from the registry can be included. No code or shell commands are accepted.</p>
          </div>
          <label htmlFor="skill-name">Name</label>
          <input id="skill-name" value={name} onChange={(event) => setName(event.target.value)} required maxLength={80} />
          <label htmlFor="skill-description">Description</label>
          <textarea
            id="skill-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            required
            maxLength={500}
            rows={2}
          />
          <div className="skill-steps-heading">
            <strong>Steps</strong>
            <button
              className="agent-text-button"
              type="button"
              onClick={() => setSteps((current) => [...current, { ...DEFAULT_STEP }])}
              disabled={steps.length >= 10}
            >
              Add step
            </button>
          </div>
          <div className="skill-step-editors">
            {steps.map((step, index) => (
              <div className="skill-step-editor" key={index}>
                <div className="skill-step-title">
                  <span className="plan-index">{String(index + 1).padStart(2, "0")}</span>
                  <button
                    className="memory-delete-button"
                    type="button"
                    onClick={() => setSteps((current) => current.filter((_, stepIndex) => stepIndex !== index))}
                    disabled={steps.length <= 1}
                  >
                    Remove
                  </button>
                </div>
                <label htmlFor={`skill-tool-${index}`}>Registered tool</label>
                <select
                  id={`skill-tool-${index}`}
                  value={step.tool_name}
                  onChange={(event) => updateStep(index, {
                    tool_name: event.target.value,
                    tool_input: defaultToolInput(event.target.value),
                  })}
                >
                  {tools.map((tool) => (
                    <option key={tool.name} value={tool.name}>
                      {tool.name}{tool.requires_approval ? " · requires task approval" : " · read-only"}
                    </option>
                  ))}
                </select>
                <label htmlFor={`skill-step-description-${index}`}>Step description</label>
                <input
                  id={`skill-step-description-${index}`}
                  value={step.description}
                  onChange={(event) => updateStep(index, { description: event.target.value })}
                  required
                  maxLength={200}
                />
                <ToolInputFields
                  toolName={step.tool_name}
                  value={step.tool_input}
                  prefix={`skill-${index}`}
                  onChange={(tool_input) => updateStep(index, { tool_input })}
                />
              </div>
            ))}
          </div>
          <div className="skill-editor-footer">
            <span>Write steps still ask for task-level approval each time they run.</span>
            <button className="agent-primary-button" type="submit" disabled={saving || loading || !name.trim() || !description.trim()}>
              {saving ? "Saving…" : "Save draft"}
            </button>
          </div>
        </form>

        <div className="skills-list-panel">
          <div className="skills-list-heading">
            <div>
              <span className="card-kicker">YOUR SKILLS</span>
              <h3>Review and approve</h3>
            </div>
            <button className="agent-text-button" type="button" onClick={() => void loadSkills()} disabled={loading}>
              {loading ? "Loading…" : "Refresh"}
            </button>
          </div>
          {success && <div className="agent-notice success" role="status">{success}</div>}
          {error && <div className="agent-notice error" role="alert">{error}</div>}
          {loading ? (
            <div className="agent-empty" role="status">Loading skills…</div>
          ) : skills.length === 0 ? (
            <div className="agent-empty">No skills saved yet. Create a draft workflow to get started.</div>
          ) : (
            <div className="skills-list">
              {skills.map((skill) => (
                <article className="skill-card" key={skill.id}>
                  <div className="skill-card-heading">
                    <div>
                      <span className="card-kicker">SKILL #{skill.id}</span>
                      <h4>{skill.name}</h4>
                    </div>
                    <span className={`skill-status ${skill.status}`}>{skill.status.replaceAll("_", " ")}</span>
                  </div>
                  <p className="skill-card-description">{skill.description}</p>
                  <ol className="skill-card-steps">
                    {skill.steps.map((step, index) => {
                      const tool = tools.find((item) => item.name === step.tool_name);
                      return (
                        <li key={`${skill.id}-${index}`}>
                          <span>{step.description}</span>
                          <code>{step.tool_name}</code>
                          {tool?.requires_approval && <span className="permission-pill write">task approval each run</span>}
                        </li>
                      );
                    })}
                  </ol>
                  <details className="skill-definition-details">
                    <summary>Inspect structured steps</summary>
                    <pre>{pretty(skill.steps)}</pre>
                  </details>
                  <div className="skill-card-actions">
                    {skill.status === "draft" && (
                      <button className="agent-secondary-button" type="button" onClick={() => void updateSkill(skill.id, "submit")} disabled={actingOnSkillId !== null}>
                        {actingOnSkillId === skill.id ? "Submitting…" : "Request approval"}
                      </button>
                    )}
                    {skill.status === "pending_approval" && (
                      <button className="agent-primary-button" type="button" onClick={() => void updateSkill(skill.id, "approve")} disabled={actingOnSkillId !== null}>
                        {actingOnSkillId === skill.id ? "Approving…" : "Approve skill"}
                      </button>
                    )}
                    {skill.status !== "disabled" && (
                      <button className="memory-delete-button" type="button" onClick={() => void updateSkill(skill.id, "disable")} disabled={actingOnSkillId !== null}>
                        {actingOnSkillId === skill.id ? "Disabling…" : "Disable"}
                      </button>
                    )}
                  </div>
                  <time className="skill-updated" dateTime={skill.updated_at}>Updated {formatTime(skill.updated_at)}</time>
                </article>
              ))}
            </div>
          )}
        </div>
      </div>
      <p className="agent-privacy-note">Skill definitions and task traces stay in the configured database · Skills run only through registered tools</p>
    </section>
  );
}
