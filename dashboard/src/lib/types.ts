export interface HealthResponse {
  ok: boolean;
  service: string;
  database: string;
  youtube_api: Record<string, any>;
  telegram?: {
    configured?: boolean;
    api_id_set?: boolean;
    api_hash_set?: boolean;
    session_string?: boolean;
    session_path?: string;
  };
  timestamp: string;
}

export interface TelegramScanParams {
  entity_name: string;
  entity_type?: string;
  telegram_seeds?: string[];
  /** Rights-holder Telegram @channels — never flagged as piracy */
  official_telegram?: string[];
  bridge_youtube_scan_id?: string;
  official_channel_ids?: string[];
  official_playlist_ids?: string[];
  enable_fingerprinting?: boolean;
  min_confidence?: number;
  include_stored_seeds?: boolean;
}

export interface ScanParams {
  entity_name: string;
  entity_type: "educator" | "politician" | "influencer" | "generic";
  scan_type: "both" | "defamatory" | "piracy";
  date_from?: string;
  date_to?: string;
  max_results?: number;
  min_confidence?: number;
  analyze_thumbnails?: boolean;
  known_channels_to_monitor?: string[];
  official_channel_ids?: string[];
  official_playlist_ids?: string[];
  enable_deep_crawl?: boolean;
  enable_fingerprinting?: boolean;
  original_content_titles?: string[];
}

export interface DetectedVideo {
  video_id: string;
  title: string;
  url: string;
  channel_title: string;
  channel_id: string;
  channel_url: string;
  published_at: string;
  views: number;
  likes: number;
  comments: number;
  duration: string;
  thumbnail_url: string;
  classification: "DEFAMATORY" | "PIRATED" | "LEGITIMATE";
  confidence: number;
  severity: "critical" | "high" | "medium" | "low";
  reason: string;
  final_score: number;
  thumbnail_analysis?: string;
  sub_scores?: Record<string, number>;
  heuristic_score?: number;
  heuristic_signals?: string[];
  catalog_match?: {
    official_video_id: string;
    official_title: string;
    similarity: number;
  } | null;
  discovery_source?: string;
  fingerprint_match?: {
    official_video_id?: string;
    official_title?: string;
    official_channel_id?: string;
    similarity: number;
    is_match?: boolean;
    is_strong?: boolean;
    backend?: string;
    error?: string;
  } | null;
  platform?: string;
  review_status?: "pending" | "confirmed" | "false_positive";
  review_note?: string;
  scan_id?: string;
  message_id?: number;
}

export interface MonitorRecord {
  monitor_id: string;
  entity_name: string;
  entity_type?: string;
  platforms: string[];
  interval_hours: number;
  enabled: boolean;
  last_run_at?: string | null;
  next_run_at?: string;
  run_count?: number;
  status?: string;
  last_error?: string | null;
  scan_params?: Record<string, any>;
  created_at?: string;
}

export interface AlertRecord {
  alert_id: string;
  type: string;
  entity_name?: string;
  platform?: string;
  scan_id?: string;
  monitor_id?: string;
  video_id?: string;
  title?: string;
  message?: string;
  severity?: string;
  classification?: string;
  confidence?: number;
  url?: string;
  read?: boolean;
  created_at: string;
}

export interface ScanSummary {
  total_analyzed: number;
  defamatory_found: number;
  pirated_found: number;
  legitimate_filtered: number;
  catalog_videos?: number;
  deep_crawl_enabled?: boolean;
  telegram_seeds_harvested?: number;
  fingerprint?: {
    enabled?: boolean;
    catalog_fingerprinted?: number;
    candidates_tested?: number;
    matches?: number;
    strong_matches?: number;
    skipped_reason?: string | null;
  };
  platform?: string;
  mode?: string;
  telegram?: {
    seeds?: number;
    discovered_seeds?: number;
    channels_scanned?: number;
    fingerprint_matches?: number;
    youtube_bridge_seeds?: number;
    warning?: string;
    error?: string;
    top_channels?: Array<{
      title?: string;
      username?: string;
      url?: string;
      risk_score?: number;
      severity?: string;
      signals?: string[];
      hits_count?: number;
    }>;
  };
  error?: string;
}

export interface ScanStartResult {
  ok: boolean;
  scan_id: string;
  entity_name: string;
  status: "running";
  message: string;
  error?: string;
}

export interface ScanStatusResult {
  ok: boolean;
  scan_id: string;
  status: "running" | "completed";
  progress: string;
  summary?: ScanSummary;
}

export interface ScanResult {
  ok: boolean;
  scan_id: string;
  entity_name: string;
  summary: ScanSummary;
  defamatory_content: DetectedVideo[];
  pirated_content: DetectedVideo[];
  error?: string;
}

export interface ScanRecord {
  scan_id: string;
  entity_name: string;
  entity_type: string;
  scan_type: string;
  params: Record<string, any>;
  status: "running" | "completed";
  created_at: string;
  completed_at: string | null;
  summary: ScanSummary;
}

export interface ScanDetailResponse {
  ok: boolean;
  scan: ScanRecord;
  detected_videos: DetectedVideo[];
}

export interface HistoryResponse {
  ok: boolean;
  scans: ScanRecord[];
}

export interface FlaggedChannel {
  channel_id: string;
  channel_title: string;
  offending_video_count: number;
  last_offence?: string;
}

export interface ChannelsResponse {
  ok: boolean;
  channels: FlaggedChannel[];
}

export interface TakedownReportParams {
  scan_id: string;
  entity_name: string;
  entity_type?: string;
  complainant_name: string;
  contact_email: string;
  require_confirmed?: boolean;
}

export interface TakedownReport {
  ok: boolean;
  report_id: string;
  dmca_report: string;
  defamation_report: string;
  csv: string;
  video_count: number;
}
