const SESSION_KEY = "egpt_access_token";

let memory: string | null | undefined = undefined;

export function getAccessToken(): string | null {
  if (typeof window === "undefined") return memory ?? null;
  if (memory === undefined) {
    memory = sessionStorage.getItem(SESSION_KEY);
  }
  return memory;
}

export function setAccessToken(token: string | null): void {
  memory = token;
  if (typeof window === "undefined") return;
  if (token) sessionStorage.setItem(SESSION_KEY, token);
  else sessionStorage.removeItem(SESSION_KEY);
}
