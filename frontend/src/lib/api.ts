/**
 * Typed client for the local Python API.
 *
 * Rules that keep the UI honest:
 *  - every call goes to this origin (`/v1/...`), never to a third party;
 *  - the bearer token comes from the `<meta name="pc-agent-token">` tag the
 *    Python server injects into the shell (loopback only, never in a URL);
 *  - a failed call returns a typed `ApiError`, and the UI renders an error or an
 *    "unavailable" state. Nothing is silently replaced with sample data.
 */

const tokenMeta = () =>
  document.querySelector<HTMLMetaElement>('meta[name="pc-agent-token"]')?.content?.trim() ?? "";

export const TOKEN = tokenMeta();
export const HAS_TOKEN = TOKEN.length > 0;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

type Envelope<T> = { ok: boolean; data?: T; error?: { code?: string; message?: string } };

export async function apiGet<T>(path: string, init?: { signal?: AbortSignal }): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  const response = await fetch(path, { headers, signal: init?.signal, credentials: "omit" });
  let payload: Envelope<T> | null = null;
  try {
    payload = (await response.json()) as Envelope<T>;
  } catch {
    payload = null;
  }
  if (!response.ok) {
    throw new ApiError(
      response.status,
      payload?.error?.code ?? "http_error",
      payload?.error?.message ?? `Request failed with status ${response.status}`,
    );
  }
  if (!payload || payload.ok !== true) {
    throw new ApiError(response.status, payload?.error?.code ?? "bad_envelope", payload?.error?.message ?? "Malformed API response");
  }
  return payload.data as T;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", Accept: "application/json" };
  if (TOKEN) headers.Authorization = `Bearer ${TOKEN}`;
  const response = await fetch(path, { method: "POST", headers, body: JSON.stringify(body ?? {}), credentials: "omit" });
  const payload = (await response.json().catch(() => null)) as Envelope<T> | null;
  if (!response.ok || !payload || payload.ok !== true) {
    throw new ApiError(
      response.status,
      payload?.error?.code ?? "http_error",
      payload?.error?.message ?? `Request failed with status ${response.status}`,
    );
  }
  return payload.data as T;
}

/** Small helper for GET queries with a query string. */
export function qs(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
