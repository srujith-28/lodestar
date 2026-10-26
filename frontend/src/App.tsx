import { useCallback, useEffect, useState, type FormEvent, type KeyboardEvent } from "react";
import { apiFetch as fetch, setCsrfToken } from "./api";
import { AgentView, SkillsView } from "./AgentViews";
import { HistoryView } from "./HistoryView";
import { HomeView } from "./HomeView";
import { MemoryView } from "./MemoryView";

type HealthStatus = "loading" | "online" | "offline";

type HealthPayload = {
  status: string;
  service: string;
  database: string;
};

type ProviderStatusPayload = {
  provider: string;
  status: "missing_configuration" | "configured_unverified";
  api_key_configured: boolean;
  model_id_configured: boolean;
};

type ProviderCallState = "idle" | "in_progress" | "succeeded" | "failed";

type ChatRole = "user" | "assistant";

type ChatMessage = {
  role: ChatRole;
  content: string;
};

type PageId = "home" | "chat" | "memory" | "agent" | "skills" | "history";

const NAV_ITEMS: Array<{ id: PageId; label: string; group: "workspace" | "activity" }> = [
  { id: "home", label: "Home", group: "workspace" },
  { id: "chat", label: "Chat", group: "workspace" },
  { id: "memory", label: "Memory", group: "workspace" },
  { id: "agent", label: "Agent", group: "workspace" },
  { id: "skills", label: "Skills", group: "workspace" },
  { id: "history", label: "History", group: "activity" },
];

