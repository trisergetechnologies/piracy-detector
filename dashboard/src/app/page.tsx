"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { getHistory, getHealth, getChannels } from "@/lib/api";
import type { ScanRecord, HealthResponse, FlaggedChannel } from "@/lib/types";
import StatCard from "@/components/StatCard";
import HealthIndicator from "@/components/HealthIndicator";
import SearchIcon from "@mui/icons-material/Search";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import GppBadIcon from "@mui/icons-material/GppBad";
import OndemandVideoIcon from "@mui/icons-material/OndemandVideo";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import { Button, Chip, Skeleton } from "@mui/material";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  CartesianGrid,
} from "recharts";

export default function DashboardHome() {
  const [scans, setScans] = useState<ScanRecord[]>([]);
  const [channels, setChannels] = useState<FlaggedChannel[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([getHistory(50), getChannels(50)])
      .then(([h, c]) => {
        setScans(h.scans || []);
        setChannels(c.channels || []);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const totalScans = scans.length;
  const totalDefamatory = scans.reduce(
    (sum, s) => sum + (s.summary?.defamatory_found || 0),
    0
  );
  const totalPirated = scans.reduce(
    (sum, s) => sum + (s.summary?.pirated_found || 0),
    0
  );
  const totalChannels = channels.length;

  const chartData = scans
    .slice(0, 10)
    .reverse()
    .map((s) => ({
      name: s.entity_name?.slice(0, 12) || s.scan_id.slice(-6),
      Defamatory: s.summary?.defamatory_found || 0,
      Pirated: s.summary?.pirated_found || 0,
    }));

  const recentScans = scans.slice(0, 8);

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Dashboard</h1>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            YouTube + Telegram piracy / defamation detection
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <HealthIndicator />
          <Link href="/alerts">
            <Button variant="outlined" size="small">
              Alerts
            </Button>
          </Link>
          <Link href="/review">
            <Button variant="outlined" size="small">
              Review
            </Button>
          </Link>
          <Link href="/scan/new">
            <Button variant="contained" startIcon={<SearchIcon />} size="small">
              New Scan
            </Button>
          </Link>
        </div>
      </div>

      {/* Stat cards */}
      {loading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[...Array(4)].map((_, i) => (
            <Skeleton
              key={i}
              variant="rounded"
              height={110}
              sx={{ borderRadius: 3 }}
            />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            title="Total Scans"
            value={totalScans}
            icon={<TrendingUpIcon />}
            color="blue"
          />
          <StatCard
            title="Defamatory Found"
            value={totalDefamatory}
            icon={<WarningAmberIcon />}
            color="red"
          />
          <StatCard
            title="Pirated Found"
            value={totalPirated}
            icon={<GppBadIcon />}
            color="amber"
          />
          <StatCard
            title="Flagged Channels"
            value={totalChannels}
            icon={<OndemandVideoIcon />}
            color="purple"
          />
        </div>
      )}

      {/* Chart + Recent scans grid */}
      <div className="grid gap-6 lg:grid-cols-5">
        {/* Chart */}
        <div className="card lg:col-span-3">
          <h2 className="mb-4 text-sm font-semibold text-gray-700 dark:text-gray-300">
            Detection Results (Recent Scans)
          </h2>
          {chartData.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <BarChart data={chartData}>
                <CartesianGrid strokeDasharray="3 3" opacity={0.2} />
                <XAxis dataKey="name" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip
                  contentStyle={{
                    borderRadius: 8,
                    fontSize: 12,
                    border: "1px solid #e5e7eb",
                  }}
                />
                <Bar
                  dataKey="Defamatory"
                  fill="#ef4444"
                  radius={[4, 4, 0, 0]}
                />
                <Bar
                  dataKey="Pirated"
                  fill="#f59e0b"
                  radius={[4, 4, 0, 0]}
                />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="flex h-[260px] items-center justify-center text-sm text-gray-400">
              No scan data yet. Run your first scan!
            </div>
          )}
        </div>

        {/* Recent scans */}
        <div className="card lg:col-span-2">
          <div className="mb-4 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300">
              Recent Scans
            </h2>
            <Link
              href="/history"
              className="text-xs font-medium text-blue-600 hover:underline"
            >
              View all
            </Link>
          </div>
          {loading ? (
            <div className="space-y-3">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} variant="rounded" height={48} />
              ))}
            </div>
          ) : recentScans.length > 0 ? (
            <div className="space-y-2">
              {recentScans.map((scan) => (
                <Link
                  key={scan.scan_id}
                  href={`/scan/${scan.scan_id}`}
                  className="flex items-center justify-between rounded-lg px-3 py-2.5 transition-colors hover:bg-gray-50 dark:hover:bg-gray-800"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">
                      {scan.entity_name}
                    </p>
                    <p className="text-xs text-gray-400">
                      {new Date(scan.created_at).toLocaleDateString()}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <Chip
                      label={scan.status}
                      size="small"
                      color={
                        scan.status === "completed" ? "success" : "warning"
                      }
                      variant="outlined"
                      sx={{ fontSize: 11 }}
                    />
                  </div>
                </Link>
              ))}
            </div>
          ) : (
            <p className="py-8 text-center text-sm text-gray-400">
              No scans yet
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
