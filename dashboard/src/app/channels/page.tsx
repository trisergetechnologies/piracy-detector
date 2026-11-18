"use client";

import { useEffect, useState } from "react";
import { getChannels } from "@/lib/api";
import type { FlaggedChannel } from "@/lib/types";
import { DataGrid, GridColDef } from "@mui/x-data-grid";
import { Chip, Skeleton, Button } from "@mui/material";
import OpenInNewIcon from "@mui/icons-material/OpenInNew";

const columns: GridColDef[] = [
  {
    field: "channel_title",
    headerName: "Channel",
    flex: 1,
    minWidth: 200,
    renderCell: (p) => (
      <span className="font-medium">{p.value || "Unknown"}</span>
    ),
  },
  {
    field: "offending_video_count",
    headerName: "Offending Videos",
    width: 160,
    type: "number",
    renderCell: (p) => (
      <Chip
        label={p.value}
        size="small"
        color={
          (p.value as number) >= 5
            ? "error"
            : (p.value as number) >= 2
            ? "warning"
            : "default"
        }
      />
    ),
  },
  {
    field: "last_offence",
    headerName: "Last Offence",
    width: 180,
    valueFormatter: (value: string) =>
      value ? new Date(value).toLocaleString() : "N/A",
  },
  {
    field: "channel_id",
    headerName: "Actions",
    width: 140,
    sortable: false,
    renderCell: (p) => (
      <Button
        size="small"
        href={`https://www.youtube.com/channel/${p.value}`}
        target="_blank"
        rel="noopener noreferrer"
        startIcon={<OpenInNewIcon />}
        sx={{ fontSize: 12 }}
      >
        YouTube
      </Button>
    ),
  },
];

export default function ChannelsPage() {
  const [channels, setChannels] = useState<FlaggedChannel[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    getChannels(200)
      .then((data) => setChannels(data.channels || []))
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          Flagged Channels
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Channels identified with offending content, sorted by violation count
        </p>
      </div>

      {loading ? (
        <Skeleton variant="rounded" height={400} sx={{ borderRadius: 3 }} />
      ) : channels.length > 0 ? (
        <div className="card !p-0 overflow-hidden">
          <DataGrid
            rows={channels}
            columns={columns}
            getRowId={(row) => row.channel_id}
            pageSizeOptions={[10, 25, 50]}
            initialState={{
              pagination: { paginationModel: { pageSize: 10 } },
              sorting: {
                sortModel: [
                  { field: "offending_video_count", sort: "desc" },
                ],
              },
            }}
            disableRowSelectionOnClick
            autoHeight
            sx={{
              border: "none",
              "& .MuiDataGrid-columnHeaders": {
                backgroundColor: "rgba(0,0,0,0.02)",
              },
            }}
          />
        </div>
      ) : (
        <div className="card py-12 text-center text-gray-400">
          No flagged channels yet. Run a scan to detect offending content.
        </div>
      )}
    </div>
  );
}
