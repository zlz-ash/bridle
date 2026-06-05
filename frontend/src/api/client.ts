import axios, { AxiosError } from 'axios';

export const apiClient = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  headers: { 'Content-Type': 'application/json' },
});

export interface ApiErrorEnvelope {
  error: {
    code: string;
    message: string;
    resource?: string;
    details?: Record<string, unknown>;
  };
}

export class ApiError extends Error {
  code: string;
  status: number;
  details?: Record<string, unknown>;
  constructor(message: string, code: string, status: number, details?: Record<string, unknown>) {
    super(message);
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

export function parseApiError(err: unknown): ApiError {
  if (err instanceof AxiosError) {
    const resp = err.response;
    if (resp?.data && typeof resp.data === 'object') {
      const data = resp.data as Record<string, unknown>;
      if ('error' in data && data.error && typeof data.error === 'object') {
        const env = resp.data as ApiErrorEnvelope;
        return new ApiError(env.error.message, env.error.code, resp.status, env.error.details);
      }
      if ('code' in data && 'message' in data) {
        return new ApiError(
          String(data.message),
          String(data.code),
          resp.status,
          data.details as Record<string, unknown> | undefined,
        );
      }
    }
    return new ApiError(err.message, 'network_error', resp?.status ?? 0);
  }
  return new ApiError(String(err), 'unknown_error', 0);
}
