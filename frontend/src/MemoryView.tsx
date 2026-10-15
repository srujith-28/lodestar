import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

type MemoryItem = {
  id: number;
  content: string;
  category: string;
  source: string;
  created_at: string;
  updated_at: string;
};

type MemoryDraft = {
  content: string;
  category: string;
  source: string;
};

type MemoryViewProps = {
  apiBaseUrl: string;
};

const EMPTY_DRAFT: MemoryDraft = { content: "", category: "general", source: "user" };

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
  return `Memory request failed (${response.status}).`;
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Timestamp unavailable" : timestamp.toLocaleString();
}

export function MemoryView({ apiBaseUrl }: MemoryViewProps) {
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [draft, setDraft] = useState<MemoryDraft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const latestRequest = useRef(0);

  const loadMemories = useCallback(async (query: string) => {
    const requestId = ++latestRequest.current;
    setLoading(true);
    setError(null);
    const endpoint = query.trim()
      ? `${apiBaseUrl}/api/memories/search?q=${encodeURIComponent(query.trim())}`
      : `${apiBaseUrl}/api/memories`;

    try {
      const response = await fetch(endpoint);
      if (!response.ok) throw new Error(await responseError(response));
      const payload = (await response.json()) as MemoryItem[];
      if (requestId === latestRequest.current) setMemories(payload);
    } catch (cause) {
      if (requestId === latestRequest.current) {
        setError(cause instanceof Error ? cause.message : "Could not load saved memories.");
      }
    } finally {
      if (requestId === latestRequest.current) setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void loadMemories("");
  }, [loadMemories]);

  function startNewMemory() {
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
    setError(null);
    setSuccess(null);
    setFormOpen(true);
  }

  function startEditing(memory: MemoryItem) {
    setEditingId(memory.id);
    setDraft({ content: memory.content, category: memory.category, source: memory.source });
    setError(null);
    setSuccess(null);
    setFormOpen(true);
  }

  async function saveMemory(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError(null);
    setSuccess(null);

    try {
      const response = await fetch(
        editingId === null
          ? `${apiBaseUrl}/api/memories`
          : `${apiBaseUrl}/api/memories/${editingId}`,
        {
          method: editingId === null ? "POST" : "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(draft),
        },
      );
      if (!response.ok) throw new Error(await responseError(response));
      setSuccess(editingId === null ? "Memory saved." : "Memory updated.");
      setFormOpen(false);
      setEditingId(null);
      setDraft(EMPTY_DRAFT);
      await loadMemories(search);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save this memory.");
    } finally {
      setSaving(false);
    }
  }

  async function deleteMemory(memory: MemoryItem) {
    const confirmed = window.confirm("Delete this memory permanently? This cannot be undone.");
    if (!confirmed) return;

    setDeletingId(memory.id);
    setError(null);
    setSuccess(null);
    try {
      const response = await fetch(`${apiBaseUrl}/api/memories/${memory.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response));
      setSuccess("Memory permanently deleted.");
      if (editingId === memory.id) {
        setFormOpen(false);
        setEditingId(null);
      }
      await loadMemories(search);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not delete this memory.");
    } finally {
      setDeletingId(null);
    }
  }

  function cancelForm() {
    setFormOpen(false);
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
  }

  return (
    <section className="memory-section" id="memories" aria-labelledby="memory-title">
      <div className="memory-heading">
        <div>
          <div className="eyebrow subtle"><span className="eyebrow-line" /> YOUR PRIVATE LIBRARY</div>
          <h2 id="memory-title">Keep what<br /><span>matters to you.</span></h2>
        </div>
        <div className="memory-heading-actions">
          <p className="section-description">
            Save only what you choose. These notes stay in your configured local database and are not sent to AI services.
          </p>
          <button className="memory-primary-button" type="button" onClick={startNewMemory}>
            Add a memory <span aria-hidden="true">＋</span>
          </button>
        </div>
      </div>

      <div className="memory-panel">
        <form
          className="memory-search"
          onSubmit={(event) => {
            event.preventDefault();
            setSuccess(null);
            void loadMemories(search);
          }}
        >
          <label className="sr-only" htmlFor="memory-search-input">Search memories</label>
          <input
            id="memory-search-input"
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            maxLength={100}
            placeholder="Search saved memories"
          />
          <button className="memory-secondary-button" type="submit" disabled={loading}>
            {loading ? "Searching…" : "Search"}
          </button>
          {search && (
            <button
              className="memory-text-button"
              type="button"
              onClick={() => {
                setSearch("");
                void loadMemories("");
              }}
            >
              Clear
            </button>
          )}
        </form>

        {success && <div className="memory-notice success" role="status">{success}</div>}
        {error && <div className="memory-notice error" role="alert">{error}</div>}

        {formOpen && (
          <form className="memory-form" onSubmit={saveMemory}>
            <div className="memory-form-heading">
              <div>
                <span className="card-kicker">{editingId === null ? "NEW MEMORY" : "EDIT MEMORY"}</span>
                <h3>{editingId === null ? "What would you like to remember?" : "Update this memory"}</h3>
              </div>
              <button className="memory-text-button" type="button" onClick={cancelForm}>Cancel</button>
            </div>
            <label htmlFor="memory-content">Memory</label>
            <textarea
              id="memory-content"
              value={draft.content}
              onChange={(event) => setDraft({ ...draft, content: event.target.value })}
              required
              maxLength={5_000}
              rows={4}
              placeholder="Write a detail you want to keep. Do not enter passwords, API keys, or other credentials."
            />
            <div className="memory-form-fields">
              <div>
                <label htmlFor="memory-category">Category</label>
                <input
                  id="memory-category"
                  value={draft.category}
                  onChange={(event) => setDraft({ ...draft, category: event.target.value })}
                  required
                  maxLength={60}
                />
              </div>
              <div>
                <label htmlFor="memory-source">Source</label>
                <input
                  id="memory-source"
                  value={draft.source}
                  onChange={(event) => setDraft({ ...draft, source: event.target.value })}
                  required
                  maxLength={80}
                />
              </div>
            </div>
            <div className="memory-form-footer">
              <span>Saved only after you choose Save memory.</span>
              <button className="memory-primary-button" type="submit" disabled={saving}>
                {saving ? "Saving…" : "Save memory"}
              </button>
            </div>
          </form>
        )}

        <div className="memory-list" aria-live="polite" aria-busy={loading}>
          {loading ? (
            <div className="memory-state" role="status">Loading saved memories…</div>
          ) : memories.length === 0 ? (
            <div className="memory-state empty">
              <strong>{search ? "No memories match that search." : "No memories saved yet."}</strong>
              <p>{search ? "Try a different keyword or clear the search." : "Add a note when you are ready. Nothing is saved automatically."}</p>
            </div>
          ) : (
            memories.map((memory) => (
              <article className="memory-card" key={memory.id}>
                <div className="memory-card-meta">
                  <span className="memory-category">{memory.category}</span>
                  <span>From {memory.source}</span>
                </div>
                <p className="memory-content">{memory.content}</p>
                <div className="memory-card-footer">
                  <time dateTime={memory.updated_at}>Updated {formatTimestamp(memory.updated_at)}</time>
                  <div className="memory-card-actions">
                    <button className="memory-text-button" type="button" onClick={() => startEditing(memory)}>
                      Edit
                    </button>
                    <button
                      className="memory-delete-button"
                      type="button"
                      onClick={() => void deleteMemory(memory)}
                      disabled={deletingId === memory.id}
                    >
                      {deletingId === memory.id ? "Deleting…" : "Delete"}
                    </button>
                  </div>
                </div>
              </article>
            ))
          )}
        </div>
      </div>
      <p className="memory-privacy-note"><span aria-hidden="true">⌑</span> Stored locally in the configured database · Never sent to the chat provider in this milestone</p>
    </section>
  );
}
