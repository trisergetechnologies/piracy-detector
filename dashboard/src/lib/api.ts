import axios from "axios";
import type {
  HealthResponse,
  ScanParams,
  TelegramScanParams,
  ScanStartResult,
  ScanStatusResult,
  ScanDetailResponse,
  HistoryResponse,
  ChannelsResponse,
  TakedownReportParams,
  TakedownReport,
  MonitorRecord,
  AlertRecord,
  DetectedVideo,
} from "./types";

const client = axios.create({
  baseURL: "/api",
  timeout: 30_000,
  headers: { "Content-Type": "application/json" },
});

// ---- Health ----

export async function getHealth(): Promise<HealthResponse> {
  const { data } = await client.get<HealthResponse>("/health");
  return data;
}

// ---- Scans (async: POST returns immediately, poll status) ----

export async function startScan(params: ScanParams): Promise<ScanStartResult> {
  const endpointMap: Record<string, string> = {
    both: "/scan",
    defamatory: "/scan/defamatory",
    piracy: "/scan/piracy",
  };
  const url = endpointMap[params.scan_type] || "/scan";
  const { data } = await client.post<ScanStartResult>(url, params);
  return data;
}

export async function startTelegramScan(
  params: TelegramScanParams
): Promise<ScanStartResult> {
  const { data } = await client.post<ScanStartResult>("/scan/telegram", params);
  return data;
}

export async function getTelegramSeeds(
  entityName?: string,
  limit = 200
): Promise<{ ok: boolean; seeds: any[]; telegram?: any }> {
  const { data } = await client.get("/telegram/seeds", {
    params: { entity_name: entityName, limit },
  });
  return data;
}

export async function getScanStatus(scanId: string): Promise<ScanStatusResult> {
  const { data } = await client.get<ScanStatusResult>(
    `/scan/status/${scanId}`
  );
  return data;
}

export async function getScanResults(
  scanId: string
): Promise<ScanDetailResponse> {
  const { data } = await client.get<ScanDetailResponse>(
    `/results/${scanId}`
  );
  return data;
}

export async function getHistory(limit = 50): Promise<HistoryResponse> {
  const { data } = await client.get<HistoryResponse>("/history", {
    params: { limit },
  });
  return data;
}

// ---- Channels ----

export async function getChannels(limit = 50): Promise<ChannelsResponse> {
  const { data } = await client.get<ChannelsResponse>("/channels", {
    params: { limit },
  });
  return data;
}

// ---- Reports ----

export async function generateTakedownReport(
  params: TakedownReportParams
): Promise<TakedownReport> {
  const { data } = await client.post<TakedownReport>(
    "/report/takedown",
    params
  );
  return data;
}

// ---- Phase D: monitors / alerts / review ----

export async function listMonitors(): Promise<{ ok: boolean; monitors: MonitorRecord[] }> {
  const { data } = await client.get("/monitors");
  return data;
}

export async function createMonitor(body: Record<string, any>): Promise<{
  ok: boolean;
  monitor_id: string;
  monitor: MonitorRecord;
  error?: string;
}> {
  const { data } = await client.post("/monitors", body);
  return data;
}

export async function updateMonitor(
  monitorId: string,
  body: Record<string, any>
): Promise<{ ok: boolean; monitor: MonitorRecord }> {
  const { data } = await client.patch(`/monitors/${monitorId}`, body);
  return data;
}

export async function deleteMonitor(monitorId: string): Promise<{ ok: boolean }> {
  const { data } = await client.delete(`/monitors/${monitorId}`);
  return data;
}

export async function runMonitorNow(monitorId: string): Promise<{ ok: boolean; message?: string }> {
  const { data } = await client.post(`/monitors/${monitorId}/run`);
  return data;
}

export async function listAlerts(unreadOnly = false): Promise<{
  ok: boolean;
  alerts: AlertRecord[];
}> {
  const { data } = await client.get("/alerts", {
    params: { unread: unreadOnly },
  });
  return data;
}

export async function markAlertsRead(payload: {
  alert_id?: string;
  all?: boolean;
  read?: boolean;
}): Promise<{ ok: boolean; marked?: number }> {
  const { data } = await client.post("/alerts/read", payload);
  return data;
}

export async function getReviewQueue(
  status = "pending",
  entityName?: string
): Promise<{ ok: boolean; items: DetectedVideo[] }> {
  const { data } = await client.get("/review", {
    params: { status, entity_name: entityName },
  });
  return data;
}

export async function submitReview(payload: {
  scan_id: string;
  video_id: string;
  decision: "confirmed" | "false_positive" | "pending";
  note?: string;
}): Promise<{ ok: boolean; item?: DetectedVideo; error?: string }> {
  const { data } = await client.post("/review", payload);
  return data;
}
