// Shared types between API contracts and the Next.js frontend.
// Concrete schemas are added incrementally as routes ship in later phases.

export type ISODateString = string;

export type UUID = string;

export interface HealthResponse {
  status: "ok" | "degraded" | "fail";
  version: string;
  timestamp: ISODateString;
}

export interface ApiErrorBody {
  detail?: string;
  message?: string;
  code?: string;
}
