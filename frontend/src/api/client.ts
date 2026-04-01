/**
 * Base API client for all REST calls to the trading bot backend.
 *
 * All endpoints follow the standard envelope:
 *   { data, meta, error }
 *
 * The Vite dev proxy forwards /api/* → http://localhost:8000/api/*
 * so callers just use relative paths: api.get<T>("/symbols")
 */

import type { ApiResponse } from "../types/api";

const BASE = "/api/v1";

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<ApiResponse<T>> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  return res.json() as Promise<ApiResponse<T>>;
}

export const api = {
  get<T>(path: string): Promise<ApiResponse<T>> {
    return request<T>(path);
  },

  post<T>(path: string, body: unknown): Promise<ApiResponse<T>> {
    return request<T>(path, {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  patch<T>(path: string, body: unknown): Promise<ApiResponse<T>> {
    return request<T>(path, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },

  delete<T>(path: string, body?: unknown): Promise<ApiResponse<T>> {
    return request<T>(path, {
      method: "DELETE",
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  },
};
