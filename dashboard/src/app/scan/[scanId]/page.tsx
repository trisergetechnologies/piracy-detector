"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { getScanResults } from "@/lib/api";
import type { ScanRecord, DetectedVideo } from "@/lib/types";
import VideoCard from "@/components/VideoCard";
import {
  Tabs,
  Tab,
  Chip,
  Skeleton,
  Button,
  Alert,
} from "@mui/material";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import GavelIcon from "@mui/icons-material/Gavel";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import GppBadIcon from "@mui/icons-material/GppBad";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import Link from "next/link";

export default function ScanResultsPage() {
  const params = useParams();
  const router = useRouter();
  const scanId = params.scanId as string;

  const [scan, setScan] = useState<ScanRecord | null>(null);
  const [videos, setVideos] = useState<DetectedVideo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [tab, setTab] = useState(0);

  useEffect(() => {
    if (!scanId) return;
    getScanResults(scanId)
      .then((data) => {
        if (data.ok) {
          setScan(data.scan);
          setVideos(data.detected_videos || []);
        } else {
          setError("Scan not found");
        }
      })
      .catch((err) => {
        setError(err?.response?.data?.error || "Failed to load scan results");
      })
      .finally(() => setLoading(false));
  }, [scanId]);

  const defamatoryVideos = videos.filter(
    (v) =>
      v.classification === "DEFAMATORY" ||
      v.score_result?.classification === "DEFAMATORY"
  );
  const piratedVideos = videos.filter(
    (v) =>
      v.classification === "PIRATED" ||
      v.score_result?.classification === "PIRATED"
  );

  const filteredVideos =
    tab === 0 ? videos : tab === 1 ? defamatoryVideos : piratedVideos;

  // Normalize video fields for VideoCard
  const normalizeVideo = (v: any): DetectedVideo => ({
    video_id: v.video_id,
    title: v.title || "",
    url: v.url || `https://www.youtube.com/watch?v=${v.video_id}`,
    channel_title: v.channel_title || "",
    channel_id: v.channel_id || "",
    channel_url:
      v.channel_url || `https://www.youtube.com/channel/${v.channel_id || ""}`,
    published_at: v.published_at || "",
    views: v.views ?? v.view_count ?? 0,
    likes: v.likes ?? v.like_count ?? 0,
    comments: v.comments ?? v.comment_count ?? 0,
    duration: v.duration || v.duration_formatted || "",
    thumbnail_url: v.thumbnail_url || "",
    classification:
      v.classification || v.score_result?.classification || "LEGITIMATE",
    confidence: v.confidence ?? v.score_result?.confidence ?? 0,
    severity: v.severity || v.score_result?.severity || "low",
    reason: v.reason || v.score_result?.reason || "",
    final_score: v.final_score ?? v.score_result?.final_score ?? 0,
    thumbnail_analysis: v.thumbnail_analysis || "",
    sub_scores: v.sub_scores || v.score_result?.sub_scores || {},
  });

  if (loading) {
    return (
      <div className="space-y-6">
        <Skeleton variant="rounded" height={80} sx={{ borderRadius: 3 }} />
        <Skeleton variant="rounded" height={60} sx={{ borderRadius: 3 }} />
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[...Array(6)].map((_, i) => (
            <Skeleton
              key={i}
              variant="rounded"
              height={320}
              sx={{ borderRadius: 3 }}
            />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-4">
        <Button startIcon={<ArrowBackIcon />} onClick={() => router.back()}>
          Back
        </Button>
        <Alert severity="error">{error}</Alert>
      </div>
    );
  }

  const summary = scan?.summary;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-center gap-3">
          <Button
            startIcon={<ArrowBackIcon />}
            onClick={() => router.back()}
            size="small"
            variant="outlined"
          >
            Back
          </Button>
          <div>
            <h1 className="text-2xl font-bold tracking-tight">
              {scan?.entity_name}
            </h1>
            <p className="text-sm text-gray-500">
              {scan?.scan_type} scan &middot;{" "}
              {scan?.created_at
                ? new Date(scan.created_at).toLocaleString()
                : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Chip
            label={scan?.entity_type}
            size="small"
            variant="outlined"
          />
          <Chip
            label={scan?.status}
            size="small"
            color={scan?.status === "completed" ? "success" : "warning"}
          />
          <Link href={`/reports?scan_id=${scanId}`}>
            <Button
              variant="outlined"
              size="small"
              startIcon={<GavelIcon />}
            >
              Generate Report
            </Button>
          </Link>
        </div>
      </div>

      {/* Summary cards */}
      {summary && (
        <div className="grid gap-3 sm:grid-cols-4">
          <div className="card flex items-center gap-3 !p-4">
            <CheckCircleIcon className="text-blue-500" />
            <div>
              <p className="text-xs text-gray-500">Analyzed</p>
              <p className="text-lg font-bold">{summary.total_analyzed}</p>
            </div>
          </div>
          <div className="card flex items-center gap-3 !p-4">
            <WarningAmberIcon className="text-red-500" />
            <div>
              <p className="text-xs text-gray-500">Defamatory</p>
              <p className="text-lg font-bold">{summary.defamatory_found}</p>
            </div>
          </div>
          <div className="card flex items-center gap-3 !p-4">
            <GppBadIcon className="text-amber-500" />
            <div>
              <p className="text-xs text-gray-500">Pirated</p>
              <p className="text-lg font-bold">{summary.pirated_found}</p>
            </div>
          </div>
          <div className="card flex items-center gap-3 !p-4">
            <CheckCircleIcon className="text-green-500" />
            <div>
              <p className="text-xs text-gray-500">Legitimate</p>
              <p className="text-lg font-bold">
                {summary.legitimate_filtered}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <Tabs value={tab} onChange={(_, v) => setTab(v)}>
        <Tab label={`All (${videos.length})`} />
        <Tab label={`Defamatory (${defamatoryVideos.length})`} />
        <Tab label={`Pirated (${piratedVideos.length})`} />
      </Tabs>

      {/* Video grid */}
      {filteredVideos.length > 0 ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {filteredVideos.map((v) => (
            <VideoCard key={v.video_id} video={normalizeVideo(v)} />
          ))}
        </div>
      ) : (
        <div className="card py-12 text-center text-gray-400">
          No videos found in this category.
        </div>
      )}
    </div>
  );
}