function pageFromHash(): PageId {
  const hash = window.location.hash.replace(/^#/, "").toLowerCase();
  if (hash === "chat") return "chat";
  if (hash === "memory" || hash === "memories") return "memory";
  if (hash === "agent") return "agent";
  if (hash === "skills") return "skills";
  if (hash === "history") return "history";
  return "home";
}

// Use Vite's same-origin proxy for local development; deployments may set an API origin.
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

function ArrowUpRight() {
  return (
    <svg aria-hidden="true" viewBox="0 0 20 20" fill="none">
      <path d="M5 15L15 5M6 5H15V14" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function BrandMark() {
  return (
    <svg aria-hidden="true" viewBox="0 0 32 32" fill="none">
      <circle cx="16" cy="16" r="14.5" stroke="currentColor" />
      <path d="M16 5.5L19 13L26.5 16L19 19L16 26.5L13 19L5.5 16L13 13L16 5.5Z" fill="currentColor" />
      <circle cx="16" cy="16" r="2.2" fill="white" />
    </svg>
  );
}

function NavigationIcon({ page }: { page: PageId }) {
  const paths: Record<PageId, string> = {
    home: "M3.5 10.5 12 3l8.5 7.5M5.5 9v10h13V9M9.5 19v-6h5v6",
    chat: "M4 5.5h16v11H9l-5 4v-15ZM8 9h8M8 13h5",
    memory: "M6 4.5h12v15l-6-3.5-6 3.5v-15ZM9 8h6M9 11h6",
    agent: "M12 3.5 14 9l5.5 2-5.5 2-2 5.5-2-5.5-5.5-2 5.5-2 2-5.5ZM18.5 16l1 2.5 2.5 1-2.5 1-1 2.5-1-2.5-2.5-1 2.5-1 1-2.5Z",
    skills: "M5 6h14M5 12h14M5 18h14M2.5 6h.01M2.5 12h.01M2.5 18h.01",
    history: "M4 7V3.5M4 7h3.5M4.5 7.5A8 8 0 1 1 4 15M12 7v5l3.5 2",
  };
  return <svg aria-hidden="true" viewBox="0 0 24 24" fill="none"><path d={paths[page]} stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>;
}

function WorkspaceApp({ onLogout }: { onLogout: () => Promise<void> }) {
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [status, setStatus] = useState<HealthStatus>("loading");
  const [health, setHealth] = useState<HealthPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatDraft, setChatDraft] = useState("");
  const [chatError, setChatError] = useState<string | null>(null);
  const [isChatLoading, setIsChatLoading] = useState(false);
  const [providerSetup, setProviderSetup] = useState<ProviderStatusPayload | null>(null);
  const [providerStatusLoading, setProviderStatusLoading] = useState(true);
  const [providerCallState, setProviderCallState] = useState<ProviderCallState>("idle");
  const [activePage, setActivePage] = useState<PageId>(() => pageFromHash());
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [historyTaskId, setHistoryTaskId] = useState<number | null>(null);

  async function submitLogout() {
    setLogoutError(null);
    try {
      await onLogout();
    } catch (cause) {
      setLogoutError(cause instanceof Error ? cause.message : "Could not end the session.");
    }
  }

  useEffect(() => {
    const syncPage = () => setActivePage(pageFromHash());
    window.addEventListener("hashchange", syncPage);
    window.addEventListener("popstate", syncPage);
    return () => {
      window.removeEventListener("hashchange", syncPage);
      window.removeEventListener("popstate", syncPage);
    };
  }, []);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
    setSidebarOpen(false);
  }, [activePage]);

  function navigateTo(page: PageId, taskId?: number) {
    const hash = page === "memory" ? "memories" : page;
    if (window.location.hash !== `#${hash}`) {
      window.history.pushState({ page }, "", `#${hash}`);
    }
    setActivePage(page);
    setHistoryTaskId(page === "history" ? taskId ?? null : null);
    setSidebarOpen(false);
  }

  const checkHealth = useCallback(async (signal?: AbortSignal) => {
    setStatus("loading");
    setError(null);

    try {
      const response = await fetch(`${API_BASE_URL}/health`, { signal });
      if (!response.ok) {
        throw new Error(`The API returned ${response.status}.`);
      }

      const payload = (await response.json()) as HealthPayload;
      if (payload.status !== "ok" || payload.database !== "connected") {
        throw new Error("The API is responding, but its database is not ready.");
      }

      setHealth(payload);
      setStatus("online");
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setHealth(null);
      setStatus("offline");
      setError(cause instanceof Error ? cause.message : "Could not reach the local API.");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void checkHealth(controller.signal);
    return () => controller.abort();
  }, [checkHealth]);

  const checkProviderStatus = useCallback(async (signal?: AbortSignal) => {
    setProviderStatusLoading(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/provider/status`, { signal });
      if (!response.ok) throw new Error(`Provider status returned ${response.status}.`);
      setProviderSetup((await response.json()) as ProviderStatusPayload);
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") return;
      setProviderSetup(null);
    } finally {
      if (!signal?.aborted) setProviderStatusLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void checkProviderStatus(controller.signal);
    return () => controller.abort();
  }, [checkProviderStatus]);

  const statusLabel = status === "online" ? "API connected" : status === "offline" ? "Connection issue" : "Checking local API";
  const providerConfigurationMissing = Boolean(
    providerSetup && (!providerSetup.api_key_configured || !providerSetup.model_id_configured),
  );
  const providerLabel = providerCallState === "in_progress"
    ? "Chat request in progress"
    : providerCallState === "succeeded"
      ? "Live reply received"
      : providerCallState === "failed"
        ? providerConfigurationMissing
          ? "Provider not configured · no live call"
          : providerSetup
            ? "Provider request failed"
            : "Chat request failed"
        : providerStatusLoading
          ? "Checking configuration"
          : !providerSetup
            ? "Status unavailable"
            : providerConfigurationMissing
              ? "Provider not configured"
              : "Configured · not verified";

  const providerDescription = providerCallState === "in_progress"
    ? "The backend is processing your chat request. No response is claimed until Token Factory returns one."
    : providerCallState === "succeeded"
      ? "Token Factory returned a live reply. Hackathon model eligibility still needs verification."
      : providerCallState === "failed"
        ? providerConfigurationMissing
          ? "The chat request was rejected because provider configuration is incomplete; no live model call was made."
          : providerSetup
            ? "The configured provider request failed. No successful inference is recorded."
            : "The chat request failed; provider call status could not be confirmed."
        : providerStatusLoading
          ? "Reading server-side configuration; this does not call the model."
          : !providerSetup
            ? "Could not read provider configuration from the backend."
            : !providerSetup.api_key_configured
              ? "Add a valid NEBIUS_API_KEY on the server to make a live request. No inference has been verified."
              : !providerSetup.model_id_configured
                ? "Select a verified Nemotron model ID on the server before making a live request."
                : "Configuration is present, but no live inference check runs until you send a chat message.";

  async function requestChatReply(nextMessages: ChatMessage[]) {
    if (isChatLoading) return;
    setChatMessages(nextMessages);
    setChatError(null);
    setIsChatLoading(true);
    setProviderCallState("in_progress");

    try {
      const response = await fetch(`${API_BASE_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: nextMessages }),
      });
      const payload = await response.json().catch(() => null);

      if (!response.ok) {
        const detail = payload && typeof payload.detail === "string" ? payload.detail : null;
        throw new Error(detail ?? `Chat request failed (${response.status}).`);
      }

      if (!payload || typeof payload.reply !== "string" || !payload.reply.trim()) {
        throw new Error("The chat service returned no text. Please try again.");
      }

      setChatMessages([...nextMessages, { role: "assistant", content: payload.reply.trim() }]);
      setProviderCallState("succeeded");
    } catch (cause) {
      setProviderCallState("failed");
      setChatError(
        cause instanceof Error
          ? cause.message
          : "Could not reach the chat service. Check the backend connection and try again.",
      );
    } finally {
      setIsChatLoading(false);
    }
  }

  async function sendChatMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = chatDraft.trim();
    if (!content || isChatLoading) return;
    setChatDraft("");
    await requestChatReply([...chatMessages, { role: "user", content }]);
  }

  async function retryLastMessage() {
    const lastMessage = chatMessages.at(-1);
    if (isChatLoading || lastMessage?.role !== "user") return;
    await requestChatReply(chatMessages);
  }

  function handleChatKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  }

  const activeLabel = NAV_ITEMS.find((item) => item.id === activePage)?.label ?? "Home";
  const providerPillTone = providerCallState === "succeeded"
    ? "success"
    : providerCallState === "failed"
      ? providerConfigurationMissing ? "warning" : "error"
      : providerSetup?.status === "configured_unverified"
        ? "warning"
        : "neutral";

  return (
    <div className="app-shell">
      {sidebarOpen && <button className="sidebar-backdrop" type="button" aria-label="Close navigation" onClick={() => setSidebarOpen(false)} />}
      <aside className={`app-sidebar ${sidebarOpen ? "open" : ""}`} aria-label="Lodestar workspace navigation">
        <a className="brand" href="#home" onClick={(event) => { event.preventDefault(); navigateTo("home"); }} aria-label="Lodestar home">
          <span className="brand-mark"><BrandMark /></span>
          <span className="brand-word">lodestar</span>
        </a>
        <p className="brand-caption">Personal AI workspace</p>

        <nav className="sidebar-nav" aria-label="Main navigation">
          <span className="sidebar-group-label">WORKSPACE</span>
          {NAV_ITEMS.filter((item) => item.group === "workspace").map((item) => (
            <a
              className={`sidebar-link ${activePage === item.id ? "active" : ""}`}
              href={`#${item.id === "memory" ? "memories" : item.id}`}
              key={item.id}
              aria-current={activePage === item.id ? "page" : undefined}
              onClick={(event) => { event.preventDefault(); navigateTo(item.id); }}
            >
              <NavigationIcon page={item.id} /><span>{item.label}</span>
              {item.id === "memory" && <span className="nav-accent" aria-hidden="true" />}
            </a>
          ))}
          <span className="sidebar-group-label activity-nav-label">ACTIVITY</span>
          {NAV_ITEMS.filter((item) => item.group === "activity").map((item) => (
            <a
              className={`sidebar-link ${activePage === item.id ? "active" : ""}`}
              href={`#${item.id}`}
              key={item.id}
              aria-current={activePage === item.id ? "page" : undefined}
              onClick={(event) => { event.preventDefault(); navigateTo(item.id); }}
            >
              <NavigationIcon page={item.id} /><span>{item.label}</span>
            </a>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <div className="sidebar-health-row">
            <span className={`health-indicator ${status}`} aria-hidden="true" />
            <span><strong>{statusLabel}</strong><small>Local API + database</small></span>
            <button className="icon-button sidebar-refresh" type="button" onClick={() => void checkHealth()} disabled={status === "loading"} aria-label="Refresh local API status" title="Refresh local API status">↻</button>
          </div>
          <p>Memory is saved only when you choose.</p>
        </div>
      </aside>

      <main className="workspace-main">
        <header className="workspace-topbar">
          <button className="mobile-menu-button" type="button" aria-label="Open navigation" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen((open) => !open)}>
            <span /><span /><span />
          </button>
          <div className="workspace-breadcrumb"><span>Workspace</span><span aria-hidden="true">/</span><strong>{activeLabel}</strong></div>
          <div className="topbar-status">
            <button className={`topbar-status-chip local ${status}`} type="button" onClick={() => void checkHealth()} title={error ?? "Refresh local API and database status"}>
              <span className={`health-indicator ${status}`} aria-hidden="true" />
              <span>{status === "online" ? "Local system ready" : status === "offline" ? "Local system offline" : "Checking local system"}</span>
            </button>
            <span className={`topbar-status-chip provider ${providerPillTone}`} title={providerDescription}>
              <span className="provider-status-dot" aria-hidden="true" />
              <span>{providerLabel}</span>
            </span>
            <button className="logout-button" type="button" onClick={() => void submitLogout()}>Log out</button>
          </div>
        </header>
        {logoutError && <p className="logout-error" role="alert">{logoutError}</p>}

        <div className="page-content">
          {activePage === "home" && (
            <HomeView
              apiBaseUrl={API_BASE_URL}
              healthStatus={status}
              healthMessage={status === "online" ? `${health?.service ?? "Lodestar API"} and its database are responding.` : status === "offline" ? error ?? "Start the backend, then refresh this status." : "Checking the local API and database."}
              providerSetup={providerSetup}
              providerStatusLoading={providerStatusLoading}
              providerCallState={providerCallState}
              onRefreshHealth={() => void checkHealth()}
              onRefreshProvider={() => void checkProviderStatus()}
              onNavigate={navigateTo}
            />
          )}

          {activePage === "chat" && (
            <section className="chat-section" id="chat" aria-labelledby="chat-title">
              <div className="page-section-heading">
                <div><p className="eyebrow"><span className="eyebrow-line" /> A PRIVATE CHAT WORKSPACE</p><h1 id="chat-title">Think it through.</h1></div>
                <p className="heading-side-note">Messages route through the backend to Nebius Token Factory when configured. Replies appear only when returned by the provider.</p>
              </div>

              <div className="chat-panel">
                <div className="chat-panel-header">
                  <span className="chat-mark"><BrandMark /></span>
                  <div className="chat-panel-title"><strong>LODESTAR CHAT</strong><span>Server-routed · credentials stay on the backend</span></div>
                  <span className={`provider-pill ${providerCallState === "succeeded" ? "success" : providerCallState === "failed" ? providerConfigurationMissing ? "warning" : "error" : ""}`}><span className="status-dot" />{providerLabel}</span>
                </div>

                <div className="provider-setup-note" role="status" aria-live="polite">
                  <div><strong>{providerSetup?.provider ?? "Nebius Token Factory"}</strong><span>{providerDescription}</span></div>
                  <button className="text-button" type="button" onClick={() => void checkProviderStatus()} disabled={providerStatusLoading}>{providerStatusLoading ? "Checking…" : "Refresh status"}</button>
                </div>

                <div className="chat-transcript" aria-live="polite" aria-relevant="additions text" aria-busy={isChatLoading}>
                  {chatMessages.length === 0 ? (
                    <div className="chat-empty">
                      <span className="empty-star"><BrandMark /></span>
                      <strong>Your conversation starts here.</strong>
                      <p>No sample AI replies are shown. Send a prompt when provider access is configured.</p>
                      <div className="prompt-suggestions" aria-label="Prompt suggestions">
                        {["Help me organize a project", "Summarize a decision I am considering", "Help me plan my week"].map((prompt) => (
                          <button type="button" key={prompt} onClick={() => setChatDraft(prompt)} disabled={isChatLoading}>{prompt}</button>
                        ))}
                      </div>
                    </div>
                  ) : (
                    <div className="message-list">
                      {chatMessages.map((message, index) => (
                        <article className={`chat-message ${message.role}`} key={`${message.role}-${index}`}>
                          <span className="message-author">{message.role === "user" ? "YOU" : "LODESTAR"}</span><p>{message.content}</p>
                        </article>
                      ))}
                      {isChatLoading && <div className="chat-pending" role="status"><span className="loading-dots"><i /><i /><i /></span><span>Waiting for the configured provider…</span></div>}
                    </div>
                  )}
                </div>

                {chatError && (
                  <div className="chat-error" role="alert"><span>{chatError}</span><button className="text-button" type="button" onClick={retryLastMessage} disabled={isChatLoading}>Retry request</button></div>
                )}

                <form className="chat-composer" onSubmit={sendChatMessage}>
                  <label className="sr-only" htmlFor="chat-input">Your message</label>
                  <textarea id="chat-input" value={chatDraft} onChange={(event) => setChatDraft(event.target.value)} onKeyDown={handleChatKeyDown} maxLength={8_000} rows={3} placeholder="What would you like to work through?" disabled={isChatLoading} />
                  <div className="composer-footer"><span>Enter to send · Shift + Enter for a new line</span><button className="send-button" type="submit" disabled={isChatLoading || !chatDraft.trim()}>{isChatLoading ? "Sending…" : "Send message"}{isChatLoading ? <span className="send-spinner" /> : <ArrowUpRight />}</button></div>
                </form>
                <div className="chat-privacy-note"><span className="lock-mark" aria-hidden="true">⌑</span>Chat history stays in this page and is not saved. Memory is never attached automatically.</div>
              </div>
            </section>
          )}

          {activePage === "memory" && <MemoryView apiBaseUrl={API_BASE_URL} />}
          {activePage === "agent" && <AgentView apiBaseUrl={API_BASE_URL} />}
          {activePage === "skills" && <SkillsView apiBaseUrl={API_BASE_URL} />}
          {activePage === "history" && <HistoryView apiBaseUrl={API_BASE_URL} initialTaskId={historyTaskId} onNavigate={(page) => navigateTo(page)} />}

          <footer className="workspace-footer"><span className="footer-brand"><BrandMark /> lodestar</span><span>Remember what matters. Pick up where you left off.</span><span>Local-first · User-controlled</span></footer>
        </div>
      </main>
    </div>
  );
}

type AuthState = "checking" | "setup_required" | "login_required" | "authenticated" | "unavailable";

type AuthSession = {
  configured: boolean;
  authenticated: boolean;
};

function AuthGate() {
  const [state, setState] = useState<AuthState>("checking");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshSession = useCallback(async () => {
    setState("checking");
    setError(null);
    try {
      const sessionResponse = await fetch(`${API_BASE_URL}/api/auth/session`);
      if (!sessionResponse.ok) throw new Error("The authentication service could not be reached.");
      const session = (await sessionResponse.json()) as AuthSession;
      if (!session.configured) {
        setCsrfToken(null);
        setState("setup_required");
        return;
      }
      const csrfResponse = await fetch(`${API_BASE_URL}/api/auth/csrf`);
      if (!csrfResponse.ok) throw new Error("The session protection token could not be prepared.");
      const csrfPayload = (await csrfResponse.json()) as { csrf_token: string };
      setCsrfToken(csrfPayload.csrf_token);
      setState(session.authenticated ? "authenticated" : "login_required");
    } catch (cause) {
      setCsrfToken(null);
      setState("unavailable");
      setError(cause instanceof Error ? cause.message : "Could not connect to the authentication service.");
    }
  }, []);

  useEffect(() => {
    void refreshSession();
    const handleUnauthorized = () => {
      setPassword("");
      void refreshSession().then(() => setError("Your session ended. Sign in again to continue."));
    };
    window.addEventListener("lodestar:unauthorized", handleUnauthorized);
    return () => window.removeEventListener("lodestar:unauthorized", handleUnauthorized);
  }, [refreshSession]);

  async function submitLogin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!password || busy) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(typeof payload?.detail === "string" ? payload.detail : `Sign-in failed (${response.status}).`);
      }
      setCsrfToken(payload.csrf_token);
      setPassword("");
      setState("authenticated");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Sign-in failed. Try again.");
    } finally {
      setBusy(false);
    }
  }

  async function logout(): Promise<void> {
    setBusy(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/logout`, { method: "POST" });
      if (!response.ok) throw new Error("Could not end the session. Please retry while connected to the API.");
      setPassword("");
      await refreshSession();
    } finally {
      setBusy(false);
    }
  }

  if (state === "authenticated") return <WorkspaceApp onLogout={logout} />;

  return (
    <main className="auth-shell">
      <section className="auth-card" aria-labelledby="auth-title">
        <a className="auth-brand" href="#home" aria-label="Lodestar">
          <span className="auth-brand-mark" aria-hidden="true">✧</span>
          <span>lodestar</span>
        </a>
        <p className="eyebrow"><span className="eyebrow-line" /> PERSONAL AI WORKSPACE</p>
        {state === "checking" ? (
          <div className="auth-state" role="status"><span className="auth-spinner" />Checking your session…</div>
        ) : state === "setup_required" ? (
          <>
            <h1 id="auth-title">Set up private access.</h1>
            <p className="auth-copy">Lodestar is locked until a demo password and session signing secret are configured on the backend.</p>
            <div className="auth-guidance" role="note">
              <strong>Configure the backend locally</strong>
              <span>Copy the values from <code>.env.example</code> into your ignored root <code>.env</code>, replace both authentication placeholders with private values, then restart the backend.</span>
              <span>Use a unique password of at least 12 characters and a random session secret of at least 32 characters. Never put either value in the frontend or commit them.</span>
            </div>
          </>
        ) : state === "unavailable" ? (
          <>
            <h1 id="auth-title">The workspace is unavailable.</h1>
            <p className="auth-copy">Lodestar could not verify the backend session, so private workspace data remains closed.</p>
            {error && <p className="auth-error" role="alert">{error}</p>}
            <button className="button-primary auth-submit" type="button" onClick={() => void refreshSession()}>Try again</button>
          </>
        ) : (
          <>
            <h1 id="auth-title">Welcome back.</h1>
            <p className="auth-copy">Sign in to open your private Lodestar workspace.</p>
            <form className="auth-form" onSubmit={submitLogin}>
              <label htmlFor="auth-password">Demo password</label>
              <input id="auth-password" type="password" autoComplete="current-password" minLength={12} maxLength={256} value={password} onChange={(event) => setPassword(event.target.value)} required disabled={busy} />
              {error && <p className="auth-error" role="alert">{error}</p>}
              <button className="button-primary auth-submit" type="submit" disabled={busy || password.length < 12}>{busy ? "Signing in…" : "Sign in"}</button>
            </form>
            <p className="auth-footnote">One private demo account · Your password stays on the backend.</p>
          </>
        )}
        <footer className="auth-footer"><span>Local-first</span><span>·</span><span>Memory is user-controlled</span></footer>
      </section>
    </main>
  );
}

function App() {
  return <AuthGate />;
}

export default App;
