"use client";

import Image from "next/image";
import SeverityBadge from "./SeverityBadge";
import VisibilityIcon from "@mui/icons-material/Visibility";
import ThumbUpIcon from "@mui/icons-material/ThumbUp";
import ChatBubbleIcon from "@mui/icons-material/ChatBubble";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";
import { Chip, LinearProgress } from "@mui/material";
import type { DetectedVideo } from "@/lib/types";

function formatNumber(n: number | undefined): string {
  if (!n) return "0";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + "M";
  if (n >= 1_000) return (n / 1_000).toFixed(1) + "K";
  return n.toString();
}

export default function VideoCard({ video }: { video: DetectedVideo }) {
  return (
    <div className="card group overflow-hidden !p-0 transition-shadow hover:shadow-md">
      {/* Thumbnail */}
      <div className="relative aspect-video w-full bg-gray-100 dark:bg-gray-800">
        {video.thumbnail_url ? (
          <Image
            src={video.thumbnail_url}
            alt={video.title}
            fill
            className="object-cover"
            sizes="(max-width: 640px) 100vw, (max-width: 1024px) 50vw, 33vw"
          />
        ) : (
          <div className="flex h-full items-center justify-center text-gray-400">
            No thumbnail
          </div>
        )}
        {video.duration && (
          <span className="absolute bottom-2 right-2 rounded bg-black/80 px-1.5 py-0.5 text-xs font-medium text-white">
            {video.duration}
          </span>
        )}
      </div>

      {/* Content */}
      <div className="space-y-3 p-4">
        {/* Title */}
        <h3 className="line-clamp-2 text-sm font-semibold leading-snug">
          {video.title}
        </h3>

        {/* Channel */}
        <a
          href={video.channel_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-block text-xs text-gray-500 hover:text-blue-600 dark:text-gray-400"
        >
          {video.channel_title}
        </a>

        {/* Badges */}
        <div className="flex flex-wrap items-center gap-2">
          <Chip
            label={video.classification}
            size="small"
            color={video.classification === "PIRATED" ? "secondary" : "error"}
            variant="outlined"
          />
          {video.severity && <SeverityBadge severity={video.severity} />}
        </div>

        {/* Confidence bar */}
        <div>
          <div className="mb-1 flex items-center justify-between text-xs text-gray-500">
            <span>Confidence</span>
            <span className="font-semibold">{video.confidence}%</span>
          </div>
          <LinearProgress
            variant="determinate"
            value={video.confidence}
            color={video.confidence >= 80 ? "error" : "warning"}
            className="!rounded-full"
          />
        </div>

        {/* Stats row */}
        <div className="flex items-center gap-4 text-xs text-gray-500 dark:text-gray-400">
          <span className="flex items-center gap-1">
            <VisibilityIcon sx={{ fontSize: 14 }} />
            {formatNumber(video.views)}
          </span>
          <span className="flex items-center gap-1">
            <ThumbUpIcon sx={{ fontSize: 14 }} />
            {formatNumber(video.likes)}
          </span>
          <span className="flex items-center gap-1">
            <ChatBubbleIcon sx={{ fontSize: 14 }} />
            {formatNumber(video.comments)}
          </span>
        </div>

        {/* Reason */}
        {video.reason && (
          <p className="text-xs italic text-gray-500 dark:text-gray-400 line-clamp-2">
            {video.reason}
          </p>
        )}

        {/* Open link */}
        <a
          href={video.url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline dark:text-blue-400"
        >
          Open on YouTube <OpenInNewIcon sx={{ fontSize: 12 }} />
        </a>
      </div>
    </div>
  );
}
