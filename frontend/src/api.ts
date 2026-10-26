/** Authenticated fetch wrapper for the Lodestar API. */

let csrfToken: string | null = null;

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const AUTH_PATHS = ["/api/auth/session", "/api/auth/csrf", "/api/auth/login"];

export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!SAFE_METHODS.has(method) && csrfToken) headers.set("X-CSRF-Token", csrfToken);

  const response = await fetch(input, {
    ...init,
    headers,
    credentials: "include",
  });

  const requestUrl = typeof input === "string" ? input : input instanceof URL ? input.pathname : input.url;
  if (response.status === 401 && requestUrl.includes("/api/") && !AUTH_PATHS.some((path) => requestUrl.includes(path))) {
    setCsrfToken(null);
    window.dispatchEvent(new Event("lodestar:unauthorized"));
  }

  return response;
}
